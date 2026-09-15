(in-package #:llm-log)

(defparameter *proxy-expert-hosts* (make-hash-table :test #'eq))
(defparameter *proxy-infill-workers* (make-hash-table :test #'eq))

(defstruct infill-worker
  thread host
  (lock (bt:make-lock "llm-log-infill-queue"))
  head tail
  (stop nil))

(defun %infill-dequeue (worker)
  (bt:with-lock-held ((infill-worker-lock worker))
    (let ((cell (infill-worker-head worker)))
      (when cell
        (setf (infill-worker-head worker) (cdr cell))
        (when (null (infill-worker-head worker))
          (setf (infill-worker-tail worker) nil))
        (car cell)))))

(defun %enqueue-infill (worker event)
  (let ((cell (list event)))
    (bt:with-lock-held ((infill-worker-lock worker))
      (if (infill-worker-tail worker)
          (setf (cdr (infill-worker-tail worker)) cell
                (infill-worker-tail worker) cell)
          (setf (infill-worker-head worker) cell
                (infill-worker-tail worker) cell))))
  event)

(defun %run-infill-worker (worker)
  "Serialize live Tek9/SWI projections through one in-process CL worker."
  (loop for event = (%infill-dequeue worker)
        do (cond
             (event
              (handler-case
                  (llm-log-expert:ingest-capture-event
                   (infill-worker-host worker) event)
                (error (condition)
                  (format *error-output*
                          "llm-log: expert infill failed for ~A: ~A~%"
                          (jsown:val-safe event "event_id") condition))))
             ((infill-worker-stop worker) (return))
             (t (sleep 0.01)))))

(defun %start-infill-worker (host)
  (let ((worker (make-infill-worker :host host)))
    (setf (infill-worker-thread worker)
          (bt:make-thread (lambda () (%run-infill-worker worker))
                          :name "llm-log-expert-infill"))
    worker))

(defun %stop-infill-worker (worker)
  "Drain queued events before the sole expert host closes."
  (setf (infill-worker-stop worker) t)
  (let ((thread (infill-worker-thread worker)))
    (when thread (ignore-errors (bt:join-thread thread))))
  worker)

(defun %ordered-headers-hash (headers)
  "Collapse ordered response headers only for durable capture metadata.
Wire delivery retains duplicates through the Clack plist."
  (let ((table (make-hash-table :test #'equalp)))
    (dolist (entry headers table)
      (setf (gethash (car entry) table) (cdr entry)))))

(defun %uri-path-query (uri)
  (let ((q (position #\? uri)))
    (values (if q (subseq uri 0 q) uri)
            (if q (subseq uri (1+ q)) ""))))

(defun %build-capture-event
    (provider upstream method uri request-headers request-body
     response-status response-headers response-body started-at start-ticks)
  (multiple-value-bind (path query) (%uri-path-query uri)
    (let* ((completed-at (%utc-now))
           (elapsed (- (get-internal-real-time) start-ticks))
           (latency-ms
             (round (* 1000 (/ elapsed internal-time-units-per-second)))))
      (make-capture-event
       :provider provider
       :upstream upstream
       :method (string-upcase (symbol-name method))
       :path path
       :query query
       :request-headers request-headers
       :request-body request-body
       :response-status response-status
       :response-headers (%ordered-headers-hash response-headers)
       :response-body response-body
       :started-at started-at
       :completed-at completed-at
       :latency-ms latency-ms))))

(defun %persist-and-queue-capture (config infill-worker event)
  (handler-case
      (progn
        (append-capture-event (runtime-config-data-directory config) event)
        (%enqueue-infill infill-worker event))
    (error (condition)
      (format *error-output* "llm-log: capture persistence failed for ~A: ~A~%"
              (jsown:val-safe event "event_id") condition)))
  event)

(defun %normal-error-response (status text)
  (list status
        (list :content-type "text/plain; charset=utf-8"
              :connection "close")
        (list text)))

(defun %proxy-response-callback (env config infill-worker)
  "Return a Clack delayed response that streams provider bytes on this request thread.

The HTTP server owns the client socket end-to-end.  llm-log never touches the
server descriptor directly.  The upstream body is decoded from provider framing,
written through the Clack streaming writer, and tee'd into one capture buffer."
  (lambda (respond)
    (let* ((method (getf env :request-method))
           (uri (getf env :request-uri))
           (request-headers (%request-headers-for-upstream env))
           (request-body (%request-body-octets (getf env :raw-body)))
           (started-at (%utc-now))
           (start-ticks (get-internal-real-time)))
      (handler-case
          (multiple-value-bind (provider upstream-target upstream-url)
              (%resolve-provider config uri)
            (unless (and provider upstream-target upstream-url)
              (funcall respond
                       (%normal-error-response
                        404 (format nil "unknown upstream: ~A" provider)))
              (return-from %proxy-response-callback nil))
            (multiple-value-bind (upstream-stream upstream-socket)
                (%open-upstream upstream-url)
              (unwind-protect
                   (progn
                     (%write-upstream-request
                      upstream-stream
                      (string-upcase (symbol-name method))
                      upstream-target
                      (%upstream-host-header (quri:uri upstream-url))
                      request-headers
                      request-body)
                     (let ((head (%read-head-octets upstream-stream)))
                       (multiple-value-bind (status response-headers)
                           (%parse-response-head head)
                         (let* ((writer
                                  (funcall respond
                                           (list status
                                                 (%downstream-headers
                                                  response-headers))))
                                (capture-body
                                  (make-array 65536
                                              :element-type '(unsigned-byte 8)
                                              :fill-pointer 0
                                              :adjustable t)))
                           (unwind-protect
                                (%relay-response-body
                                 upstream-stream response-headers
                                 (lambda (buffer start end)
                                   ;; Server-owned streaming writer flushes each
                                   ;; provider body chunk on this request thread.
                                   (funcall writer buffer :start start :end end)
                                   (loop for i from start below end
                                         do (vector-push-extend
                                             (aref buffer i) capture-body))))
                             (funcall writer nil :close t))
                           ;; Raw evidence commits before derived expert work is
                           ;; queued.  This is local CL function dispatch only.
                           (%persist-and-queue-capture
                            config infill-worker
                            (%build-capture-event
                             provider upstream-url method uri
                             request-headers request-body
                             status response-headers capture-body
                             started-at start-ticks))))))
                (ignore-errors (close upstream-stream))
                (when upstream-socket
                  (ignore-errors (usocket:socket-close upstream-socket))))))
        (error (condition)
          (format *error-output* "llm-log: upstream relay failed: ~A~%" condition)
          (ignore-errors
            (funcall respond
                     (%normal-error-response
                      502 (format nil "upstream request failed: ~A" condition)))))))))

(defun %make-proxy-app (config expert-host)
  (let ((infill-worker (gethash expert-host *proxy-infill-workers*)))
    (lambda (env)
      (%proxy-response-callback env config infill-worker))))

(defun start-proxy (config)
  "Start the all-Common-Lisp proxy with a thread-per-request streaming server."
  (let* ((expert-host
           (llm-log-expert:start-expert-host
            (runtime-config-expert-data-directory config)))
         (infill-worker (%start-infill-worker expert-host)))
    (setf (gethash expert-host *proxy-infill-workers*) infill-worker)
    (handler-case
        (let* ((handler
                 (clack:clackup
                  (%make-proxy-app config expert-host)
                  :server :hunchentoot
                  :address (runtime-config-listen-address config)
                  :port (runtime-config-port config)
                  :use-thread t
                  :debug nil
                  :persistent-connections-p nil))
               (server (make-proxy-server :thread handler :config config)))
          (setf (gethash server *proxy-expert-hosts*) expert-host)
          server)
      (error (condition)
        (%stop-infill-worker infill-worker)
        (remhash expert-host *proxy-infill-workers*)
        (llm-log-expert:stop-expert-host expert-host)
        (error condition)))))

(defun stop-proxy (server)
  (let ((handler (proxy-server-thread server))
        (expert-host (gethash server *proxy-expert-hosts*)))
    (when handler (ignore-errors (clack:stop handler)))
    (when expert-host
      (let ((worker (gethash expert-host *proxy-infill-workers*)))
        (when worker (%stop-infill-worker worker))
        (remhash expert-host *proxy-infill-workers*))
      (remhash server *proxy-expert-hosts*)
      (ignore-errors (llm-log-expert:stop-expert-host expert-host))))
  server)

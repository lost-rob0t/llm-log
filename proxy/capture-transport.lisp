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
  "Enqueue EVENT in FIFO order without putting expert work on the relay path."
  (let ((cell (list event)))
    (bt:with-lock-held ((infill-worker-lock worker))
      (if (infill-worker-tail worker)
          (setf (cdr (infill-worker-tail worker)) cell
                (infill-worker-tail worker) cell)
          (setf (infill-worker-head worker) cell
                (infill-worker-tail worker) cell))))
  event)

(defun %run-infill-worker (worker)
  "Serialize all live Tek9/SWI projection through one in-process CL worker."
  (loop
    for event = (%infill-dequeue worker)
    do (cond
         (event
          (handler-case
              (llm-log-expert:ingest-capture-event (infill-worker-host worker) event)
            (error (condition)
              (format *error-output* "llm-log: expert infill failed for ~A: ~A~%"
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
  "Drain queued events before allowing the sole expert host to close."
  (setf (infill-worker-stop worker) t)
  (let ((thread (infill-worker-thread worker)))
    (when thread (ignore-errors (bt:join-thread thread))))
  worker)

(defun %response-head-metadata (lines)
  (let ((status 0)
        (headers (make-hash-table :test #'equalp)))
    (when lines
      (let ((parts (uiop:split-string (first lines) :separator '(#\Space))))
        (when (>= (length parts) 2)
          (setf status (or (ignore-errors (parse-integer (second parts))) 0))))
      (dolist (line (rest lines))
        (let ((sep (position #\: line)))
          (when sep
            (let ((name (subseq line 0 sep))
                  (value (string-trim '(#\Space #\Tab) (subseq line (1+ sep)))))
              (setf (gethash name headers) value))))))
    (values status headers)))

(defun %relay-upstream-response (client-stream stream)
  "Tee upstream response bytes to client and capture without changing framing."
  (let* ((head (%read-head-octets stream))
         (lines (loop for line in
                         (uiop:split-string
                          (trivial-utf-8:utf-8-bytes-to-string head)
                          :separator (format nil "~C~C" #\Return #\Linefeed))
                      when (plusp (length line)) collect line))
         (head-bytes (make-array 0 :element-type '(unsigned-byte 8)
                                 :fill-pointer 0 :adjustable t))
         (body (make-array 65536 :element-type '(unsigned-byte 8)
                           :fill-pointer 0 :adjustable t)))
    (flet ((push-head-line (line)
             (loop for byte across (%utf8-octets line)
                   do (vector-push-extend byte head-bytes))
             (vector-push-extend 13 head-bytes)
             (vector-push-extend 10 head-bytes)))
      (dolist (line lines)
        (let ((sep (position #\: line)))
          (unless (and sep (%header-name-p (subseq line 0 sep) '("connection")))
            (push-head-line line))))
      (push-head-line "Connection: close")
      (vector-push-extend 13 head-bytes)
      (vector-push-extend 10 head-bytes))
    (write-sequence head-bytes client-stream)
    (force-output client-stream)
    (loop with buffer = (make-array +relay-buffer-size+
                                    :element-type '(unsigned-byte 8))
          for n = (read-sequence buffer stream)
          until (zerop n)
          do (write-sequence buffer client-stream :end n)
             (force-output client-stream)
             (loop for i below n do (vector-push-extend (aref buffer i) body)))
    (multiple-value-bind (status headers) (%response-head-metadata lines)
      (values status headers body))))

(defun %uri-path-query (uri)
  (let ((q (position #\? uri)))
    (values (if q (subseq uri 0 q) uri)
            (if q (subseq uri (1+ q)) ""))))

(defun %build-capture-event
    (provider upstream method uri headers request-body
     response-status response-headers response-body started-at start-ticks)
  (multiple-value-bind (path query) (%uri-path-query uri)
    (let* ((completed-at (%utc-now))
           (elapsed (- (get-internal-real-time) start-ticks))
           (latency-ms (round (* 1000 (/ elapsed internal-time-units-per-second)))))
      (make-capture-event
       :provider provider :upstream upstream
       :method (string-upcase (symbol-name method))
       :path path :query query
       :request-headers headers :request-body request-body
       :response-status response-status :response-headers response-headers
       :response-body response-body :started-at started-at
       :completed-at completed-at :latency-ms latency-ms))))

(defun %persist-and-queue-capture (config infill-worker event)
  (handler-case
      (progn
        (append-capture-event (runtime-config-data-directory config) event)
        (%enqueue-infill infill-worker event))
    (error (condition)
      (format *error-output* "llm-log: capture persistence failed for ~A: ~A~%"
              (jsown:val-safe event "event_id") condition)))
  event)

(defun %relay-request (client-stream config method uri headers body)
  "Forward one exchange and return its completed capture event, if any."
  (let ((started-at (%utc-now))
        (start-ticks (get-internal-real-time)))
    (handler-case
        (multiple-value-bind (provider upstream-target upstream-url)
            (%resolve-provider config uri)
          (cond
            ((or (null provider) (null upstream-url))
             (%write-raw-response client-stream 404 "Not Found"
                                  (format nil "unknown upstream: ~A" provider))
             nil)
            (t
             (multiple-value-bind (stream socket) (%open-upstream upstream-url)
               (unwind-protect
                    (progn
                      (%write-upstream-request
                       stream (string-upcase (symbol-name method))
                       upstream-target (%upstream-host-header (quri:uri upstream-url))
                       headers body)
                      (multiple-value-bind (status response-headers response-body)
                          (%relay-upstream-response client-stream stream)
                        (%build-capture-event
                         provider upstream-url method uri headers body
                         status response-headers response-body started-at start-ticks)))
                 (ignore-errors (close stream))
                 (when socket (ignore-errors (usocket:socket-close socket))))))))
      (error (condition)
        (let* ((text (format nil "upstream request failed: ~A" condition))
               (response-body (%utf8-octets text))
               (response-headers (make-hash-table :test #'equalp)))
          (ignore-errors (%write-raw-response client-stream 502 "Bad Gateway" text))
          (ignore-errors
            (multiple-value-bind (provider target upstream-url)
                (%resolve-provider config uri)
              (declare (ignore target))
              (when (and provider upstream-url)
                (%build-capture-event
                 provider upstream-url method uri headers body
                 502 response-headers response-body started-at start-ticks)))))))))

(defun %make-proxy-app (config expert-host)
  (let ((infill-worker (gethash expert-host *proxy-infill-workers*)))
    (lambda (env)
      (let ((io (getf env :clack.io)))
        (bt:make-thread
         (lambda ()
           (let ((client-stream (%make-blocking-client-stream io))
                 (event nil))
             (unwind-protect
                  (setf event
                        (%relay-request
                         client-stream config
                         (getf env :request-method)
                         (getf env :request-uri)
                         (getf env :headers)
                         (%request-body-octets (getf env :raw-body))))
               ;; Response delivery is complete before any recorder/expert work.
               (setf (woo.ev.socket::socket-open-p io) nil)
               (ignore-errors (close client-stream)))
             (when event
               (%persist-and-queue-capture config infill-worker event))))
         :name "llm-log-relay")
        (lambda (respond) (declare (ignore respond)))))))

(defun start-proxy (config)
  "Start the all-Common-Lisp proxy, sole expert host, and serialized infill worker."
  (let* ((expert-host
           (llm-log-expert:start-expert-host
            (runtime-config-expert-data-directory config)))
         (infill-worker (%start-infill-worker expert-host)))
    (setf (gethash expert-host *proxy-infill-workers*) infill-worker)
    (handler-case
        (let* ((thread
                 (bt:make-thread
                  (lambda ()
                    (woo:run (%make-proxy-app config expert-host)
                             :port (runtime-config-port config)
                             :address (runtime-config-listen-address config)
                             :worker-num nil :debug nil))
                  :name "llm-log-proxy"))
               (server (make-proxy-server :thread thread :config config)))
          (setf (gethash server *proxy-expert-hosts*) expert-host)
          server)
      (error (condition)
        (%stop-infill-worker infill-worker)
        (remhash expert-host *proxy-infill-workers*)
        (llm-log-expert:stop-expert-host expert-host)
        (error condition)))))

(defun stop-proxy (server)
  (let ((thread (proxy-server-thread server))
        (expert-host (gethash server *proxy-expert-hosts*)))
    (when thread (ignore-errors (bt:destroy-thread thread)))
    (when expert-host
      (let ((worker (gethash expert-host *proxy-infill-workers*)))
        (when worker (%stop-infill-worker worker))
        (remhash expert-host *proxy-infill-workers*))
      (remhash server *proxy-expert-hosts*)
      (ignore-errors (llm-log-expert:stop-expert-host expert-host))))
  server)

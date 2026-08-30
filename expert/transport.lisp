(in-package #:llm-log-expert)

(eval-when (:compile-toplevel :load-toplevel :execute)
  (require :sb-bsd-sockets))

(defparameter +capture-schema+ "llm-log.capture/http-v1")
(defparameter +redacted-header-value+ "<redacted>")
(defparameter +secret-header-names+
  '("authorization"
    "proxy-authorization"
    "cookie"
    "set-cookie"
    "x-api-key"
    "api-key"
    "openai-api-key"
    "anthropic-api-key"))

(defstruct (http-proxy (:constructor %make-http-proxy))
  listener
  accept-thread
  (workers '())
  (stopped-p nil))

(defun %ascii-octets (string)
  (let ((result (make-array (length string)
                            :element-type '(unsigned-byte 8))))
    (loop for character across string
          for index from 0
          do (setf (aref result index) (char-code character)))
    result))

(defun %octets-as-string (octets)
  (map 'string #'code-char octets))

(defun %header-end (octets)
  (loop for index from 3 below (length octets)
        when (and (= 13 (aref octets (- index 3)))
                  (= 10 (aref octets (- index 2)))
                  (= 13 (aref octets (- index 1)))
                  (= 10 (aref octets index)))
          do (return (1+ index))))

(defun %content-length (header-string)
  (let* ((marker "Content-Length:")
         (position (search marker header-string :test #'char-equal)))
    (if position
        (or (parse-integer header-string
                           :start (+ position (length marker))
                           :junk-allowed t)
            0)
        0)))

(defun %read-http-request (stream)
  (let ((result (make-array 0
                            :element-type '(unsigned-byte 8)
                            :adjustable t
                            :fill-pointer 0))
        header-end)
    (loop until header-end
          for byte = (read-byte stream nil nil)
          do (when (null byte)
               (error "HTTP client closed before headers completed"))
             (vector-push-extend byte result)
             (setf header-end (%header-end result)))
    (let* ((header-string (%octets-as-string (subseq result 0 header-end)))
           (content-length (%content-length header-string)))
      (dotimes (index content-length)
        (declare (ignore index))
        (let ((byte (read-byte stream nil nil)))
          (when (null byte)
            (error "HTTP client closed before body completed"))
          (vector-push-extend byte result))))
    result))

(defun %split-crlf-lines (string)
  (loop with start = 0
        for end = (search (format nil "~C~C" #\Return #\Linefeed)
                          string
                          :start2 start)
        while end
        collect (subseq string start end)
        do (setf start (+ end 2))))

(defun %split-request-line (line)
  (let* ((first-space (position #\Space line))
         (second-space (and first-space
                            (position #\Space line :start (1+ first-space)))))
    (unless (and first-space second-space)
      (error "Malformed HTTP request line: ~S" line))
    (values (subseq line 0 first-space)
            (subseq line (1+ first-space) second-space)
            (subseq line (1+ second-space)))))

(defun %split-header-line (line)
  (let ((colon (position #\: line)))
    (unless colon
      (error "Malformed HTTP header line: ~S" line))
    (cons (subseq line 0 colon)
          (string-trim '(#\Space #\Tab) (subseq line (1+ colon))))))

(defun %http-message-parts (octets)
  (let ((header-end (%header-end octets)))
    (unless header-end
      (error "HTTP message lacks complete headers"))
    (let* ((header-string (%octets-as-string (subseq octets 0 header-end)))
           (lines (%split-crlf-lines header-string)))
      (values (first lines)
              (loop for line in (rest lines)
                    unless (zerop (length line))
                      collect (%split-header-line line))
              (subseq octets header-end)))))

(defun %route-target (target upstreams)
  (unless (and (plusp (length target)) (char= #\/ (char target 0)))
    (error "Proxy request target must begin with /: ~S" target))
  (let* ((query-position (position #\? target))
         (path-end (or query-position (length target)))
         (selector-end (position #\/ target :start 1 :end path-end)))
    (unless selector-end
      (error "Proxy request target lacks provider selector remainder: ~S" target))
    (let* ((selector (subseq target 1 selector-end))
           (remainder (subseq target selector-end))
           (entry (assoc selector upstreams :test #'string=)))
      (unless entry
        (error "Unknown upstream selector ~S" selector))
      (values remainder (cdr entry) selector))))

(defun %parse-http-upstream (url)
  (let ((prefix "http://"))
    (unless (and (>= (length url) (length prefix))
                 (string-equal prefix url :end2 (length prefix)))
      (error "Bootstrap HTTP transport supports only http:// upstreams: ~S" url))
    (let* ((authority-start (length prefix))
           (path-start (position #\/ url :start authority-start))
           (authority (subseq url authority-start (or path-start (length url))))
           (colon (position #\: authority :from-end t))
           (host (if colon (subseq authority 0 colon) authority))
           (port (if colon
                     (parse-integer authority :start (1+ colon))
                     80))
           (base-path (if path-start (subseq url path-start) "")))
      (when (or (zerop (length host))
                (and (plusp (length base-path))
                     (not (string= base-path "/"))))
        (error "Bootstrap HTTP upstream must not contain a path prefix: ~S" url))
      (values host port))))

(defun %resolve-ipv4 (host)
  (handler-case
      (sb-bsd-sockets:make-inet-address host)
    (error ()
      (sb-bsd-sockets:host-ent-address
       (sb-bsd-sockets:get-host-by-name host)))))

(defun %socket-stream (socket)
  (sb-bsd-sockets:socket-make-stream
   socket
   :input t
   :output t
   :element-type '(unsigned-byte 8)
   :buffering :none))

(defun %connect-upstream (host port)
  (let ((socket (make-instance 'sb-bsd-sockets:inet-socket
                               :type :stream
                               :protocol :tcp)))
    (handler-case
        (progn
          (sb-bsd-sockets:socket-connect socket (%resolve-ipv4 host) port)
          socket)
      (error (condition)
        (ignore-errors (sb-bsd-sockets:socket-close socket))
        (error condition)))))

(defun %rewrite-request (request upstreams)
  (let* ((header-end (%header-end request))
         (header-string (%octets-as-string (subseq request 0 header-end)))
         (lines (%split-crlf-lines header-string))
         (request-line (first lines))
         (body (subseq request header-end)))
    (multiple-value-bind (method target version)
        (%split-request-line request-line)
      (multiple-value-bind (forward-target upstream-url selector)
          (%route-target target upstreams)
        (multiple-value-bind (host port)
            (%parse-http-upstream upstream-url)
          (let ((header-lines
                  (loop for line in (rest lines)
                        unless (or (zerop (length line))
                                   (search "Host:" line :test #'char-equal :end2 (min 5 (length line)))
                                   (search "Connection:" line :test #'char-equal :end2 (min 11 (length line))))
                          collect line)))
            (values
             (concatenate
              '(vector (unsigned-byte 8))
              (%ascii-octets
               (with-output-to-string (out)
                 (format out "~A ~A ~A~C~C" method forward-target version #\Return #\Linefeed)
                 (dolist (line header-lines)
                   (format out "~A~C~C" line #\Return #\Linefeed))
                 (format out "Host: ~A~@[:~D~]~C~C" host (and (/= port 80) port) #\Return #\Linefeed)
                 (format out "Connection: close~C~C~C~C" #\Return #\Linefeed #\Return #\Linefeed)))
              body)
             host
             port
             selector
             upstream-url
             method
             forward-target)))))))

(defun %copy-stream-octets (input output)
  (let ((buffer (make-array 16384 :element-type '(unsigned-byte 8)))
        (captured (make-array 0
                              :element-type '(unsigned-byte 8)
                              :adjustable t
                              :fill-pointer 0)))
    (loop for count = (read-sequence buffer input)
          while (plusp count)
          do (write-sequence buffer output :end count)
             (finish-output output)
             (loop for index below count
                   do (vector-push-extend (aref buffer index) captured)))
    captured))

(defun %secret-header-p (name)
  (member (string-downcase name) +secret-header-names+ :test #'string=))

(defun %header-json (header)
  (%json-object
   (cons "name" (car header))
   (cons "value" (if (%secret-header-p (car header))
                     +redacted-header-value+
                     (cdr header)))))

(defun %status-code (status-line)
  (let* ((first-space (position #\Space status-line))
         (second-space (and first-space
                            (position #\Space status-line :start (1+ first-space)))))
    (unless first-space
      (error "Malformed HTTP status line: ~S" status-line))
    (parse-integer status-line
                   :start (1+ first-space)
                   :end second-space
                   :junk-allowed nil)))

(defun %fresh-capture-event-directory (data-directory)
  (let ((root (merge-pathnames #P"capture/http/events/"
                               (uiop:ensure-directory-pathname data-directory))))
    (ensure-directories-exist (merge-pathnames #P".keep" root))
    (loop repeat 64
          for event-id = (format nil "~36R-~36R"
                                 (get-universal-time)
                                 (random most-positive-fixnum))
          for event-directory = (merge-pathnames
                                 (make-pathname :directory `(:relative ,event-id))
                                 root)
          unless (probe-file event-directory)
            do (ensure-directories-exist (merge-pathnames #P"metadata.json" event-directory))
               (return (values event-id event-directory))
          finally (error "Unable to allocate immutable capture event directory"))))

(defun %write-binary-create-only (path octets)
  (with-open-file (stream path
                          :direction :output
                          :element-type '(unsigned-byte 8)
                          :if-does-not-exist :create
                          :if-exists :error)
    (write-sequence octets stream)
    (finish-output stream)))

(defun %write-text-create-only (path text)
  (with-open-file (stream path
                          :direction :output
                          :external-format :utf-8
                          :if-does-not-exist :create
                          :if-exists :error)
    (write-string text stream)
    (terpri stream)
    (finish-output stream)))

(defun %persist-http-capture (config request response selector upstream-url method forward-target)
  (multiple-value-bind (request-line request-headers request-body)
      (%http-message-parts request)
    (declare (ignore request-line))
    (multiple-value-bind (status-line response-headers response-body)
        (%http-message-parts response)
      (multiple-value-bind (event-id event-directory)
          (%fresh-capture-event-directory (runtime-config-data-directory config))
        (%write-binary-create-only (merge-pathnames #P"request-body.bin" event-directory)
                                   request-body)
        (%write-binary-create-only (merge-pathnames #P"response-body.bin" event-directory)
                                   response-body)
        (%write-text-create-only
         (merge-pathnames #P"metadata.json" event-directory)
         (jsown:to-json
          (%json-object
           (cons "schema" +capture-schema+)
           (cons "event_id" event-id)
           (cons "provider" selector)
           (cons "upstream" upstream-url)
           (cons "method" method)
           (cons "target" forward-target)
           (cons "request_headers" (mapcar #'%header-json request-headers))
           (cons "response_status" (%status-code status-line))
           (cons "response_headers" (mapcar #'%header-json response-headers))
           (cons "transport" "http"))))
        event-id))))

(defun %serve-http-client (client config)
  (unwind-protect
       (let ((client-stream (%socket-stream client)))
         (unwind-protect
              (let ((request (%read-http-request client-stream)))
                (multiple-value-bind
                      (forward-request host port selector upstream-url method forward-target)
                    (%rewrite-request request (runtime-config-upstreams config))
                  (let ((upstream (%connect-upstream host port)))
                    (unwind-protect
                         (let ((upstream-stream (%socket-stream upstream)))
                           (unwind-protect
                                (progn
                                  (write-sequence forward-request upstream-stream)
                                  (finish-output upstream-stream)
                                  (let ((response (%copy-stream-octets upstream-stream client-stream)))
                                    (handler-case
                                        (%persist-http-capture config
                                                               request
                                                               response
                                                               selector
                                                               upstream-url
                                                               method
                                                               forward-target)
                                      (error () nil))))
                             (ignore-errors (close upstream-stream))))
                      (ignore-errors (sb-bsd-sockets:socket-close upstream))))))
           (ignore-errors (close client-stream))))
    (ignore-errors (sb-bsd-sockets:socket-close client))))

(defun %accept-loop (proxy config)
  (loop until (http-proxy-stopped-p proxy)
        do (handler-case
               (let ((client (sb-bsd-sockets:socket-accept
                              (http-proxy-listener proxy))))
                 (let ((worker
                         (sb-thread:make-thread
                          (lambda ()
                            (handler-case
                                (%serve-http-client client config)
                              (error ()
                                (ignore-errors
                                  (sb-bsd-sockets:socket-close client)))))
                          :name "llm-log-http-client")))
                   (push worker (http-proxy-workers proxy))))
             (error ()
               (unless (http-proxy-stopped-p proxy)
                 (sleep 0.01))))))

(defun %listen-address (address)
  (if (string= address "127.0.0.1")
      #(127 0 0 1)
      (%resolve-ipv4 address)))

(defun start-http-proxy (config)
  (check-type config runtime-config)
  (let ((listener (make-instance 'sb-bsd-sockets:inet-socket
                                 :type :stream
                                 :protocol :tcp)))
    (handler-case
        (progn
          (sb-bsd-sockets:socket-bind listener
                                      (%listen-address
                                       (runtime-config-listen-address config))
                                      (runtime-config-port config))
          (sb-bsd-sockets:socket-listen listener 64)
          (let ((proxy (%make-http-proxy :listener listener)))
            (setf (http-proxy-accept-thread proxy)
                  (sb-thread:make-thread
                   (lambda () (%accept-loop proxy config))
                   :name "llm-log-http-accept"))
            proxy))
      (error (condition)
        (ignore-errors (sb-bsd-sockets:socket-close listener))
        (error condition)))))

(defun proxy-listen-port (proxy)
  (check-type proxy http-proxy)
  (nth-value 1 (sb-bsd-sockets:socket-name (http-proxy-listener proxy))))

(defun stop-http-proxy (proxy)
  (check-type proxy http-proxy)
  (unless (http-proxy-stopped-p proxy)
    (setf (http-proxy-stopped-p proxy) t)
    (ignore-errors (sb-bsd-sockets:socket-close (http-proxy-listener proxy)))
    (let ((accept-thread (http-proxy-accept-thread proxy)))
      (when (and accept-thread (sb-thread:thread-alive-p accept-thread))
        (ignore-errors (sb-thread:join-thread accept-thread :timeout 1.0))))
    (dolist (worker (http-proxy-workers proxy))
      (when (sb-thread:thread-alive-p worker)
        (ignore-errors (sb-thread:join-thread worker :timeout 1.0)))))
  t)

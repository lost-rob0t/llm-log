(in-package #:llm-log/tests)

;; HTTP transparent forwarding contract (zero-Python rewrite slice 2).
;; A raw-octet fixture upstream plus a raw-octet client prove fidelity of the
;; Common Lisp proxy path: method, path, query, body bytes, status, headers,
;; duplicate response headers, chunked streaming and large bodies.
;; See research/LLM-LOG-RESEARCH-012-cl-runtime-slice-1.org and the transport
;; rules in research/LLM-LOG-RESEARCH-011-zero-python-rewrite.org.

(defparameter +fixture-upstream-port+ 18771)
(defparameter +fixture-proxy-port+ 18772)
(defparameter +fixture-upstream-host+ "127.0.0.1")

(defun %ascii-octets (string)
  (map '(vector (unsigned-byte 8)) #'char-code string))

(defun %octets-to-string (octets)
  (with-output-to-string (out)
    (loop for b across octets do (write-char (code-char b) out))))

(defun %read-request-head (stream)
  "Read one HTTP head from a (unsigned-byte 8) stream: accumulate bytes until
the byte sequence CRLF CRLF has been seen, return the raw octets."
  (let ((head (make-array 0 :element-type '(unsigned-byte 8) :fill-pointer 0 :adjustable t)))
    (loop
      for byte = (read-byte stream)
      do (vector-push-extend byte head)
      when (and (>= (length head) 4)
                (= (aref head (- (length head) 4)) 13)
                (= (aref head (- (length head) 3)) 10)
                (= (aref head (- (length head) 2)) 13)
                (= (aref head (- (length head) 1)) 10))
        return head)))

(defun %head-lines (head-octets)
  "Split a raw HTTP head into CRLF-terminated lines (strings, no CRLF)."
  (let ((text (%octets-to-string head-octets)))
    (loop for line in (uiop:split-string text :separator '(#\Return #\Linefeed))
          when (plusp (length line))
            collect line)))

(defun %head-header (lines name)
  "Return the VALUE of the first header matching NAME (case-insensitive)."
  (loop for line in lines
        for sep = (and (> (length line) 1) (position #\: line))
        when (and sep (string-equal (subseq line 0 sep) name))
          return (string-trim " " (subseq line (1+ sep)))))

(defun %read-exactly (stream count)
  (let ((buffer (make-array count :element-type '(unsigned-byte 8))))
    (read-sequence buffer stream :end count)
    buffer))

(defun %fixture-parse-request (stream)
  "Parse one HTTP request off STREAM; return a plist with :method :path
:query :headers :body."
  (let* ((lines (%head-lines (%read-request-head stream)))
         (request-line (first lines))
         (header-lines (rest lines))
         (method (first (uiop:split-string request-line :separator " ")))
         (target (second (uiop:split-string request-line :separator " ")))
         (query-mark (position #\? target))
         (path (if query-mark (subseq target 0 query-mark) target))
         (query (if query-mark (subseq target (1+ query-mark)) ""))
         (content-length
          (let ((raw (%head-header header-lines "Content-Length")))
            (if raw (parse-integer raw) 0)))
         (body (if (plusp content-length)
                   (%read-exactly stream content-length)
                   (make-array 0 :element-type '(unsigned-byte 8)))))
    (list :method method
          :path path
          :query query
          :headers (loop for line in header-lines
                         for sep = (position #\: line)
                         when sep
                           collect (cons (subseq line 0 sep)
                                         (string-trim " " (subseq line (1+ sep)))))
          :body body)))

(defun %fixture-write-response (stream spec)
  "Write the scripted response SPEC to STREAM and close the framing."
  (let ((status (getf spec :status 200))
        (reason (getf spec :reason "OK"))
        (headers (getf spec :headers))
        (mode (getf spec :body-mode)))
    (write-sequence (%ascii-octets
                     (format nil "HTTP/1.1 ~A ~A~C~C" status reason #\Return #\Linefeed))
                    stream)
    (dolist (entry headers)
      (write-sequence
       (%ascii-octets
        (format nil "~A: ~A~C~C" (car entry) (cdr entry) #\Return #\Linefeed))
       stream))
    (write-sequence (%ascii-octets
                     (format nil "Connection: close~C~C" #\Return #\Linefeed))
                    stream)
    (write-sequence (%ascii-octets
                     (format nil "~C~C" #\Return #\Linefeed))
                    stream)
    (force-output stream)
    (ecase (first mode)
      (:fixed
       (write-sequence (second mode) stream))
      (:chunked
       (dolist (entry (rest mode))
         (typecase entry
           (number (sleep entry))
           ((vector (unsigned-byte 8))
            (write-sequence
             (%ascii-octets (format nil "~(~X~)~C~C" (length entry) #\Return #\Linefeed))
             stream)
            (write-sequence entry stream)
            (write-sequence (%ascii-octets
                             (format nil "~C~C" #\Return #\Linefeed))
                            stream))))
       (write-sequence (%ascii-octets
                        (format nil "0~C~C~C~C" #\Return #\Linefeed #\Return #\Linefeed))
                       stream)))
    (force-output stream)))

(defstruct fixture-server
  listener thread lock (stop nil) (requests nil) response-spec)

(defun %fixture-serve-connection (server stream)
  ;; A misbehaving peer must not take down the test process.
  (ignore-errors
   (unwind-protect
        (let ((request (%fixture-parse-request stream))
              (spec (with-slots (lock response-spec) server
                      (bt:with-lock-held (lock) response-spec))))
          (with-slots (lock requests) server
            (bt:with-lock-held (lock)
              (setf requests (append requests (list request)))))
          (%fixture-write-response stream spec))
     (ignore-errors (close stream)))))

(defun %fixture-upstream (server)
  (unwind-protect
       (loop
         (let ((client (usocket:socket-accept (fixture-server-listener server)
                                              :element-type '(unsigned-byte 8))))
           (format t "DIAG-FIXTURE: accepted connection~%")
           (force-output)
           (bt:make-thread
            (lambda ()
              (%fixture-serve-connection
               server (usocket:socket-stream client)))
            :name "llm-log-fixture-connection")))
    (ignore-errors (usocket:socket-close (fixture-server-listener server)))))

(defun start-fixture-upstream (&key (port +fixture-upstream-port+))
  (let ((listener nil))
    ;; rebind retry: the previous test's listener may need a moment to be
    ;; released by the kernel
    (loop for attempt below 25
          do (handler-case
                 (setf listener
                       (usocket:socket-listen "127.0.0.1" port
                                              :element-type '(unsigned-byte 8)
                                              :reuseaddress t))
               (usocket:address-in-use-error ()
                 (sleep 0.2)))
          until listener)
    (unless listener
      (error "fixture upstream could not bind port ~A" port))
    (let ((server (make-fixture-server
                   :listener listener
                   :lock (bt:make-lock "fixture-lock")
                   :response-spec (list :status 200
                                        :headers '(("Content-Type" . "text/plain"))
                                        :body-mode (list :fixed
                                                         (%ascii-octets "fixture"))))))
      (setf (fixture-server-thread server)
            (bt:make-thread (lambda () (%fixture-upstream server))
                            :name "llm-log-fixture-upstream"))
      (format t "DIAG-FIXTURE: listening on ~A thread live~%" port)
      (force-output)
      server)))

(defun stop-fixture-upstream (server)
  (when (fixture-server-thread server)
    (ignore-errors (bt:destroy-thread (fixture-server-thread server))))
  (ignore-errors (usocket:socket-close (fixture-server-listener server)))
  server)

(defun %wait-for-port (port &optional (timeout 5))
  "Poll-connect until PORT accepts connections or TIMEOUT seconds pass."
  (loop with deadline = (+ (get-universal-time) timeout)
        do (handler-case
               (let ((probe (usocket:socket-connect "127.0.0.1" port
                                                    :element-type '(unsigned-byte 8)
                                                    :timeout 1)))
                 (usocket:socket-close probe)
                 (return-from %wait-for-port t))
             (error ()))
           (when (> (get-universal-time) deadline)
             (return-from %wait-for-port nil))
           (sleep 0.1)))

(defun %random-binary (size)
  (let ((bytes (make-array size :element-type '(unsigned-byte 8))))
    (loop for i below size
          do (setf (aref bytes i) (random 256)))
    bytes))

(defun %client-request (port method target &key headers body)
  "Issue one raw HTTP/1.1 request against 127.0.0.1:PORT, read the full
response to EOF; return (VALUES status headers-list body-octets)."
  (let ((socket (usocket:socket-connect "127.0.0.1" port
                                        :element-type '(unsigned-byte 8)
                                        :timeout 30)))
    (unwind-protect
         (let ((stream (usocket:socket-stream socket)))
           (write-sequence
            (%ascii-octets
             (format nil "~A ~A HTTP/1.1~C~CHost: 127.0.0.1~C~CConnection: close~C~C"
                     method target #\Return #\Linefeed #\Return #\Linefeed
                     #\Return #\Linefeed))
            stream)
           (dolist (entry headers)
             (write-sequence
              (%ascii-octets
               (format nil "~A: ~A~C~C" (car entry) (cdr entry) #\Return #\Linefeed))
              stream))
           (when body
             (write-sequence
              (%ascii-octets
               (format nil "Content-Length: ~A~C~C" (length body)
                       #\Return #\Linefeed))
              stream))
           (write-sequence (%ascii-octets
                            (format nil "~C~C" #\Return #\Linefeed))
                           stream)
           (when body
             (write-sequence body stream))
           (force-output stream)
           (let ((response (make-array 0 :element-type '(unsigned-byte 8)
                                       :fill-pointer 0 :adjustable t)))
             (loop with buffer = (make-array 65536 :element-type '(unsigned-byte 8))
                   for n = (read-sequence buffer stream)
                   do (loop for i below n
                            do (vector-push-extend (aref buffer i) response))
                   until (zerop n))
             (let* ((head-end (loop for i from 3 below (length response)
                                    when (and (= (aref response (- i 3)) 13)
                                              (= (aref response (- i 2)) 10)
                                              (= (aref response (- i 1)) 13)
                                              (= (aref response i) 10))
                                      return i))
                    (lines (when head-end
                             (%head-lines (subseq response 0 head-end))))
                    (status (when lines
                              (parse-integer (second (uiop:split-string (first lines) :separator " "))))))
               (values
                status
                (loop for line in (rest lines)
                      for sep = (position #\: line)
                      when sep
                        collect (cons (subseq line 0 sep)
                                      (string-trim " " (subseq line (1+ sep)))))
                (if head-end
                    (subseq response (1+ head-end))
                    response)))))
      (ignore-errors (usocket:socket-close socket)))))

(defparameter +fixture-port-counter+ 0)

(defun %next-fixture-ports ()
  "Each test gets its own fixture/proxy port pair so lingering connection
sockets from one test can never affect the next."
  (let ((n (incf +fixture-port-counter+)))
    (values (+ 18770 (* n 2)) (+ 18771 (* n 2)))))

(defmacro with-fixture-proxy ((proxy-var upstream-var) &body body)
  (let ((upstream-port (gensym "UPSTREAM-PORT"))
        (proxy-port (gensym "PROXY-PORT")))
    `(multiple-value-bind (,upstream-port ,proxy-port) (%next-fixture-ports)
       (let ((+fixture-upstream-port+ ,upstream-port)
             (+fixture-proxy-port+ ,proxy-port)
             (,proxy-var nil)
             (,upstream-var nil))
         (unwind-protect
              (locally
                (setf ,upstream-var (start-fixture-upstream))
                (setf ,proxy-var
                      (start-proxy
                       (resolve-config
                        :config-file nil
                        :port +fixture-proxy-port+
                        :upstreams (list (validate-upstream
                                          "fixture"
                                          (format nil "http://127.0.0.1:~A"
                                                  +fixture-upstream-port+))))))
                (unless (%wait-for-port +fixture-proxy-port+)
                  (error "llm-log proxy did not start"))
                ,@body)
           (when ,proxy-var
             (stop-proxy ,proxy-var))
           (when ,upstream-var
             (stop-fixture-upstream ,upstream-var)))))))

(deftest http-method-path-and-query-are-preserved
  (with-fixture-proxy (proxy upstream)
    (multiple-value-bind (status headers body)
        (%client-request +fixture-proxy-port+ "GET"
                         "/fixture/v1/models?foo=bar&x=1")
      (declare (ignore headers body))
      (ok (eql status 200))
      (let ((request (first (fixture-server-requests upstream))))
        (ok (equal (getf request :method) "GET"))
        (ok (equal (getf request :path) "/v1/models"))
        (ok (equal (getf request :query) "foo=bar&x=1"))))))

(deftest http-request-and-response-bodies-round-trip-binary-identically
  (with-fixture-proxy (proxy upstream)
    (let ((payload (%random-binary 65536)))
      (setf (fixture-server-response-spec upstream)
            (list :status 200
                  :headers '(("Content-Type" . "application/octet-stream"))
                  :body-mode (list :fixed payload)))
      (multiple-value-bind (status headers body)
          (%client-request +fixture-proxy-port+ "POST"
                           "/fixture/v1/embeddings"
                           :headers '(("Content-Type" . "application/octet-stream"))
                           :body payload)
        (declare (ignore headers))
        (ok (eql status 200))
        (ok (equalp body payload))
        (let ((request (first (fixture-server-requests upstream))))
          (ok (equalp (getf request :body) payload))
          (ok (equal (%head-header
                      (mapcar (lambda (entry) (format nil "~A: ~A" (car entry) (cdr entry)))
                              (getf request :headers))
                      "Content-Type")
                     "application/octet-stream")))))))

(deftest http-status-is-preserved
  (with-fixture-proxy (proxy upstream)
    (setf (fixture-server-response-spec upstream)
          (list :status 418
                :reason "I'm a teapot"
                :headers '(("Content-Type" . "text/plain"))
                :body-mode (list :fixed (%ascii-octets "short and stout"))))
    (multiple-value-bind (status headers body)
        (%client-request +fixture-proxy-port+ "GET" "/fixture/v1/pot")
      (ok (eql status 418))
      (ok (equalp body (%ascii-octets "short and stout")))
      (ok (%head-header
           (mapcar (lambda (entry) (format nil "~A: ~A" (car entry) (cdr entry)))
                   headers)
           "Content-Type")))))

(deftest duplicate-response-headers-are-preserved
  (with-fixture-proxy (proxy upstream)
    (setf (fixture-server-response-spec upstream)
          (list :status 200
                :headers '(("Set-Cookie" . "first=1; Path=/")
                           ("Set-Cookie" . "second=2; Path=/")
                           ("Content-Type" . "text/plain"))
                :body-mode (list :fixed (%ascii-octets "ok"))))
    (multiple-value-bind (status headers body)
        (%client-request +fixture-proxy-port+ "GET" "/fixture/v1/login")
      (declare (ignore status body))
      (let ((cookies (loop for entry in headers
                           when (string-equal (car entry) "Set-Cookie")
                             collect (cdr entry))))
        (ok (= (length cookies) 2))
        (ok (member "first=1; Path=/" cookies :test #'equal))
        (ok (member "second=2; Path=/" cookies :test #'equal))))))

(deftest chunked-responses-stream-incrementally
  (with-fixture-proxy (proxy upstream)
    (setf (fixture-server-response-spec upstream)
          (list :status 200
                :headers '(("Content-Type" . "text/event-stream"))
                :body-mode (list :chunked
                                 (%ascii-octets
                                  (format nil "data: first~C~C"
                                          #\Return #\Linefeed))
                                 0.5
                                 (%ascii-octets
                                  (format nil "data: second~C~C"
                                          #\Return #\Linefeed)))))
    (let ((socket (usocket:socket-connect "127.0.0.1" +fixture-proxy-port+
                                          :element-type '(unsigned-byte 8)
                                          :timeout 30)))
      (unwind-protect
           (let ((stream (usocket:socket-stream socket)))
             (write-sequence
              (%ascii-octets
               (format nil "GET /fixture/v1/stream HTTP/1.1~C~CHost: t~C~CConnection: close~C~C~C~C"
                       #\Return #\Linefeed #\Return #\Linefeed #\Return #\Linefeed
                       #\Return #\Linefeed))
              stream)
             (force-output stream)
             (let ((buffer (make-array 16 :element-type '(unsigned-byte 8)))
                   (first-read-at nil)
                   (done nil)
                   (total 0))
               (loop until done
                     do (let ((n (read-sequence buffer stream)))
                          (when (plusp n)
                            (unless first-read-at
                              (setf first-read-at (get-internal-real-time)))
                            (incf total n))
                          (when (< n 16)
                            (setf done t))))
               (let ((gap (/ (- (get-internal-real-time) first-read-at)
                             internal-time-units-per-second)))
                 (ok (>= total 20))
                 (ok (>= gap 0.3))
                 (ok (<= gap 5.0)))))
        (ignore-errors (usocket:socket-close socket))))))

(deftest large-bodies-stream-without-full-buffering-deadlock
  (with-fixture-proxy (proxy upstream)
    (let ((payload (%random-binary (* 8 1024 1024))))
      (setf (fixture-server-response-spec upstream)
            (list :status 200
                  :headers '(("Content-Type" . "application/octet-stream"))
                  :body-mode (list :fixed payload)))
      (multiple-value-bind (status headers body)
          (%client-request +fixture-proxy-port+ "PUT" "/fixture/v1/large"
                           :headers '(("Content-Type" . "application/octet-stream"))
                           :body payload)
        (declare (ignore headers))
        (ok (eql status 200))
        (ok (equalp body payload))))))

(deftest unknown-provider-is-rejected
  (with-fixture-proxy (proxy upstream)
    (multiple-value-bind (status body)
        (%client-request +fixture-proxy-port+ "GET" "/nosuch/v1/models")
      (declare (ignore body))
      (ok (eql status 404)))))

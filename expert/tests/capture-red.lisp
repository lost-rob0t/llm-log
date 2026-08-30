(in-package #:llm-log-expert-test)

(defun %slurp-binary-file (path)
  (with-open-file (stream path :direction :input :element-type '(unsigned-byte 8))
    (let ((result (make-array (file-length stream) :element-type '(unsigned-byte 8))))
      (read-sequence result stream)
      result)))

(defun %slurp-text-file (path)
  (uiop:read-file-string path))

(defun run-http-capture-contract ()
  (let* ((listener (%listen-loopback))
         (upstream-port (%socket-port listener))
         (captured-request nil)
         (server-error nil)
         (server-thread
           (sb-thread:make-thread
            (lambda ()
              (handler-case
                  (let ((peer (sb-bsd-sockets:socket-accept listener)))
                    (unwind-protect
                         (let ((stream (%socket-stream peer)))
                           (setf captured-request (%read-http-message stream))
                           (write-sequence
                            (%concat-octets
                             (%ascii-octets
                              (format nil
                                      "HTTP/1.1 201 Created~C~CContent-Length: 5~C~CX-Visible-Response: keep-response~C~CSet-Cookie: sid=secret-cookie-a; Path=/~C~CSet-Cookie: refresh=secret-cookie-b; Path=/~C~CConnection: close~C~C~C~C"
                                      #\Return #\Linefeed #\Return #\Linefeed
                                      #\Return #\Linefeed #\Return #\Linefeed
                                      #\Return #\Linefeed #\Return #\Linefeed
                                      #\Return #\Linefeed))
                             (%octets 114 101 115 0 112))
                            stream)
                           (finish-output stream)
                           (close stream))
                      (ignore-errors (sb-bsd-sockets:socket-close peer))))
                (error (condition)
                  (setf server-error condition))))
            :name "llm-log-capture-fixture"))
         (data-directory
           (merge-pathnames
            (format nil "llm-log-capture-red-~A/" (gensym))
            (uiop:temporary-directory)))
         (config
           (llm-log-expert:make-runtime-config
            :data-directory data-directory
            :listen-address "127.0.0.1"
            :port 0
            :upstreams `(("fixture" . ,(format nil "http://127.0.0.1:~D" upstream-port)))))
         (proxy nil))
    (unwind-protect
         (progn
           (setf proxy (llm-log-expert:start-http-proxy config))
           (let* ((client (%connect-loopback (llm-log-expert:proxy-listen-port proxy)))
                  (stream (%socket-stream client))
                  (request
                    (%concat-octets
                     (%ascii-octets
                      (format nil
                              "POST /fixture/v1/capture?x=1 HTTP/1.1~C~CHost: 127.0.0.1~C~CContent-Type: application/octet-stream~C~CContent-Length: 5~C~CAuthorization: Bearer secret-auth-sentinel~C~CX-Api-Key: secret-api-sentinel~C~CCookie: session=secret-cookie-request~C~CX-Visible-Request: keep-request~C~CConnection: close~C~C~C~C"
                              #\Return #\Linefeed #\Return #\Linefeed
                              #\Return #\Linefeed #\Return #\Linefeed
                              #\Return #\Linefeed #\Return #\Linefeed
                              #\Return #\Linefeed #\Return #\Linefeed
                              #\Return #\Linefeed))
                     (%octets 114 101 113 0 98))))
             (unwind-protect
                  (progn
                    (write-sequence request stream)
                    (finish-output stream)
                    (let* ((response (%read-all-octets stream))
                           (response-string (%octets-as-string response)))
                      (ok (search "HTTP/1.1 201" response-string))
                      (ok (search "sid=secret-cookie-a" response-string))
                      (ok (search "refresh=secret-cookie-b" response-string))))
               (ignore-errors (close stream))
               (ignore-errors (sb-bsd-sockets:socket-close client))))
           (sb-thread:join-thread server-thread)
           (when server-error (error server-error))
           (let ((upstream-string (%octets-as-string captured-request)))
             (ok (search "Bearer secret-auth-sentinel" upstream-string)
                 "forwarding must preserve Authorization")
             (ok (search "secret-api-sentinel" upstream-string)
                 "forwarding must preserve X-Api-Key")
             (ok (search "session=secret-cookie-request" upstream-string)
                 "forwarding must preserve Cookie"))
           (let* ((events-root (merge-pathnames #P"capture/http/events/" data-directory))
                  (events (and (probe-file events-root) (uiop:subdirectories events-root))))
             (ok (= 1 (length events)) "one completed HTTP request must create one immutable capture event")
             (when (= 1 (length events))
               (let* ((event (first events))
                      (metadata-path (merge-pathnames #P"metadata.json" event))
                      (request-path (merge-pathnames #P"request-body.bin" event))
                      (response-path (merge-pathnames #P"response-body.bin" event)))
                 (ok (probe-file metadata-path))
                 (ok (probe-file request-path))
                 (ok (probe-file response-path))
                 (when (and (probe-file metadata-path) (probe-file request-path) (probe-file response-path))
                   (let ((metadata (%slurp-text-file metadata-path)))
                     (ok (search "llm-log.capture/http-v1" metadata))
                     (ok (search "<redacted>" metadata))
                     (ok (search "keep-request" metadata))
                     (ok (search "keep-response" metadata))
                     (ok (= 2 (%count-substring "Set-Cookie" metadata))
                         "capture metadata must preserve duplicate response-header entries")
                     (ok (not (search "secret-auth-sentinel" metadata)))
                     (ok (not (search "secret-api-sentinel" metadata)))
                     (ok (not (search "secret-cookie-request" metadata)))
                     (ok (not (search "secret-cookie-a" metadata)))
                     (ok (not (search "secret-cookie-b" metadata))))
                   (ok (equalp (%octets 114 101 113 0 98) (%slurp-binary-file request-path))
                       "request body evidence must preserve exact octets")
                   (ok (equalp (%octets 114 101 115 0 112) (%slurp-binary-file response-path))
                       "response body evidence must preserve exact octets"))))))
      (when proxy (llm-log-expert:stop-http-proxy proxy))
      (ignore-errors (sb-bsd-sockets:socket-close listener))
      (when (and server-thread (sb-thread:thread-alive-p server-thread))
        (ignore-errors (sb-thread:terminate-thread server-thread)))
      (ignore-errors (uiop:delete-directory-tree data-directory :validate t)))))

(deftest common-lisp-http-capture-redaction-red
  (testing "finite HTTP capture preserves bodies and redacts persisted credentials"
    (run-http-capture-contract)))

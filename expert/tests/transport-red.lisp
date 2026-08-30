(in-package #:llm-log-expert-test)

(eval-when (:compile-toplevel :load-toplevel :execute)
  (require :sb-bsd-sockets))

(defun runtime-symbol-function-p (name)
  (multiple-value-bind (symbol status)
      (find-symbol name :llm-log-expert)
    (and status (fboundp symbol))))

(defun runtime-function (name)
  (multiple-value-bind (symbol status)
      (find-symbol name :llm-log-expert)
    (and status (fboundp symbol) (symbol-function symbol))))

(defun %octets (&rest bytes)
  (make-array (length bytes)
              :element-type '(unsigned-byte 8)
              :initial-contents bytes))

(defun %ascii-octets (string)
  (let ((result (make-array (length string) :element-type '(unsigned-byte 8))))
    (loop for character across string
          for index from 0
          do (setf (aref result index) (char-code character)))
    result))

(defun %concat-octets (&rest vectors)
  (let* ((size (reduce #'+ vectors :key #'length :initial-value 0))
         (result (make-array size :element-type '(unsigned-byte 8))))
    (loop with offset = 0
          for vector in vectors
          do (replace result vector :start1 offset)
             (incf offset (length vector)))
    result))

(defun %octets-as-string (octets)
  (map 'string #'code-char octets))

(defun %read-all-octets (stream)
  (let ((buffer (make-array 4096 :element-type '(unsigned-byte 8)))
        (result (make-array 0
                            :element-type '(unsigned-byte 8)
                            :adjustable t
                            :fill-pointer 0)))
    (loop for count = (read-sequence buffer stream)
          while (plusp count)
          do (loop for index below count
                   do (vector-push-extend (aref buffer index) result)))
    result))

(defun %header-end (octets)
  (loop for index from 3 below (length octets)
        when (and (= 13 (aref octets (- index 3)))
                  (= 10 (aref octets (- index 2)))
                  (= 13 (aref octets (- index 1)))
                  (= 10 (aref octets index)))
          do (return (1+ index))))

(defun %read-http-message (stream)
  (let ((result (make-array 0
                            :element-type '(unsigned-byte 8)
                            :adjustable t
                            :fill-pointer 0))
        header-end)
    (loop until header-end
          for byte = (read-byte stream nil nil)
          do (when (null byte)
               (error "HTTP peer closed before headers completed"))
             (vector-push-extend byte result)
             (setf header-end (%header-end result)))
    (let* ((headers (%octets-as-string (subseq result 0 header-end)))
           (marker "Content-Length:")
           (position (search marker headers :test #'char-equal))
           (content-length
             (if position
                 (parse-integer headers
                                :start (+ position (length marker))
                                :junk-allowed t)
                 0)))
      (dotimes (ignored content-length)
        (declare (ignore ignored))
        (let ((byte (read-byte stream nil nil)))
          (when (null byte)
            (error "HTTP peer closed before body completed"))
          (vector-push-extend byte result))))
    result))

(defun %listen-loopback ()
  (let ((socket (make-instance 'sb-bsd-sockets:inet-socket
                               :type :stream
                               :protocol :tcp)))
    (sb-bsd-sockets:socket-bind socket #(127 0 0 1) 0)
    (sb-bsd-sockets:socket-listen socket 5)
    socket))

(defun %socket-port (socket)
  (nth-value 1 (sb-bsd-sockets:socket-name socket)))

(defun %socket-stream (socket)
  (sb-bsd-sockets:socket-make-stream
   socket
   :input t
   :output t
   :element-type '(unsigned-byte 8)
   :buffering :none))

(defun %connect-loopback (port)
  (let ((socket (make-instance 'sb-bsd-sockets:inet-socket
                               :type :stream
                               :protocol :tcp)))
    (sb-bsd-sockets:socket-connect socket #(127 0 0 1) port)
    socket))

(defun %count-substring (needle haystack)
  (loop with count = 0
        with start = 0
        for position = (search needle haystack :start2 start :test #'char-equal)
        while position
        do (incf count)
           (setf start (+ position (length needle)))
        finally (return count)))

(defun run-http-fidelity-contract ()
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
                                      "HTTP/1.1 418 I'm a teapot~C~CContent-Length: 7~C~CContent-Type: application/octet-stream~C~CSet-Cookie: alpha=1; Path=/~C~CSet-Cookie: beta=2; Path=/~C~CConnection: close~C~C~C~C"
                                      #\Return #\Linefeed #\Return #\Linefeed
                                      #\Return #\Linefeed #\Return #\Linefeed
                                      #\Return #\Linefeed #\Return #\Linefeed
                                      #\Return #\Linefeed))
                             (%octets 116 101 97 0 112 111 116))
                            stream)
                           (finish-output stream)
                           (close stream))
                      (ignore-errors (sb-bsd-sockets:socket-close peer))))
                (error (condition)
                  (setf server-error condition))))
            :name "llm-log-http-fixture"))
         (constructor (runtime-function "MAKE-RUNTIME-CONFIG"))
         (start-proxy (runtime-function "START-HTTP-PROXY"))
         (stop-proxy (runtime-function "STOP-HTTP-PROXY"))
         (listen-port (runtime-function "PROXY-LISTEN-PORT"))
         (data-directory
           (merge-pathnames
            (format nil "llm-log-http-red-~A/" (gensym))
            (uiop:temporary-directory)))
         (config
           (funcall constructor
                    :data-directory data-directory
                    :listen-address "127.0.0.1"
                    :port 0
                    :upstreams `(("fixture" . ,(format nil "http://127.0.0.1:~D"
                                                        upstream-port)))))
         (proxy nil))
    (unwind-protect
         (progn
           (setf proxy (funcall start-proxy config))
           (let* ((client (%connect-loopback (funcall listen-port proxy)))
                  (stream (%socket-stream client))
                  (request
                    (%concat-octets
                     (%ascii-octets
                      (format nil
                              "POST /fixture/v1/messages?alpha=1&alpha=2&encoded=%2Fkeep HTTP/1.1~C~CHost: 127.0.0.1~C~CContent-Type: application/octet-stream~C~CContent-Length: 8~C~CX-Fidelity: keep-me~C~CConnection: close~C~C~C~C"
                              #\Return #\Linefeed #\Return #\Linefeed
                              #\Return #\Linefeed #\Return #\Linefeed
                              #\Return #\Linefeed #\Return #\Linefeed
                              #\Return #\Linefeed))
                     (%octets 114 101 113 0 98 111 100 121))))
             (unwind-protect
                  (progn
                    (write-sequence request stream)
                    (finish-output stream)
                    (let* ((response (%read-all-octets stream))
                           (response-header-end (%header-end response))
                           (response-headers
                             (%octets-as-string (subseq response 0 response-header-end)))
                           (response-body (subseq response response-header-end)))
                      (ok (search "HTTP/1.1 418" response-headers)
                          "proxy must preserve upstream HTTP status")
                      (ok (= 2 (%count-substring "Set-Cookie:" response-headers))
                          "proxy must preserve duplicate Set-Cookie fields")
                      (ok (equalp (%octets 116 101 97 0 112 111 116) response-body)
                          "proxy must preserve response body octets")))
               (ignore-errors (close stream))
               (ignore-errors (sb-bsd-sockets:socket-close client))))
           (sb-thread:join-thread server-thread)
           (when server-error
             (error server-error))
           (let* ((request-header-end (%header-end captured-request))
                  (request-headers
                    (%octets-as-string (subseq captured-request 0 request-header-end)))
                  (request-body (subseq captured-request request-header-end)))
             (ok (search
                  "POST /v1/messages?alpha=1&alpha=2&encoded=%2Fkeep HTTP/1.1"
                  request-headers)
                 "proxy must preserve method/path/query and remove provider selector exactly once")
             (ok (search "X-Fidelity: keep-me" request-headers :test #'char-equal)
                 "proxy must preserve end-to-end request headers")
             (ok (equalp (%octets 114 101 113 0 98 111 100 121) request-body)
                 "proxy must preserve request body octets")))
      (when proxy
        (funcall stop-proxy proxy))
      (ignore-errors (sb-bsd-sockets:socket-close listener))
      (when (and server-thread (sb-thread:thread-alive-p server-thread))
        (ignore-errors (sb-thread:terminate-thread server-thread)))
      (ignore-errors (uiop:delete-directory-tree data-directory :validate t)))))

(deftest common-lisp-http-proxy-surface-red
  (testing "zero-Python runtime must own the HTTP proxy lifecycle"
    (ok (runtime-symbol-function-p "MAKE-RUNTIME-CONFIG")
        "Common Lisp must expose MAKE-RUNTIME-CONFIG for typed black-box configuration")
    (ok (runtime-symbol-function-p "START-HTTP-PROXY")
        "Common Lisp must expose START-HTTP-PROXY before transport implementation can be accepted")
    (ok (runtime-symbol-function-p "STOP-HTTP-PROXY")
        "Common Lisp must expose STOP-HTTP-PROXY before transport implementation can be accepted")
    (ok (runtime-symbol-function-p "PROXY-LISTEN-PORT")
        "Common Lisp must expose PROXY-LISTEN-PORT so black-box tests can bind an ephemeral port")
    (ok (multiple-value-bind (symbol status)
            (find-symbol "RUNTIME-CONFIG-UPSTREAMS" :llm-log-expert)
          (and status (fboundp symbol)))
        "Common Lisp runtime config must expose provider/upstream mapping")))

(deftest common-lisp-http-fidelity-black-box-red
  (testing "ordinary HTTP forwarding preserves the migration contract at raw boundaries"
    (let ((surface-ready
            (and (runtime-symbol-function-p "MAKE-RUNTIME-CONFIG")
                 (runtime-symbol-function-p "START-HTTP-PROXY")
                 (runtime-symbol-function-p "STOP-HTTP-PROXY")
                 (runtime-symbol-function-p "PROXY-LISTEN-PORT")
                 (multiple-value-bind (symbol status)
                     (find-symbol "RUNTIME-CONFIG-UPSTREAMS" :llm-log-expert)
                   (and status (fboundp symbol))))))
      (when surface-ready
        (run-http-fidelity-contract)))))

(deftest common-lisp-runtime-config-surface
  (testing "runtime configuration is owned by Common Lisp"
    (let ((config (llm-log-expert:make-default-runtime-config)))
      (ok (pathnamep (llm-log-expert:runtime-config-data-directory config)))
      (ok (string= "127.0.0.1"
                   (llm-log-expert:runtime-config-listen-address config)))
      (ok (= 8787 (llm-log-expert:runtime-config-port config))))))

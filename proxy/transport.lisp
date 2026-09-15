(in-package #:llm-log)

;; Common Lisp transparent forwarding core.
;;
;; The inbound HTTP server owns the client socket.  llm-log never wraps or
;; closes a server-owned file descriptor.  Provider responses are streamed
;; through Clack's delayed-response writer while the same decoded body octets
;; are tee'd into the immutable capture record.

(defparameter +hop-by-hop-headers+
  '("connection" "keep-alive" "proxy-authenticate" "proxy-authorization"
    "te" "trailer" "trailers" "transfer-encoding" "upgrade"))

(defparameter +hop-by-hop-request-headers+
  (append +hop-by-hop-headers+ '("host" "content-length" "expect")))

(defparameter +relay-buffer-size+ 65536)

(defstruct proxy-server
  thread config)

(defun %utf8-octets (string)
  (trivial-utf-8:string-to-utf-8-bytes string))

(defun %crlf ()
  (make-array 2 :element-type '(unsigned-byte 8) :initial-contents '(13 10)))

(defun %header-name-p (name names)
  (member name names :test #'string-equal))

(defun %upstream-host-header (uri)
  (let ((host (quri:uri-host uri))
        (port (quri:uri-port uri))
        (scheme (quri:uri-scheme uri)))
    (if (and port
             (not (or (and (equal scheme "http") (= port 80))
                      (and (equal scheme "https") (= port 443)))))
        (format nil "~A:~A" host port)
        host)))

(defun %resolve-provider (config uri)
  "Split inbound URI into provider, origin-form target, and upstream base URL."
  (let* ((path-end (or (position #\? uri) (length uri)))
         (path (subseq uri 0 path-end))
         (query (if (< path-end (length uri)) (subseq uri path-end) ""))
         (rest (if (and (> (length path) 1) (char= (char path 0) #\/))
                   (subseq path 1)
                   "")))
    (let ((slash (position #\/ rest)))
      (unless slash
        (return-from %resolve-provider (values nil nil nil)))
      (let* ((provider (subseq rest 0 slash))
             (base (upstream-base-url config provider)))
        (unless base
          (return-from %resolve-provider (values provider nil nil)))
        (values provider
                (concatenate 'string (subseq rest slash) query)
                base)))))

(defun %open-upstream (upstream-url)
  "Open one blocking TCP/TLS connection to UPSTREAM-URL."
  (let* ((uri (quri:uri upstream-url))
         (host (quri:uri-host uri))
         (scheme (quri:uri-scheme uri))
         (port (or (quri:uri-port uri)
                   (if (equal scheme "https") 443 80)))
         (socket (usocket:socket-connect host port
                                         :element-type '(unsigned-byte 8)
                                         :timeout 30)))
    (if (equal scheme "https")
        (values (cl+ssl:make-ssl-client-stream
                 (usocket:socket-stream socket)
                 :hostname host)
                socket)
        (values (usocket:socket-stream socket) socket))))

(defun %request-headers-for-upstream (env)
  "Copy Clack headers and restore Content-Type, which Clack exposes separately."
  (let ((result (make-hash-table :test #'equalp))
        (headers (getf env :headers)))
    (when headers
      (maphash (lambda (name value) (setf (gethash name result) value)) headers))
    (let ((content-type (getf env :content-type)))
      (when content-type (setf (gethash "content-type" result) content-type)))
    result))

(defun %write-upstream-request (stream method target host-header headers body)
  (write-sequence
   (%utf8-octets (format nil "~A ~A HTTP/1.1~C~C"
                          method target #\Return #\Linefeed))
   stream)
  (write-sequence
   (%utf8-octets (format nil "Host: ~A~C~C" host-header #\Return #\Linefeed))
   stream)
  (maphash
   (lambda (name value)
     (unless (%header-name-p name +hop-by-hop-request-headers+)
       (write-sequence
        (%utf8-octets (format nil "~A: ~A~C~C"
                              name value #\Return #\Linefeed))
        stream)))
   headers)
  (write-sequence
   (%utf8-octets (format nil "Content-Length: ~D~C~C"
                          (length body) #\Return #\Linefeed))
   stream)
  (write-sequence (%crlf) stream)
  (when (plusp (length body)) (write-sequence body stream))
  (force-output stream))

(defun %read-head-octets (stream)
  (let ((head (make-array 0 :element-type '(unsigned-byte 8)
                          :fill-pointer 0 :adjustable t)))
    (loop for byte = (read-byte stream)
          do (vector-push-extend byte head)
          when (and (>= (length head) 4)
                    (= (aref head (- (length head) 4)) 13)
                    (= (aref head (- (length head) 3)) 10)
                    (= (aref head (- (length head) 2)) 13)
                    (= (aref head (- (length head) 1)) 10))
            return head)))

(defun %response-lines (head)
  (loop for line in
          (uiop:split-string (trivial-utf-8:utf-8-bytes-to-string head)
                             :separator (format nil "~C~C" #\Return #\Linefeed))
        when (plusp (length line)) collect line))

(defun %parse-response-head (head)
  "Return STATUS and ordered (NAME . VALUE) response headers."
  (let* ((lines (%response-lines head))
         (status-parts (uiop:split-string (first lines) :separator '(#\Space)))
         (status (parse-integer (second status-parts)))
         (headers
           (loop for line in (rest lines)
                 for sep = (position #\: line)
                 when sep
                   collect (cons (subseq line 0 sep)
                                 (string-trim '(#\Space #\Tab)
                                              (subseq line (1+ sep)))))))
    (values status headers)))

(defun %response-header (headers name)
  (cdr (find name headers :key #'car :test #'string-equal)))

(defun %response-content-length (headers)
  (let ((raw (%response-header headers "Content-Length")))
    (and raw (parse-integer raw :junk-allowed nil))))

(defun %response-chunked-p (headers)
  (let ((raw (%response-header headers "Transfer-Encoding")))
    (and raw (search "chunked" raw :test #'char-equal))))

(defun %read-exactly-to-sink (stream count sink)
  (let ((remaining count)
        (buffer (make-array +relay-buffer-size+ :element-type '(unsigned-byte 8))))
    (loop while (plusp remaining)
          for wanted = (min remaining (length buffer))
          for n = (read-sequence buffer stream :end wanted)
          do (when (zerop n) (error "upstream closed before Content-Length body"))
             (funcall sink buffer 0 n)
             (decf remaining n))))

(defun %read-until-eof-to-sink (stream sink)
  (let ((buffer (make-array +relay-buffer-size+ :element-type '(unsigned-byte 8))))
    (loop for n = (read-sequence buffer stream)
          until (zerop n)
          do (funcall sink buffer 0 n))))

(defun %read-crlf-line (stream)
  (with-output-to-string (out)
    (loop for byte = (read-byte stream)
          do (cond
               ((= byte 13)
                (unless (= (read-byte stream) 10)
                  (error "malformed upstream CRLF"))
                (return))
               (t (write-char (code-char byte) out))))))

(defun %read-chunked-to-sink (stream sink)
  "Decode upstream chunk framing while preserving body-chunk timing."
  (loop
    for size-line = (%read-crlf-line stream)
    for semi = (position #\; size-line)
    for size = (parse-integer (if semi (subseq size-line 0 semi) size-line)
                              :radix 16 :junk-allowed nil)
    do (if (zerop size)
           (progn
             ;; consume trailers through their terminating empty line
             (loop for trailer = (%read-crlf-line stream)
                   until (zerop (length trailer)))
             (return))
           (progn
             (%read-exactly-to-sink stream size sink)
             (unless (and (= (read-byte stream) 13)
                          (= (read-byte stream) 10))
               (error "malformed upstream chunk terminator"))))))

(defun %relay-response-body (stream headers sink)
  (cond
    ((%response-chunked-p headers)
     (%read-chunked-to-sink stream sink))
    ((%response-content-length headers)
     (%read-exactly-to-sink stream (%response-content-length headers) sink))
    (t
     (%read-until-eof-to-sink stream sink))))

(defun %clack-header-key (name)
  (intern (string-upcase name) :keyword))

(defun %downstream-headers (headers)
  "Convert ordered upstream headers to a Clack plist and reframe downstream.

Transfer-Encoding is owned by the downstream server.  Connection is always
closed after a provider exchange, which also lets unknown-length upstream
responses stream without inventing a Content-Length."
  (let ((result nil))
    (dolist (entry headers)
      (unless (%header-name-p (car entry)
                              (append +hop-by-hop-headers+
                                      '("content-length")))
        (setf result
              (append result
                      (list (%clack-header-key (car entry)) (cdr entry))))))
    (append result '(:connection "close"))))

(defun %request-body-octets (raw-body)
  (etypecase raw-body
    (vector raw-body)
    (null (make-array 0 :element-type '(unsigned-byte 8)))
    (stream
     (let ((out (make-array 0 :element-type '(unsigned-byte 8)
                            :fill-pointer 0 :adjustable t))
           (buffer (make-array +relay-buffer-size+
                               :element-type '(unsigned-byte 8))))
       (loop for n = (read-sequence buffer raw-body)
             until (zerop n)
             do (loop for i below n do (vector-push-extend (aref buffer i) out)))
       out))))

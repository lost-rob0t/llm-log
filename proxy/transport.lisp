(in-package #:llm-log)

;; HTTP transparent forwarding core (zero-Python rewrite, slice 2).
;;
;; Woo owns the inbound evented server. Each accepted request spawns one
;; relay thread that owns the upstream connection and the client socket
;; until the exchange completes; response bytes from the upstream are
;; relayed verbatim (octet-level), so chunked framing, content-length
;; framing, duplicate response headers and the reason phrase survive.
;; See research/LLM-LOG-RESEARCH-012-cl-runtime-slice-1.org.

(defparameter +hop-by-hop-headers+
  '("connection" "keep-alive" "proxy-authenticate" "proxy-authorization"
    "te" "trailer" "trailers" "transfer-encoding" "upgrade")
  "Hop-by-hop headers per HTTP/1.1 proxy semantics; never forwarded.")

(defparameter +hop-by-hop-request-headers+
  (append +hop-by-hop-headers+ '("host" "content-length" "expect"))
  "Headers dropped from the inbound request head. Host is rebuilt for the
upstream and Content-Length is recomputed from the forwarded octets.")

(defparameter +relay-buffer-size+ 65536)

(defstruct proxy-server
  thread config)

(defun %utf8-octets (string)
  (trivial-utf-8:string-to-utf-8-bytes string))

(defun %crlf ()
  (make-array 2 :element-type '(unsigned-byte 8) :initial-contents '(13 10)))

(defun %header-name-p (name names)
  (member name names :test #'string-equal))

(defun %write-line-octets (stream octets)
  (write-sequence octets stream)
  (write-sequence (%crlf) stream))

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
  "Split the inbound target URI into VALUES provider, upstream-target.

The first path segment selects the provider; the remainder (raw, encoding
preserved) is appended to the configured upstream base URL."
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
             (upstream-target
              (concatenate 'string
                           (upstream-base-url config provider)
                           (subseq rest slash)
                           query)))
        (values provider
                upstream-target
                (upstream-base-url config provider))))))

(defun %open-upstream (upstream-url)
  "Open one TCP/TLS connection to UPSTREAM-URL; return the octet stream."
  (let* ((uri (quri:uri upstream-url))
         (host (quri:uri-host uri))
         (port (quri:uri-port uri))
         (scheme (quri:uri-scheme uri))
         (port (cond (port port)
                     ((equal scheme "https") 443)
                     (t 80)))
         (socket (usocket:socket-connect host port
                                         :element-type '(unsigned-byte 8)
                                         :timeout 30)))
    (if (equal scheme "https")
        (values (cl+ssl:make-ssl-client-stream
                 (usocket:socket-stream socket)
                 :hostname host)
                socket)
        (values (usocket:socket-stream socket) socket))))

(defun %write-upstream-request (stream method target host-header headers body)
  "Serialize one HTTP/1.1 request from METHOD, TARGET, HOST-HEADER, the
relayable inbound HEADERS and BODY."
  (write-sequence (%utf8-octets
                   (format nil "~A ~A HTTP/1.1~C~C"
                           method target #\Return #\Linefeed))
                  stream)
  (write-sequence (%utf8-octets
                   (format nil "Host: ~A~C~C" host-header #\Return #\Linefeed))
                  stream)
  (maphash (lambda (name value)
             (unless (%header-name-p name +hop-by-hop-request-headers+)
               (write-sequence (%utf8-octets
                                (format nil "~A: ~A~C~C" name value
                                        #\Return #\Linefeed))
                               stream)))
           headers)
  (write-sequence (%utf8-octets
                   (format nil "Content-Length: ~A~C~C"
                           (length body) #\Return #\Linefeed))
                  stream)
  (write-sequence (%crlf) stream)
  (when (plusp (length body))
    (write-sequence body stream))
  (force-output stream))

(defun %read-head-octets (stream)
  "Read from STREAM until CRLFCRLF; return the raw head octets including
the terminator."
  (let ((head (make-array 0 :element-type '(unsigned-byte 8)
                          :fill-pointer 0 :adjustable t)))
    (loop
      for byte = (read-byte stream)
      do (vector-push-extend byte head)
      when (and (>= (length head) 4)
                (= (aref head (- (length head) 4)) 13)
                (= (aref head (- (length head) 3)) 10)
                (= (aref head (- (length head) 2)) 13)
                (= (aref head (- (length head) 1)) 10))
        return head)))

(defun %relay-upstream-response (io stream)
  "Relay the upstream response verbatim: patch only the Connection header in
the head, stream all body octets unchanged, close both sides."
  (let* ((head (%read-head-octets stream))
         (lines (loop for line in
                         (uiop:split-string
                          (trivial-utf-8:utf-8-bytes-to-string head)
                          :separator (format nil "~C~C" #\Return #\Linefeed))
                       when (plusp (length line))
                         collect line))
         (head-bytes
          (make-array 0 :element-type '(unsigned-byte 8)
                      :fill-pointer 0 :adjustable t)))
    (flet ((push-head-line (line)
             (loop for byte across (%utf8-octets line)
                   do (vector-push-extend byte head-bytes))
             (vector-push-extend 13 head-bytes)
             (vector-push-extend 10 head-bytes)))
      (dolist (line lines)
        (let ((sep (position #\: line)))
          (unless (and sep (%header-name-p (subseq line 0 sep)
                                          '("connection")))
            (push-head-line line))))
      (push-head-line "Connection: close")
      ;; blank line terminates the response head
      (vector-push-extend 13 head-bytes)
      (vector-push-extend 10 head-bytes))
    (woo.ev.socket:write-socket-data io head-bytes)
    (loop with buffer = (make-array +relay-buffer-size+
                                    :element-type '(unsigned-byte 8))
          for n = (read-sequence buffer stream)
          until (zerop n)
          do (woo.ev.socket:write-socket-data io buffer :end n))))

(defun %write-raw-response (io status reason body-text)
  "Write one complete plain response directly to the client socket."
  (let* ((body (%utf8-octets body-text))
         (head
          (%utf8-octets
           (format nil "HTTP/1.1 ~A ~A~C~CContent-Type: text/plain; charset=utf-8~C~C~
Connection: close~C~CContent-Length: ~A~C~C~C~C"
                   status reason #\Return #\Linefeed #\Return #\Linefeed
                   #\Return #\Linefeed (length body) #\Return #\Linefeed
                   #\Return #\Linefeed))))
    (woo.ev.socket:write-socket-data io head)
    (woo.ev.socket:write-socket-data io body)))

(defun %relay-request (io config method uri headers body)
  "Own one upstream exchange: connect, forward, relay the response, close."
  (handler-case
      (multiple-value-bind (provider upstream-target upstream-url)
          (%resolve-provider config uri)
        (cond
          ((or (null provider) (null upstream-url))
           (%write-raw-response io 404 "Not Found"
                                (format nil "unknown upstream: ~A" provider))
           (woo.ev.socket:close-socket io))
          (t
           (multiple-value-bind (stream socket)
               (%open-upstream upstream-url)
             (unwind-protect
                  (progn
                    (%write-upstream-request
                     stream (string-upcase (symbol-name method))
                     upstream-target
                     (%upstream-host-header (quri:uri upstream-url))
                     headers body)
                    (%relay-upstream-response io stream))
               (ignore-errors (close stream))
               (when socket
                 (ignore-errors (usocket:socket-close socket)))
               (when (woo.ev.socket:socket-open-p io)
                 (woo.ev.socket:close-socket io)))))))
    (error (condition)
      (ignore-errors
       (%write-raw-response io 502 "Bad Gateway"
                            (format nil "upstream request failed: ~A"
                                    condition)))
      (ignore-errors (woo.ev.socket:close-socket io)))))

(defun %request-body-octets (raw-body)
  "Return the request body as an octet vector. Woo provides :raw-body as an
octet vector or an input stream depending on the build."
  (etypecase raw-body
    (vector raw-body)
    (null (make-array 0 :element-type '(unsigned-byte 8)))
    (stream
     (let ((out (make-array 0 :element-type '(unsigned-byte 8)
                            :fill-pointer 0 :adjustable t)))
       (loop with buffer = (make-array +relay-buffer-size+
                                       :element-type '(unsigned-byte 8))
             for n = (read-sequence buffer raw-body)
             until (zerop n)
             do (loop for i below n
                      do (vector-push-extend (aref buffer i) out)))
       out))))

(defun %make-proxy-app (config)
  (lambda (env)
    (let ((io (getf env :clack.io)))
      (bt:make-thread
       (lambda ()
         (%relay-request
          io
          config
          (getf env :request-method)
          (getf env :request-uri)
          (getf env :headers)
          (%request-body-octets (getf env :raw-body))))
       :name "llm-log-relay")
      (lambda (respond)
        (declare (ignore respond))))))

(defun start-proxy (config)
  "Start the transparent capture proxy for CONFIG; return a proxy-server."
  (let ((thread
          (bt:make-thread
           (lambda ()
             (woo:run (%make-proxy-app config)
                      :port (runtime-config-port config)
                      :address (runtime-config-listen-address config)
                      :worker-num nil
                      :debug nil))
           :name "llm-log-proxy")))
    (make-proxy-server :thread thread :config config)))

(defun stop-proxy (server)
  "Stop a proxy started by START-PROTO server handle. The event loop thread
is destroyed; production deployments stop via process termination."
  (let ((thread (proxy-server-thread server)))
    (when thread
      (ignore-errors (bt:destroy-thread thread))))
  server)

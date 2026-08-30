(in-package #:llm-log)

;; HTTP transparent forwarding core (zero-Python rewrite, slice 2).
;;
;; Woo owns the inbound acceptor and request framing. Each accepted request
;; spawns one relay thread that owns the upstream connection AND the client
;; socket until the exchange completes: the client descriptor is switched to
;; blocking mode and wrapped in an fd-stream, the inbound octet body is
;; forwarded with rebuilt Host and Content-Length, hop-by-hop headers are
;; dropped per proxy semantics, and the upstream response is relayed
;; verbatim at the octet level so the reason phrase, duplicate headers,
;; chunked framing and streaming timing survive.
;;
;; Documented tradeoff (research 012): Woo's output buffers only flush from
;; the event-loop thread, so the relay thread bypasses them with a blocking
;; client fd-stream. Woo's read watcher is harmless under Connection: close
;; semantics; the 15 minute idle-timeout guard is disabled per connection by
;; marking the woo socket closed after the relay completes.
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
  "Split the inbound target URI into VALUES provider, upstream-target,
upstream-base-url.

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
             (base (upstream-base-url config provider)))
        (unless base
          (return-from %resolve-provider (values provider nil nil)))
        ;; origin-form target: path and query only; the authority is
        ;; carried separately by the Host header
        (values provider
                (concatenate 'string (subseq rest slash) query)
                base)))))

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

(defun %relay-upstream-response (client-stream stream)
  "Relay the upstream response verbatim: patch only the Connection header in
the head, stream all body octets unchanged."
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
    (write-sequence head-bytes client-stream)
    (loop with buffer = (make-array +relay-buffer-size+
                                    :element-type '(unsigned-byte 8))
          for n = (read-sequence buffer stream)
          until (zerop n)
          do (write-sequence buffer client-stream :end n))
    (force-output client-stream)))

(defun %write-raw-response (client-stream status reason body-text)
  "Write one complete plain response directly to the client stream."
  (let ((body (%utf8-octets body-text)))
    (write-sequence
     (%utf8-octets
      (format nil "HTTP/1.1 ~A ~A~C~CContent-Type: text/plain; charset=utf-8~C~C~
Connection: close~C~CContent-Length: ~A~C~C~C~C"
              status reason #\Return #\Linefeed #\Return #\Linefeed
              #\Return #\Linefeed (length body) #\Return #\Linefeed
              #\Return #\Linefeed))
     client-stream)
    (write-sequence body client-stream)
    (force-output client-stream)))

(defun %make-blocking-client-stream (io)
  "Wrap the Woo client descriptor in a blocking octet fd-stream owned by the
relay thread. Woo's buffers cannot flush from a foreign thread, so the relay
bypasses them; the descriptor is closed exactly once, by this stream."
  (let ((fd (woo.ev.socket::socket-fd io)))
    (sb-posix:fcntl fd sb-posix:f-setfl
                    (logandc2 (sb-posix:fcntl fd sb-posix:f-getfl)
                              sb-posix:o-nonblock))
    (sb-sys:make-fd-stream fd
                           :input nil
                           :output t
                           :element-type '(unsigned-byte 8)
                           :buffering :none)))

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

(defun %relay-request (client-stream config method uri headers body)
  "Own one upstream exchange: connect, forward, relay the response."
  (handler-case
      (multiple-value-bind (provider upstream-target upstream-url)
          (%resolve-provider config uri)
        (cond
          ((or (null provider) (null upstream-url))
           (%write-raw-response client-stream 404 "Not Found"
                                (format nil "unknown upstream: ~A" provider)))
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
                    (%relay-upstream-response client-stream stream))
               (ignore-errors (close stream))
               (when socket
                 (ignore-errors (usocket:socket-close socket))))))))
    (error (condition)
      (ignore-errors
       (%write-raw-response client-stream 502 "Bad Gateway"
                            (format nil "upstream request failed: ~A"
                                    condition))))))

(defun %make-proxy-app (config)
  (lambda (env)
    (let ((io (getf env :clack.io)))
      (bt:make-thread
       (lambda ()
         (let ((client-stream (%make-blocking-client-stream io)))
           (unwind-protect
                (%relay-request
                 client-stream
                 config
                 (getf env :request-method)
                 (getf env :request-uri)
                 (getf env :headers)
                 (%request-body-octets (getf env :raw-body)))
             ;; disable Woo's timeout guard, then close the descriptor
             ;; exactly once, through the fd-stream
             (setf (woo.ev.socket::socket-open-p io) nil)
             (ignore-errors (close client-stream)))))
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
  "Stop a proxy started by START-PROXY. The event loop thread is destroyed;
production deployments stop via process termination (systemd)."
  (let ((thread (proxy-server-thread server)))
    (when thread
      (ignore-errors (bt:destroy-thread thread))))
  server)

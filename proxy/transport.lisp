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
;; Woo's event loop owns all watchers, timers and the descriptor registry.
;; Transfer one duplicate descriptor to the blocking relay only after closing
;; the original Woo socket in its event-loop thread. Workers never touch Woo.

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
  thread config scheduler)

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
      (vector-push-extend 13 head-bytes)
      (vector-push-extend 10 head-bytes))
    (write-sequence head-bytes client-stream)
    (loop with buffer = (make-array +relay-buffer-size+
                                    :element-type '(unsigned-byte 8))
          for n = (read-sequence buffer stream)
          until (zerop n)
          do (write-sequence buffer client-stream :end n))
    (force-output client-stream)))

(defun %write-raw-response (client-stream status reason body-text &key headers)
  "Write one complete plain response directly to the client stream."
  (let ((body (%utf8-octets body-text)))
    (write-sequence
     (%utf8-octets
      (format nil "HTTP/1.1 ~A ~A~C~C"
              status reason #\Return #\Linefeed))
     client-stream)
    (write-sequence
     (%utf8-octets
      (format nil "Content-Type: text/plain; charset=utf-8~C~C"
              #\Return #\Linefeed))
     client-stream)
    (dolist (header headers)
      (write-sequence
       (%utf8-octets
        (format nil "~A: ~A~C~C"
                (car header) (cdr header) #\Return #\Linefeed))
       client-stream))
    (write-sequence
     (%utf8-octets
      (format nil "Connection: close~C~CContent-Length: ~A~C~C~C~C"
              #\Return #\Linefeed (length body) #\Return #\Linefeed
              #\Return #\Linefeed))
     client-stream)
    (write-sequence body client-stream)
    (force-output client-stream)))

(defun %write-overload-response (client-stream config reason)
  (let* ((scheduler (runtime-config-scheduler config))
         (retry-after (scheduler-config-retry-after-seconds scheduler))
         (detail (ecase reason
                   (:queue-full "provider request queue is full")
                   (:queue-timeout "provider request queue wait expired"))))
    (%write-raw-response
     client-stream 429 "Too Many Requests" detail
     :headers (list (cons "Retry-After" retry-after)))))

(defun %make-blocking-client-stream (io)
  "Detach IO on its owning Woo event loop, returning a relay-owned duplicate."
  (let ((fd (sb-posix:dup (woo.ev.socket::socket-fd io)))
        (stream nil))
    (unwind-protect
         (progn
           (sb-posix:fcntl fd sb-posix:f-setfd sb-posix:fd-cloexec)
           ;; Stop watchers/timer and remove the registry entry in its owner.
           ;; close-socket closes ONLY the original descriptor (not shutdown).
           (woo.ev.socket:close-socket io)
           (sb-posix:fcntl fd sb-posix:f-setfl
                           (logandc2 (sb-posix:fcntl fd sb-posix:f-getfl)
                                     sb-posix:o-nonblock))
           (setf stream (sb-sys:make-fd-stream
                         fd :input nil :output t
                         :element-type '(unsigned-byte 8) :buffering :none)))
      (unless stream
        (ignore-errors (sb-posix:close fd))))))

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

(defun %relay-admitted-request
    (client-stream method headers body upstream-target upstream-url)
  "Open and relay one request after the scheduler has granted a provider slot."
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
        (ignore-errors (usocket:socket-close socket))))))

(defun %relay-request (client-stream config scheduler method uri headers body)
  "Own one scheduled upstream exchange: admit, connect, forward, relay."
  (handler-case
      (multiple-value-bind (provider upstream-target upstream-url)
          (%resolve-provider config uri)
        (cond
          ((or (null provider) (null upstream-url))
           (%write-raw-response client-stream 404 "Not Found"
                                (format nil "unknown upstream: ~A" provider)))
          (t
           (multiple-value-bind (admitted reason)
               (acquire-provider-slot scheduler provider)
             (if admitted
                 (unwind-protect
                      (%relay-admitted-request
                       client-stream method headers body
                       upstream-target upstream-url)
                   (release-provider-slot scheduler provider))
                 (%write-overload-response client-stream config reason))))))
    (error (condition)
      (ignore-errors
       (%write-raw-response client-stream 502 "Bad Gateway"
                            (format nil "upstream request failed: ~A"
                                    condition))))))

(defun %make-proxy-app (config scheduler)
  (lambda (env)
    ;; Snapshot parser-owned data before closing the Woo socket. All libev
    ;; operations happen here, never in the relay thread.
    (let* ((method (getf env :request-method))
           (target (copy-seq (getf env :request-uri)))
           (headers (make-hash-table :test 'equalp))
           (body (%request-body-octets (getf env :raw-body)))
           (stream nil)
           (transferred nil))
      (maphash (lambda (key value)
                 (setf (gethash (copy-seq key) headers)
                       (if (stringp value) (copy-seq value) value)))
               (getf env :headers))
      (setf stream (%make-blocking-client-stream (getf env :clack.io)))
      (unwind-protect
           (progn
             (bt:make-thread
              (lambda ()
                (unwind-protect
                     (%relay-request stream config scheduler method target headers body)
                  (ignore-errors (close stream))))
              :name "llm-log-relay")
             (setf transferred t)
             (lambda (respond) (declare (ignore respond))))
        (unless transferred
          (ignore-errors (close stream)))))))

(defun start-proxy (config)
  "Start the transparent capture proxy for CONFIG; return a proxy-server."
  (let* ((scheduler
           (make-request-scheduler (runtime-config-scheduler config)))
         (thread
           (bt:make-thread
            (lambda ()
              (woo:run (%make-proxy-app config scheduler)
                       :port (runtime-config-port config)
                       :address (runtime-config-listen-address config)
                       :worker-num nil
                       :debug nil))
            :name "llm-log-proxy")))
    (make-proxy-server :thread thread :config config :scheduler scheduler)))

(defun %break-proxy-event-loop ()
  "Run inside the Woo thread and ask libev to unwind normally.

Woo installs process signal watchers in each event loop. Destroying the thread
skips Woo's unwind-protect cleanup and leaves those watchers registered against
the dead loop, which makes the next Woo loop abort in libev."
  (when woo.ev:*evloop*
    (lev:ev-break woo.ev:*evloop* lev:+EVBREAK-ALL+)))

(defun %proxy-wakeup-host (address)
  (cond
    ((string= address "0.0.0.0") "127.0.0.1")
    ((or (string= address "::") (string= address "[::]")) "::1")
    (t address)))

(defun %wake-proxy-event-loop (config)
  "Wake Woo's blocking poll so a scheduled thread interrupt can run."
  (let ((socket nil))
    (unwind-protect
         (setf socket
               (usocket:socket-connect
                (%proxy-wakeup-host (runtime-config-listen-address config))
                (runtime-config-port config)
                :element-type '(unsigned-byte 8)
                :timeout 1))
      (when socket
        (ignore-errors (usocket:socket-close socket))))))

(defun stop-proxy (server)
  "Stop a proxy started by START-PROXY and let Woo run its cleanup forms."
  (let ((thread (proxy-server-thread server)))
    (when (and thread (bt:thread-alive-p thread))
      (bt:interrupt-thread thread #'%break-proxy-event-loop)
      (ignore-errors (%wake-proxy-event-loop (proxy-server-config server)))
      (bt:join-thread thread)))
  server)

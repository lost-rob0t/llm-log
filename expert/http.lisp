(in-package #:llm-log-expert)

(defparameter +expert-http-max-body-bytes+ (* 8 1024 1024))
(defparameter +expert-http-max-batch+ 256)

(defstruct expert-http-server
  thread host listen port)

(defun %expert-loopback-listen-p (listen)
  (member (string-downcase listen)
          '("127.0.0.1" "::1" "localhost") :test #'equal))

(defun %http-body-octets (raw-body)
  (etypecase raw-body
    (null (make-array 0 :element-type '(unsigned-byte 8)))
    (vector raw-body)
    (stream
     (let ((out (make-array 4096 :element-type '(unsigned-byte 8)
                            :fill-pointer 0 :adjustable t)))
       (loop for byte = (read-byte raw-body nil nil)
             while byte
             do (when (>= (length out) +expert-http-max-body-bytes+)
                  (error "expert HTTP request body exceeds ~D bytes"
                         +expert-http-max-body-bytes+))
                (vector-push-extend byte out))
       out))))

(defun %http-json-response (status object)
  (list status
        '("content-type" "application/json; charset=utf-8"
          "cache-control" "no-store")
        (list (jsown:to-json object))))

(defun %http-error (status code message)
  (%http-json-response
   status
   (%json-object
    (cons "status" "error")
    (cons "error"
          (%json-object (cons "code" code)
                        (cons "message" message))))))

(defun %http-path (env)
  (let* ((uri (or (getf env :request-uri) "/"))
         (q (position #\? uri)))
    (if q (subseq uri 0 q) uri)))

(defun %http-bearer-authorized-p (env token)
  (if (or (null token) (zerop (length token)))
      t
      (let* ((headers (getf env :headers))
             (authorization (and headers (gethash "authorization" headers))))
        (equal authorization (format nil "Bearer ~A" token)))))

(defun %http-parse-json-body (env)
  (let ((bytes (%http-body-octets (getf env :raw-body))))
    (when (> (length bytes) +expert-http-max-body-bytes+)
      (error "request body too large"))
    (jsown:parse (trivial-utf-8:utf-8-bytes-to-string bytes))))

(defun %http-dispatch-batch (host body)
  (unless (listp body)
    (error "batch body must be a JSON array"))
  (when (> (length body) +expert-http-max-batch+)
    (error "batch may contain at most ~D requests" +expert-http-max-batch+))
  (mapcar (lambda (request) (dispatch-expert-request host request)) body))

(defun make-expert-http-app (host &key token)
  "Return the Common Lisp HTTP service for an optionally remote expert host.

The wire format is the same closed, versioned expert protocol used by stdio.
Local ingestion never uses this path; it calls the same CL functions directly."
  (lambda (env)
    (handler-case
        (let ((method (getf env :request-method))
              (path (%http-path env)))
          (cond
            ((and (eq method :get) (equal path "/health"))
             (%http-json-response
              200 (dispatch-expert-request
                   host (%json-object (cons "version" +expert-protocol-version+)
                                      (cons "operation" "health")
                                      (cons "payload" (%json-object))))))
            ((not (%http-bearer-authorized-p env token))
             (%http-error 401 "unauthorized" "valid bearer token required"))
            ((and (eq method :post) (equal path "/v1/expert/rpc"))
             (let ((reply (dispatch-expert-request host (%http-parse-json-body env))))
               (%http-json-response
                (if (equal (jsown:val-safe reply "status") "ok") 200 400)
                reply)))
            ((and (eq method :post) (equal path "/v1/expert/batch"))
             (%http-json-response
              200
              (%json-object
               (cons "status" "ok")
               (cons "results"
                     (%http-dispatch-batch host (%http-parse-json-body env))))))
            (t (%http-error 404 "not_found" "unknown expert HTTP endpoint"))))
      (error (condition)
        (%http-error 400 "invalid_request" (princ-to-string condition))))))

(defun start-expert-http-server (host &key (listen "127.0.0.1") (port 8788) token)
  (unless (and (integerp port) (<= 1 port 65535))
    (error "expert HTTP port must be 1..65535"))
  (when (and (not (%expert-loopback-listen-p listen))
             (or (null token) (zerop (length token))))
    (error "non-loopback expert HTTP requires LLM_LOG_EXPERT_HTTP_TOKEN"))
  (let ((thread
          (bt:make-thread
           (lambda ()
             (woo:run (make-expert-http-app host :token token)
                      :address listen :port port :worker-num nil :debug nil))
           :name "llm-log-expert-http")))
    (make-expert-http-server :thread thread :host host :listen listen :port port)))

(defun stop-expert-http-server (server)
  (let ((thread (expert-http-server-thread server)))
    (when thread (ignore-errors (bt:destroy-thread thread))))
  server)

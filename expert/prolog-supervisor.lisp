(in-package #:llm-log-expert)

(defparameter +prolog-worker-protocol-version+ 1)
(defparameter +prolog-worker-operations+ '("health" "event_transport"))
(defparameter +default-prolog-timeout-seconds+ 5.0)

(defun %json-object (&rest pairs)
  (cons :obj pairs))

(defun %new-prolog-session-id ()
  (format nil "swipl-~36R-~36R"
          (get-universal-time)
          (random most-positive-fixnum)))

(defun %prolog-worker-path ()
  (let ((configured (uiop:getenv "LLM_LOG_PROLOG_WORKER")))
    (if (and configured (plusp (length configured)))
        (pathname configured)
        (asdf:system-relative-pathname
         "llm-log-expert"
         #P"prolog/worker.pl"))))

(defun %prolog-timeout-seconds ()
  "Return the finite operator-configured reasoner timeout.

Untrusted request data cannot alter this value. Invalid/non-positive overrides
fall back to the bounded substrate default rather than disabling the deadline."
  (let ((configured (uiop:getenv "LLM_LOG_PROLOG_TIMEOUT_SECONDS")))
    (if (and configured (plusp (length configured)))
        (handler-case
            (let ((value (read-from-string configured)))
              (if (and (realp value) (plusp value))
                  (float value 1.0)
                  +default-prolog-timeout-seconds+))
          (error () +default-prolog-timeout-seconds+))
        +default-prolog-timeout-seconds+)))

(defun %prolog-process-alive-p (host)
  (let ((process (expert-host-prolog-process host)))
    (and process (uiop:process-alive-p process))))

(defun start-prolog-worker (host)
  "Start exactly one persistent SWI-Prolog worker owned by HOST."
  (when (%prolog-process-alive-p host)
    (return-from start-prolog-worker host))
  (let* ((worker-path (%prolog-worker-path))
         (process
           (uiop:launch-program
            (list "swipl" "-q" "-f" (namestring worker-path))
            :input :stream
            :output :stream
            :error-output :stream
            :wait nil)))
    (setf (expert-host-prolog-process host) process
          (expert-host-prolog-session-id host) (%new-prolog-session-id)
          (expert-host-prolog-request-sequence host) 0)
    host))

(defun stop-prolog-worker (host)
  "Stop HOST's worker and invalidate its session identity."
  (let ((process (expert-host-prolog-process host)))
    (when process
      (when (uiop:process-alive-p process)
        (ignore-errors (uiop:terminate-process process :urgent t)))
      (ignore-errors (uiop:wait-process process))))
  (setf (expert-host-prolog-process host) nil
        (expert-host-prolog-session-id host) nil)
  host)

(defun %ensure-prolog-worker (host)
  "Ensure HOST owns one live worker, restarting only after a prior failure/crash."
  (unless (%prolog-process-alive-p host)
    (start-prolog-worker host))
  (unless (%prolog-process-alive-p host)
    (error "reasoner_unavailable"))
  host)

(defun %next-prolog-request-id (host)
  (format nil "~A:~D"
          (expert-host-prolog-session-id host)
          (incf (expert-host-prolog-request-sequence host))))

(defun %validate-prolog-reply (reply request-id operation)
  (unless (and (consp reply) (eq (first reply) :obj))
    (error "invalid_reasoner_reply: expected JSON object"))
  (unless (equal (jsown:val-safe reply "request_id") request-id)
    (error "invalid_reasoner_reply: correlation mismatch"))
  (unless (equal (jsown:val-safe reply "operation") operation)
    (error "invalid_reasoner_reply: operation mismatch"))
  (let ((status (jsown:val-safe reply "status")))
    (unless (member status '("ok" "error") :test #'equal)
      (error "invalid_reasoner_reply: invalid status")))
  reply)

(defun %read-prolog-reply-line (output)
  "Read one worker reply with a hard deadline.

The packaged expert runtime is SBCL. Timing out invalidates the entire worker;
callers must not reuse its streams or retry the in-flight inference."
  #+sbcl
  (handler-case
      (sb-ext:with-timeout (%prolog-timeout-seconds)
        (read-line output nil nil))
    (sb-ext:timeout ()
      (error "reasoner_timeout")))
  #-sbcl
  (error "reasoner_timeout_requires_supported_runtime"))

(defun prolog-worker-request (host operation data)
  "Send one declared typed request through HOST's persistent worker.

A crashed or timed-out worker invalidates the current request and is not retried
implicitly. The next request may start one fresh worker/session, keeping recovery
bounded and avoiding duplicate inference side effects."
  (unless (member operation +prolog-worker-operations+ :test #'equal)
    (error "unknown_reasoner_operation: ~A" operation))
  (%ensure-prolog-worker host)
  (let* ((process (expert-host-prolog-process host))
         (input (uiop:process-info-input process))
         (output (uiop:process-info-output process))
         (request-id (%next-prolog-request-id host))
         (request (%json-object
                   (cons "version" +prolog-worker-protocol-version+)
                   (cons "request_id" request-id)
                   (cons "operation" operation)
                   (cons "data" data))))
    (handler-case
        (progn
          (write-line (jsown:to-json request) input)
          (force-output input)
          (let ((line (%read-prolog-reply-line output)))
            (unless line
              (error "reasoner_crashed"))
            (%validate-prolog-reply (jsown:parse line) request-id operation)))
      (error (condition)
        (stop-prolog-worker host)
        (error condition)))))

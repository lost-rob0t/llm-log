(in-package #:llm-log-expert)

(defparameter +expert-protocol-version+ 1)

(defun %reply-ok (result)
  (%json-object (cons "status" "ok")
                (cons "result" result)))

(defun %reply-error (code &optional message)
  (%json-object
   (cons "status" "error")
   (cons "error"
         (%json-object
          (cons "code" code)
          (cons "message" message)))))

(defun %runtime-health (host)
  (let ((worker-reply
          (prolog-worker-request host "health" (%json-object))))
    (unless (equal (jsown:val-safe worker-reply "status") "ok")
      (error "reasoner health failed"))
    (%json-object
     (cons "runtime"
           (%json-object
            (cons "host" "common-lisp")
            (cons "store" "tek9")
            (cons "reasoner" "swipl")))
     (cons "kb_revision" (current-kb-revision host))
     (cons "prolog_session_id" (expert-host-prolog-session-id host)))))

(defun %require-event-id (request)
  (let ((event-id (jsown:val-safe request "event_id")))
    (unless (and event-id (stringp event-id) (plusp (length event-id)))
      (error "event_id is required"))
    event-id))

(defun %request-payload (request)
  (or (jsown:val-safe request "payload") (%json-object)))

(defun %dispatch-observe-request (host request)
  (let ((event-id (%require-event-id request))
        (payload (%request-payload request)))
    (handler-case
        (multiple-value-bind (state revision)
            (project-request-event host event-id payload)
          (%reply-ok
           (%json-object
            (cons "projection_state" (string-downcase (symbol-name state)))
            (cons "kb_revision" revision))))
      (event-conflict ()
        (%reply-error "event_conflict" "stable event ID has contradictory data")))))

(defun %dispatch-fixture-query (host request)
  (let* ((event-id (%require-event-id request))
         (payload (%request-payload request))
         (fixture (jsown:val-safe payload "fixture")))
    (unless (equal fixture "event_transport")
      (return-from %dispatch-fixture-query
        (%reply-error "unknown_fixture" "only event_transport is available")))
    (handler-case
        (multiple-value-bind (value rule-version revision)
            (derive-event-transport host event-id)
          (%reply-ok
           (%json-object
            (cons "expert" "fixture.event_transport")
            (cons "value" value)
            (cons "derivation_type" "deterministic")
            (cons "rule_version" rule-version)
            (cons "kb_revision" revision)
            (cons "evidence_ids" (list event-id))
            (cons "prolog_session_id" (expert-host-prolog-session-id host)))))
      (error (condition)
        (%reply-error "fixture_error" (princ-to-string condition))))))

(defun dispatch-expert-request (host request)
  "Dispatch only the declared public expert-service operations for #10."
  (unless (and (consp request) (eq (first request) :obj))
    (return-from dispatch-expert-request
      (%reply-error "invalid_request" "request must be a JSON object")))
  (let ((version (jsown:val-safe request "version"))
        (operation (jsown:val-safe request "operation")))
    (unless (eql version +expert-protocol-version+)
      (return-from dispatch-expert-request
        (%reply-error "unsupported_version" "unsupported expert protocol version")))
    (cond
      ((equal operation "health")
       (handler-case
           (%reply-ok (%runtime-health host))
         (error (condition)
           (%reply-error "reasoner_unavailable" (princ-to-string condition)))))
      ((equal operation "observe_request")
       (handler-case
           (%dispatch-observe-request host request)
         (error (condition)
           (%reply-error "invalid_request" (princ-to-string condition)))))
      ((equal operation "query_classification")
       (%dispatch-fixture-query host request))
      (t
       (%reply-error "unknown_operation" "operation is not declared")))))

(defun serve-stdio (host)
  "Serve one JSON request and reply per line until stdin closes."
  (loop for line = (read-line *standard-input* nil nil)
        while line
        do (let ((reply
                   (handler-case
                       (dispatch-expert-request host (jsown:parse line))
                     (error (condition)
                       (%reply-error "invalid_json" (princ-to-string condition))))))
             (write-line (jsown:to-json reply) *standard-output*)
             (force-output *standard-output*))))

(defun %default-expert-data-directory ()
  (merge-pathnames #P".llm-proxy/expert/" (user-homedir-pathname)))

(defun %parse-service-arguments (arguments)
  (unless (and arguments (string= (first arguments) "serve"))
    (error "usage: llm-log-expert serve --stdio [--data-dir PATH]"))
  (let ((stdio nil)
        (data-directory (%default-expert-data-directory)))
    (loop with rest = (rest arguments)
          while rest
          for argument = (pop rest)
          do (cond
               ((string= argument "--stdio")
                (setf stdio t))
               ((string= argument "--data-dir")
                (unless rest
                  (error "--data-dir requires a path"))
                (setf data-directory
                      (uiop:ensure-directory-pathname (pop rest))))
               (t
                (error "unknown argument: ~A" argument))))
    (unless stdio
      (error "only --stdio transport is available in this substrate slice"))
    data-directory))

(defun main (&optional (arguments (uiop:command-line-arguments)))
  "Entry point for the packaged llm-log-expert executable."
  (let* ((data-directory (%parse-service-arguments arguments))
         (host (start-expert-host data-directory)))
    (unwind-protect
         (serve-stdio host)
      (stop-expert-host host)))
  0)

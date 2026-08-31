(in-package #:llm-log-expert)

(defparameter *task-dispatch-expert-request*
  (symbol-function 'dispatch-expert-request))

(defun dispatch-expert-request (host request)
  "Extend the declared expert surface with bounded outcome evidence ingestion."
  (let ((operation (and (consp request)
                        (eq (first request) :obj)
                        (jsown:val-safe request "operation"))))
    (if (equal operation "record_outcome_evidence")
        (handler-case
            (%reply-ok
             (record-outcome-evidence
              host (%require-event-id request) (%request-payload request)))
          (reasoner-failure (condition)
            (%reasoner-failure-reply condition))
          (invalid-reasoner-result (condition)
            (%reply-error "invalid_reasoner_result" (princ-to-string condition)))
          (error (condition)
            (%reply-error "outcome_evidence_error" (princ-to-string condition))))
        (funcall *task-dispatch-expert-request* host request))))

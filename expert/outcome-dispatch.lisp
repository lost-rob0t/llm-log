(in-package #:llm-log-expert)

(defparameter *task-dispatch-expert-request*
  (symbol-function 'dispatch-expert-request))

(defun dispatch-expert-request (host request)
  "Extend the declared expert surface with outcome evidence and bounded queries."
  (let ((operation (and (consp request)
                        (eq (first request) :obj)
                        (jsown:val-safe request "operation"))))
    (cond
      ((equal operation "record_outcome_evidence")
       (handler-case
           (%reply-ok
            (record-outcome-evidence
             host (%require-event-id request) (%request-payload request)))
         (reasoner-failure (condition)
           (%reasoner-failure-reply condition))
         (invalid-reasoner-result (condition)
           (%reply-error "invalid_reasoner_result" (princ-to-string condition)))
         (error (condition)
           (%reply-error "outcome_evidence_error" (princ-to-string condition)))))
      ((equal operation "query_outcome_history")
       (handler-case
           (%reply-ok
            (query-outcome-history host (%request-payload request)))
         (error (condition)
           (%reply-error "outcome_history_error" (princ-to-string condition)))))
      ((equal operation "query_outcome_dataset")
       (handler-case
           (%reply-ok
            (query-outcome-dataset host (%request-payload request)))
         (error (condition)
           (%reply-error "outcome_dataset_error" (princ-to-string condition)))))
      (t
       (funcall *task-dispatch-expert-request* host request)))))

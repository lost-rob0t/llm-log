(in-package #:llm-log-expert)

(defparameter *base-dispatch-expert-request*
  (symbol-function 'dispatch-expert-request))

(defun dispatch-expert-request (host request)
  "Extend the declared service surface with bounded task accounting."
  (let ((operation (and (consp request)
                        (eq (first request) :obj)
                        (jsown:val-safe request "operation"))))
    (cond
      ((equal operation "account_task_usage")
       (handler-case
           (%reply-ok (account-task-usage host (%request-payload request)))
         (reasoner-failure (condition)
           (%reasoner-failure-reply condition))
         (invalid-reasoner-result (condition)
           (%reply-error "invalid_reasoner_result" (princ-to-string condition)))
         (error (condition)
           (%reply-error "task_accounting_error" (princ-to-string condition)))))
      ((equal operation "query_task_accounting")
       (handler-case
           (%reply-ok (query-task-accounting host (%request-payload request)))
         (error (condition)
           (%reply-error "task_accounting_query_error" (princ-to-string condition)))))
      (t
       (funcall *base-dispatch-expert-request* host request)))))

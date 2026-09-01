(in-package #:llm-log-expert-integration-test)

(defun %economics-usage (usage-id request-id attempt-id attempt-ordinal retry-of
                         provider model input output)
  (let ((usage
          (jsown:new-js
            ("usage_id" usage-id)
            ("request_id" request-id)
            ("task_id" "task-outcome-retry-economics-red")
            ("provider" provider)
            ("model" model)
            ("client" "gptel")
            ("transport" "sse")
            ("attempt_id" attempt-id)
            ("attempt_ordinal" attempt-ordinal)
            ("input_tokens" input)
            ("output_tokens" output))))
    (if retry-of
        (append usage (list (cons "retry_of_request_id" retry-of)))
        usage)))

(defun %economics-entry (analysis name)
  (and analysis (jsown:val-safe analysis name)))

(defun %economics-entry-ids (analysis name)
  (let ((entry (%economics-entry analysis name)))
    (and entry (jsown:val-safe entry "request_ids"))))

(defun %economics-non-empty-string-list-p (value)
  (and (listp value)
       value
       (every (lambda (item)
                (and (stringp item) (plusp (length item))))
              value)))

(rove:deftest task-outcome-retry-economics-red-contract
  (let* ((data-dir
           (uiop:ensure-directory-pathname
            (merge-pathnames
             (format nil "llm-log-task-outcome-retry-economics-red-~A/" (gensym))
             (uiop:temporary-directory))))
         (host (llm-log-expert:start-expert-host data-dir))
         (task
           (jsown:new-js
             ("task_id" "task-outcome-retry-economics-red")
             ("session_id" "session-task-outcome-retry-economics-red")
             ("originating_user_message_id" "msg-task-outcome-retry-economics-root")
             ("rule_id" "task.outcome.retry.economics.fixture")
             ("rule_version" 1)
             ("evidence_ids" (list "evt-task-outcome-retry-economics-root")))))
    (unwind-protect
         (progn
           ;; #15 owns these labels. This #13 contract only consumes them.
           (dolist (spec '(("evt-econ-outcome-1" "req-1" "failure")
                           ("evt-econ-outcome-2" "req-2" "failure")
                           ("evt-econ-outcome-3" "req-3" "success")))
             (destructuring-bind (event-id request-id outcome) spec
               (rove:ok
                (equal "ok"
                       (jsown:val-safe
                        (%task-outcome-record host event-id request-id outcome)
                        "status")))))

           (let ((reply
                   (llm-log-expert:dispatch-expert-request
                    host
                    (jsown:new-js
                      ("version" 1)
                      ("operation" "account_task_usage")
                      ("event_id" "evt-econ-account")
                      ("payload"
                       (jsown:new-js
                         ("task" task)
                         ("children" '())
                         ("usage_observations"
                          (list
                           (%economics-usage
                            "usage-1" "req-1" "attempt-1" 1 nil
                            "openrouter" "fixture/econ" 100 50)
                           (%economics-usage
                            "usage-2" "req-2" "attempt-2" 2 "req-1"
                            "openrouter" "fixture/econ" 80 40)
                           (%economics-usage
                            "usage-3" "req-3" "attempt-3" 3 "req-2"
                            "openrouter" "fixture/econ" 60 30)
                           (%economics-usage
                            "usage-unlabeled" "req-unlabeled" "attempt-unlabeled" 1 nil
                            "anthropic" "fixture/unlabeled" 20 10)))
                         ("pricing_snapshots"
                          (list
                           (%task-outcome-price
                            "price-econ" "openrouter" "fixture/econ" 0.001 0.002)
                           (%task-outcome-price
                            "price-unlabeled" "anthropic" "fixture/unlabeled" 0.002 0.003)))))))))
             (rove:ok (equal "ok" (jsown:val-safe reply "status"))))

           ;; Require reconstruction from Tek9 rather than live process memory.
           (llm-log-expert:stop-expert-host host)
           (setf host (llm-log-expert:start-expert-host data-dir))

           (let* ((reply
                    (llm-log-expert:dispatch-expert-request
                     host
                     (jsown:new-js
                       ("version" 1)
                       ("operation" "query_task_accounting")
                       ("event_id" "evt-econ-query")
                       ("payload"
                        (jsown:new-js
                          ("task_id" "task-outcome-retry-economics-red")
                          ("max_depth" 0)
                          ("max_nodes" 8)
                          ("include_children" nil))))))
                  (result (jsown:val-safe reply "result"))
                  (analysis (and result (jsown:val-safe result "analysis"))))
             ;; Existing accounting/retry/outcome surfaces stay as controls.
             (rove:ok (equal "ok" (jsown:val-safe reply "status")))
             (rove:ok (= 4 (jsown:val-safe result "request_count")))
             (rove:ok (= 260 (jsown:val-safe result "input_tokens")))
             (rove:ok (= 130 (jsown:val-safe result "output_tokens")))
             (rove:ok (= 2 (jsown:val-safe result "retry_request_count")))
             (rove:ok (listp (jsown:val-safe (jsown:val-safe result "breakdowns") "outcome")))

             ;; RED on untouched main: outcome-aware retry economics do not exist.
             (rove:ok (consp analysis))

             ;; Production must preserve graph semantics plus immutable provenance.
             (when analysis
               (let ((unsuccessful (%economics-entry analysis "unsuccessful"))
                     (retry (%economics-entry analysis "retry"))
                     (burn (%economics-entry analysis "burn_before_success")))
                 (rove:ok
                  (equal '("req-1" "req-2")
                         (sort (copy-list (%economics-entry-ids analysis "unsuccessful"))
                               #'string<)))
                 (rove:ok
                  (equal '("req-2" "req-3")
                         (sort (copy-list (%economics-entry-ids analysis "retry"))
                               #'string<)))
                 (rove:ok
                  (equal '("req-1" "req-2")
                         (sort (copy-list (%economics-entry-ids analysis "burn_before_success"))
                               #'string<)))
                 (rove:ok
                  (equal '("req-3")
                         (sort (copy-list (jsown:val-safe burn "successful_terminal_ids"))
                               #'string<)))
                 (rove:ok (= 1 (jsown:val-safe burn "successful_terminal_count")))
                 (rove:ok (eq t (jsown:val-safe burn "retry_graph_complete")))
                 (rove:ok
                  (not (member "req-unlabeled"
                               (%economics-entry-ids analysis "unsuccessful")
                               :test #'equal)))

                 ;; Cost assertions are immutable pricing-snapshot projections.
                 (dolist (entry (list unsuccessful retry burn))
                   (rove:ok (equal "known" (jsown:val-safe entry "cost_state")))
                   (rove:ok
                    (%economics-non-empty-string-list-p
                     (jsown:val-safe entry "cost_assertion_ids"))))

                 ;; Outcome-derived unsuccessful totals retain the two failure labels.
                 (rove:ok
                  (= 2 (length (jsown:val-safe unsuccessful "outcome_assertion_ids"))))
                 (rove:ok
                  (%economics-non-empty-string-list-p
                   (jsown:val-safe unsuccessful "rule_ids")))
                 (rove:ok
                  (%economics-non-empty-string-list-p
                   (jsown:val-safe unsuccessful "evidence_ids")))

                 ;; Burn provenance is bound to the explicit successful terminal.
                 (rove:ok
                  (= 1 (length
                        (jsown:val-safe
                         burn "successful_terminal_outcome_assertion_ids"))))
                 (rove:ok
                  (%economics-non-empty-string-list-p
                   (jsown:val-safe burn "successful_terminal_rule_ids")))
                 (rove:ok
                  (%economics-non-empty-string-list-p
                   (jsown:val-safe burn "successful_terminal_evidence_ids")))))))
      (ignore-errors (llm-log-expert:stop-expert-host host))
      (ignore-errors
        (uiop:delete-directory-tree
         data-dir :validate t :if-does-not-exist :ignore)))))

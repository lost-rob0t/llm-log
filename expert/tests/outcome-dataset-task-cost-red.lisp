(in-package #:llm-log-expert-integration-test)

(defun %dataset-cost-usage (usage-id request-id task-id input output)
  (jsown:new-js
    ("usage_id" usage-id)
    ("request_id" request-id)
    ("task_id" task-id)
    ("provider" "openrouter")
    ("model" "fixture/cost-model")
    ("client" "gptel")
    ("transport" "sse")
    ("input_tokens" input)
    ("output_tokens" output)))

(defun %dataset-cost-price (snapshot-id)
  (jsown:new-js
    ("snapshot_id" snapshot-id)
    ("provider" "openrouter")
    ("model" "fixture/cost-model")
    ("currency" "USD")
    ("effective_at" "2026-09-01T00:00:00Z")
    ("source" "fixture")
    ("source_version" "dataset-cost-v1")
    ("input_per_token" 0.001)
    ("output_per_token" 0.002)))

(defun %dataset-cost-account-task
    (host event-id task-id request-id usage-id snapshot-id input output)
  (llm-log-expert:dispatch-expert-request
   host
   (jsown:new-js
     ("version" 1)
     ("operation" "account_task_usage")
     ("event_id" event-id)
     ("payload"
      (jsown:new-js
        ("task"
         (jsown:new-js
           ("task_id" task-id)
           ("session_id" "session-dataset-cost")
           ("originating_user_message_id" "msg-dataset-cost")
           ("rule_id" "task.dataset.cost.fixture")
           ("rule_version" "1")
           ("evidence_ids" (list event-id))))
        ("children" '())
        ("usage_observations"
         (list (%dataset-cost-usage
                usage-id request-id task-id input output)))
        ("pricing_snapshots" (list (%dataset-cost-price snapshot-id))))))))

(defun %dataset-cost-query ()
  (jsown:new-js
    ("version" 1)
    ("operation" "query_outcome_dataset")
    ("event_id" "evt-dataset-cost-query")
    ("payload"
     (jsown:new-js
       ("outcome" "success")
       ("limit" 8)
       ("task_cost_state" "known")
       ("task_cost_currency" "USD")
       ("task_cost_min_amount" 0.30)))))

(rove:deftest outcome-dataset-task-cost-red-contract
  (let* ((data-dir
           (uiop:ensure-directory-pathname
            (merge-pathnames
             (format nil "llm-log-outcome-dataset-cost-red-~A/" (gensym))
             (uiop:temporary-directory))))
         (host (llm-log-expert:start-expert-host data-dir)))
    (unwind-protect
         (progn
           (let ((cheap
                   (%dataset-cost-account-task
                    host "evt-dataset-cost-account-cheap"
                    "task-dataset-cost-cheap"
                    "req-dataset-cost-cheap"
                    "usage-dataset-cost-cheap"
                    "price-dataset-cost-cheap"
                    100 20))
                 (expensive
                   (%dataset-cost-account-task
                    host "evt-dataset-cost-account-expensive"
                    "task-dataset-cost-expensive"
                    "req-dataset-cost-expensive"
                    "usage-dataset-cost-expensive"
                    "price-dataset-cost-expensive"
                    200 100)))
             (rove:ok (equal "ok" (jsown:val-safe cheap "status")))
             (rove:ok (equal "ok" (jsown:val-safe expensive "status"))))

           (dolist (fixture
                    (list
                     (list "evt-dataset-cost-outcome-cheap"
                           "req-dataset-cost-cheap"
                           "ev-dataset-cost-cheap"
                           "capture:dataset-cost-cheap")
                     (list "evt-dataset-cost-outcome-expensive"
                           "req-dataset-cost-expensive"
                           "ev-dataset-cost-expensive"
                           "capture:dataset-cost-expensive")))
             (destructuring-bind (event-id request-id evidence-id source-id)
                 fixture
               (let ((reply
                       (%dataset-record
                        host event-id request-id
                        (list (%dataset-evidence
                               evidence-id "test_result" "authoritative"
                               "success" source-id)))))
                 (rove:ok (equal "ok" (jsown:val-safe reply "status"))))))

           ;; Force task/outcome/cost joins through durable Tek9 state.
           (llm-log-expert:stop-expert-host host)
           (setf host (llm-log-expert:start-expert-host data-dir))

           (let* ((reply
                    (llm-log-expert:dispatch-expert-request
                     host (%dataset-cost-query)))
                  (result (jsown:val-safe reply "result"))
                  (examples (and result (jsown:val-safe result "examples"))))
             (rove:ok (equal "ok" (jsown:val-safe reply "status")))
             ;; Untouched production ignores task-cost selectors and therefore
             ;; returns both success assertions. GREEN must keep only the task
             ;; whose complete known USD cost is >= 0.30.
             (rove:ok (= 1 (length examples)))
             (let* ((example (first examples))
                    (accounting (jsown:val-safe example "task_accounting"))
                    (amount (and accounting
                                 (jsown:val-safe accounting "known_cost_amount")))
                    (usage-ids (and accounting
                                    (jsown:val-safe accounting
                                                    "usage_observation_ids")))
                    (cost-ids (and accounting
                                   (jsown:val-safe accounting
                                                   "cost_assertion_ids"))))
               (rove:ok
                (equal "req-dataset-cost-expensive"
                       (jsown:val-safe example "scope_id")))
               (rove:ok
                (equal "task-dataset-cost-expensive"
                       (jsown:val-safe accounting "task_id")))
               (rove:ok (equal "known" (jsown:val-safe accounting "cost_state")))
               (rove:ok (equal "USD"
                               (jsown:val-safe accounting "known_cost_currency")))
               (rove:ok (and (numberp amount) (> amount 0.39) (< amount 0.41)))
               (rove:ok (member "usage-dataset-cost-expensive"
                                usage-ids :test #'equal))
               (rove:ok (member "cost/usage-dataset-cost-expensive"
                                cost-ids :test #'equal)))))
      (ignore-errors (llm-log-expert:stop-expert-host host))
      (ignore-errors
        (uiop:delete-directory-tree
         data-dir :validate t :if-does-not-exist :ignore)))))

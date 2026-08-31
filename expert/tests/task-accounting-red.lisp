(in-package #:llm-log-expert-integration-test)

(rove:deftest task-accounting-red-contract
  (let* ((data-dir (uiop:ensure-directory-pathname
                    (merge-pathnames
                     (format nil "llm-log-task-accounting-red-~A/" (gensym))
                     (uiop:temporary-directory))))
         (host (llm-log-expert:start-expert-host data-dir)))
    (unwind-protect
         (let* ((request
                  (jsown:new-js
                    ("version" 1)
                    ("operation" "account_task_usage")
                    ("event_id" "evt-task-red-1")
                    ("payload"
                     (jsown:new-js
                       ("task"
                        (jsown:new-js
                          ("task_id" "task-parent")
                          ("session_id" "session-red")
                          ("originating_user_message_id" "msg-red")
                          ("rule_id" "task.fixture")
                          ("rule_version" 1)
                          ("evidence_ids" (list "evt-task-red-1"))))
                       ("children"
                        (list
                         (jsown:new-js
                           ("task_id" "task-child")
                           ("parent_task_id" "task-parent")
                           ("rule_id" "task.fixture.child")
                           ("rule_version" 1)
                           ("evidence_ids" (list "evt-child-red-1")))))
                       ("usage_observations"
                        (list
                         (jsown:new-js
                           ("usage_id" "usage-1")
                           ("request_id" "req-1")
                           ("task_id" "task-parent")
                           ("provider" "openrouter")
                           ("model" "fixture/model")
                           ("input_tokens" 100)
                           ("output_tokens" 50))
                         (jsown:new-js
                           ("usage_id" "usage-2")
                           ("request_id" "req-2")
                           ("task_id" "task-child")
                           ("provider" "unknown-provider")
                           ("model" "unknown/model")
                           ("input_tokens" 10)
                           ("output_tokens" 5))))
                       ("pricing_snapshots"
                        (list
                         (jsown:new-js
                           ("snapshot_id" "price-1")
                           ("provider" "openrouter")
                           ("model" "fixture/model")
                           ("currency" "USD")
                           ("effective_at" "2026-08-31T00:00:00Z")
                           ("source" "fixture")
                           ("source_version" "v1")
                           ("input_per_token" 0.001)
                           ("output_per_token" 0.002)))))))
                (reply (llm-log-expert:dispatch-expert-request host request))
                (status (jsown:val-safe reply "status"))
                (result (jsown:val-safe reply "result")))
           ;; Untouched #12 baseline has no declared account_task_usage operation.
           ;; RED requires durable task accounting, exact known cost, and explicit unknown pricing.
           (rove:ok (equal status "ok"))
           (rove:ok (equal (jsown:val-safe result "task_id") "task-parent"))
           (rove:ok (equal (jsown:val-safe result "pricing_snapshot_id") "price-1"))
           (rove:ok (equal (jsown:val-safe result "known_cost_currency") "USD"))
           (rove:ok (= (jsown:val-safe result "known_cost_amount") 0.2))
           (rove:ok (equal (jsown:val-safe result "unknown_cost_state") "unknown"))
           (rove:ok (= (jsown:val-safe result "request_count") 2)))
      (ignore-errors (llm-log-expert:stop-expert-host host))
      (ignore-errors
        (uiop:delete-directory-tree data-dir :validate t :if-does-not-exist :ignore))))))

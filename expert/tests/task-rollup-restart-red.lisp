(in-package #:llm-log-expert-integration-test)

(defun %rollup-fixture-task (task-id &optional parent-id)
  (let ((pairs
          (list (cons "task_id" task-id)
                (cons "rule_id" "task.rollup.fixture")
                (cons "rule_version" 1)
                (cons "evidence_ids" (list (format nil "evt-~A" task-id))))))
    (when parent-id
      (setf pairs (append pairs (list (cons "parent_task_id" parent-id)))))
    (cons :obj pairs)))

(defun %rollup-fixture-usage (usage-id request-id task-id input output)
  (jsown:new-js
    ("usage_id" usage-id)
    ("request_id" request-id)
    ("task_id" task-id)
    ("provider" "openrouter")
    ("model" "fixture/rollup")
    ("input_tokens" input)
    ("output_tokens" output)))

(defun %rollup-fixture-price (snapshot-id)
  (jsown:new-js
    ("snapshot_id" snapshot-id)
    ("provider" "openrouter")
    ("model" "fixture/rollup")
    ("currency" "USD")
    ("effective_at" "2026-08-31T00:00:00Z")
    ("source" "fixture")
    ("source_version" "rollup-v1")
    ("input_per_token" 0.001)
    ("output_per_token" 0.002)))

(defun %account-rollup-fixture (host event-id task usage snapshot)
  (llm-log-expert:dispatch-expert-request
   host
   (jsown:new-js
     ("version" 1)
     ("operation" "account_task_usage")
     ("event_id" event-id)
     ("payload"
      (jsown:new-js
        ("task" task)
        ("children" '())
        ("usage_observations" (list usage))
        ("pricing_snapshots" (list snapshot)))))))

(rove:deftest task-rollup-restart-red-contract
  (let* ((data-dir (uiop:ensure-directory-pathname
                    (merge-pathnames
                     (format nil "llm-log-task-rollup-red-~A/" (gensym))
                     (uiop:temporary-directory))))
         (parent-task (%rollup-fixture-task "task-rollup-parent"))
         (child-task (%rollup-fixture-task "task-rollup-child" "task-rollup-parent"))
         (parent-usage (%rollup-fixture-usage "usage-rollup-parent" "req-rollup-parent"
                                              "task-rollup-parent" 100 50))
         (child-usage (%rollup-fixture-usage "usage-rollup-child" "req-rollup-child"
                                             "task-rollup-child" 30 10))
         (parent-price (%rollup-fixture-price "price-rollup-parent"))
         (child-price (%rollup-fixture-price "price-rollup-child"))
         (host (llm-log-expert:start-expert-host data-dir)))
    (unwind-protect
         (progn
           ;; Existing #13 substrate must remain green before the new query gate.
           (rove:ok (equal "ok"
                           (jsown:val-safe
                            (%account-rollup-fixture host "evt-rollup-parent"
                                                     parent-task parent-usage parent-price)
                            "status")))
           (rove:ok (equal "ok"
                           (jsown:val-safe
                            (%account-rollup-fixture host "evt-rollup-child"
                                                     child-task child-usage child-price)
                            "status")))
           ;; Re-ingest the exact child observation. Durable query accounting must
           ;; deduplicate by immutable usage/request identity after restart.
           (rove:ok (equal "ok"
                           (jsown:val-safe
                            (%account-rollup-fixture host "evt-rollup-child-retry"
                                                     child-task child-usage child-price)
                            "status")))
           (llm-log-expert:stop-expert-host host)
           (setf host (llm-log-expert:start-expert-host data-dir))
           (let* ((query
                    (jsown:new-js
                      ("version" 1)
                      ("operation" "query_task_accounting")
                      ("event_id" "evt-rollup-query")
                      ("payload"
                       (jsown:new-js
                         ("task_id" "task-rollup-parent")
                         ("max_depth" 1)
                         ("max_nodes" 16)
                         ("include_children" t)))))
                  (reply (llm-log-expert:dispatch-expert-request host query))
                  (result (jsown:val-safe reply "result")))
             ;; RED on untouched main: query_task_accounting is not declared.
             ;; GREEN must reconstruct the bounded recursive rollup from Tek9.
             (rove:ok (equal (jsown:val-safe reply "status") "ok"))
             (rove:ok (equal (jsown:val-safe result "task_id") "task-rollup-parent"))
             (rove:ok (= (jsown:val-safe result "request_count") 2))
             (rove:ok (= (jsown:val-safe result "known_cost_amount") 0.25))
             (rove:ok (equal (jsown:val-safe result "known_cost_currency") "USD"))
             (rove:ok (equal (jsown:val-safe result "cost_state") "known"))
             (rove:ok (null (jsown:val-safe result "truncated")))
             (rove:ok (= (length (jsown:val-safe result "usage_observation_ids")) 2))
             (rove:ok (= (length (jsown:val-safe result "cost_assertion_ids")) 2))))
      (ignore-errors (llm-log-expert:stop-expert-host host))
      (ignore-errors
        (uiop:delete-directory-tree data-dir :validate t :if-does-not-exist :ignore)))))

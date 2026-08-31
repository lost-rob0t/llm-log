(in-package #:llm-log-expert-integration-test)

(defun %breakdown-fixture-usage (usage-id request-id provider model client input output)
  (jsown:new-js
    ("usage_id" usage-id)
    ("request_id" request-id)
    ("task_id" "task-breakdown-red")
    ("provider" provider)
    ("model" model)
    ("client" client)
    ("transport" "sse")
    ("input_tokens" input)
    ("output_tokens" output)))

(defun %breakdown-fixture-price (snapshot-id provider model input-rate output-rate)
  (jsown:new-js
    ("snapshot_id" snapshot-id)
    ("provider" provider)
    ("model" model)
    ("currency" "USD")
    ("effective_at" "2026-08-31T00:00:00Z")
    ("source" "fixture")
    ("source_version" "breakdown-v1")
    ("input_per_token" input-rate)
    ("output_per_token" output-rate)))

(defun %breakdown-entry (entries value)
  (find value entries
        :test #'equal
        :key (lambda (entry) (and entry (jsown:val-safe entry "value")))))

(defun %close-enough-p (actual expected)
  (and (numberp actual)
       (< (abs (- actual expected)) 0.000001)))

(rove:deftest task-usage-breakdown-red-contract
  (let* ((data-dir (uiop:ensure-directory-pathname
                    (merge-pathnames
                     (format nil "llm-log-task-breakdown-red-~A/" (gensym))
                     (uiop:temporary-directory))))
         (host (llm-log-expert:start-expert-host data-dir))
         (task
           (jsown:new-js
             ("task_id" "task-breakdown-red")
             ("session_id" "session-breakdown-red")
             ("originating_user_message_id" "msg-breakdown-red")
             ("rule_id" "task.breakdown.fixture")
             ("rule_version" 1)
             ("evidence_ids" (list "evt-breakdown-red"))))
         (usages
           (list
            (%breakdown-fixture-usage
             "usage-breakdown-or-a" "req-breakdown-or-a"
             "openrouter" "fixture/model-a" "opencode" 100 50)
            (%breakdown-fixture-usage
             "usage-breakdown-or-b" "req-breakdown-or-b"
             "openrouter" "fixture/model-b" "gptel" 40 10)
            (%breakdown-fixture-usage
             "usage-breakdown-anthropic" "req-breakdown-anthropic"
             "anthropic" "fixture/model-c" "claude-code" 70 30)))
         (prices
           (list
            (%breakdown-fixture-price
             "price-breakdown-a" "openrouter" "fixture/model-a" 0.001 0.002)
            (%breakdown-fixture-price
             "price-breakdown-b" "openrouter" "fixture/model-b" 0.002 0.003))))
    (unwind-protect
         (progn
           (let ((reply
                   (llm-log-expert:dispatch-expert-request
                    host
                    (jsown:new-js
                      ("version" 1)
                      ("operation" "account_task_usage")
                      ("event_id" "evt-breakdown-red")
                      ("payload"
                       (jsown:new-js
                         ("task" task)
                         ("children" '())
                         ("usage_observations" usages)
                         ("pricing_snapshots" prices))))))))
             (rove:ok (equal "ok" (jsown:val-safe reply "status")))
             (rove:ok (equal 3
                             (jsown:val-safe
                              (jsown:val-safe reply "result")
                              "request_count"))))
           ;; Prove the grouped projection is restart-durable and comes only from
           ;; the canonical Tek9 usage/cost records, not process-local state.
           (llm-log-expert:stop-expert-host host)
           (setf host (llm-log-expert:start-expert-host data-dir))
           (let* ((reply
                    (llm-log-expert:dispatch-expert-request
                     host
                     (jsown:new-js
                       ("version" 1)
                       ("operation" "query_task_accounting")
                       ("event_id" "evt-breakdown-query")
                       ("payload"
                        (jsown:new-js
                          ("task_id" "task-breakdown-red")
                          ("max_depth" 0)
                          ("max_nodes" 8)
                          ("include_children" nil))))))
                  (result (jsown:val-safe reply "result"))
                  (breakdowns (and result (jsown:val-safe result "breakdowns")))
                  (providers (and breakdowns (jsown:val-safe breakdowns "provider")))
                  (models (and breakdowns (jsown:val-safe breakdowns "model")))
                  (clients (and breakdowns (jsown:val-safe breakdowns "client")))
                  (openrouter (%breakdown-entry providers "openrouter"))
                  (anthropic (%breakdown-entry providers "anthropic"))
                  (model-a (%breakdown-entry models "fixture/model-a"))
                  (opencode (%breakdown-entry clients "opencode"))
                  (claude-code (%breakdown-entry clients "claude-code")))
             (rove:ok (equal "ok" (jsown:val-safe reply "status")))
             ;; Existing aggregate semantics remain intact.
             (rove:ok (equal 3 (jsown:val-safe result "request_count")))
             (rove:ok (equal 210 (jsown:val-safe result "input_tokens")))
             (rove:ok (equal 90 (jsown:val-safe result "output_tokens")))
             (rove:ok (equal "partial" (jsown:val-safe result "cost_state")))
             ;; Untouched main has no grouped task breakdown surface: these are
             ;; the intentional RED expectations for this bounded slice.
             (rove:ok breakdowns)
             (rove:ok (equal '("anthropic" "openrouter")
                             (mapcar (lambda (entry)
                                       (jsown:val-safe entry "value"))
                                     providers)))
             (rove:ok (equal 2 (and openrouter
                                    (jsown:val-safe openrouter "request_count"))))
             (rove:ok (equal 140 (and openrouter
                                      (jsown:val-safe openrouter "input_tokens"))))
             (rove:ok (equal 60 (and openrouter
                                     (jsown:val-safe openrouter "output_tokens"))))
             (rove:ok (equal "known" (and openrouter
                                           (jsown:val-safe openrouter "cost_state"))))
             (rove:ok (%close-enough-p
                       (and openrouter
                            (jsown:val-safe openrouter "known_cost_amount"))
                       0.31))
             (rove:ok (equal "USD" (and openrouter
                                         (jsown:val-safe openrouter
                                                         "known_cost_currency"))))
             (rove:ok (equal '(
                              "usage-breakdown-or-a"
                              "usage-breakdown-or-b")
                             (and openrouter
                                  (jsown:val-safe openrouter
                                                  "usage_observation_ids"))))
             (rove:ok (equal 1 (and anthropic
                                    (jsown:val-safe anthropic "request_count"))))
             (rove:ok (equal "unknown" (and anthropic
                                             (jsown:val-safe anthropic
                                                             "cost_state"))))
             (rove:ok (equal 1 (and model-a
                                    (jsown:val-safe model-a "request_count"))))
             (rove:ok (equal 100 (and model-a
                                      (jsown:val-safe model-a "input_tokens"))))
             (rove:ok (equal 1 (and opencode
                                    (jsown:val-safe opencode "request_count"))))
             (rove:ok (equal "known" (and opencode
                                           (jsown:val-safe opencode "cost_state"))))
             (rove:ok (equal 1 (and claude-code
                                    (jsown:val-safe claude-code "request_count"))))
             (rove:ok (equal "unknown" (and claude-code
                                             (jsown:val-safe claude-code
                                                             "cost_state"))))))
      (ignore-errors (llm-log-expert:stop-expert-host host))
      (ignore-errors
        (uiop:delete-directory-tree data-dir :validate t :if-does-not-exist :ignore)))))
(in-package #:llm-log-expert-integration-test)

(defun %retry-fixture-usage (usage-id request-id input output
                             &key attempt-id attempt-ordinal retry-of-request-id)
  (let ((pairs
          (list (cons "usage_id" usage-id)
                (cons "request_id" request-id)
                (cons "task_id" "task-retry-red")
                (cons "provider" "openrouter")
                (cons "model" "fixture/retry")
                (cons "client" "opencode")
                (cons "transport" "sse")
                (cons "input_tokens" input)
                (cons "output_tokens" output))))
    (when attempt-id
      (setf pairs (append pairs (list (cons "attempt_id" attempt-id)))))
    (when attempt-ordinal
      (setf pairs (append pairs (list (cons "attempt_ordinal" attempt-ordinal)))))
    (when retry-of-request-id
      (setf pairs (append pairs (list (cons "retry_of_request_id" retry-of-request-id)))))
    (cons :obj pairs)))

(defun %retry-fixture-price ()
  (jsown:new-js
    ("snapshot_id" "price-retry-red")
    ("provider" "openrouter")
    ("model" "fixture/retry")
    ("currency" "USD")
    ("effective_at" "2026-08-31T00:00:00Z")
    ("source" "fixture")
    ("source_version" "retry-v1")
    ("input_per_token" 0.001)
    ("output_per_token" 0.002)))

(rove:deftest task-retry-attempt-red-contract
  (let* ((data-dir (uiop:ensure-directory-pathname
                    (merge-pathnames
                     (format nil "llm-log-task-retry-red-~A/" (gensym))
                     (uiop:temporary-directory))))
         (host (llm-log-expert:start-expert-host data-dir))
         (task
           (jsown:new-js
             ("task_id" "task-retry-red")
             ("session_id" "session-retry-red")
             ("originating_user_message_id" "msg-retry-red")
             ("rule_id" "task.retry.fixture")
             ("rule_version" 1)
             ("evidence_ids" (list "evt-retry-red"))))
         (usages
           (list
            (%retry-fixture-usage "usage-retry-a1" "req-retry-a1" 100 50
                                  :attempt-id "attempt-a1"
                                  :attempt-ordinal 1)
            (%retry-fixture-usage "usage-retry-a2" "req-retry-a2" 120 60
                                  :attempt-id "attempt-a2"
                                  :attempt-ordinal 2
                                  :retry-of-request-id "req-retry-a1")
            ;; Ordinary request intentionally has no attempt metadata. This must
            ;; remain billable while making retry metadata completeness explicit.
            (%retry-fixture-usage "usage-retry-normal" "req-retry-normal" 20 10)))
         (price (%retry-fixture-price)))
    (unwind-protect
         (progn
           (let ((reply
                   (llm-log-expert:dispatch-expert-request
                    host
                    (jsown:new-js
                      ("version" 1)
                      ("operation" "account_task_usage")
                      ("event_id" "evt-retry-red")
                      ("payload"
                       (jsown:new-js
                         ("task" task)
                         ("children" '())
                         ("usage_observations" usages)
                         ("pricing_snapshots" (list price))))))))
             ;; Existing #13 accounting remains the baseline: all three distinct
             ;; requests are charged, regardless of retry metadata support.
             (rove:ok (equal "ok" (jsown:val-safe reply "status")))
             (rove:ok (= 3 (jsown:val-safe (jsown:val-safe reply "result")
                                           "request_count"))))
           (llm-log-expert:stop-expert-host host)
           (setf host (llm-log-expert:start-expert-host data-dir))
           (let* ((reply
                    (llm-log-expert:dispatch-expert-request
                     host
                     (jsown:new-js
                       ("version" 1)
                       ("operation" "query_task_accounting")
                       ("event_id" "evt-retry-query")
                       ("payload"
                        (jsown:new-js
                          ("task_id" "task-retry-red")
                          ("max_depth" 0)
                          ("max_nodes" 8)
                          ("include_children" nil))))))
                  (result (jsown:val-safe reply "result")))
             ;; Untouched main drops explicit attempt/retry metadata from the
             ;; immutable usage projection, so these fields are legitimately RED.
             (rove:ok (equal "ok" (jsown:val-safe reply "status")))
             (rove:ok (= 3 (jsown:val-safe result "request_count")))
             (rove:ok (= 240 (jsown:val-safe result "input_tokens")))
             (rove:ok (= 120 (jsown:val-safe result "output_tokens")))
             (rove:ok (= 0.48 (jsown:val-safe result "known_cost_amount")))
             (rove:ok (equal "USD" (jsown:val-safe result "known_cost_currency")))
             (rove:ok (= 2 (jsown:val-safe result "attempt_count")))
             (rove:ok (= 1 (jsown:val-safe result "retry_request_count")))
             (rove:ok (equal '("attempt-a1" "attempt-a2")
                             (jsown:val-safe result "attempt_ids")))
             (rove:ok (equal '("req-retry-a2")
                             (jsown:val-safe result "retry_request_ids")))
             (rove:ok (null (jsown:val-safe result "attempt_metadata_complete")))))
      (ignore-errors (llm-log-expert:stop-expert-host host))
      (ignore-errors
        (uiop:delete-directory-tree data-dir :validate t :if-does-not-exist :ignore)))))

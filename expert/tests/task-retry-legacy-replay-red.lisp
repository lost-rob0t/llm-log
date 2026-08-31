(in-package #:llm-log-expert-integration-test)

(rove:deftest task-retry-legacy-replay-red-contract
  (let* ((data-dir (uiop:ensure-directory-pathname
                    (merge-pathnames
                     (format nil "llm-log-task-retry-legacy-red-~A/" (gensym))
                     (uiop:temporary-directory))))
         (host (llm-log-expert:start-expert-host data-dir))
         (legacy-usage-id "usage-retry-legacy")
         (legacy-request-id "req-retry-legacy")
         (legacy-task-id "task-retry-legacy")
         (legacy-projection
           (list :schema-version 1
                 :usage-id legacy-usage-id
                 :request-id legacy-request-id
                 :task-id legacy-task-id
                 :provider "openrouter"
                 :model "fixture/legacy"
                 :input-tokens 10
                 :output-tokens 5
                 :cached-input-tokens nil
                 :cached-output-tokens nil
                 :reasoning-tokens nil)))
    (unwind-protect
         (progn
           ;; Simulate an immutable usage projection written by the pre-retry schema.
           (llm-log-expert::%put-immutable
            host
            (llm-log-expert::%usage-key legacy-usage-id)
            legacy-projection
            "usage")
           ;; Replaying the same logical observation after upgrade must stay
           ;; idempotent. Optional retry fields that are absent must not turn the
           ;; old projection into a conflicting new value merely by adding NIL keys.
           (let ((reply
                   (llm-log-expert:dispatch-expert-request
                    host
                    (jsown:new-js
                      ("version" 1)
                      ("operation" "account_task_usage")
                      ("event_id" "evt-retry-legacy")
                      ("payload"
                       (jsown:new-js
                         ("task"
                          (jsown:new-js
                            ("task_id" legacy-task-id)
                            ("session_id" "session-retry-legacy")
                            ("originating_user_message_id" "msg-retry-legacy")
                            ("rule_id" "task.retry.legacy.fixture")
                            ("rule_version" 1)
                            ("evidence_ids" (list "evt-retry-legacy"))))
                         ("children" '())
                         ("usage_observations"
                          (list
                           (jsown:new-js
                             ("usage_id" legacy-usage-id)
                             ("request_id" legacy-request-id)
                             ("task_id" legacy-task-id)
                             ("provider" "openrouter")
                             ("model" "fixture/legacy")
                             ("input_tokens" 10)
                             ("output_tokens" 5))))
                         ("pricing_snapshots" '())))))))
             (rove:ok (equal "ok" (jsown:val-safe reply "status")))
             (rove:ok (= 1
                         (jsown:val-safe (jsown:val-safe reply "result")
                                         "request_count")))))
      (ignore-errors (llm-log-expert:stop-expert-host host))
      (ignore-errors
        (uiop:delete-directory-tree data-dir :validate t :if-does-not-exist :ignore)))))

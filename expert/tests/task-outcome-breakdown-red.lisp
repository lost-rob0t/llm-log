(in-package #:llm-log-expert-integration-test)

(defun %task-outcome-usage (usage-id request-id provider model input output)
  (jsown:new-js
    ("usage_id" usage-id)
    ("request_id" request-id)
    ("task_id" "task-outcome-breakdown-red")
    ("provider" provider)
    ("model" model)
    ("client" "gptel")
    ("transport" "sse")
    ("input_tokens" input)
    ("output_tokens" output)))

(defun %task-outcome-price (snapshot-id provider model input-rate output-rate)
  (jsown:new-js
    ("snapshot_id" snapshot-id)
    ("provider" provider)
    ("model" model)
    ("currency" "USD")
    ("effective_at" "2026-09-01T00:00:00Z")
    ("source" "fixture")
    ("source_version" "task-outcome-breakdown-v1")
    ("input_per_token" input-rate)
    ("output_per_token" output-rate)))

(defun %task-outcome-evidence (id value source-id)
  (jsown:new-js
    ("evidence_id" id)
    ("observed_at" "2026-09-01T00:40:00Z")
    ("evidence_type" "test_result")
    ("authority" "authoritative")
    ("observed_value" value)
    ("source_id" source-id)))

(defun %task-outcome-record (host event-id request-id value)
  (llm-log-expert:dispatch-expert-request
   host
   (jsown:new-js
     ("version" 1)
     ("operation" "record_outcome_evidence")
     ("event_id" event-id)
     ("payload"
      (jsown:new-js
        ("scope" "request")
        ("scope_id" request-id)
        ("evidence"
         (list (%task-outcome-evidence
                (format nil "ev-~A" event-id)
                value
                (format nil "capture:~A" event-id)))))))))

(defun %task-outcome-entry (entries value)
  (find value entries
        :key (lambda (entry) (jsown:val-safe entry "value"))
        :test #'equal))

(rove:deftest task-outcome-breakdown-red-contract
  (let* ((data-dir
           (uiop:ensure-directory-pathname
            (merge-pathnames
             (format nil "llm-log-task-outcome-breakdown-red-~A/" (gensym))
             (uiop:temporary-directory))))
         (host (llm-log-expert:start-expert-host data-dir))
         (task
           (jsown:new-js
             ("task_id" "task-outcome-breakdown-red")
             ("session_id" "session-task-outcome-breakdown-red")
             ("originating_user_message_id" "msg-task-outcome-breakdown-root")
             ("rule_id" "task.outcome.breakdown.fixture")
             ("rule_version" 1)
             ("evidence_ids" (list "evt-task-outcome-breakdown-root")))))
    (unwind-protect
         (progn
           (dolist (spec '(("evt-task-outcome-success" "req-task-outcome-success" "success")
                           ("evt-task-outcome-failure" "req-task-outcome-failure" "failure")))
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
                      ("event_id" "evt-task-outcome-account")
                      ("payload"
                       (jsown:new-js
                         ("task" task)
                         ("children" '())
                         ("usage_observations"
                          (list
                           (%task-outcome-usage
                            "usage-task-outcome-success" "req-task-outcome-success"
                            "openrouter" "fixture/model-success" 100 50)
                           (%task-outcome-usage
                            "usage-task-outcome-failure" "req-task-outcome-failure"
                            "anthropic" "fixture/model-failure" 60 20)
                           (%task-outcome-usage
                            "usage-task-outcome-unlabeled" "req-task-outcome-unlabeled"
                            "openai" "fixture/model-unlabeled" 40 10)))
                         ("pricing_snapshots"
                          (list
                           (%task-outcome-price
                            "price-task-outcome-success" "openrouter"
                            "fixture/model-success" 0.001 0.002)
                           (%task-outcome-price
                            "price-task-outcome-failure" "anthropic"
                            "fixture/model-failure" 0.002 0.003)
                           (%task-outcome-price
                            "price-task-outcome-unlabeled" "openai"
                            "fixture/model-unlabeled" 0.001 0.001)))))))))
             (rove:ok (equal "ok" (jsown:val-safe reply "status"))))

           ;; Prove the join is reconstructible from durable Tek9 state.
           (llm-log-expert:stop-expert-host host)
           (setf host (llm-log-expert:start-expert-host data-dir))

           (let* ((reply
                    (llm-log-expert:dispatch-expert-request
                     host
                     (jsown:new-js
                       ("version" 1)
                       ("operation" "query_task_accounting")
                       ("event_id" "evt-task-outcome-query")
                       ("payload"
                        (jsown:new-js
                          ("task_id" "task-outcome-breakdown-red")
                          ("max_depth" 0)
                          ("max_nodes" 8)
                          ("include_children" nil))))))
                  (result (jsown:val-safe reply "result"))
                  (breakdowns (and result (jsown:val-safe result "breakdowns")))
                  (outcomes (and breakdowns (jsown:val-safe breakdowns "outcome"))))
             (rove:ok (equal "ok" (jsown:val-safe reply "status")))
             (rove:ok (= 3 (jsown:val-safe result "request_count")))
             (rove:ok (= 200 (jsown:val-safe result "input_tokens")))
             (rove:ok (= 80 (jsown:val-safe result "output_tokens")))

             ;; RED on untouched main: the outcome dimension does not exist.
             (rove:ok (listp outcomes))
             (rove:ok (plusp (length outcomes)))

             (let ((success (%task-outcome-entry outcomes "success"))
                   (failure (%task-outcome-entry outcomes "failure"))
                   (unlabeled (%task-outcome-entry outcomes "unlabeled")))
               (dolist (entry (list success failure unlabeled))
                 (rove:ok (consp entry))
                 (rove:ok (= 1 (jsown:val-safe entry "request_count")))
                 (rove:ok (= 1 (jsown:val-safe entry "usage_observation_count")))
                 (rove:ok (consp (jsown:val-safe entry "usage_observation_ids")))
                 (rove:ok (consp (jsown:val-safe entry "cost_assertion_ids"))))
               (dolist (entry (list success failure))
                 (rove:ok (consp (jsown:val-safe entry "outcome_assertion_ids")))
                 (rove:ok (consp (jsown:val-safe entry "rule_ids")))
                 (rove:ok (consp (jsown:val-safe entry "evidence_ids"))))
               (rove:ok (null (jsown:val-safe unlabeled "outcome_assertion_ids")))
               (rove:ok (null (jsown:val-safe unlabeled "rule_ids")))
               (rove:ok (null (jsown:val-safe unlabeled "evidence_ids"))))))
      (ignore-errors (llm-log-expert:stop-expert-host host))
      (ignore-errors
        (uiop:delete-directory-tree
         data-dir :validate t :if-does-not-exist :ignore)))))

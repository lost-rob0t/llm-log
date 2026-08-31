(in-package #:llm-log-expert-integration-test)

(defun %classification-breakdown-query-request
    (event-id user-message-id request-id message provider model)
  (jsown:new-js
    ("version" 1)
    ("operation" "query_classification")
    ("event_id" event-id)
    ("payload"
     (jsown:new-js
       ("user_message_id" user-message-id)
       ("request_id" request-id)
       ("task_id" "task-classification-breakdown-red")
       ("message" message)
       ("provider" provider)
       ("model" model)
       ("client" "gptel")))))

(defun %classification-breakdown-usage
    (usage-id request-id provider model input output)
  (jsown:new-js
    ("usage_id" usage-id)
    ("request_id" request-id)
    ("task_id" "task-classification-breakdown-red")
    ("provider" provider)
    ("model" model)
    ("client" "gptel")
    ("transport" "sse")
    ("input_tokens" input)
    ("output_tokens" output)))

(defun %classification-breakdown-price
    (snapshot-id provider model input-rate output-rate)
  (jsown:new-js
    ("snapshot_id" snapshot-id)
    ("provider" provider)
    ("model" model)
    ("currency" "USD")
    ("effective_at" "2026-08-31T00:00:00Z")
    ("source" "fixture")
    ("source_version" "classification-breakdown-v1")
    ("input_per_token" input-rate)
    ("output_per_token" output-rate)))

(rove:deftest task-classification-breakdown-red-contract
  (let* ((data-dir
           (uiop:ensure-directory-pathname
            (merge-pathnames
             (format nil "llm-log-task-classification-breakdown-red-~A/" (gensym))
             (uiop:temporary-directory))))
         (host (llm-log-expert:start-expert-host data-dir))
         (task
           (jsown:new-js
             ("task_id" "task-classification-breakdown-red")
             ("session_id" "session-classification-breakdown-red")
             ("originating_user_message_id" "msg-classification-breakdown-root")
             ("rule_id" "task.classification.breakdown.fixture")
             ("rule_version" 1)
             ("evidence_ids" (list "evt-classification-breakdown-root")))))
    (unwind-protect
         (progn
           (dolist
               (request
                (list
                 (%classification-breakdown-query-request
                  "evt-classification-breakdown-a"
                  "msg-classification-breakdown-a"
                  "req-classification-breakdown-a"
                  "Fix the classifier implementation and add regression tests."
                  "openrouter" "fixture/model-a")
                 (%classification-breakdown-query-request
                  "evt-classification-breakdown-b"
                  "msg-classification-breakdown-b"
                  "req-classification-breakdown-b"
                  "Explain the classifier design and its tradeoffs."
                  "anthropic" "fixture/model-b")))
             (rove:ok
              (equal "ok"
                     (jsown:val-safe
                      (llm-log-expert:dispatch-expert-request host request)
                      "status"))))

           (let ((reply
                   (llm-log-expert:dispatch-expert-request
                    host
                    (jsown:new-js
                      ("version" 1)
                      ("operation" "account_task_usage")
                      ("event_id" "evt-classification-breakdown-account")
                      ("payload"
                       (jsown:new-js
                         ("task" task)
                         ("children" '())
                         ("usage_observations"
                          (list
                           (%classification-breakdown-usage
                            "usage-classification-breakdown-a"
                            "req-classification-breakdown-a"
                            "openrouter" "fixture/model-a" 100 50)
                           (%classification-breakdown-usage
                            "usage-classification-breakdown-b"
                            "req-classification-breakdown-b"
                            "anthropic" "fixture/model-b" 60 20)))
                         ("pricing_snapshots"
                          (list
                           (%classification-breakdown-price
                            "price-classification-breakdown-a"
                            "openrouter" "fixture/model-a" 0.001 0.002)
                           (%classification-breakdown-price
                            "price-classification-breakdown-b"
                            "anthropic" "fixture/model-b" 0.002 0.003)))))))))
             (rove:ok (equal "ok" (jsown:val-safe reply "status")))
             (rove:ok
              (> (jsown:val-safe (jsown:val-safe reply "result")
                                 "known_cost_amount")
                 0)))

           (llm-log-expert:stop-expert-host host)
           (setf host (llm-log-expert:start-expert-host data-dir))
           (rove:ok
            (consp
             (llm-log-expert::fetch*
              (llm-log-expert::expert-host-database host)
              (llm-log-expert::%cost-key
               "usage-classification-breakdown-a"))))
           (rove:ok
            (consp
             (llm-log-expert::fetch*
              (llm-log-expert::expert-host-database host)
              (llm-log-expert::%cost-key
               "usage-classification-breakdown-b"))))

           (let* ((reply
                    (llm-log-expert:dispatch-expert-request
                     host
                     (jsown:new-js
                       ("version" 1)
                       ("operation" "query_task_accounting")
                       ("event_id" "evt-classification-breakdown-query")
                       ("payload"
                        (jsown:new-js
                          ("task_id" "task-classification-breakdown-red")
                          ("max_depth" 0)
                          ("max_nodes" 8)
                          ("include_children" nil))))))
                  (result (jsown:val-safe reply "result"))
                  (breakdowns (and result (jsown:val-safe result "breakdowns")))
                  (classifications
                    (and breakdowns
                         (jsown:val-safe breakdowns "classification"))))
             (rove:ok (equal "ok" (jsown:val-safe reply "status")))
             (rove:ok (= 2 (jsown:val-safe result "request_count")))
             (rove:ok (= 160 (jsown:val-safe result "input_tokens")))
             (rove:ok (= 70 (jsown:val-safe result "output_tokens")))
             ;; RED on untouched main: classification breakdowns do not exist yet.
             (rove:ok (listp classifications))
             (rove:ok (plusp (length classifications)))
             (dolist (entry classifications)
               (rove:ok (stringp (jsown:val-safe entry "dimension")))
               (rove:ok (stringp (jsown:val-safe entry "value")))
               (rove:ok (stringp (jsown:val-safe entry "state")))
               (rove:ok (plusp (jsown:val-safe entry "request_count")))
               (rove:ok
                (consp (jsown:val-safe entry "usage_observation_ids")))
               (rove:ok
                (consp (jsown:val-safe entry "cost_assertion_ids")))
               (rove:ok
                (consp (jsown:val-safe entry "classification_assertion_ids")))
               (rove:ok (consp (jsown:val-safe entry "rule_ids")))
               (rove:ok (consp (jsown:val-safe entry "evidence_ids"))))))
      (ignore-errors (llm-log-expert:stop-expert-host host))
      (ignore-errors
        (uiop:delete-directory-tree
         data-dir :validate t :if-does-not-exist :ignore)))))
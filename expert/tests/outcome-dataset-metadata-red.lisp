(in-package #:llm-log-expert-integration-test)

(defun %dataset-metadata-query-request (provider model)
  (jsown:new-js
    ("version" 1)
    ("operation" "query_outcome_dataset")
    ("event_id" "evt-dataset-metadata-query")
    ("payload"
     (jsown:new-js
       ("outcome" "success")
       ("limit" 8)
       ("provider" provider)
       ("model" model)))))

(defun %dataset-metadata-record-usage (host)
  (let ((task
          (jsown:new-js
            ("task_id" "task-dataset-metadata")
            ("session_id" "session-dataset-metadata")
            ("originating_user_message_id" "msg-dataset-metadata")
            ("rule_id" "task.dataset.metadata.fixture")
            ("rule_version" 1)
            ("evidence_ids" (list "evt-dataset-metadata-account"))))
        (usages
          (list
           (%breakdown-fixture-usage
            "usage-dataset-openrouter" "req-dataset-openrouter"
            "openrouter" "fixture/model-a" "gptel" 100 20)
           (%breakdown-fixture-usage
            "usage-dataset-anthropic" "req-dataset-anthropic"
            "anthropic" "fixture/model-b" "claude-code" 80 10)))
        (prices
          (list
           (%breakdown-fixture-price
            "price-dataset-openrouter" "openrouter" "fixture/model-a"
            0.001 0.002)
           (%breakdown-fixture-price
            "price-dataset-anthropic" "anthropic" "fixture/model-b"
            0.002 0.003))))
    (llm-log-expert:dispatch-expert-request
     host
     (jsown:new-js
       ("version" 1)
       ("operation" "account_task_usage")
       ("event_id" "evt-dataset-metadata-account")
       ("payload"
        (jsown:new-js
          ("task" task)
          ("children" '())
          ("usage_observations" usages)
          ("pricing_snapshots" prices)))))))

(rove:deftest outcome-dataset-request-metadata-red-contract
  (let* ((data-dir
           (uiop:ensure-directory-pathname
            (merge-pathnames
             (format nil "llm-log-outcome-dataset-metadata-red-~A/" (gensym))
             (uiop:temporary-directory))))
         (host (llm-log-expert:start-expert-host data-dir)))
    (unwind-protect
         (progn
           (let ((reply (%dataset-metadata-record-usage host)))
             (rove:ok (equal "ok" (jsown:val-safe reply "status"))))

           (dolist (fixture
                    (list
                     (list "evt-dataset-metadata-openrouter"
                           "req-dataset-openrouter"
                           "ev-dataset-metadata-openrouter"
                           "capture:dataset-openrouter")
                     (list "evt-dataset-metadata-anthropic"
                           "req-dataset-anthropic"
                           "ev-dataset-metadata-anthropic"
                           "capture:dataset-anthropic")))
             (destructuring-bind (event-id request-id evidence-id source-id)
                 fixture
               (let ((reply
                       (%dataset-record
                        host event-id request-id
                        (list (%dataset-evidence
                               evidence-id "test_result" "authoritative"
                               "success" source-id)))))
                 (rove:ok (equal "ok" (jsown:val-safe reply "status"))))))

           ;; Force all joins through durable Tek9 state instead of process-local
           ;; fixture objects.
           (llm-log-expert:stop-expert-host host)
           (setf host (llm-log-expert:start-expert-host data-dir))

           (let* ((reply
                    (llm-log-expert:dispatch-expert-request
                     host
                     (%dataset-metadata-query-request
                      "openrouter" "fixture/model-a")))
                  (result (jsown:val-safe reply "result"))
                  (examples (and result (jsown:val-safe result "examples"))))
             (rove:ok (equal "ok" (jsown:val-safe reply "status")))
             ;; Untouched baseline returns both success assertions because the
             ;; provider/model fields are not implemented. GREEN must select
             ;; exactly the request whose durable usage metadata matches.
             (rove:ok (= 1 (length examples)))
             (let* ((example (first examples))
                    (metadata (jsown:val-safe example "request_metadata")))
               (rove:ok
                (equal "req-dataset-openrouter"
                       (jsown:val-safe example "scope_id")))
               (rove:ok
                (equal "usage-dataset-openrouter"
                       (jsown:val-safe metadata "usage_id")))
               (rove:ok
                (equal "openrouter" (jsown:val-safe metadata "provider")))
               (rove:ok
                (equal "fixture/model-a" (jsown:val-safe metadata "model")))
               (rove:ok (equal "gptel" (jsown:val-safe metadata "client")))
               (rove:ok (equal "sse" (jsown:val-safe metadata "transport"))))))
      (ignore-errors (llm-log-expert:stop-expert-host host))
      (ignore-errors
        (uiop:delete-directory-tree
         data-dir :validate t :if-does-not-exist :ignore)))))

(in-package #:llm-log-expert-integration-test)

(defun %capture-usage-observe-request (event-id provider model)
  (jsown:new-js
    ("version" 1)
    ("operation" "observe_request")
    ("event_id" event-id)
    ("payload"
     (jsown:new-js
       ("provider" provider)
       ("model" model)
       ("transport" "http")
       ("started_at" "2026-09-14T01:00:00Z")
       ("completed_at" "2026-09-14T01:00:01Z")
       ("request_sha256" "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
       ("response_sha256" "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")))))

(defun %capture-usage-observation (event-id provider model)
  (jsown:new-js
    ("version" 1)
    ("operation" "observe_usage")
    ("event_id" event-id)
    ("payload"
     (jsown:new-js
       ("usage_id" (format nil "capture-usage:~A" event-id))
       ("request_id" event-id)
       ("provider" provider)
       ("model" model)
       ("client" "proxy")
       ("transport" "http")
       ("input_tokens" 120)
       ("output_tokens" 30)))))

(defun %capture-usage-success (event-id)
  (jsown:new-js
    ("version" 1)
    ("operation" "record_outcome_evidence")
    ("event_id" (format nil "outcome-~A" event-id))
    ("payload"
     (jsown:new-js
       ("scope" "request")
       ("scope_id" event-id)
       ("evidence"
        (list
         (jsown:new-js
           ("evidence_id" (format nil "evidence-~A" event-id))
           ("observed_at" "2026-09-14T01:00:02Z")
           ("evidence_type" "test_result")
           ("authority" "authoritative")
           ("observed_value" "success")
           ("source_id" event-id))))))))

(defun %capture-usage-dataset-query (provider model)
  (jsown:new-js
    ("version" 1)
    ("operation" "query_outcome_dataset")
    ("event_id" "capture-usage-dataset-query")
    ("payload"
     (jsown:new-js
       ("outcome" "success")
       ("scope" "request")
       ("provider" provider)
       ("model" model)
       ("limit" 8)))))

(rove:deftest capture-usage-red-contract
  (let* ((data-dir
           (uiop:ensure-directory-pathname
            (merge-pathnames
             (format nil "llm-log-capture-usage-red-~A/" (gensym))
             (uiop:temporary-directory))))
         (event-id "req-capture-usage")
         (host (llm-log-expert:start-expert-host data-dir)))
    (unwind-protect
         (progn
           (rove:ok
            (equal "ok"
                   (jsown:val-safe
                    (llm-log-expert:dispatch-expert-request
                     host (%capture-usage-observe-request
                           event-id "openrouter" "fixture/model"))
                    "status")))
           (let* ((reply
                    (llm-log-expert:dispatch-expert-request
                     host (%capture-usage-observation
                           event-id "openrouter" "fixture/model")))
                  (result (jsown:val-safe reply "result")))
             (rove:ok (equal "ok" (jsown:val-safe reply "status")))
             (rove:ok (equal "created"
                             (jsown:val-safe result "projection_state")))
             (rove:ok (equal (format nil "capture-usage:~A" event-id)
                             (jsown:val-safe result "usage_id"))))

           ;; Stable replay is idempotent rather than a second usage row.
           (let* ((reply
                    (llm-log-expert:dispatch-expert-request
                     host (%capture-usage-observation
                           event-id "openrouter" "fixture/model")))
                  (result (jsown:val-safe reply "result")))
             (rove:ok (equal "ok" (jsown:val-safe reply "status")))
             (rove:ok (equal "existing"
                             (jsown:val-safe result "projection_state"))))

           (rove:ok
            (equal "ok"
                   (jsown:val-safe
                    (llm-log-expert:dispatch-expert-request
                     host (%capture-usage-success event-id))
                    "status")))

           ;; Restart proves both the taskless usage projection and request
           ;; lookup index are durable and queryable by the outcome dataset.
           (llm-log-expert:stop-expert-host host)
           (setf host (llm-log-expert:start-expert-host data-dir))
           (let* ((reply
                    (llm-log-expert:dispatch-expert-request
                     host (%capture-usage-dataset-query
                           "openrouter" "fixture/model")))
                  (result (jsown:val-safe reply "result"))
                  (examples (jsown:val-safe result "examples"))
                  (metadata
                    (and examples
                         (jsown:val-safe (first examples) "request_metadata"))))
             (rove:ok (equal "ok" (jsown:val-safe reply "status")))
             (rove:ok (= 1 (length examples)))
             (rove:ok (equal "openrouter"
                             (jsown:val-safe metadata "provider")))
             (rove:ok (equal "fixture/model"
                             (jsown:val-safe metadata "model")))
             (rove:ok (equal (format nil "capture-usage:~A" event-id)
                             (jsown:val-safe metadata "usage_id")))))
      (ignore-errors (llm-log-expert:stop-expert-host host))
      (ignore-errors
        (uiop:delete-directory-tree
         data-dir :validate t :if-does-not-exist :ignore)))))

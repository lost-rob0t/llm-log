(in-package #:llm-log-expert-integration-test)

(defun %dataset-classification-query ()
  (jsown:new-js
    ("version" 1)
    ("operation" "query_outcome_dataset")
    ("event_id" "evt-dataset-classification-query")
    ("payload"
     (jsown:new-js
       ("outcome" "success")
       ("limit" 8)
       ("classification_dimension" "activity")
       ("classification_value" "coding")
       ("classification_state" "asserted")))))

(defun %dataset-classify-request (host event-id message-id request-id message)
  (llm-log-expert:dispatch-expert-request
   host
   (jsown:new-js
     ("version" 1)
     ("operation" "query_classification")
     ("event_id" event-id)
     ("payload"
      (jsown:new-js
        ("user_message_id" message-id)
        ("request_id" request-id)
        ("message" message)
        ("provider" "openrouter")
        ("model" "fixture/model-a")
        ("client" "gptel"))))))

(rove:deftest outcome-dataset-classification-red-contract
  (let* ((data-dir
           (uiop:ensure-directory-pathname
            (merge-pathnames
             (format nil "llm-log-outcome-dataset-classification-red-~A/" (gensym))
             (uiop:temporary-directory))))
         (host (llm-log-expert:start-expert-host data-dir)))
    (unwind-protect
         (progn
           (let ((reply (%dataset-metadata-record-usage host)))
             (rove:ok (equal "ok" (jsown:val-safe reply "status"))))

           (let ((coding
                   (%dataset-classify-request
                    host "evt-dataset-classification-coding"
                    "msg-dataset-classification-coding"
                    "req-dataset-openrouter"
                    "Fix this code and add tests."))
                 (unknown
                   (%dataset-classify-request
                    host "evt-dataset-classification-unknown"
                    "msg-dataset-classification-unknown"
                    "req-dataset-anthropic"
                    "Hello there.")))
             (rove:ok (equal "ok" (jsown:val-safe coding "status")))
             (rove:ok (equal "ok" (jsown:val-safe unknown "status"))))

           (dolist (fixture
                    (list
                     (list "evt-dataset-classification-outcome-a"
                           "req-dataset-openrouter"
                           "ev-dataset-classification-a"
                           "capture:dataset-classification-a")
                     (list "evt-dataset-classification-outcome-b"
                           "req-dataset-anthropic"
                           "ev-dataset-classification-b"
                           "capture:dataset-classification-b")))
             (destructuring-bind (event-id request-id evidence-id source-id)
                 fixture
               (let ((reply
                       (%dataset-record
                        host event-id request-id
                        (list (%dataset-evidence
                               evidence-id "test_result" "authoritative"
                               "success" source-id)))))
                 (rove:ok (equal "ok" (jsown:val-safe reply "status"))))))

           (llm-log-expert:stop-expert-host host)
           (setf host (llm-log-expert:start-expert-host data-dir))

           (let* ((reply
                    (llm-log-expert:dispatch-expert-request
                     host (%dataset-classification-query)))
                  (result (jsown:val-safe reply "result"))
                  (examples (and result (jsown:val-safe result "examples"))))
             (rove:ok (equal "ok" (jsown:val-safe reply "status")))
             ;; Untouched baseline ignores classification selectors, so both
             ;; success examples survive. GREEN must return coding only.
             (rove:ok (= 1 (length examples)))
             (let* ((example (first examples))
                    (classifications
                      (jsown:val-safe example "request_classifications"))
                    (metadata (jsown:val-safe example "request_metadata")))
               (rove:ok
                (equal "req-dataset-openrouter"
                       (jsown:val-safe example "scope_id")))
               (rove:ok
                (some
                 (lambda (assertion)
                   (and (equal "activity"
                               (jsown:val-safe assertion "dimension"))
                        (equal "coding" (jsown:val-safe assertion "value"))
                        (equal "asserted" (jsown:val-safe assertion "state"))
                        (stringp (jsown:val-safe assertion "assertion_id"))
                        (stringp (jsown:val-safe assertion "rule_id"))
                        (jsown:val-safe assertion "rule_version")
                        (consp (jsown:val-safe assertion "evidence_ids"))))
                 classifications))
               ;; Existing #52 request metadata join must remain intact.
               (rove:ok
                (equal "openrouter" (jsown:val-safe metadata "provider")))
               (rove:ok
                (equal "fixture/model-a" (jsown:val-safe metadata "model")))))
      (ignore-errors (llm-log-expert:stop-expert-host host))
      (ignore-errors
        (uiop:delete-directory-tree
         data-dir :validate t :if-does-not-exist :ignore)))))

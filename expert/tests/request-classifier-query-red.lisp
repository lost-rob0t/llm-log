(in-package #:llm-log-expert-integration-test)

(defun classifier-query-request (event-id user-message-id request-id message provider model)
  (jsown:new-js
    ("version" 1)
    ("operation" "query_classification")
    ("event_id" event-id)
    ("payload"
     (jsown:new-js
       ("user_message_id" user-message-id)
       ("request_id" request-id)
       ("message" message)
       ("provider" provider)
       ("model" model)
       ("client" "gptel")))))

(rove:deftest request-classifier-indexed-query-restart-red-contract
  (let* ((data-dir (uiop:ensure-directory-pathname
                    (merge-pathnames
                     (format nil "llm-log-classifier-query-red-~A/" (gensym))
                     (uiop:temporary-directory))))
         (host (llm-log-expert:start-expert-host data-dir)))
    (unwind-protect
         (progn
           (dolist (request
                    (list
                     (classifier-query-request
                      "evt-query-a" "msg-query-a" "req-query-a"
                      "Fix the classifier and add tests."
                      "openrouter" "fixture/model-a")
                     (classifier-query-request
                      "evt-query-b" "msg-query-b" "req-query-b"
                      "Explain the classifier design."
                      "anthropic" "fixture/model-b")))
             (rove:ok (equal "ok"
                             (jsown:val-safe
                              (llm-log-expert:dispatch-expert-request host request)
                              "status"))))

           ;; Durable assertions must remain queryable after process restart.
           (llm-log-expert:stop-expert-host host)
           (setf host (llm-log-expert:start-expert-host data-dir))

           (let* ((query
                    (jsown:new-js
                      ("version" 1)
                      ("operation" "query_classification_history")
                      ("event_id" "evt-history-query")
                      ("payload"
                       (jsown:new-js
                         ("request_id" "req-query-a")
                         ("provider" "openrouter")
                         ("model" "fixture/model-a")
                         ("limit" 16)))))
                  (reply (llm-log-expert:dispatch-expert-request host query))
                  (result (jsown:val-safe reply "result"))
                  (assertions (and result (jsown:val-safe result "assertions"))))
             (rove:ok (equal "ok" (jsown:val-safe reply "status")))
             (rove:ok (listp assertions))
             (rove:ok (plusp (length assertions)))
             (rove:ok (<= (length assertions) 16))
             (dolist (assertion assertions)
               (rove:ok (equal "req-query-a"
                               (jsown:val-safe assertion "request_id")))
               (rove:ok (equal "openrouter"
                               (jsown:val-safe assertion "provider")))
               (rove:ok (equal "fixture/model-a"
                               (jsown:val-safe assertion "model")))
               (rove:ok (stringp (jsown:val-safe assertion "assertion_id")))
               (rove:ok (stringp (jsown:val-safe assertion "rule_id")))
               (rove:ok (jsown:val-safe assertion "rule_version"))
               (rove:ok (consp (jsown:val-safe assertion "evidence_ids")))
               (rove:ok (jsown:val-safe assertion "expert_version"))))

           ;; An unbounded corpus history request must be rejected, not scanned.
           (let* ((unbounded
                    (jsown:new-js
                      ("version" 1)
                      ("operation" "query_classification_history")
                      ("event_id" "evt-history-unbounded")
                      ("payload" (jsown:new-js))))
                  (reply (llm-log-expert:dispatch-expert-request host unbounded))
                  (error-object (jsown:val-safe reply "error")))
             (rove:ok (equal "error" (jsown:val-safe reply "status")))
             (rove:ok (member (and error-object
                                   (jsown:val-safe error-object "code"))
                              '("invalid_request" "bounded_query_required")
                              :test #'equal))))
      (ignore-errors (llm-log-expert:stop-expert-host host))
      (ignore-errors
        (uiop:delete-directory-tree data-dir :validate t :if-does-not-exist :ignore)))))

(rove:deftest request-classifier-ambiguity-red-contract
  (let* ((data-dir (uiop:ensure-directory-pathname
                    (merge-pathnames
                     (format nil "llm-log-classifier-ambiguity-red-~A/" (gensym))
                     (uiop:temporary-directory))))
         (host (llm-log-expert:start-expert-host data-dir)))
    (unwind-protect
         (let* ((request
                  (classifier-query-request
                   "evt-ambiguous" "msg-ambiguous" "req-ambiguous"
                   "Review this patch and maybe fix it if needed, but do not change anything unless necessary."
                   "openrouter" "fixture/model-a"))
                (reply (llm-log-expert:dispatch-expert-request host request))
                (result (jsown:val-safe reply "result"))
                (assertions (and result (jsown:val-safe result "assertions")))
                (ambiguous
                  (remove-if-not
                   (lambda (assertion)
                     (equal "ambiguous" (jsown:val-safe assertion "state")))
                   assertions)))
           (rove:ok (equal "ok" (jsown:val-safe reply "status")))
           (rove:ok (>= (length ambiguous) 2))
           (rove:ok
            (some
             (lambda (left)
               (some
                (lambda (right)
                  (and (not (eq left right))
                       (equal (jsown:val-safe left "dimension")
                              (jsown:val-safe right "dimension"))
                       (not (equal (jsown:val-safe left "value")
                                   (jsown:val-safe right "value")))))
                ambiguous))
             ambiguous))
           (dolist (assertion assertions)
             (rove:ok (not (equal "granted"
                                  (jsown:val-safe assertion "authority"))))))
      (ignore-errors (llm-log-expert:stop-expert-host host))
      (ignore-errors
        (uiop:delete-directory-tree data-dir :validate t :if-does-not-exist :ignore)))))

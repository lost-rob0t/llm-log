(in-package #:llm-log-expert-integration-test)

(rove:deftest request-classifier-red-contract
  (let* ((data-dir (uiop:ensure-directory-pathname
                    (merge-pathnames
                     (format nil "llm-log-classifier-red-~A/" (gensym))
                     (uiop:temporary-directory))))
         (host (llm-log-expert:start-expert-host data-dir))
         (message "Fix the llm-log #12 classifier, add tests, and open a PR; do not merge it."))
    (unwind-protect
         (let* ((request
                  (jsown:new-js
                    ("version" 1)
                    ("operation" "query_classification")
                    ("event_id" "evt-classifier-red-1")
                    ("payload"
                     (jsown:new-js
                       ("user_message_id" "msg-classifier-red-1")
                       ("request_id" "req-classifier-red-1")
                       ("message" message)
                       ("provider" "openrouter")
                       ("model" "fixture/model")
                       ("client" "gptel")))))
                (reply (llm-log-expert:dispatch-expert-request host request))
                (status (jsown:val-safe reply "status"))
                (result (jsown:val-safe reply "result"))
                (assertions (and result (jsown:val-safe result "assertions"))))
           ;; Baseline #10 fixture dispatcher returns unknown_fixture here.
           ;; RED requires a real multi-label classifier result instead.
           (rove:ok (equal status "ok"))
           (rove:ok (listp assertions))
           (rove:ok (>= (length assertions) 5))
           (dolist (assertion assertions)
             (let ((state (jsown:val-safe assertion "state")))
               (unless (equal state "unknown")
                 (rove:ok (stringp (jsown:val-safe assertion "rule_id")))
                 (rove:ok (jsown:val-safe assertion "rule_version"))
                 (rove:ok (consp (jsown:val-safe assertion "evidence_ids"))))))
           (flet ((has (dimension value)
                    (some (lambda (assertion)
                            (and (equal (jsown:val-safe assertion "dimension") dimension)
                                 (equal (jsown:val-safe assertion "value") value)))
                          assertions)))
             (rove:ok (has "activity" "coding"))
             (rove:ok (has "operation" "fix"))
             (rove:ok (or (has "operation" "test")
                          (has "expected_validation" "test")))
             (rove:ok (has "artifact_target" "code"))
             (rove:ok (has "artifact_target" "pr"))
             (rove:ok (has "authority_effect" "write_without_merge"))))
      (ignore-errors (llm-log-expert:stop-expert-host host))
      (ignore-errors (uiop:delete-directory-tree data-dir :validate t :if-does-not-exist :ignore)))))

(in-package #:llm-log-expert-integration-test)

(defun %outcome-history-evidence (id type authority value)
  (jsown:new-js
    ("evidence_id" id)
    ("observed_at" "2026-08-31T23:00:00Z")
    ("evidence_type" type)
    ("authority" authority)
    ("observed_value" value)
    ("source_id" id)))

(defun %outcome-history-record-request
    (event-id scope-id evidence &optional supersedes)
  (let ((payload
          (jsown:new-js
            ("scope" "request")
            ("scope_id" scope-id)
            ("evidence" evidence))))
    (when supersedes
      (jsown:extend-js payload ("supersedes_assertion_id" supersedes)))
    (jsown:new-js
      ("version" 1)
      ("operation" "record_outcome_evidence")
      ("event_id" event-id)
      ("payload" payload))))

(defun %outcome-history-query-request (scope-id limit)
  (jsown:new-js
    ("version" 1)
    ("operation" "query_outcome_history")
    ("event_id" "evt-outcome-history-query")
    ("payload"
     (jsown:new-js
       ("scope" "request")
       ("scope_id" scope-id)
       ("limit" limit)))))

(defun %record-outcome-for-contract
    (host event-id scope-id evidence &optional supersedes)
  (llm-log-expert:dispatch-expert-request
   host
   (%outcome-history-record-request
    event-id scope-id evidence supersedes)))

(rove:deftest outcome-history-red-contract
  (let* ((data-dir
           (uiop:ensure-directory-pathname
            (merge-pathnames
             (format nil "llm-log-outcome-history-red-~A/" (gensym))
             (uiop:temporary-directory))))
         (host (llm-log-expert:start-expert-host data-dir))
         failure-id
         success-id)
    (unwind-protect
         (progn
           ;; Authoritative test failure is task evidence, not transport state.
           (let* ((reply
                    (%record-outcome-for-contract
                     host "evt-failure" "req-corrected"
                     (list (%outcome-history-evidence
                            "ev-test-failure" "test_result"
                            "authoritative" "failure"))))
                  (result (jsown:val-safe reply "result")))
             (rove:ok (equal "ok" (jsown:val-safe reply "status")))
             (rove:ok (equal "failure" (jsown:val-safe result "outcome")))
             (setf failure-id (jsown:val-safe result "outcome_assertion_id")))

           ;; A later authoritative passing test may explicitly supersede the
           ;; failed interpretation without deleting it.
           (let* ((reply
                    (%record-outcome-for-contract
                     host "evt-success" "req-corrected"
                     (list (%outcome-history-evidence
                            "ev-test-success" "test_result"
                            "authoritative" "success"))
                     failure-id))
                  (result (jsown:val-safe reply "result")))
             (rove:ok (equal "ok" (jsown:val-safe reply "status")))
             (rove:ok (equal "success" (jsown:val-safe result "outcome")))
             (rove:ok (equal failure-id
                             (jsown:val-safe result "supersedes_assertion_id")))
             (setf success-id (jsown:val-safe result "outcome_assertion_id")))

           (dolist (fixture
                     '(("evt-partial" "req-partial" "manual_label" "partial" "partial")
                       ("evt-cancelled" "req-cancelled" "task_state" "cancelled" "cancelled")
                       ("evt-timeout" "req-timeout" "task_state" "timeout" "timeout")))
             (destructuring-bind (event-id scope-id type value expected) fixture
               (let* ((reply
                        (%record-outcome-for-contract
                         host event-id scope-id
                         (list (%outcome-history-evidence
                                (format nil "ev-~A" expected)
                                type "authoritative" value))))
                      (result (jsown:val-safe reply "result")))
                 (rove:ok (equal "ok" (jsown:val-safe reply "status")))
                 (rove:ok (equal expected (jsown:val-safe result "outcome"))))))

           ;; Transport success by itself must remain unknown.
           (let* ((reply
                    (%record-outcome-for-contract
                     host "evt-http-200" "req-http-200"
                     (list (%outcome-history-evidence
                            "ev-http-200" "provider_transport"
                            "weak" "http_200"))))
                  (result (jsown:val-safe reply "result")))
             (rove:ok (equal "unknown" (jsown:val-safe result "outcome"))))

           ;; A cross-scope supersession is invalid and must not create a new
           ;; assertion.
           (let ((reply
                   (%record-outcome-for-contract
                    host "evt-cross-scope" "req-other"
                    (list (%outcome-history-evidence
                           "ev-cross-scope" "manual_label"
                           "authoritative" "success"))
                    failure-id)))
             (rove:ok (equal "error" (jsown:val-safe reply "status"))))

           (llm-log-expert:stop-expert-host host)
           (setf host (llm-log-expert:start-expert-host data-dir))

           ;; RED on current main: query_outcome_history is not declared and
           ;; there is no replaced-by projection/index yet.
           (let* ((reply
                    (llm-log-expert:dispatch-expert-request
                     host (%outcome-history-query-request "req-corrected" 8)))
                  (result (jsown:val-safe reply "result"))
                  (history (and result (jsown:val-safe result "assertions"))))
             (rove:ok (equal "ok" (jsown:val-safe reply "status")))
             (rove:ok (= 2 (length history)))
             (let ((newest (first history))
                   (older (second history)))
               (rove:ok (equal success-id
                               (jsown:val-safe newest "assertion_id")))
               (rove:ok (equal "success" (jsown:val-safe newest "outcome")))
               (rove:ok (equal failure-id
                               (jsown:val-safe newest "supersedes_assertion_id")))
               (rove:ok (equal failure-id
                               (jsown:val-safe older "assertion_id")))
               (rove:ok (equal "failure" (jsown:val-safe older "outcome")))
               (rove:ok (equal success-id
                               (jsown:val-safe older "replaced_by_assertion_id")))
               (dolist (entry history)
                 (rove:ok (stringp (jsown:val-safe entry "rule_id")))
                 (rove:ok (stringp (jsown:val-safe entry "rule_version")))
                 (rove:ok (listp (jsown:val-safe entry "evidence_ids")))))))
      (ignore-errors (llm-log-expert:stop-expert-host host))
      (ignore-errors
        (uiop:delete-directory-tree
         data-dir :validate t :if-does-not-exist :ignore)))))

(in-package #:llm-log-expert-integration-test)

(defun %dataset-evidence (id type authority value source-id)
  (jsown:new-js
    ("evidence_id" id)
    ("observed_at" "2026-08-31T23:40:00Z")
    ("evidence_type" type)
    ("authority" authority)
    ("observed_value" value)
    ("source_id" source-id)))

(defun %dataset-record-request (event-id scope-id evidence &optional supersedes)
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

(defun %dataset-query-request (outcome limit &key include-superseded)
  (let ((payload
          (jsown:new-js
            ("outcome" outcome)
            ("limit" limit))))
    (when include-superseded
      (jsown:extend-js payload ("include_superseded" t)))
    (jsown:new-js
      ("version" 1)
      ("operation" "query_outcome_dataset")
      ("event_id" (format nil "evt-dataset-~A" outcome))
      ("payload" payload))))

(defun %dataset-record (host event-id scope-id evidence &optional supersedes)
  (llm-log-expert:dispatch-expert-request
   host (%dataset-record-request event-id scope-id evidence supersedes)))

(rove:deftest outcome-dataset-red-contract
  (let* ((data-dir
           (uiop:ensure-directory-pathname
            (merge-pathnames
             (format nil "llm-log-outcome-dataset-red-~A/" (gensym))
             (uiop:temporary-directory))))
         (host (llm-log-expert:start-expert-host data-dir))
         failure-id)
    (unwind-protect
         (progn
           ;; First request starts as an authoritative failure.
           (let* ((reply
                    (%dataset-record
                     host "evt-dataset-failure" "req-dataset-corrected"
                     (list (%dataset-evidence
                            "ev-dataset-failure" "test_result"
                            "authoritative" "failure" "capture:test-failure"))))
                  (result (jsown:val-safe reply "result")))
             (rove:ok (equal "ok" (jsown:val-safe reply "status")))
             (setf failure-id (jsown:val-safe result "outcome_assertion_id")))

           ;; A later authoritative correction supersedes but never deletes it.
           (let ((reply
                   (%dataset-record
                    host "evt-dataset-corrected" "req-dataset-corrected"
                    (list (%dataset-evidence
                           "ev-dataset-corrected" "test_result"
                           "authoritative" "success" "capture:test-success"))
                    failure-id)))
             (rove:ok (equal "ok" (jsown:val-safe reply "status"))))

           ;; A second current success proves the bounded query reports
           ;; truncation. Positive user feedback alone intentionally remains
           ;; unknown, so use evidence recognized as authoritative success.
           (let ((reply
                   (%dataset-record
                    host "evt-dataset-success-2" "req-dataset-success-2"
                    (list (%dataset-evidence
                           "ev-dataset-success-2" "test_result"
                           "authoritative" "success" "capture:test-success-2")))))
             (rove:ok (equal "ok" (jsown:val-safe reply "status"))))

           ;; Adversarial upgrade model: assertions predate the new dataset
           ;; index and its migration marker. Erase only those two derived
           ;; pieces while retaining canonical Tek9 outcome documents, then
           ;; restart. The production host must perform a versioned one-time
           ;; backfill rather than silently exporting an incomplete corpus.
           (let ((database (llm-log-expert::expert-host-database host)))
             (tek9:clear-index database "outcome-assertion-outcome")
             (tek9:delete-document
              database
              llm-log-expert::+outcome-dataset-index-migration-key+))
           (llm-log-expert:stop-expert-host host)
           (setf host (llm-log-expert:start-expert-host data-dir))

           (let* ((reply
                    (llm-log-expert:dispatch-expert-request
                     host (%dataset-query-request "success" 1)))
                  (result (jsown:val-safe reply "result"))
                  (examples (and result (jsown:val-safe result "examples"))))
             (rove:ok (equal "ok" (jsown:val-safe reply "status")))
             (rove:ok (= 1 (length examples)))
             (rove:ok (eq t (jsown:val-safe result "truncated")))
             (let* ((example (first examples))
                    (evidence (first (jsown:val-safe example "evidence"))))
               (rove:ok (equal "success" (jsown:val-safe example "outcome")))
               (rove:ok (stringp (jsown:val-safe example "assertion_id")))
               (rove:ok (stringp (jsown:val-safe example "rule_id")))
               (rove:ok (stringp (jsown:val-safe example "rule_version")))
               (rove:ok (stringp (jsown:val-safe example "expert_version")))
               (rove:ok (listp (jsown:val-safe example "evidence_ids")))
               (rove:ok (stringp (jsown:val-safe evidence "evidence_id")))
               (rove:ok (stringp (jsown:val-safe evidence "source_id")))
               (rove:ok (stringp (jsown:val-safe evidence "observed_at")))
               (rove:ok (stringp (jsown:val-safe evidence "authority")))))

           ;; Superseded assertions stay historical, but are excluded from the
           ;; ordinary dataset unless explicitly requested.
           (let* ((reply
                    (llm-log-expert:dispatch-expert-request
                     host (%dataset-query-request "failure" 8)))
                  (examples
                    (jsown:val-safe (jsown:val-safe reply "result") "examples")))
             (rove:ok (equal "ok" (jsown:val-safe reply "status")))
             (rove:ok (null examples)))

           (let* ((reply
                    (llm-log-expert:dispatch-expert-request
                     host (%dataset-query-request
                           "failure" 8 :include-superseded t)))
                  (examples
                    (jsown:val-safe (jsown:val-safe reply "result") "examples"))
                  (example (first examples)))
             (rove:ok (equal "ok" (jsown:val-safe reply "status")))
             (rove:ok (= 1 (length examples)))
             (rove:ok (equal failure-id
                             (jsown:val-safe example "assertion_id")))
             (rove:ok (stringp
                       (jsown:val-safe example "replaced_by_assertion_id")))))
      (ignore-errors (llm-log-expert:stop-expert-host host))
      (ignore-errors
        (uiop:delete-directory-tree
         data-dir :validate t :if-does-not-exist :ignore)))))

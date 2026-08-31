(in-package #:llm-log-expert-integration-test)

(defun %outcome-red-request (event-id scope-id evidence)
  (jsown:new-js
    ("version" 1)
    ("operation" "record_outcome_evidence")
    ("event_id" event-id)
    ("payload"
     (jsown:new-js
       ("scope" "request")
       ("scope_id" scope-id)
       ("evidence" evidence)))))

(defun %outcome-evidence (id type authority value)
  (jsown:new-js
    ("evidence_id" id)
    ("observed_at" "2026-08-31T22:00:00Z")
    ("evidence_type" type)
    ("authority" authority)
    ("observed_value" value)
    ("source_id" id)))

(rove:deftest outcome-evidence-red-contract
  (let* ((data-dir
           (uiop:ensure-directory-pathname
            (merge-pathnames
             (format nil "llm-log-outcome-evidence-red-~A/" (gensym))
             (uiop:temporary-directory))))
         (host (llm-log-expert:start-expert-host data-dir)))
    (unwind-protect
         (progn
           ;; RED on untouched main: record_outcome_evidence is not declared yet.
           ;; Transport success must not erase authoritative user rejection.
           (let* ((reply
                    (llm-log-expert:dispatch-expert-request
                     host
                     (%outcome-red-request
                      "evt-outcome-rejected"
                      "req-outcome-rejected"
                      (list
                       (%outcome-evidence
                        "ev-transport-200"
                        "provider_transport" "weak" "http_200")
                       (%outcome-evidence
                        "ev-user-rejected"
                        "user_feedback" "authoritative" "rejected")))))
                  (result (jsown:val-safe reply "result")))
             (rove:ok (equal "ok" (jsown:val-safe reply "status")))
             (rove:ok (equal "rejected" (jsown:val-safe result "outcome")))
             (rove:ok (stringp (jsown:val-safe result "outcome_assertion_id")))
             (rove:ok (stringp (jsown:val-safe result "rule_id")))
             (rove:ok (jsown:val-safe result "rule_version"))
             (rove:ok
              (equal '("ev-transport-200" "ev-user-rejected")
                     (sort (copy-list (jsown:val-safe result "evidence_ids"))
                           #'string<))))

           ;; A plain HTTP 200 is evidence about transport only. It is not
           ;; sufficient evidence of task success in the outcome expert.
           (let* ((reply
                    (llm-log-expert:dispatch-expert-request
                     host
                     (%outcome-red-request
                      "evt-outcome-transport-only"
                      "req-outcome-transport-only"
                      (list
                       (%outcome-evidence
                        "ev-transport-only-200"
                        "provider_transport" "weak" "http_200")))))
                  (result (jsown:val-safe reply "result")))
             (rove:ok (equal "ok" (jsown:val-safe reply "status")))
             (rove:ok (equal "unknown" (jsown:val-safe result "outcome"))))

           (llm-log-expert:stop-expert-host host)
           (setf host (llm-log-expert:start-expert-host data-dir))

           ;; Restart durability is part of the substrate contract. These key
           ;; names intentionally define the first durable boundary for #15.
           (rove:ok
            (consp
             (llm-log-expert::fetch*
              (llm-log-expert::expert-host-database host)
              "outcome-evidence:ev-user-rejected")))
           (rove:ok
            (consp
             (llm-log-expert::fetch*
              (llm-log-expert::expert-host-database host)
              "outcome-evidence:ev-transport-200"))))
      (ignore-errors (llm-log-expert:stop-expert-host host))
      (ignore-errors
        (uiop:delete-directory-tree
         data-dir :validate t :if-does-not-exist :ignore)))))

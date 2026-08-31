(in-package #:llm-log-expert-integration-test)

(deftest reasoner-failure-recovery-red
  (testing "#10 returns typed reasoner failures and recovers only on a later request"
    (let ((root (%temporary-expert-directory))
          (host nil))
      (unwind-protect
           (progn
             (setf host (llm-log-expert:start-expert-host root))
             (llm-log-expert:project-request-event
              host "fixture-recovery-event" (%roundtrip-payload))

             (let ((caught nil))
               (handler-case
                   (llm-log-expert::%validate-prolog-reply
                    (llm-log-expert::%json-object
                     (cons "request_id" "wrong-request")
                     (cons "operation" "event_transport")
                     (cons "status" "ok"))
                    "expected-request"
                    "event_transport")
                 (error (condition)
                   (setf caught condition)))
               (let ((condition-type
                       (find-symbol "REASONER-FAILURE" "LLM-LOG-EXPERT"))
                     (kind-reader
                       (find-symbol "REASONER-FAILURE-KIND" "LLM-LOG-EXPERT")))
                 (ok (and condition-type
                          caught
                          (typep caught condition-type))
                     "malformed/correlation-invalid replies must use the typed reasoner-failure condition")
                 (when (and condition-type kind-reader caught
                            (typep caught condition-type)
                            (fboundp kind-reader))
                   (ok (eq (funcall kind-reader caught) :malformed-reply)
                       "correlation-invalid reply must classify as :malformed-reply"))))

             (let ((old-session
                     (llm-log-expert:expert-host-prolog-session-id host)))
               (llm-log-expert::stop-prolog-worker host)
               (ok (null (llm-log-expert:expert-host-prolog-session-id host))
                   "invalidated/dead worker session must not remain reusable")
               (multiple-value-bind (transport rule-version kb-revision)
                   (llm-log-expert:derive-event-transport
                    host "fixture-recovery-event")
                 (ok (equal transport "http")
                     "a later independent request may recover through a fresh worker")
                 (ok (and (stringp rule-version) (plusp (length rule-version)))
                     "recovered inference retains rule provenance")
                 (ok (= kb-revision
                        (llm-log-expert:current-kb-revision host))
                     "recovered inference remains bound to the durable KB revision"))
               (let ((new-session
                       (llm-log-expert:expert-host-prolog-session-id host)))
                 (ok (and new-session
                          (not (equal old-session new-session)))
                     "later recovery must create a fresh SWI-Prolog session"))))
        (when host
          (ignore-errors (llm-log-expert:stop-expert-host host)))
        (ignore-errors
          (uiop:delete-directory-tree root :validate t))))))

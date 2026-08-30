(in-package #:llm-log-expert-integration-test)

(defun %temporary-expert-directory ()
  (uiop:ensure-directory-pathname
   (merge-pathnames
    (format nil "llm-log-expert-roundtrip-~36R/" (random most-positive-fixnum))
    (uiop:temporary-directory))))

(defun %roundtrip-payload ()
  (llm-log-expert::%json-object
   (cons "provider" "fixture-provider")
   (cons "upstream" "https://fixture.invalid")
   (cons "model" "fixture-model")
   (cons "transport" "http")
   (cons "started_at" "2026-08-30T00:00:00Z")
   (cons "completed_at" "2026-08-30T00:00:01Z")
   (cons "request_sha256" "fixture-request-sha256")
   (cons "response_sha256" "fixture-response-sha256")))

(deftest common-lisp-tek9-prolog-roundtrip-red
  (testing "#10 performs one bounded Tek9 -> declared SWI-Prolog round trip"
    (let ((root (%temporary-expert-directory))
          (host nil))
      (unwind-protect
           (progn
             (setf host (llm-log-expert:start-expert-host root))
             (ok (llm-log-expert:expert-host-open-p host)
                 "expert host must own one open Tek9 environment and one live SWI-Prolog worker")

             (multiple-value-bind (projection-state revision)
                 (llm-log-expert:project-request-event
                  host "fixture-event-1" (%roundtrip-payload))
               (ok (eq projection-state :created)
                   "source observation must be projected once into Tek9")
               (ok (> revision 1)
                   "creating the projection must advance the durable KB revision"))

             (let ((projected (llm-log-expert:fetch-request-event host "fixture-event-1")))
               (ok projected "bounded primary-key lookup must retrieve the projected observation")
               (ok (equal (getf projected :transport) "http")
                   "typed projection must retain the materialized transport fact"))

             (let ((session-id (llm-log-expert:expert-host-prolog-session-id host)))
               (multiple-value-bind (transport rule-version kb-revision)
                   (llm-log-expert:derive-event-transport host "fixture-event-1")
                 (ok (equal transport "http")
                     "declared event_transport inference must preserve grounded evidence")
                 (ok (and (stringp rule-version) (plusp (length rule-version)))
                     "derived result must carry a non-empty rule version")
                 (ok (= kb-revision (llm-log-expert:current-kb-revision host))
                     "derived result must bind to the current durable KB revision"))

               (llm-log-expert:derive-event-transport host "fixture-event-1")
               (ok (equal session-id
                          (llm-log-expert:expert-host-prolog-session-id host))
                   "healthy requests must reuse the persistent supervised Prolog worker"))

             (let ((rejected nil))
               (handler-case
                   (llm-log-expert::prolog-worker-request
                    host "arbitrary_callable_term" (llm-log-expert::%json-object))
                 (error () (setf rejected t)))
               (ok rejected
                   "undeclared operations must be rejected before Prolog invocation")))
        (when host
          (ignore-errors (llm-log-expert:stop-expert-host host)))
        (ignore-errors (uiop:delete-directory-tree root :validate t))))))

(deftest derived-assertion-provenance-supersession-red
  (testing "#10 persists derived assertions with immutable provenance and supersession"
    (let ((root (%temporary-expert-directory))
          (host nil))
      (unwind-protect
           (progn
             (setf host (llm-log-expert:start-expert-host root))
             (llm-log-expert:project-request-event
              host "fixture-assertion-source" (%roundtrip-payload))
             (let* ((source-before
                      (copy-tree
                       (llm-log-expert:fetch-request-event
                        host "fixture-assertion-source")))
                    (revision-before (llm-log-expert:current-kb-revision host)))
               (multiple-value-bind (state revision)
                   (llm-log-expert::persist-derived-assertion
                    host
                    "fixture-assertion-1"
                    "fixture-assertion-source"
                    "fixture.event_transport"
                    "expert-v1"
                    "event-transport-v1"
                    "deterministic"
                    "http")
                 (ok (eq state :created)
                     "first derived assertion publication must create one durable record")
                 (ok (= revision (1+ revision-before))
                     "new assertion publication must advance KB revision exactly once"))

               (let* ((first
                        (llm-log-expert::fetch-derived-assertion
                         host "fixture-assertion-1"))
                      (first-snapshot (copy-tree first))
                      (revision-after-first
                        (llm-log-expert:current-kb-revision host)))
                 (ok first "assertion must be retrievable by bounded stable primary key")
                 (ok (equal (getf first :source-ids)
                            '("fixture-assertion-source"))
                     "assertion must retain exact source evidence IDs")
                 (ok (equal (getf first :expert-name) "fixture.event_transport")
                     "assertion must retain expert identity")
                 (ok (equal (getf first :expert-version) "expert-v1")
                     "assertion must retain expert version")
                 (ok (equal (getf first :rule-version) "event-transport-v1")
                     "assertion must retain rule version")
                 (ok (equal (getf first :derivation-type) "deterministic")
                     "assertion must retain derivation type")
                 (ok (equal (getf first :value) "http")
                     "assertion must retain typed derived value")
                 (ok (= (getf first :published-kb-revision) revision-after-first)
                     "assertion must bind to its publication KB revision")

                 (multiple-value-bind (replay-state replay-revision)
                     (llm-log-expert::persist-derived-assertion
                      host
                      "fixture-assertion-1"
                      "fixture-assertion-source"
                      "fixture.event_transport"
                      "expert-v1"
                      "event-transport-v1"
                      "deterministic"
                      "http")
                   (ok (eq replay-state :existing)
                       "identical assertion replay must be idempotent")
                   (ok (= replay-revision revision-after-first)
                       "identical assertion replay must not advance KB revision"))

                 (let ((conflicted nil))
                   (handler-case
                       (llm-log-expert::persist-derived-assertion
                        host
                        "fixture-assertion-1"
                        "fixture-assertion-source"
                        "fixture.event_transport"
                        "expert-v1"
                        "event-transport-v1"
                        "deterministic"
                        "https")
                     (error (condition)
                       (when (string= (symbol-name (type-of condition))
                                      "ASSERTION-CONFLICT")
                         (setf conflicted t))))
                   (ok conflicted
                       "contradictory stable assertion replay must signal assertion-conflict")
                   (ok (= (llm-log-expert:current-kb-revision host)
                          revision-after-first)
                       "contradictory replay must not advance KB revision")
                   (ok (equal (llm-log-expert::fetch-derived-assertion
                               host "fixture-assertion-1")
                              first-snapshot)
                       "contradictory replay must not overwrite historical assertion"))

                 (multiple-value-bind (state revision)
                     (llm-log-expert::persist-derived-assertion
                      host
                      "fixture-assertion-2"
                      "fixture-assertion-source"
                      "fixture.event_transport"
                      "expert-v2"
                      "event-transport-v2"
                      "deterministic"
                      "http"
                      :supersedes "fixture-assertion-1")
                   (ok (eq state :created)
                       "superseding assertion must be a new durable record")
                   (ok (= revision (1+ revision-after-first))
                       "superseding publication must advance KB revision exactly once"))

                 (let ((second
                         (llm-log-expert::fetch-derived-assertion
                          host "fixture-assertion-2")))
                   (ok (equal (getf second :supersedes)
                              "fixture-assertion-1")
                       "new assertion must explicitly link to the prior assertion")
                   (ok (equal (llm-log-expert::fetch-derived-assertion
                               host "fixture-assertion-1")
                              first-snapshot)
                       "supersession must preserve the prior assertion unchanged")
                   (ok (equal (llm-log-expert:fetch-request-event
                               host "fixture-assertion-source")
                              source-before)
                       "derived assertion publication must not mutate raw source evidence"))

                 (let ((revision-before-invalid
                         (llm-log-expert:current-kb-revision host))
                       (rejected nil))
                   (handler-case
                       (llm-log-expert::persist-derived-assertion
                        host
                        "fixture-assertion-invalid"
                        "fixture-assertion-source"
                        "fixture.event_transport"
                        "expert-v3"
                        "event-transport-v3"
                        "deterministic"
                        "http"
                        :supersedes "missing-assertion")
                     (error () (setf rejected t)))
                   (ok rejected
                       "supersession of an unknown assertion must fail")
                   (ok (= (llm-log-expert:current-kb-revision host)
                          revision-before-invalid)
                       "failed supersession must not advance KB revision"))))))
        (when host
          (ignore-errors (llm-log-expert:stop-expert-host host)))
        (ignore-errors (uiop:delete-directory-tree root :validate t))))))

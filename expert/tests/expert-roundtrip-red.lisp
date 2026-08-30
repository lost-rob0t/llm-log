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

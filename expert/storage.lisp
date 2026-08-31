(in-package #:llm-log-expert)

(defparameter +kb-revision-key+ "meta:kb-revision")
(defparameter +event-schema-version+ 1)
(defparameter +assertion-schema-version+ 1)

(define-condition event-conflict (error)
  ((event-id :initarg :event-id :reader event-conflict-event-id))
  (:report (lambda (condition stream)
             (format stream "Conflicting projection for event ~A"
                     (event-conflict-event-id condition)))))

(define-condition assertion-conflict (error)
  ((assertion-id :initarg :assertion-id :reader assertion-conflict-assertion-id))
  (:report (lambda (condition stream)
             (format stream "Conflicting projection for assertion ~A"
                     (assertion-conflict-assertion-id condition)))))

(define-condition invalid-reasoner-result (error)
  ((message :initarg :message :reader invalid-reasoner-result-message))
  (:report (lambda (condition stream)
             (write-string (invalid-reasoner-result-message condition) stream))))

(defun %reject-invalid-reasoner-result (host format-control &rest arguments)
  "Invalidate HOST's worker and reject one malformed/contradictory derivation."
  (stop-prolog-worker host)
  (error 'invalid-reasoner-result
         :message (apply #'format nil format-control arguments)))

(defun %event-key (event-id)
  (format nil "event:~A" event-id))

(defun %assertion-key (assertion-id)
  (format nil "assertion:~A" assertion-id))

(defun %ensure-kb-revision (host)
  (let ((database (expert-host-database host)))
    (with-write-transaction (database)
      (or (fetch* database +kb-revision-key+)
          (progn
            (put* database 1 :id +kb-revision-key+)
            1)))))

(defun current-kb-revision (host)
  (or (fetch* (expert-host-database host) +kb-revision-key+) 1))

(defun %projection-field (payload key)
  (jsown:val-safe payload key))

(defun %event-projection (event-id payload)
  (list :schema-version +event-schema-version+
        :event-id event-id
        :provider (%projection-field payload "provider")
        :upstream (%projection-field payload "upstream")
        :model (%projection-field payload "model")
        :transport (%projection-field payload "transport")
        :started-at (%projection-field payload "started_at")
        :completed-at (%projection-field payload "completed_at")
        :request-sha256 (%projection-field payload "request_sha256")
        :response-sha256 (%projection-field payload "response_sha256")))

(defun project-request-event (host event-id payload)
  "Project one request event into Tek9 with create/existing/conflict semantics."
  (check-type event-id string)
  (let* ((database (expert-host-database host))
         (event-key (%event-key event-id))
         (projection (%event-projection event-id payload)))
    (with-write-transaction (database)
      (let ((existing (fetch* database event-key))
            (revision (or (fetch* database +kb-revision-key+) 1)))
        (cond
          ((null existing)
           (let ((next-revision (1+ revision)))
             (put* database projection :id event-key)
             (put* database next-revision :id +kb-revision-key+)
             (values :created next-revision)))
          ((equal existing projection)
           (values :existing revision))
          (t
           (error 'event-conflict :event-id event-id)))))))

(defun fetch-request-event (host event-id)
  "Fetch exactly one projected event by stable primary key."
  (fetch* (expert-host-database host) (%event-key event-id)))

(defun %non-empty-string-p (value)
  (and (stringp value) (plusp (length value))))

(defun %validate-assertion-inputs (assertion-id source-event-id expert-name
                                   expert-version rule-version derivation-type
                                   supersedes)
  (dolist (value (list assertion-id source-event-id expert-name expert-version
                       rule-version derivation-type))
    (unless (%non-empty-string-p value)
      (error "derived assertion identifiers and provenance must be non-empty strings")))
  (when (and supersedes (not (%non-empty-string-p supersedes)))
    (error "supersedes must be NIL or a non-empty assertion ID")))

(defun %derived-assertion-projection
    (assertion-id source-event-id expert-name expert-version rule-version
     derivation-type value published-kb-revision supersedes)
  (list :schema-version +assertion-schema-version+
        :assertion-id assertion-id
        :source-ids (list source-event-id)
        :expert-name expert-name
        :expert-version expert-version
        :rule-version rule-version
        :derivation-type derivation-type
        :value value
        :published-kb-revision published-kb-revision
        :supersedes supersedes))

(defun persist-derived-assertion
    (host assertion-id source-event-id expert-name expert-version rule-version
     derivation-type value &key supersedes)
  "Persist one derived assertion with bounded provenance and immutable supersession."
  (%validate-assertion-inputs assertion-id source-event-id expert-name
                              expert-version rule-version derivation-type
                              supersedes)
  (let* ((database (expert-host-database host))
         (assertion-key (%assertion-key assertion-id)))
    (with-write-transaction (database)
      (unless (fetch* database (%event-key source-event-id))
        (error "unknown_source_event: ~A" source-event-id))
      (when (and supersedes
                 (null (fetch* database (%assertion-key supersedes))))
        (error "unknown_superseded_assertion: ~A" supersedes))
      (let* ((existing (fetch* database assertion-key))
             (revision (or (fetch* database +kb-revision-key+) 1)))
        (if existing
            (let ((candidate
                    (%derived-assertion-projection
                     assertion-id source-event-id expert-name expert-version
                     rule-version derivation-type value
                     (getf existing :published-kb-revision)
                     supersedes)))
              (if (equal existing candidate)
                  (values :existing revision)
                  (error 'assertion-conflict :assertion-id assertion-id)))
            (let* ((next-revision (1+ revision))
                   (projection
                     (%derived-assertion-projection
                      assertion-id source-event-id expert-name expert-version
                      rule-version derivation-type value next-revision
                      supersedes)))
              (put* database projection :id assertion-key)
              (put* database next-revision :id +kb-revision-key+)
              (values :created next-revision)))))))

(defun fetch-derived-assertion (host assertion-id)
  "Fetch exactly one derived assertion by stable primary key."
  (check-type assertion-id string)
  (fetch* (expert-host-database host) (%assertion-key assertion-id)))

(defun %validate-event-transport-result (host reply expected-transport)
  "Validate the operation-specific typed result returned by SWI-Prolog."
  (let ((result (jsown:val-safe reply "result"))
        (rule-version (jsown:val-safe reply "rule_version")))
    (unless (and (consp result) (eq (first result) :obj))
      (%reject-invalid-reasoner-result
       host "event_transport result must be a JSON object"))
    (let ((derived-transport (jsown:val-safe result "transport")))
      (unless (and (stringp derived-transport)
                   (plusp (length derived-transport)))
        (%reject-invalid-reasoner-result
         host "event_transport result.transport must be a non-empty string"))
      (unless (equal derived-transport expected-transport)
        (%reject-invalid-reasoner-result
         host
         "event_transport contradicted materialized evidence: expected ~S got ~S"
         expected-transport
         derived-transport))
      (unless (and (stringp rule-version) (plusp (length rule-version)))
        (%reject-invalid-reasoner-result
         host "event_transport rule_version must be a non-empty string"))
      (values derived-transport rule-version))))

(defun derive-event-transport (host event-id)
  "Derive transport through one bounded Tek9 point lookup and declared Prolog rule."
  (let ((event (fetch-request-event host event-id)))
    (unless event
      (error "unknown_event: ~A" event-id))
    (let* ((transport (getf event :transport))
           (reply (prolog-worker-request
                   host
                   "event_transport"
                   (%json-object (cons "transport" transport)))))
      (unless (equal (jsown:val-safe reply "status") "ok")
        (error "reasoner_error: ~A" (jsown:to-json reply)))
      (multiple-value-bind (derived-transport rule-version)
          (%validate-event-transport-result host reply transport)
        (values derived-transport
                rule-version
                (current-kb-revision host))))))

(in-package #:llm-log-expert)

(defparameter +kb-revision-key+ "meta:kb-revision")
(defparameter +event-schema-version+ 1)

(define-condition event-conflict (error)
  ((event-id :initarg :event-id :reader event-conflict-event-id))
  (:report (lambda (condition stream)
             (format stream "Conflicting projection for event ~A"
                     (event-conflict-event-id condition)))))

(defun %event-key (event-id)
  (format nil "event:~A" event-id))

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
      (let ((result (jsown:val-safe reply "result"))
            (rule-version (jsown:val-safe reply "rule_version")))
        (values (jsown:val-safe result "transport")
                rule-version
                (current-kb-revision host))))))

(in-package #:llm-log-expert)

(defparameter +usage-request-index-migration-v2-key+
  "meta:index-migration:usage-request-id:v2")

(defparameter *capture-base-register-task-accounting-indexes*
  (symbol-function '%register-task-accounting-indexes))

(defparameter *capture-base-ensure-usage-request-index-v1*
  (symbol-function '%ensure-usage-request-index-v1))

(defparameter *capture-base-dispatch-expert-request*
  (symbol-function 'dispatch-expert-request))

(defun %register-task-accounting-indexes (database)
  "Extend request usage lookup so taskless capture usage is indexed safely."
  (funcall *capture-base-register-task-accounting-indexes* database)
  (register-index
   database "usage-request-id"
   (lambda (document)
     (let ((usage-id (%plist-index-value document :usage-id))
           (request-id (%plist-index-value document :request-id)))
       ;; Cost assertions also carry request IDs; require usage identity so only
       ;; immutable usage observations enter this index. Task identity is not
       ;; required because historical raw captures predate task attribution.
       (and usage-id request-id request-id))))
  database)

(defun %ensure-usage-request-index-v1 (database)
  "Run the prior migration, then rebuild once for taskless capture usage v2."
  (funcall *capture-base-ensure-usage-request-index-v1* database)
  (unless (fetch* database +usage-request-index-migration-v2-key+)
    (tek9:rebuild-index database "usage-request-id")
    (with-write-transaction (database)
      (unless (fetch* database +usage-request-index-migration-v2-key+)
        (put* database
              (list :schema-version 2
                    :index-name "usage-request-id"
                    :task-id-required nil)
              :id +usage-request-index-migration-v2-key+))))
  database)

(defun %capture-usage-count (usage field)
  (let ((value (jsown:val-safe usage field)))
    (when value
      (unless (and (numberp value) (>= value 0))
        (error "~A must be a non-negative number when present" field)))
    value))

(defun %capture-usage-projection (usage)
  "Project provider-reported request usage without inventing task/cost context."
  (let ((projection
          (list :schema-version +usage-schema-version+
                :usage-id (%required-json-string usage "usage_id")
                :request-id (%required-json-string usage "request_id")
                :provider (%required-json-string usage "provider")
                :model (%required-json-string usage "model")
                :input-tokens (%capture-usage-count usage "input_tokens")
                :output-tokens (%capture-usage-count usage "output_tokens")
                :cached-input-tokens (%capture-usage-count usage "cached_input_tokens")
                :cached-output-tokens (%capture-usage-count usage "cached_output_tokens")
                :reasoning-tokens (%capture-usage-count usage "reasoning_tokens"))))
    (setf projection
          (%append-present-plist-field
           projection :client (%optional-json-string usage "client")))
    (%append-present-plist-field
     projection :transport (%optional-json-string usage "transport"))))

(defun %capture-request-usages (database request-id)
  (remove-if-not
   (lambda (projection)
     (and (listp projection)
          (%non-empty-string-p (getf projection :usage-id))
          (equal request-id (getf projection :request-id))))
   (select-index-range database "usage-request-id" request-id
                       :end request-id :limit 2)))

(defun %capture-usage-metadata-compatible-p (left right)
  (and (equal (getf left :request-id) (getf right :request-id))
       (equal (getf left :provider) (getf right :provider))
       (equal (getf left :model) (getf right :model))))

(defun observe-capture-usage (host event-id payload)
  "Persist request-level usage from raw capture without synthesizing a task."
  (unless (%non-empty-string-p event-id)
    (error "event_id is required"))
  (let* ((projection (%capture-usage-projection payload))
         (request-id (getf projection :request-id))
         (usage-id (getf projection :usage-id))
         (database (expert-host-database host)))
    (unless (equal event-id request-id)
      (error "observe_usage event_id must equal request_id"))
    (unless (fetch-request-event host request-id)
      (error "unknown_source_event: ~A" request-id))
    (let ((existing (%capture-request-usages database request-id)))
      (when (> (length existing) 1)
        (error "request_usage_integrity_error: ~A" request-id))
      (let ((current (first existing)))
        (cond
          ((and current (not (equal usage-id (getf current :usage-id))))
           (unless (%capture-usage-metadata-compatible-p current projection)
             (error "request_usage_metadata_conflict: ~A" request-id))
           (%json-object
            (cons "projection_state" "existing_request_usage")
            (cons "usage_id" (getf current :usage-id))
            (cons "request_id" request-id)
            (cons "kb_revision" (current-kb-revision host))))
          (t
           (multiple-value-bind (state revision)
               (%put-immutable host (%usage-key usage-id) projection "usage")
             (%json-object
              (cons "projection_state"
                    (string-downcase (symbol-name state)))
              (cons "usage_id" usage-id)
              (cons "request_id" request-id)
              (cons "kb_revision" revision)))))))))

(defun dispatch-expert-request (host request)
  "Extend the declared expert surface with historical request usage ingest."
  (let ((operation (and (consp request)
                        (eq (first request) :obj)
                        (jsown:val-safe request "operation"))))
    (if (equal operation "observe_usage")
        (handler-case
            (%reply-ok
             (observe-capture-usage
              host (%require-event-id request) (%request-payload request)))
          (error (condition)
            (%reply-error "usage_observation_error" (princ-to-string condition))))
        (funcall *capture-base-dispatch-expert-request* host request))))

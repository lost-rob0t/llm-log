(in-package #:llm-log-expert)

(defparameter +max-classification-history-limit+ 64)
(defparameter +max-classification-history-candidates+ 128)

(defun %history-string-filter (payload key)
  (let ((value (jsown:val-safe payload key)))
    (cond
      ((null value) nil)
      ((%non-empty-string-p value) value)
      (t (error "~A must be a non-empty string" key)))))

(defun %classification-history-limit (payload)
  (let ((limit (jsown:val-safe payload "limit")))
    (unless (and (integerp limit)
                 (plusp limit)
                 (<= limit +max-classification-history-limit+))
      (error "limit must be an integer from 1 through ~D"
             +max-classification-history-limit+))
    limit))

(defun %classification-history-seed (payload)
  "Return INDEX and KEY for one mandatory selective equality predicate."
  (let ((request-id (%history-string-filter payload "request_id"))
        (message-id (%history-string-filter payload "user_message_id"))
        (provider (%history-string-filter payload "provider"))
        (model (%history-string-filter payload "model")))
    (cond
      (request-id
       (values "classification-source-request-id" request-id))
      (message-id
       (values "classification-source-user-message-id" message-id))
      (provider
       (values "classification-source-provider" provider))
      (model
       (values "classification-source-model" model))
      (t
       (error "bounded_query_required: request_id, user_message_id, provider, or model is required")))))

(defun %classification-source-matches-p (source payload)
  (flet ((matches (payload-key source-key)
           (let ((wanted (jsown:val-safe payload payload-key)))
             (or (null wanted)
                 (equal wanted (getf source source-key))))))
    (and (matches "request_id" :request-id)
         (matches "user_message_id" :user-message-id)
         (matches "provider" :provider)
         (matches "model" :model))))

(defun %bounded-index-equality (database index-name key limit)
  "Use Tek9's ordered secondary-index cursor with an exact bounded range."
  (select-index-range database index-name key :end key :limit limit))

(defun %classifier-assertion-p (projection)
  (equal "request.classifier" (getf projection :expert-name)))

(defun %classification-history-json (projection source)
  (let ((value (getf projection :value)))
    (%json-object
     (cons "assertion_id" (getf projection :assertion-id))
     (cons "event_id" (getf source :event-id))
     (cons "user_message_id" (getf source :user-message-id))
     (cons "request_id" (getf source :request-id))
     (cons "provider" (getf source :provider))
     (cons "model" (getf source :model))
     (cons "client" (getf source :client))
     (cons "dimension" (getf value :dimension))
     (cons "value" (getf value :value))
     (cons "state" (getf value :state))
     (cons "confidence" (getf value :confidence))
     (cons "rule_id" (getf value :rule-id))
     (cons "rule_version" (getf projection :rule-version))
     (cons "evidence_ids" (copy-list (getf value :evidence-ids)))
     (cons "expert_version" (getf projection :expert-version))
     (cons "published_kb_revision" (getf projection :published-kb-revision))
     (cons "supersedes" (getf projection :supersedes)))))

(defun query-classification-history (host payload)
  "Return a finite Tek9-indexed classification history slice.

At least one selective equality predicate is mandatory. Retrieval is bounded at
both the source-index and assertion-index boundaries; this function never falls
back to Tek9 SELECT or whole-corpus materialization."
  (unless (and (consp payload) (eq (first payload) :obj))
    (error "payload must be a JSON object"))
  (let* ((limit (%classification-history-limit payload))
         (database (expert-host-database host))
         (candidate-limit (min +max-classification-history-candidates+
                               (max limit (* 4 limit)))))
         (results nil))
    (multiple-value-bind (index-name index-key)
        (%classification-history-seed payload)
      (let ((sources
              (%bounded-index-equality database index-name index-key candidate-limit)))
        (dolist (source sources)
          (when (%classification-source-matches-p source payload)
            (let* ((remaining (- limit (length results)))
                   (event-id (getf source :event-id))
                   (assertions
                     (%bounded-index-equality
                      database
                      "classification-assertion-source-event"
                      event-id
                      remaining)))
              (dolist (projection assertions)
                (when (%classifier-assertion-p projection)
                  (push (%classification-history-json projection source) results)
                  (when (>= (length results) limit)
                    (return))))))
          (when (>= (length results) limit)
            (return)))))
    (nreverse results)))

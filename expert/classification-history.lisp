(in-package #:llm-log-expert)

(defparameter +max-classification-history-limit+ 64)
(defparameter +max-classification-history-candidates+ 128)

(defun %history-string-filter (payload key)
  (let ((value (jsown:val-safe payload key)))
    (cond
      ((null value) nil)
      ((%non-empty-string-p value) value)
      (t (error "~A must be a non-empty string" key)))))

(defun %canonical-utc-second-p (value)
  "Accept the sortable RFC3339 UTC whole-second form YYYY-MM-DDTHH:MM:SSZ."
  (and (stringp value)
       (= (length value) 20)
       (char= (char value 4) #\-)
       (char= (char value 7) #\-)
       (char= (char value 10) #\T)
       (char= (char value 13) #\:)
       (char= (char value 16) #\:)
       (char= (char value 19) #\Z)
       (handler-case
           (let* ((year (parse-integer value :start 0 :end 4))
                  (month (parse-integer value :start 5 :end 7))
                  (day (parse-integer value :start 8 :end 10))
                  (hour (parse-integer value :start 11 :end 13))
                  (minute (parse-integer value :start 14 :end 16))
                  (second (parse-integer value :start 17 :end 19))
                  (universal-time
                    (encode-universal-time second minute hour day month year 0)))
             (multiple-value-bind
                   (decoded-second decoded-minute decoded-hour decoded-day
                    decoded-month decoded-year)
                 (decode-universal-time universal-time 0)
               (and (= second decoded-second)
                    (= minute decoded-minute)
                    (= hour decoded-hour)
                    (= day decoded-day)
                    (= month decoded-month)
                    (= year decoded-year))))
         (error () nil))))

(defun %history-time-filter (payload key)
  (let ((value (jsown:val-safe payload key)))
    (cond
      ((null value) nil)
      ((%canonical-utc-second-p value) value)
      (t
       (error "~A must use canonical UTC timestamp YYYY-MM-DDTHH:MM:SSZ" key)))))

(defun %classification-history-time-bounds (payload)
  (let ((lower (%history-time-filter payload "started_at_gte"))
        (upper (%history-time-filter payload "started_at_lt")))
    (when (and lower upper (not (string< lower upper)))
      (error "started_at_gte must be earlier than started_at_lt"))
    (values lower upper)))

(defun %classification-history-limit (payload)
  (let ((limit (jsown:val-safe payload "limit")))
    (unless (and (integerp limit)
                 (plusp limit)
                 (<= limit +max-classification-history-limit+))
      (error "limit must be an integer from 1 through ~D"
             +max-classification-history-limit+))
    limit))

(defun %classification-history-seed (payload lower upper)
  "Return KIND, INDEX, START and END for one mandatory bounded selector."
  (let ((request-id (%history-string-filter payload "request_id"))
        (message-id (%history-string-filter payload "user_message_id"))
        (provider (%history-string-filter payload "provider"))
        (model (%history-string-filter payload "model")))
    (cond
      (request-id
       (values :equality "classification-source-request-id" request-id request-id))
      (message-id
       (values :equality "classification-source-user-message-id" message-id message-id))
      (provider
       (values :equality "classification-source-provider" provider provider))
      (model
       (values :equality "classification-source-model" model model))
      ((and lower upper)
       (values :time-range "classification-source-started-at" lower upper))
      (t
       (error
        "bounded_query_required: request_id, user_message_id, provider, model, or a complete started_at range is required")))))

(defun %classification-source-matches-p (source payload lower upper)
  (flet ((matches (payload-key source-key)
           (let ((wanted (jsown:val-safe payload payload-key)))
             (or (null wanted)
                 (equal wanted (getf source source-key))))))
    (let ((started-at (getf source :started-at)))
      (and (matches "request_id" :request-id)
           (matches "user_message_id" :user-message-id)
           (matches "provider" :provider)
           (matches "model" :model)
           (or (null lower)
               (and started-at (not (string< started-at lower))))
           (or (null upper)
               (and started-at (string< started-at upper)))))))

(defun %bounded-index-range (database index-name start end limit)
  "Use Tek9's ordered secondary-index cursor with an explicit finite bound."
  (select-index-range database index-name start :end end :limit limit))

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
     (cons "started_at" (getf source :started-at))
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

A selective equality predicate or a complete timestamp range is mandatory.
Retrieval is bounded at both source-index and assertion-index boundaries; this
function never falls back to Tek9 SELECT or whole-corpus materialization."
  (unless (and (consp payload) (eq (first payload) :obj))
    (error "payload must be a JSON object"))
  (let* ((limit (%classification-history-limit payload))
         (database (expert-host-database host))
         (candidate-limit (min +max-classification-history-candidates+
                               (max limit (* 4 limit))))
         (results nil))
    (multiple-value-bind (lower upper)
        (%classification-history-time-bounds payload)
      (multiple-value-bind (seed-kind index-name start-key end-key)
          (%classification-history-seed payload lower upper)
        (declare (ignore seed-kind))
        (let ((sources
                (%bounded-index-range
                 database index-name start-key end-key candidate-limit)))
          (dolist (source sources)
            (when (%classification-source-matches-p source payload lower upper)
              (let* ((remaining (- limit (length results)))
                     (event-id (getf source :event-id))
                     (assertions
                       (%bounded-index-range
                        database
                        "classification-assertion-source-event"
                        event-id
                        event-id
                        remaining)))
                (dolist (projection assertions)
                  (when (%classifier-assertion-p projection)
                    (push (%classification-history-json projection source) results)
                    (when (>= (length results) limit)
                      (return))))))
            (when (>= (length results) limit)
              (return))))))
    (nreverse results)))

(in-package #:llm-log-expert)

(defparameter +analytics-schema-version+ 1)

(defun %analytics-summary-key () "analytics:summary")
(defun %analytics-event-key (event-id) (format nil "analytics:event:~A" event-id))

(defun %analytics-id-fragment (provider model)
  (%octets-hex
   (ironclad:digest-sequence
    :sha256
    (trivial-utf-8:string-to-utf-8-bytes
     (format nil "~A~C~A" provider #\Null model)))))

(defun %analytics-model-key (provider model)
  (format nil "analytics:model:~A" (%analytics-id-fragment provider model)))

(defun %analytics-minute-key (minute provider model)
  (format nil "analytics:minute:~A:~A"
          minute (%analytics-id-fragment provider model)))

(defun %analytics-minute (timestamp)
  (unless (and (stringp timestamp) (>= (length timestamp) 16))
    (error "analytics timestamp is invalid: ~S" timestamp))
  (format nil "~A:00Z" (subseq timestamp 0 16)))

(defun %analytics-empty (&key provider model minute)
  (list :schema-version +analytics-schema-version+
        :provider provider :model model :minute minute
        :request-count 0 :requests-with-usage 0
        :requests-with-input-usage 0 :requests-with-output-usage 0
        :input-tokens 0 :output-tokens 0 :total-tokens 0))

(defun %analytics-number (event field)
  (let ((value (jsown:val-safe event field)))
    (and (numberp value) (not (minusp value)) value)))

(defun %analytics-apply-event (stats event)
  (let ((input (%analytics-number event "input_tokens"))
        (output (%analytics-number event "output_tokens")))
    (incf (getf stats :request-count))
    (when (or input output) (incf (getf stats :requests-with-usage)))
    (when input
      (incf (getf stats :requests-with-input-usage))
      (incf (getf stats :input-tokens) input)
      (incf (getf stats :total-tokens) input))
    (when output
      (incf (getf stats :requests-with-output-usage))
      (incf (getf stats :output-tokens) output)
      (incf (getf stats :total-tokens) output))
    stats))

(defun %analytics-fetch-or-new (database key &rest initargs)
  (or (fetch* database key) (apply #'%analytics-empty initargs)))

(defun project-capture-analytics (host event)
  "Idempotently aggregate one capture into durable Tek9 analytics state."
  (let* ((database (expert-host-database host))
         (event-id (%capture-required-string event "event_id"))
         (provider (%capture-required-string event "provider"))
         (model (let ((value (jsown:val-safe event "model")))
                  (if (%non-empty-string-p value) value "unknown")))
         (minute (%analytics-minute (%capture-required-string event "completed_at")))
         (marker-key (%analytics-event-key event-id)))
    (with-write-transaction (database)
      (when (fetch* database marker-key)
        (return-from project-capture-analytics :existing))
      (let* ((summary-key (%analytics-summary-key))
             (model-key (%analytics-model-key provider model))
             (minute-key (%analytics-minute-key minute provider model))
             (summary (%analytics-fetch-or-new database summary-key))
             (model-stats (%analytics-fetch-or-new database model-key
                                                   :provider provider :model model))
             (minute-stats (%analytics-fetch-or-new database minute-key
                                                    :provider provider :model model
                                                    :minute minute)))
        (%analytics-apply-event summary event)
        (%analytics-apply-event model-stats event)
        (%analytics-apply-event minute-stats event)
        (put* database summary :id summary-key)
        (put* database model-stats :id model-key)
        (put* database minute-stats :id minute-key)
        (put* database
              (list :schema-version +analytics-schema-version+
                    :event-id event-id :provider provider :model model :minute minute)
              :id marker-key)))
    :created))

(defun %analytics-stats-json (stats &key coverage)
  (%json-object
   (cons "request_count" (getf stats :request-count 0))
   (cons "requests_with_usage" (getf stats :requests-with-usage 0))
   (cons "input_tokens" (getf stats :input-tokens 0))
   (cons "output_tokens" (getf stats :output-tokens 0))
   (cons "total_tokens" (getf stats :total-tokens 0))
   (cons "requests_with_input_usage"
         (and coverage (getf stats :requests-with-input-usage 0)))
   (cons "requests_with_output_usage"
         (and coverage (getf stats :requests-with-output-usage 0)))))

(defun %analytics-add-stats (target source)
  (dolist (field '(:request-count :requests-with-usage
                   :requests-with-input-usage :requests-with-output-usage
                   :input-tokens :output-tokens :total-tokens))
    (incf (getf target field) (getf source field 0)))
  target)

(defun %analytics-minute-records (database)
  (mapcar #'cdr
          (select-primary-range database "analytics:minute:"
                                :end "analytics:minute;")))

(defun %analytics-selected-p (record &key start end provider model)
  (let ((minute (getf record :minute)))
    (and (or (null start) (string<= start minute))
         (or (null end) (string< minute end))
         (or (null provider) (equal provider (getf record :provider)))
         (or (null model) (equal model (getf record :model))))))

(defun query-analytics-summary (host &key start end provider model)
  (let ((database (expert-host-database host)))
    (if (and (null start) (null end) (null provider) (null model))
        (%analytics-stats-json
         (or (fetch* database (%analytics-summary-key)) (%analytics-empty)))
        (let ((total (%analytics-empty)))
          (dolist (record (%analytics-minute-records database))
            (when (%analytics-selected-p record :start start :end end
                                                :provider provider :model model)
              (%analytics-add-stats total record)))
          (%analytics-stats-json total)))))

(defun query-analytics-models (host &key start end provider model)
  (let ((groups (make-hash-table :test #'equal)))
    (dolist (record (%analytics-minute-records (expert-host-database host)))
      (when (%analytics-selected-p record :start start :end end
                                          :provider provider :model model)
        (let* ((key (cons (getf record :provider) (getf record :model)))
               (stats (or (gethash key groups)
                          (setf (gethash key groups)
                                (%analytics-empty :provider (car key) :model (cdr key))))))
          (%analytics-add-stats stats record))))
    (let ((entries nil))
      (maphash
       (lambda (_key stats)
         (declare (ignore _key))
         (push
          (%json-object
           (cons "provider" (getf stats :provider))
           (cons "model" (getf stats :model))
           (cons "request_count" (getf stats :request-count 0))
           (cons "requests_with_usage" (getf stats :requests-with-usage 0))
           (cons "input_tokens" (getf stats :input-tokens 0))
           (cons "output_tokens" (getf stats :output-tokens 0))
           (cons "total_tokens" (getf stats :total-tokens 0)))
          entries))
       groups)
      (%json-object
       (cons "models"
             (sort entries #'string<
                   :key (lambda (item)
                          (format nil "~A/~A"
                                  (jsown:val-safe item "provider")
                                  (jsown:val-safe item "model")))))))))

(defun %analytics-bucket-start (minute granularity)
  (ecase granularity
    (:minute minute)
    (:hour (format nil "~A:00:00Z" (subseq minute 0 13)))
    (:day (format nil "~AT00:00:00Z" (subseq minute 0 10)))))

(defun query-analytics-timeline
    (host &key (granularity :minute) start end provider model coverage)
  (let ((groups (make-hash-table :test #'equal)))
    (dolist (record (%analytics-minute-records (expert-host-database host)))
      (when (%analytics-selected-p record :start start :end end
                                          :provider provider :model model)
        (let* ((bucket (%analytics-bucket-start (getf record :minute) granularity))
               (stats (or (gethash bucket groups)
                          (setf (gethash bucket groups) (%analytics-empty)))))
          (%analytics-add-stats stats record))))
    (let ((buckets nil)
          (seconds (ecase granularity (:minute 60) (:hour 3600) (:day 86400))))
      (maphash
       (lambda (bucket stats)
         (let ((json (%analytics-stats-json stats :coverage coverage)))
           (jsown:extend-js json ("start" bucket))
           (jsown:extend-js json ("bucket_seconds" seconds))
           (push json buckets)))
       groups)
      (let ((result
              (%json-object
               (cons "granularity" (string-downcase (symbol-name granularity)))
               (cons "buckets"
                     (sort buckets #'string<
                           :key (lambda (item) (jsown:val-safe item "start")))))))
        (when coverage
          (jsown:extend-js result ("coverage" "fields"))
          (jsown:extend-js result ("accounting" "completed_requests")))
        result))))

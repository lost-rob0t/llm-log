(in-package #:llm-log-expert)

(defparameter *base-query-task-accounting-with-retry*
  (symbol-function 'query-task-accounting))

(defconstant +max-task-classification-sources-per-request+ 16)
(defconstant +max-task-classification-assertions-per-source+ 64)

(defstruct (%breakdown-accumulator
            (:constructor %make-breakdown-accumulator (value)))
  value
  (requests (make-hash-table :test #'equal))
  (usage-ids '())
  (cost-ids '())
  (currencies (make-hash-table :test #'equal))
  (known-total 0)
  (known-count 0)
  (unknown-count 0)
  (input-total 0)
  (input-seen nil)
  (output-total 0)
  (output-seen nil)
  (cached-input-total 0)
  (cached-input-seen nil)
  (cached-output-total 0)
  (cached-output-seen nil)
  (reasoning-total 0)
  (reasoning-seen nil))

(defstruct (%classification-breakdown-accumulator
            (:constructor %make-classification-breakdown-accumulator
                (dimension value state)))
  dimension
  value
  state
  (usage (%make-breakdown-accumulator value))
  (classification-assertion-ids '())
  (rule-ids '())
  (evidence-ids '()))

(defun %bounded-task-usage-records (host payload)
  "Return the finite durable usage set admitted by QUERY_TASK_ACCOUNTING."
  (let* ((task-id (%required-json-string payload "task_id"))
         (max-depth (%required-rollup-integer payload "max_depth"
                                              0 +max-task-rollup-depth+))
         (max-nodes (%required-rollup-integer payload "max_nodes"
                                              1 +max-task-rollup-nodes+))
         (include-children
           (not (null (jsown:val-safe payload "include_children"))))
         (database (expert-host-database host))
         (seen-usages (make-hash-table :test #'equal))
         (result '()))
    (multiple-value-bind (task-ids graph-truncated)
        (%bounded-task-rollup-ids
         host task-id max-depth max-nodes include-children)
      (declare (ignore graph-truncated))
      (dolist (current-task-id task-ids)
        (let* ((candidates
                 (select-index-range
                  database "usage-task-id" current-task-id
                  :end current-task-id
                  :limit (1+ +max-task-rollup-usage-candidates+)))
               (usages
                 (remove-if-not
                  (lambda (projection)
                    (%usage-record-p projection current-task-id))
                  candidates)))
          (when (> (length usages) +max-task-rollup-usage-candidates+)
            (setf usages
                  (subseq usages 0 +max-task-rollup-usage-candidates+)))
          (dolist (usage usages)
            (let ((usage-id (getf usage :usage-id)))
              (unless (gethash usage-id seen-usages)
                (setf (gethash usage-id seen-usages) t)
                (push usage result)))))))
    (nreverse result)))

(defun %breakdown-add-token (accumulator usage key total-reader total-writer
                              seen-reader seen-writer)
  (declare (ignore seen-reader))
  (let ((value (getf usage key)))
    (when (numberp value)
      (funcall total-writer
               (+ (funcall total-reader accumulator) value)
               accumulator)
      (funcall seen-writer t accumulator))))

(defun %breakdown-add-usage (host accumulator usage)
  (let* ((database (expert-host-database host))
         (usage-id (getf usage :usage-id))
         (request-id (getf usage :request-id))
         (cost (fetch* database (%cost-key usage-id))))
    (when (%non-empty-string-p request-id)
      (setf (gethash request-id
                     (%breakdown-accumulator-requests accumulator))
            t))
    (pushnew usage-id (%breakdown-accumulator-usage-ids accumulator)
             :test #'equal)
    (%breakdown-add-token
     accumulator usage :input-tokens
     #'%breakdown-accumulator-input-total
     (lambda (value object)
       (setf (%breakdown-accumulator-input-total object) value))
     #'%breakdown-accumulator-input-seen
     (lambda (value object)
       (setf (%breakdown-accumulator-input-seen object) value)))
    (%breakdown-add-token
     accumulator usage :output-tokens
     #'%breakdown-accumulator-output-total
     (lambda (value object)
       (setf (%breakdown-accumulator-output-total object) value))
     #'%breakdown-accumulator-output-seen
     (lambda (value object)
       (setf (%breakdown-accumulator-output-seen object) value)))
    (%breakdown-add-token
     accumulator usage :cached-input-tokens
     #'%breakdown-accumulator-cached-input-total
     (lambda (value object)
       (setf (%breakdown-accumulator-cached-input-total object) value))
     #'%breakdown-accumulator-cached-input-seen
     (lambda (value object)
       (setf (%breakdown-accumulator-cached-input-seen object) value)))
    (%breakdown-add-token
     accumulator usage :cached-output-tokens
     #'%breakdown-accumulator-cached-output-total
     (lambda (value object)
       (setf (%breakdown-accumulator-cached-output-total object) value))
     #'%breakdown-accumulator-cached-output-seen
     (lambda (value object)
       (setf (%breakdown-accumulator-cached-output-seen object) value)))
    (%breakdown-add-token
     accumulator usage :reasoning-tokens
     #'%breakdown-accumulator-reasoning-total
     (lambda (value object)
       (setf (%breakdown-accumulator-reasoning-total object) value))
     #'%breakdown-accumulator-reasoning-seen
     (lambda (value object)
       (setf (%breakdown-accumulator-reasoning-seen object) value)))
    (let ((cost-id (and (listp cost) (getf cost :cost-assertion-id))))
      (when (%non-empty-string-p cost-id)
        (pushnew cost-id (%breakdown-accumulator-cost-ids accumulator)
                 :test #'equal)))
    (if (and (listp cost)
             (equal "known" (getf cost :state))
             (numberp (getf cost :amount))
             (%non-empty-string-p (getf cost :currency)))
        (progn
          (incf (%breakdown-accumulator-known-count accumulator))
          (incf (%breakdown-accumulator-known-total accumulator)
                (getf cost :amount))
          (setf (gethash (getf cost :currency)
                         (%breakdown-accumulator-currencies accumulator))
                t))
        (incf (%breakdown-accumulator-unknown-count accumulator)))))

(defun %breakdown-entry-json (accumulator)
  (let* ((currency-list
           (loop for currency being the hash-keys of
                 (%breakdown-accumulator-currencies accumulator)
                 collect currency))
         (currency-count (length currency-list))
         (coherent-currency
           (and (= currency-count 1) (first currency-list)))
         (cost-state
           (%task-rollup-cost-state
            (%breakdown-accumulator-known-count accumulator)
            (%breakdown-accumulator-unknown-count accumulator)
            currency-count))
         (known-amount
           (and coherent-currency
                (plusp (%breakdown-accumulator-known-count accumulator))
                (float (%breakdown-accumulator-known-total accumulator) 0.0))))
    (%json-object
     (cons "value" (%breakdown-accumulator-value accumulator))
     (cons "request_count"
           (hash-table-count (%breakdown-accumulator-requests accumulator)))
     (cons "usage_observation_count"
           (length (%breakdown-accumulator-usage-ids accumulator)))
     (cons "input_tokens"
           (and (%breakdown-accumulator-input-seen accumulator)
                (%breakdown-accumulator-input-total accumulator)))
     (cons "output_tokens"
           (and (%breakdown-accumulator-output-seen accumulator)
                (%breakdown-accumulator-output-total accumulator)))
     (cons "cached_input_tokens"
           (and (%breakdown-accumulator-cached-input-seen accumulator)
                (%breakdown-accumulator-cached-input-total accumulator)))
     (cons "cached_output_tokens"
           (and (%breakdown-accumulator-cached-output-seen accumulator)
                (%breakdown-accumulator-cached-output-total accumulator)))
     (cons "reasoning_tokens"
           (and (%breakdown-accumulator-reasoning-seen accumulator)
                (%breakdown-accumulator-reasoning-total accumulator)))
     (cons "known_cost_amount" known-amount)
     (cons "known_cost_currency" coherent-currency)
     (cons "cost_state" cost-state)
     (cons "usage_observation_ids"
           (sort (copy-list (%breakdown-accumulator-usage-ids accumulator))
                 #'string<))
     (cons "cost_assertion_ids"
           (sort (copy-list (%breakdown-accumulator-cost-ids accumulator))
                 #'string<)))))

(defun %breakdown-value< (left right)
  (let ((left-value (%breakdown-accumulator-value left))
        (right-value (%breakdown-accumulator-value right)))
    (cond
      ((null left-value) (not (null right-value)))
      ((null right-value) nil)
      (t (string< left-value right-value)))))

(defun %dimension-breakdown (host usages dimension)
  (let ((groups (make-hash-table :test #'equal)))
    (dolist (usage usages)
      (let* ((value (getf usage dimension))
             (accumulator
               (or (gethash value groups)
                   (setf (gethash value groups)
                         (%make-breakdown-accumulator value)))))
        (%breakdown-add-usage host accumulator usage)))
    (mapcar #'%breakdown-entry-json
            (sort (loop for accumulator being the hash-values of groups
                        collect accumulator)
                  #'%breakdown-value<))))

(defun %classification-source-records-for-request (host request-id)
  (when (%non-empty-string-p request-id)
    (let ((database (expert-host-database host)))
      (remove-if-not
       (lambda (projection)
         (and (listp projection)
              (equal "classification-source" (getf projection :type))
              (equal request-id (getf projection :request-id))))
       (select-index-range
        database "classification-source-request-id" request-id
        :end request-id
        :limit +max-task-classification-sources-per-request+)))))

(defun %classification-assertions-for-source (host source)
  (let ((event-id (getf source :event-id)))
    (when (%non-empty-string-p event-id)
      (remove-if-not
       #'%classifier-assertion-p
       (select-index-range
        (expert-host-database host)
        "classification-assertion-source-event" event-id
        :end event-id
        :limit +max-task-classification-assertions-per-source+)))))

(defun %classification-group-key (assertion)
  (let ((value (getf assertion :value)))
    (list (getf value :dimension)
          (getf value :value)
          (getf value :state))))

(defun %classification-group-valid-p (key)
  (every #'%non-empty-string-p key))

(defun %classification-add-provenance (accumulator assertion)
  (let ((value (getf assertion :value)))
    (pushnew (getf assertion :assertion-id)
             (%classification-breakdown-accumulator-classification-assertion-ids
              accumulator)
             :test #'equal)
    (pushnew (getf value :rule-id)
             (%classification-breakdown-accumulator-rule-ids accumulator)
             :test #'equal)
    (dolist (evidence-id (getf value :evidence-ids))
      (pushnew evidence-id
               (%classification-breakdown-accumulator-evidence-ids accumulator)
               :test #'equal))))

(defun %classification-add-usage-once (host accumulator usage)
  (let* ((base (%classification-breakdown-accumulator-usage accumulator))
         (usage-id (getf usage :usage-id)))
    (unless (member usage-id (%breakdown-accumulator-usage-ids base)
                    :test #'equal)
      (%breakdown-add-usage host base usage))))

(defun %classification-breakdown-key< (left right)
  (labels ((before-p (left-value right-value)
             (cond
               ((string< left-value right-value) t)
               ((string> left-value right-value) nil)
               (t :equal))))
    (let ((dimension-order
            (before-p
             (%classification-breakdown-accumulator-dimension left)
             (%classification-breakdown-accumulator-dimension right))))
      (if (eq dimension-order :equal)
          (let ((value-order
                  (before-p
                   (%classification-breakdown-accumulator-value left)
                   (%classification-breakdown-accumulator-value right))))
            (if (eq value-order :equal)
                (string<
                 (%classification-breakdown-accumulator-state left)
                 (%classification-breakdown-accumulator-state right))
                value-order))
          dimension-order))))

(defun %classification-breakdown-entry-json (accumulator)
  (let* ((base (%classification-breakdown-accumulator-usage accumulator))
         (entry (%breakdown-entry-json base)))
    (append
     (%json-object
      (cons "dimension"
            (%classification-breakdown-accumulator-dimension accumulator))
      (cons "state"
            (%classification-breakdown-accumulator-state accumulator)))
     entry
     (%json-object
      (cons "classification_assertion_ids"
            (sort
             (remove-if-not
              #'%non-empty-string-p
              (copy-list
               (%classification-breakdown-accumulator-classification-assertion-ids
                accumulator)))
             #'string<))
      (cons "rule_ids"
            (sort
             (remove-if-not
              #'%non-empty-string-p
              (copy-list
               (%classification-breakdown-accumulator-rule-ids accumulator)))
             #'string<))
      (cons "evidence_ids"
            (sort
             (remove-if-not
              #'%non-empty-string-p
              (copy-list
               (%classification-breakdown-accumulator-evidence-ids accumulator)))
             #'string<))))))

(defun %classification-breakdown (host usages)
  (let ((groups (make-hash-table :test #'equal)))
    (dolist (usage usages)
      (let ((request-id (getf usage :request-id)))
        (dolist (source (%classification-source-records-for-request host request-id))
          (dolist (assertion (%classification-assertions-for-source host source))
            (let ((key (%classification-group-key assertion)))
              (when (%classification-group-valid-p key)
                (destructuring-bind (dimension value state) key
                  (let ((accumulator
                          (or (gethash key groups)
                              (setf (gethash key groups)
                                    (%make-classification-breakdown-accumulator
                                     dimension value state)))))
                    (%classification-add-provenance accumulator assertion)
                    (%classification-add-usage-once host accumulator usage)))))))))
    (mapcar
     #'%classification-breakdown-entry-json
     (sort
      (loop for accumulator being the hash-values of groups
            collect accumulator)
      #'%classification-breakdown-key<))))

(defun %task-usage-breakdowns (host payload)
  (let ((usages (%bounded-task-usage-records host payload)))
    (%json-object
     (cons "provider" (%dimension-breakdown host usages :provider))
     (cons "model" (%dimension-breakdown host usages :model))
     (cons "client" (%dimension-breakdown host usages :client))
     (cons "classification" (%classification-breakdown host usages)))))

(defun query-task-accounting (host payload)
  "Add bounded provider/model/client/classification projections to the durable task rollup."
  (let ((result
          (funcall *base-query-task-accounting-with-retry* host payload)))
    (append result
            (list (cons "breakdowns"
                        (%task-usage-breakdowns host payload))))))
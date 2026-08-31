(in-package #:llm-log-expert)

(defparameter *base-query-task-accounting-with-retry*
  (symbol-function 'query-task-accounting))

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
    (push usage-id (%breakdown-accumulator-usage-ids accumulator))
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
        (push cost-id (%breakdown-accumulator-cost-ids accumulator))))
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

(defun %task-usage-breakdowns (host payload)
  (let ((usages (%bounded-task-usage-records host payload)))
    (%json-object
     (cons "provider" (%dimension-breakdown host usages :provider))
     (cons "model" (%dimension-breakdown host usages :model))
     (cons "client" (%dimension-breakdown host usages :client)))))

(defun query-task-accounting (host payload)
  "Add bounded provider/model/client projections to the durable task rollup."
  (let ((result
          (funcall *base-query-task-accounting-with-retry* host payload)))
    (append result
            (list (cons "breakdowns"
                        (%task-usage-breakdowns host payload))))))
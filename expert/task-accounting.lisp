(in-package #:llm-log-expert)

(defparameter +task-schema-version+ 1)
(defparameter +usage-schema-version+ 1)
(defparameter +pricing-schema-version+ 1)
(defparameter +cost-schema-version+ 1)
(defparameter +max-task-rollup-depth+ 16)
(defparameter +max-task-rollup-nodes+ 512)
(defparameter +max-task-rollup-usage-candidates+ 4096)

(defun %task-key (task-id) (format nil "task:~A" task-id))
(defun %usage-key (usage-id) (format nil "usage:~A" usage-id))
(defun %pricing-key (snapshot-id) (format nil "pricing:~A" snapshot-id))
(defun %cost-key (usage-id) (format nil "cost:~A" usage-id))

(defun %required-json-string (object field)
  (let ((value (jsown:val-safe object field)))
    (unless (%non-empty-string-p value)
      (error "~A is required" field))
    value))

(defun %optional-number (object field)
  (let ((value (jsown:val-safe object field)))
    (when value
      (unless (numberp value) (error "~A must be numeric" field)))
    value))

(defun %put-immutable (host key projection conflict-label)
  (let ((database (expert-host-database host)))
    (with-write-transaction (database)
      (let ((existing (fetch* database key))
            (revision (or (fetch* database +kb-revision-key+) 1)))
        (cond
          ((null existing)
           (let ((next (1+ revision)))
             (put* database projection :id key)
             (put* database next :id +kb-revision-key+)
             (values :created next)))
          ((equal existing projection) (values :existing revision))
          (t (error "~A_conflict: ~A" conflict-label key)))))))

(defun %task-projection (task &optional parent-id)
  (let ((task-id (%required-json-string task "task_id")))
    (list :schema-version +task-schema-version+
          :task-id task-id
          :parent-task-id (or (jsown:val-safe task "parent_task_id") parent-id)
          :session-id (jsown:val-safe task "session_id")
          :originating-user-message-id (jsown:val-safe task "originating_user_message_id")
          :rule-id (%required-json-string task "rule_id")
          :rule-version (jsown:val-safe task "rule_version")
          :evidence-ids (copy-list (or (jsown:val-safe task "evidence_ids") '())))))

(defun %usage-projection (usage)
  (list :schema-version +usage-schema-version+
        :usage-id (%required-json-string usage "usage_id")
        :request-id (%required-json-string usage "request_id")
        :task-id (%required-json-string usage "task_id")
        :provider (%required-json-string usage "provider")
        :model (%required-json-string usage "model")
        :input-tokens (%optional-number usage "input_tokens")
        :output-tokens (%optional-number usage "output_tokens")
        :cached-input-tokens (%optional-number usage "cached_input_tokens")
        :cached-output-tokens (%optional-number usage "cached_output_tokens")
        :reasoning-tokens (%optional-number usage "reasoning_tokens")))

(defun %pricing-projection (snapshot)
  (list :schema-version +pricing-schema-version+
        :snapshot-id (%required-json-string snapshot "snapshot_id")
        :provider (%required-json-string snapshot "provider")
        :model (%required-json-string snapshot "model")
        :currency (%required-json-string snapshot "currency")
        :effective-at (%required-json-string snapshot "effective_at")
        :source (%required-json-string snapshot "source")
        :source-version (%required-json-string snapshot "source_version")
        :input-per-token (%optional-number snapshot "input_per_token")
        :output-per-token (%optional-number snapshot "output_per_token")))

(defun %find-pricing-snapshot (usage snapshots)
  (find-if (lambda (snapshot)
             (and (equal (jsown:val-safe snapshot "provider")
                         (jsown:val-safe usage "provider"))
                  (equal (jsown:val-safe snapshot "model")
                         (jsown:val-safe usage "model"))))
           snapshots))

(defun %prolog-cost (host usage snapshot)
  (let* ((known (not (null snapshot)))
         (data (%json-object
                (cons "pricing_state" (if known "known" "unknown"))
                (cons "input_tokens" (or (jsown:val-safe usage "input_tokens") 0))
                (cons "output_tokens" (or (jsown:val-safe usage "output_tokens") 0))
                (cons "input_per_token" (if known (or (jsown:val-safe snapshot "input_per_token") 0) 0))
                (cons "output_per_token" (if known (or (jsown:val-safe snapshot "output_per_token") 0) 0))))
         (reply (prolog-worker-request host "task_cost" data)))
    (unless (equal (jsown:val-safe reply "status") "ok")
      (error "reasoner_error: ~A" (jsown:to-json reply)))
    (let* ((result (jsown:val-safe reply "result"))
           (state (jsown:val-safe result "state"))
           (amount (jsown:val-safe result "amount"))
           (rule-version (jsown:val-safe reply "rule_version")))
      (unless (member state '("known" "unknown") :test #'equal)
        (%reject-invalid-reasoner-result host "task_cost returned invalid state"))
      (when (and (equal state "known") (not (numberp amount)))
        (%reject-invalid-reasoner-result host "task_cost known state requires numeric amount"))
      (values state amount rule-version))))

(defun %cost-projection (usage snapshot state amount rule-version)
  (let ((usage-id (%required-json-string usage "usage_id")))
    (list :schema-version +cost-schema-version+
          :cost-assertion-id (format nil "cost/~A" usage-id)
          :task-id (%required-json-string usage "task_id")
          :request-id (%required-json-string usage "request_id")
          :usage-observation-id usage-id
          :pricing-snapshot-id (and snapshot (%required-json-string snapshot "snapshot_id"))
          :state state
          :amount amount
          :currency (and snapshot (jsown:val-safe snapshot "currency"))
          :rule-id "task.cost"
          :rule-version rule-version
          :expert-version "task-accounting/1"
          :evidence-ids (list usage-id))))

(defun account-task-usage (host payload)
  "Persist one bounded task fragment and derive per-request cost through declared Prolog rules."
  (let* ((task (jsown:val-safe payload "task"))
         (children (or (jsown:val-safe payload "children") '()))
         (usages (or (jsown:val-safe payload "usage_observations") '()))
         (snapshots (or (jsown:val-safe payload "pricing_snapshots") '())))
    (unless (and (consp task) (eq (first task) :obj)) (error "task must be an object"))
    (when (> (length children) 64) (error "too many children"))
    (when (> (length usages) 256) (error "too many usage observations"))
    (when (> (length snapshots) 128) (error "too many pricing snapshots"))
    (let* ((task-id (%required-json-string task "task_id"))
           (task-projection (%task-projection task)))
      (%put-immutable host (%task-key task-id) task-projection "task")
      (dolist (child children)
        (let* ((projection (%task-projection child task-id))
               (child-id (getf projection :task-id)))
          (%put-immutable host (%task-key child-id) projection "task")))
      (dolist (snapshot snapshots)
        (let* ((projection (%pricing-projection snapshot))
               (snapshot-id (getf projection :snapshot-id)))
          (%put-immutable host (%pricing-key snapshot-id) projection "pricing")))
      (let ((seen-requests '())
            (known-total 0)
            (known-currency nil)
            (known-snapshot-id nil)
            (unknown-p nil))
        (dolist (usage usages)
          (let ((request-id (%required-json-string usage "request_id")))
            (unless (member request-id seen-requests :test #'equal)
              (push request-id seen-requests)
              (let* ((usage-projection (%usage-projection usage))
                     (usage-id (getf usage-projection :usage-id))
                     (snapshot (%find-pricing-snapshot usage snapshots)))
                (%put-immutable host (%usage-key usage-id) usage-projection "usage")
                (multiple-value-bind (state amount rule-version)
                    (%prolog-cost host usage snapshot)
                  (let ((cost (%cost-projection usage snapshot state amount rule-version)))
                    (%put-immutable host (%cost-key usage-id) cost "cost"))
                  (if (equal state "known")
                      (progn
                        (incf known-total amount)
                        (unless known-currency (setf known-currency (jsown:val-safe snapshot "currency")))
                        (unless known-snapshot-id (setf known-snapshot-id (jsown:val-safe snapshot "snapshot_id"))))
                      (setf unknown-p t)))))))
        (%json-object
         (cons "task_id" task-id)
         (cons "pricing_snapshot_id" known-snapshot-id)
         (cons "known_cost_currency" known-currency)
         (cons "known_cost_amount" (float known-total 0.0))
         (cons "unknown_cost_state" (if unknown-p "unknown" "known"))
         (cons "request_count" (length seen-requests))
         (cons "kb_revision" (current-kb-revision host)))))))

(defun %required-rollup-integer (payload field minimum maximum)
  (let ((value (jsown:val-safe payload field)))
    (unless (and (integerp value) (<= minimum value maximum))
      (error "~A must be an integer from ~D through ~D" field minimum maximum))
    value))

(defun %task-record-p (projection)
  (and (listp projection)
       (%non-empty-string-p (getf projection :task-id))
       (%non-empty-string-p (getf projection :rule-id))))

(defun %usage-record-p (projection task-id)
  (and (listp projection)
       (%non-empty-string-p (getf projection :usage-id))
       (equal task-id (getf projection :task-id))))

(defun %indexed-task-children (database task-id limit)
  (remove-if-not
   #'%task-record-p
   (select-index-range database "task-parent-task-id"
                       task-id :end task-id :limit limit)))

(defun %bounded-task-rollup-ids (host root-task-id max-depth max-nodes include-children)
  "Return visited task IDs and whether the requested subtree was truncated."
  (let* ((database (expert-host-database host))
         (root (fetch* database (%task-key root-task-id)))
         (queue (list (cons root-task-id 0)))
         (seen (make-hash-table :test #'equal))
         (visited '())
         (truncated nil))
    (unless (%task-record-p root)
      (error "unknown_task: ~A" root-task-id))
    (loop while queue
          do (let* ((entry (pop queue))
                    (task-id (car entry))
                    (depth (cdr entry)))
               (unless (gethash task-id seen)
                 (when (>= (length visited) max-nodes)
                   (setf truncated t)
                   (return))
                 (setf (gethash task-id seen) t)
                 (push task-id visited)
                 (when include-children
                   (if (< depth max-depth)
                       (let ((children
                               (%indexed-task-children
                                database task-id (1+ max-nodes))))
                         (dolist (child children)
                           (let ((child-id (getf child :task-id)))
                             (unless (gethash child-id seen)
                               (setf queue
                                     (nconc queue
                                            (list (cons child-id (1+ depth)))))))))
                       (when (%indexed-task-children database task-id 1)
                         (setf truncated t)))))))
    (values (nreverse visited) truncated)))

(defun %add-optional-token-count (usage key current seen-p)
  (let ((value (getf usage key)))
    (if (numberp value)
        (values (+ current value) t)
        (values current seen-p))))

(defun %task-rollup-cost-state (known-count unknown-count currency-count)
  "Describe completeness of already-derived immutable cost assertions.

This is projection-state validation, not a second pricing/rules engine: all
per-request cost inference remains owned by the declared SWI-Prolog task_cost
predicate."
  (cond
    ((zerop known-count) "unknown")
    ((or (plusp unknown-count) (> currency-count 1)) "partial")
    (t "known")))

(defun query-task-accounting (host payload)
  "Reconstruct one finite task rollup from durable Tek9 task/usage/cost records."
  (unless (and (consp payload) (eq (first payload) :obj))
    (error "payload must be a JSON object"))
  (let* ((task-id (%required-json-string payload "task_id"))
         (max-depth (%required-rollup-integer payload "max_depth"
                                              0 +max-task-rollup-depth+))
         (max-nodes (%required-rollup-integer payload "max_nodes"
                                              1 +max-task-rollup-nodes+))
         (include-children (not (null (jsown:val-safe payload "include_children"))))
         (database (expert-host-database host)))
    (multiple-value-bind (task-ids graph-truncated)
        (%bounded-task-rollup-ids host task-id max-depth max-nodes include-children)
      (let ((seen-usages (make-hash-table :test #'equal))
            (seen-requests (make-hash-table :test #'equal))
            (currencies (make-hash-table :test #'equal))
            (usage-ids '())
            (cost-ids '())
            (known-total 0)
            (known-count 0)
            (unknown-count 0)
            (input-total 0) (input-seen nil)
            (output-total 0) (output-seen nil)
            (cached-input-total 0) (cached-input-seen nil)
            (cached-output-total 0) (cached-output-seen nil)
            (reasoning-total 0) (reasoning-seen nil)
            (observation-truncated nil))
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
              (setf observation-truncated t
                    usages (subseq usages 0 +max-task-rollup-usage-candidates+)))
            (dolist (usage usages)
              (let ((usage-id (getf usage :usage-id)))
                (unless (gethash usage-id seen-usages)
                  (setf (gethash usage-id seen-usages) t)
                  (push usage-id usage-ids)
                  (let ((request-id (getf usage :request-id)))
                    (when (%non-empty-string-p request-id)
                      (setf (gethash request-id seen-requests) t)))
                  (multiple-value-setq (input-total input-seen)
                    (%add-optional-token-count usage :input-tokens input-total input-seen))
                  (multiple-value-setq (output-total output-seen)
                    (%add-optional-token-count usage :output-tokens output-total output-seen))
                  (multiple-value-setq (cached-input-total cached-input-seen)
                    (%add-optional-token-count usage :cached-input-tokens
                                               cached-input-total cached-input-seen))
                  (multiple-value-setq (cached-output-total cached-output-seen)
                    (%add-optional-token-count usage :cached-output-tokens
                                               cached-output-total cached-output-seen))
                  (multiple-value-setq (reasoning-total reasoning-seen)
                    (%add-optional-token-count usage :reasoning-tokens
                                               reasoning-total reasoning-seen))
                  (let ((cost (fetch* database (%cost-key usage-id))))
                    (if (and (listp cost)
                             (equal "known" (getf cost :state))
                             (numberp (getf cost :amount))
                             (%non-empty-string-p (getf cost :currency)))
                        (progn
                          (incf known-count)
                          (incf known-total (getf cost :amount))
                          (setf (gethash (getf cost :currency) currencies) t)
                          (when (%non-empty-string-p (getf cost :cost-assertion-id))
                            (push (getf cost :cost-assertion-id) cost-ids)))
                        (progn
                          (incf unknown-count)
                          (when (and (listp cost)
                                     (%non-empty-string-p
                                      (getf cost :cost-assertion-id)))
                            (push (getf cost :cost-assertion-id) cost-ids))))))))))
        (let* ((currency-list
                 (loop for currency being the hash-keys of currencies collect currency))
               (currency-count (length currency-list))
               (cost-state (%task-rollup-cost-state
                            known-count unknown-count currency-count))
               (coherent-currency (and (= currency-count 1) (first currency-list)))
               (known-amount (and coherent-currency
                                  (float known-total 0.0)))
               (truncated (or graph-truncated observation-truncated)))
          (%json-object
           (cons "task_id" task-id)
           (cons "task_ids" (sort (copy-list task-ids) #'string<))
           (cons "request_count" (hash-table-count seen-requests))
           (cons "input_tokens" (and input-seen input-total))
           (cons "output_tokens" (and output-seen output-total))
           (cons "cached_input_tokens" (and cached-input-seen cached-input-total))
           (cons "cached_output_tokens" (and cached-output-seen cached-output-total))
           (cons "reasoning_tokens" (and reasoning-seen reasoning-total))
           (cons "known_cost_amount" known-amount)
           (cons "known_cost_currency" coherent-currency)
           (cons "cost_state" cost-state)
           (cons "usage_observation_ids" (sort usage-ids #'string<))
           (cons "cost_assertion_ids" (sort cost-ids #'string<))
           (cons "truncated" truncated)
           (cons "kb_revision" (current-kb-revision host))))))))

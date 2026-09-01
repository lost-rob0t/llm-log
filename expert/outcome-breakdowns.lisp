(in-package #:llm-log-expert)

(defconstant +max-task-outcome-assertions-per-request+ 64)

(defstruct (%outcome-breakdown-accumulator
            (:constructor %make-outcome-breakdown-accumulator (value)))
  value
  (usage (%make-breakdown-accumulator value))
  (outcome-assertion-ids '())
  (rule-ids '())
  (evidence-ids '()))

(defparameter *base-task-usage-breakdowns-with-classification*
  (symbol-function '%task-usage-breakdowns))

(defun %task-outcome-assertion-valid-p (assertion request-id)
  (and (listp assertion)
       (%non-empty-string-p (getf assertion :assertion-id))
       (equal "request" (getf assertion :scope))
       (equal request-id (getf assertion :scope-id))
       (member (getf assertion :outcome) +outcome-values+ :test #'equal)))

(defun %task-current-outcome-assertion (host request-id)
  "Return the single current durable request outcome, or NIL when unlabeled."
  (unless (%non-empty-string-p request-id)
    (return-from %task-current-outcome-assertion nil))
  (let* ((database (expert-host-database host))
         (scope-key (%outcome-scope-key "request" request-id))
         (candidates
           (select-index-range
            database "outcome-assertion-scope-key" scope-key
            :end scope-key
            :limit +max-task-outcome-assertions-per-request+))
         (assertions
           (remove-if-not
            (lambda (assertion)
              (%task-outcome-assertion-valid-p assertion request-id))
            candidates))
         (current
           (remove-if
            (lambda (assertion)
              (%outcome-successors
               database (getf assertion :assertion-id) 1))
            assertions)))
    (when (> (length current) 1)
      (error "outcome_current_assertion_conflict: request ~A" request-id))
    (first current)))

(defun %validate-task-outcome-provenance (assertion)
  (let ((assertion-id (getf assertion :assertion-id))
        (rule-id (getf assertion :rule-id))
        (evidence-ids (getf assertion :evidence-ids)))
    (unless (%non-empty-string-p assertion-id)
      (error "outcome_assertion_missing_id"))
    (unless (%non-empty-string-p rule-id)
      (error "outcome_assertion_missing_rule_id: ~A" assertion-id))
    (unless (and (listp evidence-ids)
                 evidence-ids
                 (every #'%non-empty-string-p evidence-ids))
      (error "outcome_assertion_missing_evidence: ~A" assertion-id))
    assertion))

(defun %outcome-breakdown-add-provenance (accumulator assertion)
  (%validate-task-outcome-provenance assertion)
  (pushnew (getf assertion :assertion-id)
           (%outcome-breakdown-accumulator-outcome-assertion-ids accumulator)
           :test #'equal)
  (pushnew (getf assertion :rule-id)
           (%outcome-breakdown-accumulator-rule-ids accumulator)
           :test #'equal)
  (dolist (evidence-id (getf assertion :evidence-ids))
    (pushnew evidence-id
             (%outcome-breakdown-accumulator-evidence-ids accumulator)
             :test #'equal)))

(defun %outcome-breakdown-add-usage-once (host accumulator usage)
  (let* ((base (%outcome-breakdown-accumulator-usage accumulator))
         (usage-id (getf usage :usage-id)))
    (unless (member usage-id (%breakdown-accumulator-usage-ids base)
                    :test #'equal)
      (%breakdown-add-usage host base usage))))

(defun %outcome-breakdown-entry-json (accumulator)
  (let ((entry
          (%breakdown-entry-json
           (%outcome-breakdown-accumulator-usage accumulator))))
    (%json-merge-objects
     entry
     (%json-object
      (cons "outcome_assertion_ids"
            (sort (copy-list
                   (%outcome-breakdown-accumulator-outcome-assertion-ids
                    accumulator))
                  #'string<))
      (cons "rule_ids"
            (sort (copy-list
                   (%outcome-breakdown-accumulator-rule-ids accumulator))
                  #'string<))
      (cons "evidence_ids"
            (sort (copy-list
                   (%outcome-breakdown-accumulator-evidence-ids accumulator))
                  #'string<))))))

(defun %outcome-breakdown-key< (left right)
  (string< (%outcome-breakdown-accumulator-value left)
           (%outcome-breakdown-accumulator-value right)))

(defun %outcome-breakdown (host usages)
  (let ((groups (make-hash-table :test #'equal)))
    (dolist (usage usages)
      (let* ((request-id (getf usage :request-id))
             (assertion (%task-current-outcome-assertion host request-id))
             (value (if assertion (getf assertion :outcome) "unlabeled"))
             (accumulator
               (or (gethash value groups)
                   (setf (gethash value groups)
                         (%make-outcome-breakdown-accumulator value)))))
        (%outcome-breakdown-add-usage-once host accumulator usage)
        (when assertion
          (%outcome-breakdown-add-provenance accumulator assertion))))
    (mapcar #'%outcome-breakdown-entry-json
            (sort (loop for accumulator being the hash-values of groups
                        collect accumulator)
                  #'%outcome-breakdown-key<))))

(defun %task-usage-breakdowns (host payload)
  "Extend existing bounded usage breakdowns with stored #15 outcome labels."
  (let ((base
          (funcall *base-task-usage-breakdowns-with-classification*
                   host payload))
        (usages (%bounded-task-usage-records host payload)))
    (append base
            (list (cons "outcome" (%outcome-breakdown host usages))))))

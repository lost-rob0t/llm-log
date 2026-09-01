(in-package #:llm-log-expert)

(defparameter *base-query-task-accounting-with-breakdowns*
  (symbol-function 'query-task-accounting))

(defparameter +unsuccessful-outcomes+
  '("failure" "rejected" "timeout" "cancelled"))

(defstruct (%retry-economics-accumulator
            (:constructor %make-retry-economics-accumulator (value)))
  value
  (usage (%make-breakdown-accumulator value))
  (outcome-assertion-ids '())
  (rule-ids '())
  (evidence-ids '()))

(defun %retry-economics-add-usage (host accumulator usage)
  (let* ((base (%retry-economics-accumulator-usage accumulator))
         (usage-id (getf usage :usage-id)))
    (unless (member usage-id (%breakdown-accumulator-usage-ids base)
                    :test #'equal)
      (%breakdown-add-usage host base usage))))

(defun %retry-economics-add-outcome-provenance (accumulator assertion)
  (%validate-task-outcome-provenance assertion)
  (pushnew (getf assertion :assertion-id)
           (%retry-economics-accumulator-outcome-assertion-ids accumulator)
           :test #'equal)
  (pushnew (getf assertion :rule-id)
           (%retry-economics-accumulator-rule-ids accumulator)
           :test #'equal)
  (dolist (evidence-id (getf assertion :evidence-ids))
    (pushnew evidence-id
             (%retry-economics-accumulator-evidence-ids accumulator)
             :test #'equal)))

(defun %retry-economics-request-ids (accumulator)
  (sort
   (loop for request-id being the hash-keys of
         (%breakdown-accumulator-requests
          (%retry-economics-accumulator-usage accumulator))
         collect request-id)
   #'string<))

(defun %retry-economics-entry-json (accumulator)
  (%json-merge-objects
   (%breakdown-entry-json (%retry-economics-accumulator-usage accumulator))
   (%json-object
    (cons "request_ids" (%retry-economics-request-ids accumulator))
    (cons "outcome_assertion_ids"
          (sort (copy-list
                 (%retry-economics-accumulator-outcome-assertion-ids accumulator))
                #'string<))
    (cons "rule_ids"
          (sort (copy-list (%retry-economics-accumulator-rule-ids accumulator))
                #'string<))
    (cons "evidence_ids"
          (sort (copy-list (%retry-economics-accumulator-evidence-ids accumulator))
                #'string<)))))

(defun %usage-records-by-request (usages)
  (let ((records (make-hash-table :test #'equal)))
    (dolist (usage usages)
      (let ((request-id (getf usage :request-id)))
        (when (%non-empty-string-p request-id)
          (push usage (gethash request-id records)))))
    records))

(defun %retry-predecessors (usages)
  "Return request -> explicit predecessor map, rejecting conflicting durable edges."
  (let ((predecessors (make-hash-table :test #'equal))
        (seen (make-hash-table :test #'equal)))
    (dolist (usage usages)
      (let ((request-id (getf usage :request-id))
            (predecessor (getf usage :retry-of-request-id)))
        (when (and (%non-empty-string-p request-id)
                   (%non-empty-string-p predecessor))
          (when (and (gethash request-id seen)
                     (not (equal predecessor (gethash request-id predecessors))))
            (error "retry_graph_conflict: request ~A" request-id))
          (setf (gethash request-id predecessors) predecessor
                (gethash request-id seen) t))))
    predecessors))

(defun %retry-economics-add-request (host accumulator request-id records)
  (dolist (usage (gethash request-id records))
    (%retry-economics-add-usage host accumulator usage)))

(defun %retry-burn-predecessor-ids (terminal-id predecessors records)
  "Walk the strict explicit retry ancestry for TERMINAL-ID inside bounded RECORDS."
  (let ((result '())
        (seen (make-hash-table :test #'equal))
        (complete t)
        (current terminal-id))
    (setf (gethash terminal-id seen) t)
    (loop
      for predecessor = (gethash current predecessors)
      while predecessor
      do
         (when (gethash predecessor seen)
           (error "retry_graph_cycle: request ~A" predecessor))
         (setf (gethash predecessor seen) t)
         (unless (gethash predecessor records)
           (setf complete nil)
           (return))
         (push predecessor result)
         (setf current predecessor))
    (values (nreverse result) complete)))

(defun %task-retry-economics-analysis (host payload)
  (let* ((usages (%bounded-task-usage-records host payload))
         (records (%usage-records-by-request usages))
         (predecessors (%retry-predecessors usages))
         (unsuccessful (%make-retry-economics-accumulator "unsuccessful"))
         (retry (%make-retry-economics-accumulator "retry"))
         (burn (%make-retry-economics-accumulator "burn_before_success"))
         (successful-terminal-ids '())
         (successful-terminal-assertion-ids '())
         (successful-terminal-rule-ids '())
         (successful-terminal-evidence-ids '())
         (retry-graph-complete t))
    (maphash
     (lambda (request-id request-usages)
       (declare (ignore request-usages))
       (let ((assertion (%task-current-outcome-assertion host request-id)))
         (when (and assertion
                    (member (getf assertion :outcome)
                            +unsuccessful-outcomes+
                            :test #'equal))
           (%retry-economics-add-request host unsuccessful request-id records)
           (%retry-economics-add-outcome-provenance unsuccessful assertion))
         (when (gethash request-id predecessors)
           (%retry-economics-add-request host retry request-id records))
         (when (and assertion (equal "success" (getf assertion :outcome)))
           (%validate-task-outcome-provenance assertion)
           (pushnew request-id successful-terminal-ids :test #'equal)
           (pushnew (getf assertion :assertion-id)
                    successful-terminal-assertion-ids :test #'equal)
           (pushnew (getf assertion :rule-id)
                    successful-terminal-rule-ids :test #'equal)
           (dolist (evidence-id (getf assertion :evidence-ids))
             (pushnew evidence-id successful-terminal-evidence-ids :test #'equal))
           (multiple-value-bind (ancestor-ids complete)
               (%retry-burn-predecessor-ids request-id predecessors records)
             (unless complete
               (setf retry-graph-complete nil))
             (dolist (ancestor-id ancestor-ids)
               (%retry-economics-add-request host burn ancestor-id records))))))
     records)
    (%json-object
     (cons "unsuccessful" (%retry-economics-entry-json unsuccessful))
     (cons "retry" (%retry-economics-entry-json retry))
     (cons "burn_before_success"
           (%json-merge-objects
            (%retry-economics-entry-json burn)
            (%json-object
             (cons "successful_terminal_count"
                   (length successful-terminal-ids))
             (cons "successful_terminal_ids"
                   (sort successful-terminal-ids #'string<))
             (cons "successful_terminal_outcome_assertion_ids"
                   (sort successful-terminal-assertion-ids #'string<))
             (cons "successful_terminal_rule_ids"
                   (sort successful-terminal-rule-ids #'string<))
             (cons "successful_terminal_evidence_ids"
                   (sort successful-terminal-evidence-ids #'string<))
             (cons "retry_graph_complete" retry-graph-complete)))))))

(defun query-task-accounting (host payload)
  "Add bounded outcome-aware retry economics to durable task accounting."
  (let ((result
          (funcall *base-query-task-accounting-with-breakdowns* host payload)))
    (append result
            (list (cons "analysis"
                        (%task-retry-economics-analysis host payload))))))

(in-package #:llm-log-expert)

(defparameter *base-usage-projection*
  (symbol-function '%usage-projection))

(defparameter *base-query-task-accounting*
  (symbol-function 'query-task-accounting))

(defun %optional-json-string (object field)
  (let ((value (jsown:val-safe object field)))
    (when value
      (unless (%non-empty-string-p value)
        (error "~A must be a non-empty string when present" field)))
    value))

(defun %optional-positive-integer (object field)
  (let ((value (jsown:val-safe object field)))
    (when value
      (unless (and (integerp value) (plusp value))
        (error "~A must be a positive integer when present" field)))
    value))

(defun %usage-projection (usage)
  "Extend the immutable usage projection with explicit retry/attempt evidence."
  (let* ((projection (funcall *base-usage-projection* usage))
         (request-id (getf projection :request-id))
         (attempt-id (%optional-json-string usage "attempt_id"))
         (attempt-ordinal (%optional-positive-integer usage "attempt_ordinal"))
         (retry-of-request-id (%optional-json-string usage "retry_of_request_id"))
         (client (%optional-json-string usage "client"))
         (transport (%optional-json-string usage "transport")))
    (when (and retry-of-request-id
               (equal retry-of-request-id request-id))
      (error "retry_of_request_id must not equal request_id"))
    (append projection
            (list :attempt-id attempt-id
                  :attempt-ordinal attempt-ordinal
                  :retry-of-request-id retry-of-request-id
                  :client client
                  :transport transport))))

(defun %retry-accounting-fields (host payload)
  "Derive bounded retry metadata from the same finite task/usage materialization."
  (let* ((task-id (%required-json-string payload "task_id"))
         (max-depth (%required-rollup-integer payload "max_depth"
                                              0 +max-task-rollup-depth+))
         (max-nodes (%required-rollup-integer payload "max_nodes"
                                              1 +max-task-rollup-nodes+))
         (include-children (not (null (jsown:val-safe payload "include_children"))))
         (database (expert-host-database host))
         (seen-usages (make-hash-table :test #'equal))
         (attempts (make-hash-table :test #'equal))
         (retry-requests (make-hash-table :test #'equal))
         (metadata-complete t))
    (multiple-value-bind (task-ids graph-truncated)
        (%bounded-task-rollup-ids host task-id max-depth max-nodes include-children)
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
            (setf usages (subseq usages 0 +max-task-rollup-usage-candidates+)))
          (dolist (usage usages)
            (let ((usage-id (getf usage :usage-id)))
              (unless (gethash usage-id seen-usages)
                (setf (gethash usage-id seen-usages) t)
                (let ((attempt-id (getf usage :attempt-id))
                      (attempt-ordinal (getf usage :attempt-ordinal))
                      (retry-of-request-id (getf usage :retry-of-request-id))
                      (request-id (getf usage :request-id)))
                  (if (and (%non-empty-string-p attempt-id)
                           (integerp attempt-ordinal)
                           (plusp attempt-ordinal))
                      (setf (gethash attempt-id attempts) t)
                      (setf metadata-complete nil))
                  (when (%non-empty-string-p retry-of-request-id)
                    (if (%non-empty-string-p request-id)
                        (setf (gethash request-id retry-requests) t)
                        (setf metadata-complete nil))))))))))
    (values
     (sort (loop for key being the hash-keys of attempts collect key) #'string<)
     (sort (loop for key being the hash-keys of retry-requests collect key) #'string<)
     metadata-complete)))

(defun query-task-accounting (host payload)
  "Add restart-durable retry/attempt evidence to the existing bounded task rollup."
  (let ((result (funcall *base-query-task-accounting* host payload)))
    (multiple-value-bind (attempt-ids retry-request-ids metadata-complete)
        (%retry-accounting-fields host payload)
      (append result
              (list (cons "attempt_count" (length attempt-ids))
                    (cons "retry_request_count" (length retry-request-ids))
                    (cons "attempt_ids" attempt-ids)
                    (cons "retry_request_ids" retry-request-ids)
                    (cons "attempt_metadata_complete" metadata-complete))))))

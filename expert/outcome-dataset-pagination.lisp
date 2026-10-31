(in-package #:llm-log-expert)

(defparameter +outcome-dataset-page-index-migration-key+
  "meta:index-migration:outcome-assertion-outcome-id:v1")

(defparameter *page-base-register-outcome-indexes*
  (symbol-function '%register-outcome-indexes))

(defparameter *page-base-ensure-outcome-dataset-index-v1*
  (symbol-function '%ensure-outcome-dataset-index-v1))

(defun %outcome-dataset-page-key (outcome assertion-id)
  (format nil "~A:~A" outcome assertion-id))

(defun %register-outcome-indexes (database)
  "Add a deterministic outcome/assertion index for bounded corpus pagination."
  (funcall *page-base-register-outcome-indexes* database)
  (register-index
   database "outcome-assertion-outcome-id"
   (lambda (document)
     (let ((assertion-id (%plist-index-value document :assertion-id))
           (scope (%plist-index-value document :scope))
           (scope-id (%plist-index-value document :scope-id))
           (outcome (%plist-index-value document :outcome)))
       (and assertion-id scope scope-id outcome
            (%outcome-dataset-page-key outcome assertion-id)))))
  database)

(defun %ensure-outcome-dataset-index-v1 (database)
  "Run the original migration and one-time backfill the paginated index."
  (funcall *page-base-ensure-outcome-dataset-index-v1* database)
  (unless (fetch* database +outcome-dataset-page-index-migration-key+)
    (tek9:rebuild-index database "outcome-assertion-outcome-id")
    (with-write-transaction (database)
      (unless (fetch* database +outcome-dataset-page-index-migration-key+)
        (put* database
              (list :schema-version 1
                    :index-name "outcome-assertion-outcome-id")
              :id +outcome-dataset-page-index-migration-key+))))
  database)

(defun %outcome-dataset-cursor (payload)
  (%outcome-dataset-optional-string payload "cursor"))

(defun %outcome-dataset-page-candidates (database outcome cursor)
  "Read at most one bounded candidate window after CURSOR."
  (let* ((start (if cursor
                    (%outcome-dataset-page-key outcome cursor)
                    (format nil "~A:" outcome)))
         ;; ';' sorts immediately after ':' at the prefix boundary, so every
         ;; '<outcome>:<id>' key is inside this range without inventing a max ID.
         (end (format nil "~A;" outcome))
         (raw
           (select-index-range
            database "outcome-assertion-outcome-id" start
            :end end :limit (+ +max-outcome-dataset-candidates+ 2)))
         (after-cursor
           (if cursor
               (remove cursor raw
                       :key (lambda (projection)
                              (getf projection :assertion-id))
                       :test #'equal)
               raw))
         (more-p (> (length after-cursor) +max-outcome-dataset-candidates+))
         (bounded
           (subseq after-cursor
                   0 (min +max-outcome-dataset-candidates+
                          (length after-cursor)))))
    (values bounded more-p)))

(defun query-outcome-dataset (host payload)
  "Return one cursor-paginated, provenance-complete outcome dataset page."
  (multiple-value-bind
        (outcome limit scope rule-version include-superseded provider model
         classification-dimension classification-value classification-state
         task-cost-state task-cost-currency task-cost-min-amount
         task-cost-max-amount)
      (%validate-outcome-dataset-payload payload)
    (let* ((cursor (%outcome-dataset-cursor payload))
           (database (expert-host-database host))
           (metadata-filter-p (or provider model))
           (classification-filter-p
             (or classification-dimension
                 classification-value
                 classification-state))
           (task-cost-filter-p
             (or task-cost-state task-cost-currency
                 task-cost-min-amount task-cost-max-amount))
           (task-accounting-cache (make-hash-table :test #'equal)))
      (multiple-value-bind (candidates candidate-more-p)
          (%outcome-dataset-page-candidates database outcome cursor)
        (let ((joined '()))
          (dolist (projection candidates)
            (when (%outcome-dataset-candidate-p
                   database projection scope rule-version include-superseded)
              (let* ((request-usage
                       (%outcome-dataset-request-usage
                        database projection provider model))
                     (request-classifications
                       (%outcome-dataset-request-classifications host projection))
                     (classification-match-p
                       (and (equal "request" (getf projection :scope))
                            (some
                             (lambda (assertion)
                               (%outcome-dataset-classification-matches-p
                                assertion
                                classification-dimension
                                classification-value
                                classification-state))
                             request-classifications)))
                     (task-accounting
                       (and task-cost-filter-p
                            (%outcome-dataset-task-accounting
                             host request-usage task-accounting-cache)))
                     (task-cost-match-p
                       (and task-cost-filter-p
                            (%outcome-dataset-task-accounting-matches-p
                             task-accounting task-cost-state task-cost-currency
                             task-cost-min-amount task-cost-max-amount))))
                (when (and (or (not metadata-filter-p) request-usage)
                           (or (not classification-filter-p)
                               classification-match-p)
                           (or (not task-cost-filter-p) task-cost-match-p))
                  (when (or (and (not metadata-filter-p)
                                 (not classification-filter-p)
                                 (not task-cost-filter-p))
                            (equal "request" (getf projection :scope)))
                    (push (list projection request-usage request-classifications
                                task-accounting)
                          joined))))))
          (let* ((ordered (nreverse joined))
                 (matching-more-p (> (length ordered) limit))
                 (bounded (subseq ordered 0 (min limit (length ordered))))
                 (truncated (or matching-more-p candidate-more-p))
                 (next-cursor
                   (cond
                     (matching-more-p
                      (getf (first (first (last bounded))) :assertion-id))
                     (candidate-more-p
                      (getf (first (last candidates)) :assertion-id))
                     (t nil))))
            (%json-object
             (cons "outcome" outcome)
             (cons "examples"
                   (mapcar
                    (lambda (entry)
                      (%outcome-dataset-example-json
                       database (first entry) (second entry) (third entry)
                       (fourth entry)))
                    bounded))
             (cons "truncated" truncated)
             (cons "next_cursor" next-cursor))))))))

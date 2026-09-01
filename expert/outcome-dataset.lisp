(in-package #:llm-log-expert)

(defparameter +max-outcome-dataset-limit+ 64)
(defparameter +max-outcome-dataset-candidates+ 256)

(defun %json-field-present-p (object field)
  (and (consp object)
       (eq (first object) :obj)
       (assoc field (rest object) :test #'string=)))

(defun %outcome-dataset-optional-string (payload field)
  (let ((value (jsown:val-safe payload field)))
    (when value
      (unless (%non-empty-string-p value)
        (error "~A must be absent or a non-empty string" field)))
    value))

(defun %validate-outcome-dataset-payload (payload)
  "Validate the finite version-1 dataset query surface before Tek9 access."
  (unless (and (consp payload) (eq (first payload) :obj))
    (error "payload must be a JSON object"))
  (let ((outcome (%outcome-required-string payload "outcome"))
        (limit (jsown:val-safe payload "limit"))
        (scope (jsown:val-safe payload "scope"))
        (rule-version (jsown:val-safe payload "rule_version"))
        (provider (%outcome-dataset-optional-string payload "provider"))
        (model (%outcome-dataset-optional-string payload "model"))
        (classification-dimension
          (%outcome-dataset-optional-string payload "classification_dimension"))
        (classification-value
          (%outcome-dataset-optional-string payload "classification_value"))
        (classification-state
          (%outcome-dataset-optional-string payload "classification_state"))
        (include-superseded (jsown:val-safe payload "include_superseded")))
    (unless (member outcome +outcome-values+ :test #'equal)
      (error "unsupported outcome filter: ~A" outcome))
    (unless (and (integerp limit) (plusp limit)
                 (<= limit +max-outcome-dataset-limit+))
      (error "limit must be an integer from 1 through ~D"
             +max-outcome-dataset-limit+))
    (when (and scope (not (member scope '("request" "task") :test #'equal)))
      (error "scope must be request or task"))
    (when (and rule-version (not (%non-empty-string-p rule-version)))
      (error "rule_version must be absent or a non-empty string"))
    (when (%json-field-present-p payload "include_superseded")
      (unless (or (eq include-superseded t) (null include-superseded))
        (error "include_superseded must be boolean")))
    (values outcome limit scope rule-version (eq include-superseded t)
            provider model
            classification-dimension classification-value classification-state)))

(defun %outcome-dataset-evidence-json (database evidence-id)
  "Point-fetch one immutable evidence projection; missing provenance is fatal."
  (let ((projection (fetch* database (%outcome-evidence-key evidence-id))))
    (unless projection
      (error "missing_outcome_evidence: ~A" evidence-id))
    (%json-object
     (cons "evidence_id" (getf projection :evidence-id))
     (cons "source_id" (getf projection :source-id))
     (cons "observed_at" (getf projection :observed-at))
     (cons "evidence_type" (getf projection :evidence-type))
     (cons "authority" (getf projection :authority))
     (cons "observed_value" (getf projection :observed-value)))))

(defun %outcome-dataset-request-usage (database projection provider model)
  "Resolve at most one durable usage observation for one request assertion.

Task assertions intentionally do not acquire synthetic provider/model metadata.
When a metadata filter is active, missing request usage is simply a non-match;
multiple usage projections are an integrity error rather than a guess."
  (when (equal "request" (getf projection :scope))
    (let* ((request-id (getf projection :scope-id))
           (candidates
             (select-index-range database "usage-request-id" request-id
                                 :end request-id :limit 2))
           (usages
             (remove-if-not
              (lambda (usage)
                (and (listp usage)
                     (%non-empty-string-p (getf usage :usage-id))
                     (equal request-id (getf usage :request-id))))
              candidates)))
      (when (> (length usages) 1)
        (error "outcome_dataset_request_usage_integrity_error: ~A" request-id))
      (let ((usage (first usages)))
        (and usage
             (or (null provider) (equal provider (getf usage :provider)))
             (or (null model) (equal model (getf usage :model)))
             usage)))))

(defun %outcome-dataset-request-metadata-json (usage)
  (and usage
       (%json-object
        (cons "usage_id" (getf usage :usage-id))
        (cons "provider" (getf usage :provider))
        (cons "model" (getf usage :model))
        (cons "client" (getf usage :client))
        (cons "transport" (getf usage :transport)))))

(defun %outcome-dataset-classification-json (assertion)
  (let ((value (getf assertion :value)))
    (%json-object
     (cons "assertion_id" (getf assertion :assertion-id))
     (cons "dimension" (getf value :dimension))
     (cons "value" (getf value :value))
     (cons "state" (getf value :state))
     (cons "rule_id" (getf value :rule-id))
     (cons "rule_version" (getf assertion :rule-version))
     (cons "evidence_ids" (copy-list (getf value :evidence-ids))))))

(defun %outcome-dataset-request-classifications (host projection)
  "Materialize a bounded stored-classification slice for one request outcome."
  (when (equal "request" (getf projection :scope))
    (let ((assertions '()))
      (dolist (source
               (%classification-source-records-for-request
                host (getf projection :scope-id)))
        (dolist (assertion (%classification-assertions-for-source host source))
          (push assertion assertions)))
      (stable-sort
       (remove-duplicates assertions
                          :key (lambda (assertion)
                                 (getf assertion :assertion-id))
                          :test #'equal)
       #'string<
       :key (lambda (assertion)
              (getf assertion :assertion-id))))))

(defun %outcome-dataset-classification-matches-p
    (assertion dimension filter-value state)
  (let ((value (getf assertion :value)))
    (and (or (null dimension)
             (equal dimension (getf value :dimension)))
         (or (null filter-value)
             (equal filter-value (getf value :value)))
         (or (null state)
             (equal state (getf value :state))))))

(defun %outcome-dataset-example-json
    (database projection request-usage request-classifications)
  "Project one stored assertion without re-running expert inference."
  (let ((evidence-ids (copy-list (getf projection :evidence-ids))))
    (%json-object
     (cons "assertion_id" (getf projection :assertion-id))
     (cons "scope" (getf projection :scope))
     (cons "scope_id" (getf projection :scope-id))
     (cons "outcome" (getf projection :outcome))
     (cons "rule_id" (getf projection :rule-id))
     (cons "rule_version" (getf projection :rule-version))
     (cons "expert_version" (getf projection :expert-version))
     (cons "evidence_ids" evidence-ids)
     (cons "evidence"
           (mapcar (lambda (evidence-id)
                     (%outcome-dataset-evidence-json database evidence-id))
                   evidence-ids))
     (cons "supersedes_assertion_id" (getf projection :supersedes))
     (cons "replaced_by_assertion_id"
           (%outcome-replaced-by database (getf projection :assertion-id)))
     (cons "request_metadata"
           (%outcome-dataset-request-metadata-json request-usage))
     (cons "request_classifications"
           (mapcar #'%outcome-dataset-classification-json
                   request-classifications)))))

(defun %outcome-dataset-candidate-p
    (database projection scope rule-version include-superseded)
  (and (or (null scope) (equal scope (getf projection :scope)))
       (or (null rule-version)
           (equal rule-version (getf projection :rule-version)))
       (or include-superseded
           (null (%outcome-replaced-by
                  database (getf projection :assertion-id))))))

(defun query-outcome-dataset (host payload)
  "Return one bounded, provenance-complete view over stored outcome assertions.

This operation deliberately does not invoke SWI-Prolog. Historical labels are
immutable products of the rule/expert versions recorded on each assertion.
Provider/model and stored classifications are metadata selectors over bounded
request-local projections, never outcome authority."
  (multiple-value-bind
        (outcome limit scope rule-version include-superseded provider model
         classification-dimension classification-value classification-state)
      (%validate-outcome-dataset-payload payload)
    (let* ((database (expert-host-database host))
           (metadata-filter-p (or provider model))
           (classification-filter-p
             (or classification-dimension
                 classification-value
                 classification-state))
           (candidates
             (select-index-range
              database "outcome-assertion-outcome" outcome
              :end outcome :limit (1+ +max-outcome-dataset-candidates+))))
      ;; Never silently turn a candidate ceiling into an incomplete dataset.
      (when (> (length candidates) +max-outcome-dataset-candidates+)
        (error "outcome_dataset_candidate_limit_exceeded: ~A" outcome))
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
                           request-classifications))))
              (when (and (or (not metadata-filter-p) request-usage)
                         (or (not classification-filter-p)
                             classification-match-p))
                ;; Active request-local filters never admit task assertions.
                (when (or (and (not metadata-filter-p)
                               (not classification-filter-p))
                          (equal "request" (getf projection :scope)))
                  (push (list projection request-usage request-classifications)
                        joined))))))
        (let* ((ordered
                 (stable-sort
                  (nreverse joined) #'string<
                  :key (lambda (entry)
                         (getf (first entry) :assertion-id))))
               (truncated (> (length ordered) limit))
               (bounded (subseq ordered 0 (min limit (length ordered)))))
          (%json-object
           (cons "outcome" outcome)
           (cons "examples"
                 (mapcar
                  (lambda (entry)
                    (%outcome-dataset-example-json
                     database (first entry) (second entry) (third entry)))
                  bounded))
           (cons "truncated" truncated)))))))
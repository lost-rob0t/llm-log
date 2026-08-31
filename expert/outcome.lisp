(in-package #:llm-log-expert)

(defparameter +outcome-evidence-schema-version+ 1)
(defparameter +outcome-assertion-schema-version+ 1)
(defparameter +outcome-expert-version+ "outcome-expert/1")
(defparameter +max-outcome-evidence+ 64)
(defparameter +max-outcome-history-limit+ 64)
(defparameter +outcome-evidence-types+
  '("provider_transport" "tool_result" "test_result" "user_feedback"
    "task_state" "manual_label"))
(defparameter +outcome-authorities+ '("weak" "normal" "authoritative"))
(defparameter +outcome-values+
  '("success" "failure" "partial" "cancelled" "rejected" "timeout" "unknown"))

(defun %outcome-evidence-key (evidence-id)
  (format nil "outcome-evidence:~A" evidence-id))

(defun %outcome-assertion-key (assertion-id)
  (format nil "outcome-assertion:~A" assertion-id))

(defun %outcome-scope-key (scope scope-id)
  (format nil "~A:~A" scope scope-id))

(defun %outcome-required-string (object field)
  (let ((value (jsown:val-safe object field)))
    (unless (%non-empty-string-p value)
      (error "~A must be a non-empty string" field))
    value))

(defun %validate-outcome-evidence-item (item)
  (unless (and (consp item) (eq (first item) :obj))
    (error "outcome evidence must be a JSON object"))
  (let ((evidence-id (%outcome-required-string item "evidence_id"))
        (observed-at (%outcome-required-string item "observed_at"))
        (evidence-type (%outcome-required-string item "evidence_type"))
        (authority (%outcome-required-string item "authority"))
        (observed-value (jsown:val-safe item "observed_value"))
        (source-id (jsown:val-safe item "source_id")))
    (unless (member evidence-type +outcome-evidence-types+ :test #'equal)
      (error "unsupported outcome evidence_type: ~A" evidence-type))
    (unless (member authority +outcome-authorities+ :test #'equal)
      (error "unsupported outcome authority: ~A" authority))
    (unless (or (stringp observed-value) (numberp observed-value)
                (eq observed-value t) (null observed-value))
      (error "observed_value must be a scalar JSON value"))
    (when (and source-id (not (%non-empty-string-p source-id)))
      (error "source_id must be absent or a non-empty string"))
    (list :schema-version +outcome-evidence-schema-version+
          :evidence-id evidence-id
          :observed-at observed-at
          :evidence-type evidence-type
          :authority authority
          :observed-value observed-value
          :source-id source-id)))

(defun %validate-outcome-payload (payload)
  (let ((scope (%outcome-required-string payload "scope"))
        (scope-id (%outcome-required-string payload "scope_id"))
        (evidence (jsown:val-safe payload "evidence"))
        (supersedes (jsown:val-safe payload "supersedes_assertion_id")))
    (unless (member scope '("request" "task") :test #'equal)
      (error "scope must be request or task"))
    (unless (and (listp evidence) evidence
                 (<= (length evidence) +max-outcome-evidence+))
      (error "evidence must be a non-empty list of at most ~D items"
             +max-outcome-evidence+))
    (when (and supersedes (not (%non-empty-string-p supersedes)))
      (error "supersedes_assertion_id must be absent or non-empty"))
    (let ((projections (mapcar #'%validate-outcome-evidence-item evidence)))
      (unless (= (length projections)
                 (length (remove-duplicates
                          (mapcar (lambda (projection)
                                    (getf projection :evidence-id))
                                  projections)
                          :test #'equal)))
        (error "evidence_id values must be unique within one request"))
      (values scope scope-id evidence projections supersedes))))

(defun %outcome-reasoner-data (scope scope-id evidence)
  (%json-object (cons "scope" scope)
                (cons "scope_id" scope-id)
                (cons "evidence" evidence)))

(defun %validate-outcome-reasoner-result (host reply expected-evidence-ids)
  (unless (equal (jsown:val-safe reply "status") "ok")
    (error "reasoner_error: ~A" (jsown:to-json reply)))
  (let* ((result (jsown:val-safe reply "result"))
         (outcome (and result (jsown:val-safe result "outcome")))
         (rule-id (and result (jsown:val-safe result "rule_id")))
         (rule-version (jsown:val-safe reply "rule_version"))
         (expert-version (and result (jsown:val-safe result "expert_version")))
         (evidence-ids (and result (jsown:val-safe result "evidence_ids"))))
    (unless (and (consp result) (eq (first result) :obj))
      (%reject-invalid-reasoner-result host "outcome_decision result must be an object"))
    (unless (member outcome +outcome-values+ :test #'equal)
      (%reject-invalid-reasoner-result host "outcome_decision returned invalid outcome"))
    (dolist (value (list rule-id rule-version expert-version))
      (unless (%non-empty-string-p value)
        (%reject-invalid-reasoner-result host "outcome provenance must be non-empty")))
    (unless (equal expert-version +outcome-expert-version+)
      (%reject-invalid-reasoner-result host "outcome expert version mismatch"))
    (unless (and (listp evidence-ids)
                 (equal (sort (copy-list evidence-ids) #'string<)
                        (sort (copy-list expected-evidence-ids) #'string<)))
      (%reject-invalid-reasoner-result host "outcome evidence provenance mismatch"))
    (values outcome rule-id rule-version expert-version evidence-ids)))

(defun %outcome-assertion-id (scope scope-id event-id)
  (format nil "~A:~A:~A" scope scope-id event-id))

(defun %outcome-assertion-projection
    (assertion-id scope scope-id outcome evidence-ids rule-id rule-version
     expert-version supersedes)
  (list :schema-version +outcome-assertion-schema-version+
        :assertion-id assertion-id
        :scope scope
        :scope-id scope-id
        :outcome outcome
        :evidence-ids (copy-list evidence-ids)
        :rule-id rule-id
        :rule-version rule-version
        :expert-version expert-version
        :supersedes supersedes))

(defun %outcome-successors (database assertion-id &optional (limit 2))
  (select-index-range database "outcome-assertion-supersedes"
                      assertion-id :end assertion-id :limit limit))

(defun %validate-outcome-supersession
    (database assertion-id scope scope-id supersedes)
  (when supersedes
    (when (equal supersedes assertion-id)
      (error "outcome_self_supersession: ~A" assertion-id))
    (let ((previous (fetch* database (%outcome-assertion-key supersedes))))
      (unless previous
        (error "unknown_superseded_outcome_assertion: ~A" supersedes))
      (unless (and (equal scope (getf previous :scope))
                   (equal scope-id (getf previous :scope-id)))
        (error "outcome_cross_scope_supersession: ~A" supersedes)))
    (let ((other-successors
            (remove assertion-id
                    (mapcar (lambda (projection)
                              (getf projection :assertion-id))
                            (%outcome-successors database supersedes 2))
                    :test #'equal)))
      (when other-successors
        (error "outcome_supersession_conflict: ~A" supersedes)))))

(defun %persist-outcome-decision
    (host evidence-projections assertion-id assertion-projection supersedes)
  (let ((database (expert-host-database host)))
    (with-write-transaction (database)
      (%validate-outcome-supersession
       database assertion-id
       (getf assertion-projection :scope)
       (getf assertion-projection :scope-id)
       supersedes)
      (dolist (projection evidence-projections)
        (let* ((id (getf projection :evidence-id))
               (key (%outcome-evidence-key id))
               (existing (fetch* database key)))
          (cond
            ((null existing) (put* database projection :id key))
            ((equal existing projection) nil)
            (t (error "outcome_evidence_conflict: ~A" id)))))
      (let* ((key (%outcome-assertion-key assertion-id))
             (existing (fetch* database key)))
        (cond
          ((null existing) (put* database assertion-projection :id key))
          ((equal existing assertion-projection) nil)
          (t (error "outcome_assertion_conflict: ~A" assertion-id))))
      (let* ((revision (or (fetch* database +kb-revision-key+) 1))
             (next-revision (1+ revision)))
        (put* database next-revision :id +kb-revision-key+)
        next-revision))))

(defun record-outcome-evidence (host event-id payload)
  "Validate bounded evidence, derive one Prolog-owned outcome, and persist provenance."
  (unless (%non-empty-string-p event-id)
    (error "event_id is required"))
  (multiple-value-bind (scope scope-id raw-evidence projections supersedes)
      (%validate-outcome-payload payload)
    (let* ((evidence-ids (mapcar (lambda (projection)
                                   (getf projection :evidence-id))
                                 projections))
           (reply (prolog-worker-request
                   host "outcome_decision"
                   (%outcome-reasoner-data scope scope-id raw-evidence))))
      (multiple-value-bind (outcome rule-id rule-version expert-version grounded-ids)
          (%validate-outcome-reasoner-result host reply evidence-ids)
        (let* ((assertion-id (%outcome-assertion-id scope scope-id event-id))
               (assertion (%outcome-assertion-projection
                           assertion-id scope scope-id outcome grounded-ids rule-id
                           rule-version expert-version supersedes))
               (revision (%persist-outcome-decision
                          host projections assertion-id assertion supersedes)))
          (%json-object
           (cons "outcome_assertion_id" assertion-id)
           (cons "outcome" outcome)
           (cons "evidence_state" (if (equal outcome "unknown") "insufficient" "grounded"))
           (cons "rule_id" rule-id)
           (cons "rule_version" rule-version)
           (cons "expert_version" expert-version)
           (cons "evidence_ids" grounded-ids)
           (cons "supersedes_assertion_id" supersedes)
           (cons "kb_revision" revision)))))))

(defun %validate-outcome-history-payload (payload)
  (unless (and (consp payload) (eq (first payload) :obj))
    (error "payload must be a JSON object"))
  (let ((scope (%outcome-required-string payload "scope"))
        (scope-id (%outcome-required-string payload "scope_id"))
        (limit (jsown:val-safe payload "limit"))
        (outcome (jsown:val-safe payload "outcome")))
    (unless (member scope '("request" "task") :test #'equal)
      (error "scope must be request or task"))
    (unless (and (integerp limit) (plusp limit)
                 (<= limit +max-outcome-history-limit+))
      (error "limit must be an integer from 1 through ~D"
             +max-outcome-history-limit+))
    (when (and outcome (not (member outcome +outcome-values+ :test #'equal)))
      (error "unsupported outcome filter: ~A" outcome))
    (values scope scope-id limit outcome)))

(defun %outcome-history-depth (projection projections &optional seen)
  (let ((supersedes (getf projection :supersedes))
        (assertion-id (getf projection :assertion-id)))
    (if (or (null supersedes) (member assertion-id seen :test #'equal))
        0
        (let ((previous
                (find supersedes projections
                      :key (lambda (item) (getf item :assertion-id))
                      :test #'equal)))
          (if previous
              (1+ (%outcome-history-depth previous projections
                                          (cons assertion-id seen)))
              1)))))

(defun %order-outcome-history (projections)
  (stable-sort (copy-list projections) #'>
               :key (lambda (projection)
                      (%outcome-history-depth projection projections))))

(defun %outcome-replaced-by (database assertion-id)
  (let ((successors (%outcome-successors database assertion-id 2)))
    (when (> (length successors) 1)
      (error "outcome_supersession_conflict: ~A" assertion-id))
    (and successors (getf (first successors) :assertion-id))))

(defun %outcome-history-json (database projection)
  (%json-object
   (cons "assertion_id" (getf projection :assertion-id))
   (cons "scope" (getf projection :scope))
   (cons "scope_id" (getf projection :scope-id))
   (cons "outcome" (getf projection :outcome))
   (cons "rule_id" (getf projection :rule-id))
   (cons "rule_version" (getf projection :rule-version))
   (cons "expert_version" (getf projection :expert-version))
   (cons "evidence_ids" (copy-list (getf projection :evidence-ids)))
   (cons "supersedes_assertion_id" (getf projection :supersedes))
   (cons "replaced_by_assertion_id"
         (%outcome-replaced-by database (getf projection :assertion-id)))))

(defun query-outcome-history (host payload)
  "Return one exact-scope bounded immutable outcome assertion history."
  (multiple-value-bind (scope scope-id limit outcome-filter)
      (%validate-outcome-history-payload payload)
    (let* ((database (expert-host-database host))
           (scope-key (%outcome-scope-key scope scope-id))
           (candidates
             (select-index-range database "outcome-assertion-scope-key"
                                 scope-key :end scope-key
                                 :limit +max-outcome-history-limit+))
           (filtered
             (if outcome-filter
                 (remove-if-not
                  (lambda (projection)
                    (equal outcome-filter (getf projection :outcome)))
                  candidates)
                 candidates))
           (ordered (%order-outcome-history filtered))
           (bounded (subseq ordered 0 (min limit (length ordered)))))
      (%json-object
       (cons "scope" scope)
       (cons "scope_id" scope-id)
       (cons "assertions"
             (mapcar (lambda (projection)
                       (%outcome-history-json database projection))
                     bounded))))))

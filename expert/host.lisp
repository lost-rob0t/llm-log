(in-package #:llm-log-expert)

(defstruct (expert-host
            (:constructor %make-expert-host))
  (data-directory nil :type (or null pathname))
  database
  prolog-process
  prolog-session-id
  (prolog-request-sequence 0 :type integer))

(defun %expert-database-path (data-directory)
  (merge-pathnames #P"tek9/"
                   (uiop:ensure-directory-pathname data-directory)))

(defun %plist-index-value (document field)
  "Extract FIELD only from plist-backed expert documents.

The expert Tek9 main database is heterogeneous: metadata such as the KB
revision shares the same document space as projections. Index extractors must
therefore ignore non-plist values instead of assuming every document has expert
projection fields."
  (let ((value (doc-value document)))
    (and (listp value)
         (getf value field))))

(defun %register-classification-indexes (database)
  "Register process-local handles for durable bounded classifier indexes."
  (flet ((value-field (field)
           (lambda (document)
             (%plist-index-value document field))))
    (register-index database "classification-source-request-id"
                    (value-field :request-id))
    (register-index database "classification-source-user-message-id"
                    (value-field :user-message-id))
    (register-index database "classification-source-task-id"
                    (value-field :task-id))
    (register-index database "classification-source-provider"
                    (value-field :provider))
    (register-index database "classification-source-model"
                    (value-field :model))
    (register-index database "classification-source-started-at"
                    (value-field :started-at))
    (register-index
     database "classification-assertion-source-event"
     (lambda (document)
       (let ((source-ids (%plist-index-value document :source-ids)))
         (and (listp source-ids)
              (first source-ids))))))
  database)

(defun %register-task-accounting-indexes (database)
  "Register bounded task graph and usage lookup indexes.

These are process-local index handles over Tek9's durable documents. Query
execution must use them rather than enumerating the expert corpus."
  (register-index
   database "task-parent-task-id"
   (lambda (document)
     (let ((task-id (%plist-index-value document :task-id))
           (rule-id (%plist-index-value document :rule-id))
           (parent-id (%plist-index-value document :parent-task-id)))
       (and task-id rule-id parent-id))))
  (register-index
   database "usage-task-id"
   (lambda (document)
     (let ((usage-id (%plist-index-value document :usage-id))
           (task-id (%plist-index-value document :task-id)))
       (and usage-id task-id))))
  database)

(defun %register-outcome-indexes (database)
  "Register exact-scope and supersession indexes for bounded outcome history."
  (register-index
   database "outcome-assertion-scope-key"
   (lambda (document)
     (let ((assertion-id (%plist-index-value document :assertion-id))
           (scope-key (%plist-index-value document :scope-key)))
       (and assertion-id scope-key))))
  (register-index
   database "outcome-assertion-supersedes"
   (lambda (document)
     (let ((assertion-id (%plist-index-value document :assertion-id))
           (supersedes (%plist-index-value document :supersedes)))
       (and assertion-id supersedes))))
  database)

(defun start-expert-host (data-directory)
  "Open Tek9 and one persistent supervised SWI-Prolog worker for HOST."
  (let* ((root (uiop:ensure-directory-pathname data-directory))
         (database-path (%expert-database-path root))
         (database (open-database
                    (new-database "llm-log-expert"
                                  :path database-path
                                  :durability :full
                                  :index-definitions nil)))
         (host (%make-expert-host
                :data-directory root
                :database database)))
    (handler-case
        (progn
          (%register-classification-indexes database)
          (%register-task-accounting-indexes database)
          (%register-outcome-indexes database)
          (%ensure-kb-revision host)
          (start-prolog-worker host)
          host)
      (error (condition)
        (when (db-is-open-p database)
          (close-database database))
        (error condition)))))

(defun stop-expert-host (host)
  "Close exactly the resources owned by HOST."
  (stop-prolog-worker host)
  (let ((database (expert-host-database host)))
    (when (and database (db-is-open-p database))
      (close-database database)))
  (setf (expert-host-database host) nil)
  host)

(defun expert-host-open-p (host)
  (let ((database (expert-host-database host)))
    (and database
         (db-is-open-p database)
         (%prolog-process-alive-p host))))

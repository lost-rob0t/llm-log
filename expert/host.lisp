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

(defun %register-classification-indexes (database)
  "Register process-local handles for durable bounded classifier indexes."
  (flet ((value-field (field)
           (lambda (document)
             (getf (doc-value document) field))))
    (register-index database "classification-source-request-id"
                    (value-field :request-id))
    (register-index database "classification-source-user-message-id"
                    (value-field :user-message-id))
    (register-index database "classification-source-provider"
                    (value-field :provider))
    (register-index database "classification-source-model"
                    (value-field :model))
    (register-index database "classification-source-started-at"
                    (value-field :started-at))
    (register-index
     database "classification-assertion-source-event"
     (lambda (document)
       (car (getf (doc-value document) :source-ids)))))
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

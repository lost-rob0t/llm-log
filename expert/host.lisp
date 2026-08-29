(in-package #:llm-log-expert)

(defstruct (expert-host
            (:constructor %make-expert-host))
  (data-directory nil :type (or null pathname))
  database
  prolog-process
  prolog-session-id)

(defun %expert-database-path (data-directory)
  (merge-pathnames #P"tek9/"
                   (uiop:ensure-directory-pathname data-directory)))

(defun start-expert-host (data-directory)
  "Open the durable Tek9 side of the expert host.

The persistent SWI-Prolog supervisor is deliberately a separate lifecycle seam;
this function establishes only the reusable Common Lisp/Tek9 host boundary for
#10. It never spawns a per-query Prolog process."
  (let* ((root (uiop:ensure-directory-pathname data-directory))
         (database-path (%expert-database-path root))
         (database (open-database
                    (new-database "llm-log-expert"
                                  :path database-path
                                  :durability :full
                                  :index-definitions nil))))
    (%make-expert-host
     :data-directory root
     :database database)))

(defun stop-expert-host (host)
  "Close resources owned by HOST.

Prolog shutdown will be added at the same boundary once the supervised persistent
session exists; Tek9 is closed exactly once here."
  (let ((database (expert-host-database host)))
    (when (and database (db-is-open-p database))
      (close-database database)))
  (setf (expert-host-database host) nil
        (expert-host-prolog-process host) nil
        (expert-host-prolog-session-id host) nil)
  host)

(defun expert-host-open-p (host)
  (let ((database (expert-host-database host)))
    (and database (db-is-open-p database))))

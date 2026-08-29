(in-package #:llm-log-expert)

;; RED contract for the zero-Python rewrite.  Production implementation follows
;; only after this surface is fixed by the Common Lisp tests.

(defstruct runtime-config
  data-directory
  listen-address
  port)

(defun default-data-directory (&optional (home (user-homedir-pathname)))
  (merge-pathnames #P".llm-proxy/" (uiop:ensure-directory-pathname home)))

(defun make-default-runtime-config ()
  (make-runtime-config
   :data-directory (default-data-directory)
   :listen-address "127.0.0.1"
   :port 8787))

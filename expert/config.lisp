(in-package #:llm-log-expert)

(defstruct runtime-config
  data-directory
  listen-address
  port
  (upstreams '()))

(defun default-data-directory (&optional (home (user-homedir-pathname)))
  (merge-pathnames #P".llm-proxy/" (uiop:ensure-directory-pathname home)))

(defun make-default-runtime-config ()
  (make-runtime-config
   :data-directory (default-data-directory)
   :listen-address "127.0.0.1"
   :port 8787
   :upstreams '()))

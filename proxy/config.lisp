(in-package #:llm-log)

(define-condition invalid-configuration (error)
  ((detail :initarg :detail :initform nil :reader invalid-configuration-detail))
  (:report (lambda (condition stream)
             (format stream "invalid llm-log configuration~@[: ~A~]"
                     (invalid-configuration-detail condition)))))

(defun %invalid (control &rest arguments)
  (error 'invalid-configuration :detail (apply #'format nil control arguments)))

(defstruct runtime-config
  (data-directory nil)
  (expert-data-directory nil)
  (listen-address nil)
  (port nil)
  (upstreams nil))

(defparameter +default-listen-address+ "127.0.0.1")
(defparameter +default-port+ 8787)
(defparameter +default-upstreams+
  '(("openai" . "https://api.openai.com")
    ("openrouter" . "https://openrouter.ai")
    ("anthropic" . "https://api.anthropic.com")
    ("chatgpt" . "https://chatgpt.com")))

(defun default-data-directory ()
  (merge-pathnames #P".llm-proxy/"
                   (uiop:ensure-directory-pathname (user-homedir-pathname))))

(defun default-config-file ()
  (merge-pathnames #P"config.toml" (default-data-directory)))

(defun normalize-upstream-url (url) (string-right-trim "/" url))

(defun validate-upstream (name url)
  (unless (and (stringp name) (plusp (length name)))
    (%invalid "upstream name must be a non-empty string"))
  (unless (and (stringp url) (plusp (length url)))
    (%invalid "upstream ~A URL must be a non-empty string" name))
  (unless (or (uiop:string-prefix-p "http://" url)
              (uiop:string-prefix-p "https://" url))
    (%invalid "upstream ~A URL must start with http:// or https://: ~S" name url))
  (cons name (normalize-upstream-url url)))

(defun %merge-upstream-entry (registry entry)
  (let ((existing (assoc (car entry) registry :test #'equal)))
    (if existing
        (progn (setf (cdr existing) (cdr entry)) registry)
        (append registry (list entry)))))

(defun %merge-upstreams (&rest layers)
  (let ((registry nil))
    (dolist (layer layers registry)
      (dolist (entry layer)
        (setf registry (%merge-upstream-entry registry entry))))))

(defun upstream-base-url (config name)
  (cdr (assoc name (runtime-config-upstreams config) :test #'equal)))

(defun %validate-port (port)
  (unless (and (integerp port) (typep port '(integer 1 65535)))
    (%invalid "port must be an integer between 1 and 65535, got ~S" port))
  port)

(defun %parse-upstreams-table (table)
  (unless (and (listp table) (every #'consp table))
    (%invalid "upstreams must be a table of name = base-url"))
  (mapcar (lambda (entry) (validate-upstream (car entry) (cdr entry))) table))

(defun %toml-config (root)
  (unless (listp root) (%invalid "configuration root must be a TOML table"))
  (let ((config (make-runtime-config)))
    (loop for (key . value) in root
          do (cond
               ((equal key "data_dir")
                (unless (and (stringp value) (plusp (length value)))
                  (%invalid "data_dir must be a non-empty string"))
                (setf (runtime-config-data-directory config)
                      (uiop:ensure-directory-pathname value)))
               ((equal key "expert_data_dir")
                (unless (and (stringp value) (plusp (length value)))
                  (%invalid "expert_data_dir must be a non-empty string"))
                (setf (runtime-config-expert-data-directory config)
                      (uiop:ensure-directory-pathname value)))
               ((equal key "listen")
                (unless (and (stringp value) (plusp (length value)))
                  (%invalid "listen must be a non-empty string"))
                (setf (runtime-config-listen-address config) value))
               ((equal key "port")
                (setf (runtime-config-port config) (%validate-port value)))
               ((equal key "upstreams")
                (setf (runtime-config-upstreams config) (%parse-upstreams-table value)))
               (t (%invalid "unknown configuration key: ~S" key))))
    config))

(defun parse-toml-config (text)
  (handler-case (%toml-config (clop:parse text))
    (invalid-configuration (condition) (error condition))
    (error (condition)
      (%invalid "configuration is not valid TOML: ~A" condition))))

(defun load-config-file (path)
  (let ((path (pathname path)))
    (unless (uiop:file-exists-p path)
      (%invalid "configuration file does not exist: ~A" (uiop:native-namestring path)))
    (parse-toml-config (uiop:read-file-string path))))

(defun resolve-config (&key config-file data-directory expert-data-directory
                            listen port upstreams)
  (let* ((file-config
           (cond
             ((null config-file) nil)
             ((eq config-file :default)
              (let ((path (default-config-file)))
                (when (uiop:file-exists-p path) (load-config-file path))))
             (t (load-config-file config-file))))
         (listen (or listen
                     (and file-config (runtime-config-listen-address file-config))
                     +default-listen-address+))
         (port (%validate-port
                (or port
                    (and file-config (runtime-config-port file-config))
                    +default-port+)))
         (data-directory
           (uiop:ensure-directory-pathname
            (or data-directory
                (and file-config (runtime-config-data-directory file-config))
                (default-data-directory))))
         (expert-data-directory
           (uiop:ensure-directory-pathname
            (or expert-data-directory
                (and file-config (runtime-config-expert-data-directory file-config))
                (merge-pathnames #P"expert/" data-directory)))))
    (unless (and (stringp listen) (plusp (length listen)))
      (%invalid "listen address must be a non-empty string, got ~S" listen))
    (make-runtime-config
     :data-directory data-directory
     :expert-data-directory expert-data-directory
     :listen-address listen
     :port port
     :upstreams (%merge-upstreams
                 +default-upstreams+
                 (and file-config (runtime-config-upstreams file-config))
                 upstreams))))

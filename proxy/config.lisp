(in-package #:llm-log)

;; Common Lisp runtime configuration (zero-Python rewrite, slice 1).
;; Precedence: built-in defaults < config file < CLI arguments.

(define-condition invalid-configuration (error)
  ((detail :initarg :detail :initform nil :reader invalid-configuration-detail))
  (:report (lambda (condition stream)
             (format stream "invalid llm-log configuration~@[: ~A~]"
                     (invalid-configuration-detail condition)))))

(defun %invalid (control &rest arguments)
  (error 'invalid-configuration :detail (apply #'format nil control arguments)))

(defstruct scheduler-config
  (max-active 4)
  (max-queue-depth 32)
  (queue-timeout-seconds 30)
  (retry-after-seconds 1)
  ;; Zero preserves existing behavior; operators must configure a real limit.
  (requests-per-minute 0)
  (burst 1))

(defstruct outbound-profile
  name version user-agent title
  (drop-headers '()))

(defstruct runtime-config
  (data-directory nil)
  (listen-address nil)
  (port nil)
  (upstreams nil)
  (scheduler nil)
  (profiles nil)
  (provider-profiles nil))

(defparameter +default-listen-address+ "127.0.0.1")
(defparameter +default-port+ 8787)
(defparameter +default-upstreams+
  '(("openai" . "https://api.openai.com")
    ("openrouter" . "https://openrouter.ai")
    ("anthropic" . "https://api.anthropic.com"))
  "Built-in provider-prefix to upstream base-url registry.")

(defparameter +profile-removable-headers+
  '("user-agent" "x-title" "http-referer"
    "x-client-name" "x-client-version" "x-agent-name" "x-agent-version"
    "x-session-id" "traceparent" "tracestate" "baggage"
    "x-stainless-arch" "x-stainless-lang" "x-stainless-os"
    "x-stainless-package-version" "x-stainless-runtime"
    "x-stainless-runtime-version" "x-stainless-retry-count" "x-stainless-timeout")
  "Bounded metadata-only removal surface; auth and framing are not editable.")

(defun default-data-directory ()
  (merge-pathnames #P".llm-proxy/"
                   (uiop:ensure-directory-pathname (user-homedir-pathname))))

(defun default-config-file ()
  (merge-pathnames #P"config.toml" (default-data-directory)))

(defun normalize-upstream-url (url)
  (string-right-trim "/" url))

(defun validate-upstream (name url)
  "Validate one NAME/BASE-URL pair; return (NAME . NORMALIZED-URL)."
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
    (cond (existing (setf (cdr existing) (cdr entry)) registry)
          (t (append registry (list entry))))))

(defun %merge-upstreams (&rest layers)
  "Merge upstream registry layers; higher layers replace same-named entries."
  (let ((registry '()))
    (dolist (layer layers registry)
      (dolist (entry layer)
        (setf registry (%merge-upstream-entry registry entry))))))

(defun upstream-base-url (config name)
  "Return the configured base URL for provider NAME, or NIL."
  (cdr (assoc name (runtime-config-upstreams config) :test #'equal)))

(defun %validate-port (port)
  (unless (and (integerp port) (typep port '(integer 1 65535)))
    (%invalid "port must be an integer between 1 and 65535, got ~S" port))
  port)

(defun %validate-positive-integer (name value)
  (unless (and (integerp value) (plusp value))
    (%invalid "~A must be a positive integer, got ~S" name value))
  value)

(defun %validate-nonnegative-integer (name value)
  (unless (and (integerp value) (not (minusp value)))
    (%invalid "~A must be a non-negative integer, got ~S" name value))
  value)

(defun validate-scheduler-config (scheduler)
  "Validate and return one scheduler-config."
  (unless (scheduler-config-p scheduler)
    (%invalid "scheduler must be a scheduler configuration"))
  (%validate-positive-integer "scheduler.max_active"
                              (scheduler-config-max-active scheduler))
  (%validate-nonnegative-integer "scheduler.max_queue_depth"
                                 (scheduler-config-max-queue-depth scheduler))
  (%validate-nonnegative-integer "scheduler.queue_timeout_seconds"
                                 (scheduler-config-queue-timeout-seconds scheduler))
  (%validate-positive-integer "scheduler.retry_after_seconds"
                              (scheduler-config-retry-after-seconds scheduler))
  (unless (typep (scheduler-config-requests-per-minute scheduler) '(integer 0 1000000))
    (%invalid "scheduler.requests_per_minute must be an integer in [0, 1000000]"))
  (unless (typep (scheduler-config-burst scheduler) '(integer 1 100000))
    (%invalid "scheduler.burst must be an integer in [1, 100000]"))
  scheduler)

(defun %validate-profile-text (field value &key required)
  (unless (or (and (not required) (null value))
              (and (stringp value) (<= 1 (length value) 512)
                   (every (lambda (char) (<= 32 (char-code char) 126)) value)))
    (%invalid "profile ~A must be nonempty printable ASCII, at most 512 bytes" field))
  value)

(defun validate-outbound-profile (profile)
  (unless (outbound-profile-p profile) (%invalid "expected an outbound profile"))
  (%validate-profile-text "name" (outbound-profile-name profile) :required t)
  (%validate-profile-text "version" (outbound-profile-version profile) :required t)
  (%validate-profile-text "user_agent" (outbound-profile-user-agent profile))
  (%validate-profile-text "title" (outbound-profile-title profile))
  (let ((headers (outbound-profile-drop-headers profile)))
    (unless (and (listp headers) (<= (length headers) 32)
                 (every (lambda (name)
                          (and (stringp name)
                               (member name +profile-removable-headers+
                                       :test #'string-equal)))
                        headers))
      (%invalid "profile drop_headers may contain only supported metadata header names")))
  profile)

(defun %configuration-table (name table &optional (limit 256))
  (unless (and (listp table) (<= (length table) limit) (every #'consp table))
    (%invalid "~A must be a TOML table with at most ~D entries" name limit))
  table)

(defun %parse-upstreams-table (table)
  (mapcar (lambda (entry) (validate-upstream (car entry) (cdr entry)))
          (%configuration-table "upstreams" table)))

(defun %parse-scheduler-table (table)
  (%configuration-table "scheduler" table)
  (let ((scheduler (make-scheduler-config)))
    (dolist (entry table (validate-scheduler-config scheduler))
      (let ((key (car entry)) (value (cdr entry)))
        (cond
          ((equal key "max_active")
           (setf (scheduler-config-max-active scheduler) value))
          ((equal key "max_queue_depth")
           (setf (scheduler-config-max-queue-depth scheduler) value))
          ((equal key "queue_timeout_seconds")
           (setf (scheduler-config-queue-timeout-seconds scheduler) value))
          ((equal key "retry_after_seconds")
           (setf (scheduler-config-retry-after-seconds scheduler) value))
          ((equal key "requests_per_minute")
           (setf (scheduler-config-requests-per-minute scheduler) value))
          ((equal key "burst")
           (setf (scheduler-config-burst scheduler) value))
          (t (%invalid "unknown scheduler configuration key: ~S" key)))))))

(defun %parse-profile (name table)
  (%configuration-table "profile" table)
  (let ((profile (make-outbound-profile :name name)))
    (dolist (entry table (validate-outbound-profile profile))
      (let ((key (car entry)) (value (cdr entry)))
        (cond
          ((equal key "version") (setf (outbound-profile-version profile) value))
          ((equal key "user_agent") (setf (outbound-profile-user-agent profile) value))
          ((equal key "title") (setf (outbound-profile-title profile) value))
          ((equal key "drop_headers")
           (unless (and (or (listp value) (vectorp value)) (not (stringp value)))
             (%invalid "profile drop_headers must be an array of metadata names"))
           (setf (outbound-profile-drop-headers profile) (coerce value 'list)))
          (t (%invalid "unknown profile configuration key: ~S" key)))))))

(defun %parse-profiles-table (table)
  (mapcar (lambda (entry) (%parse-profile (car entry) (cdr entry)))
          (%configuration-table "profiles" table 64)))

(defun %parse-provider-profiles-table (table)
  (mapcar (lambda (entry)
            (%validate-profile-text "provider name" (car entry) :required t)
            (%validate-profile-text "profile reference" (cdr entry) :required t)
            (cons (car entry) (cdr entry)))
          (%configuration-table "provider_profiles" table)))

(defun %validate-profile-mappings (profiles mappings)
  (dolist (entry mappings)
    (unless (find (cdr entry) profiles :key #'outbound-profile-name :test #'equal)
      (%invalid "provider ~S references unknown profile ~S" (car entry) (cdr entry)))))

(defun %toml-config (root)
  (%configuration-table "configuration root" root)
  (let ((config (make-runtime-config)))
    (loop for (key . value) in root
          do (cond
               ((equal key "data_dir")
                (unless (and (stringp value) (plusp (length value)))
                  (%invalid "data_dir must be a non-empty string, got ~S" value))
                (setf (runtime-config-data-directory config)
                      (uiop:ensure-directory-pathname value)))
               ((equal key "listen")
                (unless (and (stringp value) (plusp (length value)))
                  (%invalid "listen must be a non-empty string, got ~S" value))
                (setf (runtime-config-listen-address config) value))
               ((equal key "port") (setf (runtime-config-port config) (%validate-port value)))
               ((equal key "upstreams")
                (setf (runtime-config-upstreams config) (%parse-upstreams-table value)))
               ((equal key "scheduler")
                (setf (runtime-config-scheduler config) (%parse-scheduler-table value)))
               ((equal key "profiles")
                (setf (runtime-config-profiles config) (%parse-profiles-table value)))
               ((equal key "provider_profiles")
                (setf (runtime-config-provider-profiles config)
                      (%parse-provider-profiles-table value)))
               (t (%invalid "unknown configuration key: ~S" key))))
    (%validate-profile-mappings (runtime-config-profiles config)
                                (runtime-config-provider-profiles config))
    config))

(defun parse-toml-config (text)
  "Parse TOML configuration TEXT into a partial runtime-config."
  (handler-case (%toml-config (clop:parse text))
    (invalid-configuration (condition) (error condition))
    (error (condition) (%invalid "configuration is not valid TOML: ~A" condition))))

(defun load-config-file (path)
  "Load and parse one configuration file; a missing explicit file is an error."
  (let ((path (pathname path)))
    (unless (uiop:file-exists-p path)
      (%invalid "configuration file does not exist: ~A" (uiop:native-namestring path)))
    (parse-toml-config (uiop:read-file-string path))))

(defun resolve-config (&key config-file data-directory listen port upstreams scheduler)
  "Merge built-in defaults, config file and CLI overrides.
CONFIG-FILE is NIL (no file), :DEFAULT (optional default file), or an explicit path.
Profiles are operator file configuration, not caller-selected request headers."
  (let* ((file-config
           (cond ((null config-file) nil)
                 ((eq config-file :default)
                  (let ((path (default-config-file)))
                    (when (uiop:file-exists-p path) (load-config-file path))))
                 (t (load-config-file config-file))))
         (listen (or listen (and file-config (runtime-config-listen-address file-config))
                     +default-listen-address+))
         (port (%validate-port
                (or port (and file-config (runtime-config-port file-config)) +default-port+)))
         (data-directory
           (uiop:ensure-directory-pathname
            (or data-directory (and file-config (runtime-config-data-directory file-config))
                (default-data-directory))))
         (scheduler
           (validate-scheduler-config
            (or scheduler (and file-config (runtime-config-scheduler file-config))
                (make-scheduler-config)))))
    (unless (and (stringp listen) (plusp (length listen)))
      (%invalid "listen address must be a non-empty string, got ~S" listen))
    (let ((config
            (make-runtime-config
             :data-directory data-directory :listen-address listen :port port
             :upstreams (%merge-upstreams +default-upstreams+
                                          (and file-config (runtime-config-upstreams file-config))
                                          upstreams)
             :scheduler scheduler
             :profiles (and file-config (runtime-config-profiles file-config))
             :provider-profiles (and file-config (runtime-config-provider-profiles file-config)))))
      (dolist (entry (runtime-config-provider-profiles config))
        (unless (upstream-base-url config (car entry))
          (%invalid "profile mapping references unknown upstream: ~S" (car entry))))
      config)))

(in-package #:llm-log)

(defun %require-value (flag rest)
  (unless (consp rest) (%invalid "~A requires a value" flag)))

(defun %parse-integer-argument (flag value)
  (handler-case (%validate-port (parse-integer value :junk-allowed nil))
    (error () (%invalid "~A expects an integer between 1 and 65535, got ~S"
                        flag value))))

(defun %parse-upstream-argument (value)
  (let ((separator (position #\= value :test #'char=)))
    (unless separator (%invalid "--upstream expects NAME=URL, got ~S" value))
    (let ((name (subseq value 0 separator))
          (url (subseq value (1+ separator))))
      (when (zerop (length name)) (%invalid "--upstream name must not be empty"))
      (validate-upstream name url))))

(defun parse-serve-arguments (arguments)
  (unless (and (consp arguments) (equal (first arguments) "serve"))
    (%invalid "usage: llm-log serve [options]"))
  (let ((data-directory nil)
        (expert-data-directory nil)
        (listen nil)
        (port nil)
        (upstreams nil)
        (config-file nil))
    (loop with rest = (rest arguments)
          while rest
          for argument = (pop rest)
          do (cond
               ((equal argument "--data-dir")
                (%require-value "--data-dir" rest)
                (setf data-directory (pop rest)))
               ((equal argument "--expert-data-dir")
                (%require-value "--expert-data-dir" rest)
                (setf expert-data-directory (pop rest)))
               ((equal argument "--config")
                (%require-value "--config" rest)
                (setf config-file (pop rest)))
               ((equal argument "--listen")
                (%require-value "--listen" rest)
                (setf listen (pop rest)))
               ((equal argument "--port")
                (%require-value "--port" rest)
                (setf port (%parse-integer-argument "--port" (pop rest))))
               ((equal argument "--upstream")
                (%require-value "--upstream" rest)
                (push (%parse-upstream-argument (pop rest)) upstreams))
               (t (%invalid "unknown argument: ~S" argument))))
    (resolve-config
     :config-file (or config-file :default)
     :data-directory data-directory
     :expert-data-directory expert-data-directory
     :listen listen :port port :upstreams (nreverse upstreams))))

(defun %serve-main (arguments)
  (handler-case
      (let* ((config (parse-serve-arguments arguments))
             (server nil))
        (uiop:ensure-all-directories-exist
         (list (runtime-config-data-directory config)
               (runtime-config-expert-data-directory config)))
        (setf server (start-proxy config))
        (unwind-protect
             (progn
               (format *error-output*
                       "llm-log: Common Lisp runtime on ~A:~A; capture ~A; expert ~A~%"
                       (runtime-config-listen-address config)
                       (runtime-config-port config)
                       (uiop:native-namestring (runtime-config-data-directory config))
                       (uiop:native-namestring
                        (runtime-config-expert-data-directory config)))
               (loop (sleep 3600)))
          (when server (stop-proxy server)))
        0)
    (invalid-configuration (condition)
      (format *error-output* "llm-log: ~A~%" condition)
      2)))

(defun main (&optional (arguments (uiop:command-line-arguments)))
  "Single all-Common-Lisp llm-log command surface."
  (unless arguments
    (format *error-output*
            "usage: llm-log {serve|bulk-load|infill} ...~%")
    (return-from main 2))
  (cond
    ((equal (first arguments) "serve") (%serve-main arguments))
    ((member (first arguments) '("bulk-load" "infill") :test #'equal)
     ;; The packaged runtime carries the expert system in-process. Corpus
     ;; commands delegate to its CL command implementation, not to a child
     ;; process and not through HTTP.
     (llm-log-expert:main arguments))
    (t
     (format *error-output* "llm-log: unknown command ~A~%" (first arguments))
     2)))

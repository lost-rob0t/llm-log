(in-package #:llm-log)

;; Common Lisp CLI surface (zero-Python rewrite, slice 1).
;;
;; llm-log serve [--config PATH] [--data-dir PATH] [--listen ADDR]
;;               [--port N] [--upstream NAME=URL]...

(defun %require-value (flag rest)
  "Validate that FLAG has a remaining value; the caller pops REST itself."
  (unless (consp rest)
    (%invalid "~A requires a value" flag)))

(defun %parse-integer-argument (flag value)
  (handler-case
      (%validate-port (parse-integer value :junk-allowed nil))
    (error ()
      (%invalid "~A expects an integer between 1 and 65535, got ~S"
                flag value))))

(defun %parse-upstream-argument (value)
  (let ((separator (position #\= value :test #'char=)))
    (unless separator
      (%invalid "--upstream expects NAME=URL, got ~S" value))
    (let* ((name (subseq value 0 separator))
           (url (subseq value (1+ separator))))
      (when (zerop (length name))
        (%invalid "--upstream name must not be empty: ~S" value))
      (validate-upstream name url))))

(defun parse-serve-arguments (arguments)
  "Parse `llm-log serve [options]` into a fully resolved configuration."
  (unless (consp arguments)
    (%invalid "usage: llm-log serve [--config PATH] [--data-dir PATH] ~
[--listen ADDR] [--port N] [--upstream NAME=URL]..."))
  (unless (equal (first arguments) "serve")
    (%invalid "unknown command: ~S (expected \"serve\")" (first arguments)))
  (let ((data-directory nil)
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
               (t
                (%invalid "unknown argument: ~S" argument))))
    (resolve-config
     :config-file (or config-file :default)
     :data-directory data-directory
     :listen listen
     :port port
     :upstreams (nreverse upstreams))))

(defun main (&optional (arguments (uiop:command-line-arguments)))
  "Entry point for the packaged llm-log executable.

Resolves configuration, starts the transparent Common Lisp proxy and blocks
until process termination. Exit codes: 2 invalid configuration."
  (handler-case
      (let* ((config (parse-serve-arguments arguments))
             (server (start-proxy config)))
        (uiop:ensure-all-directories-exist
         (runtime-config-data-directory config))
        (format *error-output*
                "llm-log: listening on ~A:~A; capture root ~A~%"
                (runtime-config-listen-address config)
                (runtime-config-port config)
                (uiop:native-namestring
                 (runtime-config-data-directory config)))
        (loop (sleep 3600))
        (stop-proxy server)
        0)
    (invalid-configuration (condition)
      (format *error-output* "llm-log: ~A~%" condition)
      2)))

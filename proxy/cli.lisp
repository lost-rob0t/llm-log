(in-package #:llm-log)

;; Common Lisp CLI surface (zero-Python rewrite, slice 1).
;;
;; llm-log serve [--config PATH] [--data-dir PATH] [--listen ADDR]
;;               [--port N] [--upstream NAME=URL]...

(defun %require-value (flag rest)
  (unless (consp rest)
    (%invalid "~A requires a value" flag))
  (pop rest))

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
                (setf data-directory (%require-value "--data-dir" rest)))
               ((equal argument "--config")
                (setf config-file (%require-value "--config" rest)))
               ((equal argument "--listen")
                (setf listen (%require-value "--listen" rest)))
               ((equal argument "--port")
                (setf port (%parse-integer-argument
                            "--port" (%require-value "--port" rest))))
               ((equal argument "--upstream")
                (push (%parse-upstream-argument
                       (%require-value "--upstream" rest))
                      upstreams))
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

Slice 1 delivers configuration/CLI resolution; the transparent transport
wiring lands with the Common Lisp HTTP slice (research 012). Exit codes:
2 invalid configuration, 3 configuration valid but transport pending."
  (handler-case
      (let ((config (parse-serve-arguments arguments)))
        (format *error-output*
                "llm-log: configuration valid (data-dir=~A listen=~A ~
port=~A); transport implementation pending ~
(research/LLM-LOG-RESEARCH-012)~%"
                (uiop:native-namestring
                 (runtime-config-data-directory config))
                (runtime-config-listen-address config)
                (runtime-config-port config))
        3)
    (invalid-configuration (condition)
      (format *error-output* "llm-log: ~A~%" condition)
      2)))

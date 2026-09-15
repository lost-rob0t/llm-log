(in-package #:llm-log-expert)

(defun %command-value (flag rest)
  (unless rest (error "~A requires a value" flag))
  (values (first rest) (rest rest)))

(defun %parse-common-corpus-options (arguments)
  (let ((source nil)
        (data-directory (%default-expert-data-directory))
        (checkpoint nil)
        (from-start nil)
        (record-transport-evidence nil)
        (checkpoint-every +default-checkpoint-every+)
        (limit 0)
        (dry-run nil))
    (loop with rest = arguments
          while rest
          for argument = (pop rest)
          do (cond
               ((string= argument "--source")
                (unless rest (error "--source requires a path"))
                (setf source (pathname (pop rest))))
               ((string= argument "--data-dir")
                (unless rest (error "--data-dir requires a path"))
                (setf data-directory (uiop:ensure-directory-pathname (pop rest))))
               ((string= argument "--checkpoint")
                (unless rest (error "--checkpoint requires a path"))
                (setf checkpoint (pathname (pop rest))))
               ((string= argument "--checkpoint-every")
                (unless rest (error "--checkpoint-every requires an integer"))
                (setf checkpoint-every (parse-integer (pop rest) :junk-allowed nil)))
               ((string= argument "--limit")
                (unless rest (error "--limit requires an integer"))
                (setf limit (parse-integer (pop rest) :junk-allowed nil)))
               ((string= argument "--from-start") (setf from-start t))
               ((string= argument "--record-transport-evidence")
                (setf record-transport-evidence t))
               ((string= argument "--dry-run") (setf dry-run t))
               (t (error "unknown corpus option: ~A" argument))))
    (unless source (error "--source is required"))
    (values source data-directory checkpoint from-start
            record-transport-evidence checkpoint-every limit dry-run)))

(defun %run-corpus-command (arguments &key require-checkpoint)
  (multiple-value-bind
        (source data-directory checkpoint from-start record-transport-evidence
         checkpoint-every limit dry-run)
      (%parse-common-corpus-options arguments)
    (let ((host (start-expert-host data-directory)))
      (unwind-protect
           (let ((result
                   (import-capture-corpus
                    host source
                    :checkpoint checkpoint
                    :from-start from-start
                    :require-checkpoint require-checkpoint
                    :record-transport-evidence record-transport-evidence
                    :checkpoint-every checkpoint-every
                    :limit limit
                    :dry-run dry-run)))
             (write-line (jsown:to-json result) *standard-output*)
             (force-output *standard-output*)
             0)
        (stop-expert-host host)))))

(defun %run-serve-command (arguments)
  (let ((stdio nil)
        (http nil)
        (listen "127.0.0.1")
        (port 8788)
        (data-directory (%default-expert-data-directory)))
    (loop with rest = arguments
          while rest
          for argument = (pop rest)
          do (cond
               ((string= argument "--stdio") (setf stdio t))
               ((string= argument "--http") (setf http t))
               ((string= argument "--listen")
                (unless rest (error "--listen requires an address"))
                (setf listen (pop rest)))
               ((string= argument "--port")
                (unless rest (error "--port requires an integer"))
                (setf port (parse-integer (pop rest) :junk-allowed nil)))
               ((string= argument "--data-dir")
                (unless rest (error "--data-dir requires a path"))
                (setf data-directory (uiop:ensure-directory-pathname (pop rest))))
               (t (error "unknown serve option: ~A" argument))))
    (when (and stdio http)
      (error "choose exactly one expert transport: --stdio or --http"))
    (unless (or stdio http)
      (error "serve requires --stdio or --http"))
    (let ((host (start-expert-host data-directory)))
      (unwind-protect
           (if stdio
               (serve-stdio host)
               (let* ((token (uiop:getenv "LLM_LOG_EXPERT_HTTP_TOKEN"))
                      (server (start-expert-http-server
                               host :listen listen :port port :token token)))
                 (format *error-output*
                         "llm-log-expert: HTTP service listening on ~A:~D~%"
                         listen port)
                 (unwind-protect
                      (loop (sleep 3600))
                   (stop-expert-http-server server))))
        (stop-expert-host host)))
    0))

(defun main (&optional (arguments (uiop:command-line-arguments)))
  "Packaged Common Lisp expert entrypoint.

Commands:
  serve --stdio|--http [--data-dir DIR] [--listen ADDR] [--port N]
  bulk-load --source events.jsonl [options]
  infill --source events.jsonl [options]

BULK-LOAD is the high-volume local historical path. INFILL resumes the byte
checkpoint created by BULK-LOAD and is intended for local catch-up. Neither
uses HTTP. HTTP exists only as an optional remote deployment transport."
  (unless arguments
    (error "usage: llm-log-expert {serve|bulk-load|infill} ..."))
  (let ((command (first arguments))
        (rest (rest arguments)))
    (cond
      ((string= command "serve") (%run-serve-command rest))
      ((string= command "bulk-load")
       (%run-corpus-command rest :require-checkpoint nil))
      ((string= command "infill")
       (%run-corpus-command rest :require-checkpoint t))
      (t (error "unknown llm-log-expert command: ~A" command)))))

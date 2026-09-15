(in-package #:llm-log)

(defparameter *runtime-base-start-proxy* (symbol-function 'start-proxy))
(defparameter *runtime-base-stop-proxy* (symbol-function 'stop-proxy))

(defun %quota-enabled-p ()
  (member (string-downcase (or (uiop:getenv "LLM_LOG_QUOTAS_ENABLED") ""))
          '("1" "true" "yes" "on") :test #'equal))

(defun start-proxy (config)
  "Start the CL proxy/expert runtime and opt-in CL quota collector."
  (let ((server (funcall *runtime-base-start-proxy* config)))
    (handler-case
        (progn
          (when (%quota-enabled-p) (start-quota-collector))
          server)
      (error (condition)
        (funcall *runtime-base-stop-proxy* server)
        (error condition)))))

(defun stop-proxy (server)
  (when *quota-thread* (stop-quota-collector))
  (funcall *runtime-base-stop-proxy* server))

(in-package #:llm-log-expert)

(defparameter *base-ingest-capture-event*
  (symbol-function 'ingest-capture-event))

(defun ingest-capture-event (host event &key record-transport-evidence)
  "Extend capture infill with idempotent durable analytics aggregation."
  (let ((result
          (funcall *base-ingest-capture-event*
                   host event :record-transport-evidence record-transport-evidence)))
    (let ((analytics-state (project-capture-analytics host event)))
      (jsown:extend-js result
        ("analytics_state" (string-downcase (symbol-name analytics-state))))
      result)))

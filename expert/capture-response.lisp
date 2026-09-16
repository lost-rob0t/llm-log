(in-package #:llm-log-expert)

(defparameter +capture-response-schema-version+ 1)

(defparameter *response-base-dispatch-expert-request*
  (symbol-function 'dispatch-expert-request))

(defun %capture-response-key (event-id)
  (format nil "response:~A" event-id))

(defun %capture-response-status (payload)
  (let ((value (jsown:val-safe payload "response_status")))
    (unless (and (integerp value) (<= 100 value 599))
      (error "response_status must be an HTTP status integer"))
    value))

(defun %capture-response-latency (payload)
  (let ((value (jsown:val-safe payload "latency_ms")))
    (unless (and (integerp value) (>= value 0))
      (error "latency_ms must be a non-negative integer"))
    value))

(defun %capture-stream-state (payload)
  (let ((state (%optional-json-string payload "stream_state")))
    (when state
      (unless (member state '("completed" "incomplete" "unknown") :test #'equal)
        (error "stream_state must be completed, incomplete, or unknown"))
      (return-from %capture-stream-state state)))
  (multiple-value-bind (value present-p)
      (jsown:val-safe payload "stream_completed")
    (cond
      ((not present-p) "unknown")
      ((member value '(t :true :t) :test #'eq) "completed")
      ((member value '(:null) :test #'eq) "unknown")
      ((member value '(nil :false :f :n) :test #'eq) "incomplete")
      (t (error "stream_completed must be boolean or null")))))

(defun %capture-response-projection (event-id payload)
  (let ((projection
          (list :schema-version +capture-response-schema-version+
                :response-id event-id
                :request-id event-id
                :provider (%required-json-string payload "provider")
                :model (%required-json-string payload "model")
                :transport (%required-json-string payload "transport")
                :completed-at (%required-json-string payload "completed_at")
                :response-sha256 (%required-json-string payload "response_sha256")
                :response-status (%capture-response-status payload)
                :status-kind (%required-json-string payload "status_kind")
                :latency-ms (%capture-response-latency payload)
                :stream-state (%capture-stream-state payload))))
    (%append-present-plist-field
     projection :finish-reason (%optional-json-string payload "finish_reason"))))

(defun %capture-response-source-compatible-p (request projection)
  (and (equal (getf request :provider) (getf projection :provider))
       (equal (getf request :model) (getf projection :model))
       (equal (getf request :transport) (getf projection :transport))
       (equal (getf request :completed-at) (getf projection :completed-at))
       (equal (getf request :response-sha256)
              (getf projection :response-sha256))))

(defun observe-capture-response (host event-id payload)
  "Persist safe terminal response metadata bound to one immutable request capture."
  (unless (%non-empty-string-p event-id)
    (error "event_id is required"))
  (let ((request-id (%optional-json-string payload "request_id")))
    (when (and request-id (not (equal request-id event-id)))
      (error "observe_response event_id must equal request_id")))
  (let* ((request (fetch-request-event host event-id))
         (projection (%capture-response-projection event-id payload)))
    (unless request
      (error "unknown_source_event: ~A" event-id))
    (unless (%capture-response-source-compatible-p request projection)
      (error "response_source_mismatch: ~A" event-id))
    (multiple-value-bind (state revision)
        (%put-immutable host (%capture-response-key event-id) projection "response")
      (%json-object
       (cons "projection_state" (string-downcase (symbol-name state)))
       (cons "response_id" event-id)
       (cons "request_id" event-id)
       (cons "kb_revision" revision)))))

(defun dispatch-expert-request (host request)
  "Extend the declared expert surface with safe response observation ingest."
  (let ((operation (and (consp request)
                        (eq (first request) :obj)
                        (jsown:val-safe request "operation"))))
    (if (equal operation "observe_response")
        (handler-case
            (%reply-ok
             (observe-capture-response
              host (%require-event-id request) (%request-payload request)))
          (error (condition)
            (%reply-error "response_observation_error"
                          (princ-to-string condition))))
        (funcall *response-base-dispatch-expert-request* host request))))

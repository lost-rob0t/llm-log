(in-package #:llm-log-expert)

(defparameter +capture-user-message-limit+ 4000)

(defun %capture-required-string (event field)
  (let ((value (jsown:val-safe event field)))
    (unless (%non-empty-string-p value)
      (error "capture event ~A must be a non-empty string" field))
    value))

(defun %capture-request-text (event)
  "Return the captured request as UTF-8 text when it is textual.

Binary/base64 request bodies remain durable in events.jsonl but are deliberately
not decoded for classification: the expert projection does not need to copy or
materialize arbitrary binary payloads."
  (let ((body (jsown:val-safe event "request_body")))
    (when (and (consp body) (eq (first body) :obj)
               (equal (jsown:val-safe body "encoding") "utf-8"))
      (let ((text (jsown:val-safe body "text")))
        (and (stringp text) text)))))

(defun %capture-user-content (message)
  (let ((content (and (consp message)
                      (eq (first message) :obj)
                      (jsown:val-safe message "content"))))
    (cond
      ((stringp content) content)
      ((listp content)
       (with-output-to-string (out)
         (let ((first t))
           (dolist (part content)
             (when (and (consp part) (eq (first part) :obj)
                        (equal (jsown:val-safe part "type") "text"))
               (let ((text (jsown:val-safe part "text")))
                 (when (stringp text)
                   (unless first (write-char #\Space out))
                   (setf first nil)
                   (write-string text out))))))))
      (t nil))))

(defun %capture-last-user-message (object)
  (let ((messages (and (consp object)
                       (eq (first object) :obj)
                       (jsown:val-safe object "messages"))))
    (when (listp messages)
      (dolist (message (reverse messages))
        (when (and (consp message) (eq (first message) :obj)
                   (equal (jsown:val-safe message "role") "user"))
          (let ((text (%capture-user-content message)))
            (when (and (stringp text) (plusp (length text)))
              (return-from %capture-last-user-message text))))))))

(defun capture-user-message (event)
  "Extract the last user message without retaining the full captured body."
  (let ((text (%capture-request-text event)))
    (when text
      (labels ((bounded (value)
                 (and value
                      (subseq value 0 (min (length value)
                                           +capture-user-message-limit+)))))
        (handler-case
            (let ((parsed (jsown:parse text)))
              (let ((message (%capture-last-user-message parsed)))
                (when message (return-from capture-user-message (bounded message)))))
          (error () nil))
        (dolist (line (uiop:split-string text :separator '(#\Newline)))
          (handler-case
              (let ((frame (jsown:parse line)))
                (when (and (consp frame) (eq (first frame) :obj)
                           (equal (jsown:val-safe frame "type") "text"))
                  (let ((inner (jsown:val-safe frame "text")))
                    (when (stringp inner)
                      (let* ((payload (jsown:parse inner))
                             (message (%capture-last-user-message payload)))
                        (when message
                          (return-from capture-user-message (bounded message))))))))
            (error () nil)))))))

(defun capture-request-projection (event)
  (%json-object
   (cons "provider" (%capture-required-string event "provider"))
   (cons "upstream" (jsown:val-safe event "upstream"))
   (cons "model" (or (jsown:val-safe event "model") "unknown"))
   (cons "transport" (or (jsown:val-safe event "transport") "http"))
   (cons "started_at" (%capture-required-string event "started_at"))
   (cons "completed_at" (%capture-required-string event "completed_at"))
   (cons "request_sha256" (%capture-required-string event "request_sha256"))
   (cons "response_sha256" (%capture-required-string event "response_sha256"))))

(defun capture-usage-projection (event)
  (let* ((event-id (%capture-required-string event "event_id"))
         (fields '("input_tokens" "output_tokens" "cached_input_tokens"
                   "cached_output_tokens" "reasoning_tokens"))
         (present (remove-if-not (lambda (field)
                                   (not (null (jsown:val-safe event field))))
                                 fields)))
    (when present
      (let ((usage
              (%json-object
               (cons "usage_id" (format nil "capture-usage:~A" event-id))
               (cons "request_id" event-id)
               (cons "provider" (%capture-required-string event "provider"))
               (cons "model" (or (jsown:val-safe event "model") "unknown"))
               (cons "client" "proxy")
               (cons "transport" (or (jsown:val-safe event "transport") "http")))))
        (dolist (field present)
          (push (cons field (jsown:val-safe event field)) (cdr usage)))
        usage))))

(defun %capture-transport-outcome (host event)
  (let* ((event-id (%capture-required-string event "event_id"))
         (status (jsown:val-safe event "response_status"))
         (outcome-event-id (format nil "capture-transport:~A" event-id)))
    (unless (and (integerp status) (<= 100 status 599))
      (error "capture event response_status must be an HTTP status integer"))
    (record-outcome-evidence
     host outcome-event-id
     (%json-object
      (cons "scope" "request")
      (cons "scope_id" event-id)
      (cons "evidence"
            (list
             (%json-object
              (cons "evidence_id" outcome-event-id)
              (cons "observed_at" (%capture-required-string event "completed_at"))
              (cons "evidence_type" "provider_transport")
              (cons "authority" "weak")
              (cons "observed_value" status)
              (cons "source_id" event-id))))))))

(defun ingest-capture-event (host event &key record-transport-evidence)
  "Project one immutable raw capture into the CL-owned expert plane."
  (unless (and (consp event) (eq (first event) :obj))
    (error "capture event must be a JSON object"))
  (let* ((event-id (%capture-required-string event "event_id"))
         (payload (capture-request-projection event))
         (message (capture-user-message event))
         (usage (capture-usage-projection event))
         (request-state nil)
         (classification nil)
         (usage-result nil)
         (transport-outcome nil))
    (multiple-value-bind (state revision)
        (project-request-event host event-id payload)
      (declare (ignore revision))
      (setf request-state state))
    (when message
      (let ((classification-payload
              (%json-object
               (cons "provider" (jsown:val-safe payload "provider"))
               (cons "model" (jsown:val-safe payload "model"))
               (cons "transport" (jsown:val-safe payload "transport"))
               (cons "started_at" (jsown:val-safe payload "started_at"))
               (cons "completed_at" (jsown:val-safe payload "completed_at"))
               (cons "request_sha256" (jsown:val-safe payload "request_sha256"))
               (cons "response_sha256" (jsown:val-safe payload "response_sha256"))
               (cons "message" message)
               (cons "user_message_id" (format nil "um-~A"
                                                  (subseq event-id 0 (min 8 (length event-id)))))
               (cons "request_id" event-id)
               (cons "client" "proxy"))))
        (project-classification-source host event-id classification-payload)
        (multiple-value-bind (assertions revision)
            (derive-request-classification host event-id)
          (declare (ignore revision))
          (setf classification assertions))))
    (when usage
      (setf usage-result (observe-capture-usage host event-id usage)))
    (when record-transport-evidence
      (setf transport-outcome (%capture-transport-outcome host event)))
    (%json-object
     (cons "event_id" event-id)
     (cons "request_state" (string-downcase (symbol-name request-state)))
     (cons "classified" (not (null classification)))
     (cons "usage_projected" (not (null usage-result)))
     (cons "transport_outcome" transport-outcome)
     (cons "kb_revision" (current-kb-revision host)))))

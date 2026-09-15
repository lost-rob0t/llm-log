(in-package #:llm-log)

(defparameter +secret-headers+
  '("authorization" "proxy-authorization" "cookie" "set-cookie"
    "x-api-key" "api-key" "openai-api-key" "anthropic-api-key"))

(defparameter +input-token-keys+
  '("input_tokens" "prompt_tokens" "promptTokenCount" "inputTokens"
    "prompt_eval_count"))
(defparameter +output-token-keys+
  '("output_tokens" "completion_tokens" "candidatesTokenCount" "outputTokens"
    "eval_count"))
(defparameter +base64-alphabet+
  "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/")
(defparameter *recorder-lock* (bt:make-lock "llm-log-events-jsonl"))

(defun %jobj (&rest pairs)
  (cons :obj (mapcar (lambda (pair) (cons (car pair) (cdr pair))) pairs)))

(defun %sha256-hex (octets)
  (with-output-to-string (out)
    (loop for byte across (ironclad:digest-sequence :sha256 octets)
          do (format out "~2,'0x" byte))))

(defun %base64-encode (octets)
  (with-output-to-string (out)
    (loop for i from 0 below (length octets) by 3
          for remain = (- (length octets) i)
          for a = (aref octets i)
          for b = (if (> remain 1) (aref octets (1+ i)) 0)
          for c = (if (> remain 2) (aref octets (+ i 2)) 0)
          for bits = (logior (ash a 16) (ash b 8) c)
          do (write-char (char +base64-alphabet+ (ldb (byte 6 18) bits)) out)
             (write-char (char +base64-alphabet+ (ldb (byte 6 12) bits)) out)
             (if (> remain 1)
                 (write-char (char +base64-alphabet+ (ldb (byte 6 6) bits)) out)
                 (write-char #\= out))
             (if (> remain 2)
                 (write-char (char +base64-alphabet+ (ldb (byte 6 0) bits)) out)
                 (write-char #\= out)))))

(defun %captured-body (octets)
  (handler-case
      (%jobj (cons "encoding" "utf-8")
             (cons "text" (trivial-utf-8:utf-8-bytes-to-string octets)))
    (error ()
      (%jobj (cons "encoding" "base64")
             (cons "data" (%base64-encode octets))))))

(defun %json-object-p (value)
  (and (consp value) (eq (first value) :obj)))

(defun %json-find-model (value)
  (cond
    ((%json-object-p value)
     (let ((model (jsown:val-safe value "model")))
       (or (and (stringp model) model)
           (loop for pair in (rest value)
                 for found = (%json-find-model (cdr pair))
                 when found return found))))
    ((listp value)
     (loop for child in value
           for found = (%json-find-model child)
           when found return found))
    (t nil)))

(defun %request-model (body)
  (handler-case
      (%json-find-model (jsown:parse (trivial-utf-8:utf-8-bytes-to-string body)))
    (error () nil)))

(defun %nonnegative-number (value)
  (and (numberp value) (not (minusp value)) value))

(defun %first-token-field (object keys)
  (loop for key in keys
        for value = (and (%json-object-p object) (jsown:val-safe object key))
        for valid = (%nonnegative-number value)
        when valid return valid))

(defun %token-candidates (value)
  (let ((result nil))
    (labels ((walk (node)
               (cond
                 ((%json-object-p node)
                  (let ((incoming (%first-token-field node +input-token-keys+))
                        (outgoing (%first-token-field node +output-token-keys+)))
                    (when (or incoming outgoing)
                      (push (cons incoming outgoing) result))
                    (dolist (pair (rest node)) (walk (cdr pair)))))
                 ((listp node) (dolist (child node) (walk child))))))
      (walk value))
    result))

(defun %response-json-documents (body)
  (let ((text (handler-case
                  (trivial-utf-8:utf-8-bytes-to-string body)
                (error () nil)))
        (documents nil))
    (when text
      (handler-case (push (jsown:parse text) documents) (error () nil))
      (dolist (line (uiop:split-string text :separator '(#\Newline)))
        (let ((payload (string-trim '(#\Space #\Tab #\Return) line)))
          (when (uiop:string-prefix-p "data:" payload)
            (setf payload (string-trim '(#\Space #\Tab)
                                       (subseq payload 5))))
          (unless (or (zerop (length payload)) (equal payload "[DONE]"))
            (handler-case (push (jsown:parse payload) documents)
              (error () nil))))))
    documents))

(defun %token-usage (body)
  (let ((incoming nil) (outgoing nil))
    (dolist (document (%response-json-documents body))
      (dolist (candidate (%token-candidates document))
        (when (car candidate)
          (setf incoming (max (or incoming 0) (car candidate))))
        (when (cdr candidate)
          (setf outgoing (max (or outgoing 0) (cdr candidate))))))
    (values incoming outgoing)))

(defun %headers-json (headers)
  (let ((object (list :obj)))
    (when headers
      (maphash
       (lambda (name value)
         (push (cons name
                     (if (member name +secret-headers+ :test #'string-equal)
                         "<redacted>"
                         value))
               (cdr object)))
       headers))
    object))

(defun %utc-now ()
  (multiple-value-bind (second minute hour day month year)
      (decode-universal-time (get-universal-time) 0)
    (format nil "~4,'0D-~2,'0D-~2,'0DT~2,'0D:~2,'0D:~2,'0DZ"
            year month day hour minute second)))

(defun %event-id ()
  (format nil "cl-~36R-~36R-~36R"
          (get-universal-time)
          (get-internal-real-time)
          (random most-positive-fixnum)))

(defun make-capture-event
    (&key event-id provider upstream method path query request-headers request-body
          response-status response-headers response-body started-at completed-at
          latency-ms (transport "http"))
  (multiple-value-bind (input-tokens output-tokens) (%token-usage response-body)
    (let ((event
            (%jobj
             (cons "event_id" (or event-id (%event-id)))
             (cons "provider" provider)
             (cons "upstream" upstream)
             (cons "method" method)
             (cons "path" path)
             (cons "query" (or query ""))
             (cons "request_headers" (%headers-json request-headers))
             (cons "request_body" (%captured-body request-body))
             (cons "response_status" response-status)
             (cons "response_headers" (%headers-json response-headers))
             (cons "response_body" (%captured-body response-body))
             (cons "started_at" started-at)
             (cons "completed_at" completed-at)
             (cons "latency_ms" latency-ms)
             (cons "model" (%request-model request-body))
             (cons "request_sha256" (%sha256-hex request-body))
             (cons "response_sha256" (%sha256-hex response-body))
             (cons "transport" transport)
             (cons "input_tokens" input-tokens)
             (cons "output_tokens" output-tokens)
             (cons "total_tokens" (and input-tokens output-tokens
                                        (+ input-tokens output-tokens)))))))
      event)))

(defun append-capture-event (data-directory event)
  "Append one complete capture atomically with respect to other relay threads."
  (let* ((root (uiop:ensure-directory-pathname data-directory))
         (path (merge-pathnames #P"events.jsonl" root)))
    (ensure-directories-exist path)
    (bt:with-lock-held (*recorder-lock*)
      (with-open-file (stream path :direction :output :if-exists :append
                                  :if-does-not-exist :create
                                  :external-format :utf-8)
        (write-line (jsown:to-json event) stream)
        (finish-output stream)))
    path))

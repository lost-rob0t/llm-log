(in-package #:llm-log)

(defparameter +quota-max-bytes+ 262144)
(defparameter +quota-interval-seconds+ 60)
(defparameter *quota-lock* (bt:make-lock "llm-log-quota-snapshot"))
(defparameter *quota-thread* nil)
(defparameter *quota-stop* nil)
(defparameter *quota-providers* nil)

(defun %unix-time () (- (get-universal-time) 2208988800))

(defun %quota-window (id meter label percent &key seconds resets-at (state "ok"))
  (%jobj (cons "id" id) (cons "meter" meter) (cons "label" label)
         (cons "used_percent" percent) (cons "window_seconds" seconds)
         (cons "resets_at" resets-at)
         (cons "state" (if (and (equal state "ok") (null percent)) "unknown" state))))

(defun %quota-provider (id label scope source windows &key plan (state "ok") error)
  (%jobj (cons "id" id) (cons "label" label) (cons "scope" scope)
         (cons "source" source) (cons "plan" (or plan "unknown"))
         (cons "updated_at" (if (equal state "ok") (%unix-time) 0))
         (cons "state" state) (cons "error" error) (cons "windows" windows)))

(defun %quota-unavailable (id &optional error)
  (if (equal id "zai")
      (%quota-provider "zai" "z.AI" "coding-plan" "zai-monitor" nil
                       :state "unavailable" :error error)
      (%quota-provider "gpt" "GPT" "account-rate-limits" "codex-app-server" nil
                       :state "unavailable" :error error)))

(defun %read-limited-stream (stream)
  (let ((out (make-array 4096 :element-type '(unsigned-byte 8)
                         :fill-pointer 0 :adjustable t)))
    (loop with buffer = (make-array 16384 :element-type '(unsigned-byte 8))
          for n = (read-sequence buffer stream)
          until (zerop n)
          do (when (> (+ (length out) n) +quota-max-bytes+)
               (error "quota response exceeds ~D bytes" +quota-max-bytes+))
             (loop for i below n do (vector-push-extend (aref buffer i) out)))
    out))

(defun %simple-json-get (url headers)
  (multiple-value-bind (stream socket) (%open-upstream url)
    (unwind-protect
         (let* ((uri (quri:uri url))
                (path (or (quri:uri-path uri) "/"))
                (query (quri:uri-query uri))
                (target (if query (format nil "~A?~A" path query) path)))
           (write-sequence
            (%utf8-octets
             (format nil "GET ~A HTTP/1.1~C~CHost: ~A~C~CConnection: close~C~C"
                     target #\Return #\Linefeed (%upstream-host-header uri)
                     #\Return #\Linefeed #\Return #\Linefeed))
            stream)
           (dolist (header headers)
             (write-sequence
              (%utf8-octets
               (format nil "~A: ~A~C~C" (car header) (cdr header)
                       #\Return #\Linefeed))
              stream))
           (write-sequence (%crlf) stream)
           (force-output stream)
           (let* ((head (%read-head-octets stream))
                  (lines (loop for line in
                                  (uiop:split-string
                                   (trivial-utf-8:utf-8-bytes-to-string head)
                                   :separator (format nil "~C~C" #\Return #\Linefeed))
                               when (plusp (length line)) collect line)))
             (multiple-value-bind (status response-headers)
                 (%response-head-metadata lines)
               (declare (ignore response-headers))
               (unless (= status 200)
                 (error "quota provider returned HTTP ~D" status))
               (jsown:parse
                (trivial-utf-8:utf-8-bytes-to-string
                 (%read-limited-stream stream))))))
      (ignore-errors (close stream))
      (when socket (ignore-errors (usocket:socket-close socket))))))

(defun %zai-api-key ()
  (let ((key (uiop:getenv "ZAI_API_KEY"))
        (file (uiop:getenv "LLM_LOG_ZAI_KEY_FILE")))
    (when (and file (plusp (length file)))
      (setf key (string-trim '(#\Space #\Tab #\Newline #\Return)
                             (uiop:read-file-string file))))
    (unless (and key (plusp (length key)))
      (error "z.AI credentials unavailable"))
    key))

(defun %window-label (seconds)
  (cond ((= seconds 604800) "week")
        ((zerop (mod seconds 3600)) (format nil "~Dh" (/ seconds 3600)))
        ((zerop (mod seconds 60)) (format nil "~Dm" (/ seconds 60)))
        (t (format nil "~Ds" seconds))))

(defun %zai-quota-provider ()
  (let* ((payload (%simple-json-get
                   "https://api.z.ai/api/monitor/usage/quota/limit"
                   (list (cons "Authorization" (%zai-api-key))
                         (cons "Accept" "application/json"))))
         (data (or (jsown:val-safe payload "data") payload))
         (limits (jsown:val-safe data "limits"))
         (windows nil))
    (unless (listp limits) (error "invalid z.AI quota response"))
    (dolist (row limits)
      (when (%json-object-p row)
        (let* ((kind (jsown:val-safe row "type"))
               (unit (jsown:val-safe row "unit"))
               (number (jsown:val-safe row "number"))
               (seconds
                 (cond ((and (equal kind "TOKENS_LIMIT")
                             (or (and (eql unit 3) (eql number 5))
                                 (and (null unit) (null number)))) 18000)
                       ((and (equal kind "TOKENS_LIMIT")
                             (eql unit 6) (eql number 1)) 604800)
                       (t nil)))
               (percent (let ((p (jsown:val-safe row "percentage")))
                          (and (numberp p) (<= 0 p 100) p)))
               (raw-reset (jsown:val-safe row "nextResetTime"))
               (reset (and (numberp raw-reset)
                           (if (>= raw-reset 100000000000)
                               (/ raw-reset 1000.0)
                               raw-reset))))
          (when (or seconds (equal kind "TIME_LIMIT"))
            (push (%quota-window
                   (format nil "coding-plan:~A" (or seconds kind))
                   "coding-plan"
                   (if seconds (%window-label seconds) "MCP month")
                   percent :seconds seconds :resets-at reset)
                  windows)))))
    (%quota-provider "zai" "z.AI" "coding-plan" "zai-monitor"
                     (nreverse windows)
                     :plan (or (jsown:val-safe data "planType")
                               (jsown:val-safe data "planName")
                               "unknown"))))

(defun %codex-send (stream object)
  (write-line (jsown:to-json object) stream)
  (force-output stream))

(defun %codex-request (input output method id &optional params)
  (%codex-send input
               (%jobj (cons "id" id) (cons "method" method)
                      (cons "params" (or params (%jobj)))))
  (loop repeat 128
        for line = (read-line output nil nil)
        while line
        for message = (jsown:parse line)
        do (cond
             ((and (jsown:val-safe message "method")
                   (jsown:val-safe message "id"))
              (%codex-send
               input
               (%jobj
                (cons "id" (jsown:val-safe message "id"))
                (cons "error" (%jobj (cons "code" -32601)
                                      (cons "message" "read-only quota client"))))))
             ((eql (jsown:val-safe message "id") id)
              (when (jsown:val-safe message "error")
                (error "codex account query failed"))
              (return (jsown:val-safe message "result"))))
        finally (error "codex app-server response limit exceeded")))

(defun %gpt-window (meter slot row)
  (let* ((minutes (jsown:val-safe row "windowDurationMins"))
         (seconds (and (numberp minutes) (* minutes 60)))
         (percent (jsown:val-safe row "usedPercent")))
    (%quota-window
     (format nil "~A:~A" meter slot) meter
     (if seconds (%window-label seconds) "period?")
     (and (numberp percent) (<= 0 percent 100) percent)
     :seconds seconds :resets-at (jsown:val-safe row "resetsAt"))))

(defun %gpt-quota-provider ()
  (let* ((binary (or (uiop:getenv "LLM_LOG_CODEX_BIN") "codex"))
         (process (uiop:launch-program (list binary "app-server")
                                       :input :stream :output :stream
                                       :error-output nil))
         (input (uiop:process-info-input process))
         (output (uiop:process-info-output process)))
    (unwind-protect
         (sb-ext:with-timeout 15
           (%codex-request
            input output "initialize" 1
            (%jobj (cons "clientInfo"
                         (%jobj (cons "name" "llm-log-quota")
                                (cons "version" "1.0.0")))))
           (%codex-send input (%jobj (cons "method" "initialized")))
           (let* ((account-result (%codex-request
                                   input output "account/read" 2
                                   (%jobj (cons "refreshToken" nil))))
                  (account (jsown:val-safe account-result "account")))
             (unless (and (%json-object-p account)
                          (equal (jsown:val-safe account "type") "chatgpt"))
               (error "ChatGPT login required"))
             (let* ((limits (%codex-request input output "account/rateLimits/read" 3))
                    (groups (jsown:val-safe limits "rateLimitsByLimitId"))
                    (windows nil))
               (when (%json-object-p groups)
                 (dolist (pair (rest groups))
                   (let* ((group (cdr pair))
                          (meter (or (jsown:val-safe group "limitId") (car pair))))
                     (dolist (slot '("primary" "secondary"))
                       (let ((row (jsown:val-safe group slot)))
                         (when (%json-object-p row)
                           (push (%gpt-window meter slot row) windows)))))))
               (%quota-provider
                "gpt" "GPT" "account-rate-limits" "codex-app-server"
                (nreverse windows)
                :plan (or (jsown:val-safe account "planType") "unknown")))))
      (ignore-errors (uiop:terminate-process process))
      (ignore-errors (uiop:wait-process process)))))

(defun %refresh-quota-provider (id thunk)
  (handler-case (funcall thunk)
    (error (condition)
      (%quota-unavailable id (princ-to-string condition)))))

(defun %refresh-quotas ()
  (let ((providers
          (list (%refresh-quota-provider "zai" #'%zai-quota-provider)
                (%refresh-quota-provider "gpt" #'%gpt-quota-provider))))
    (bt:with-lock-held (*quota-lock*)
      (setf *quota-providers* providers))))

(defun %quota-snapshot-json ()
  (bt:with-lock-held (*quota-lock*)
    (%jobj
     (cons "schema_version" 1)
     (cons "generated_at" (%unix-time))
     (cons "stale_after_seconds" (* +quota-interval-seconds+ 3))
     (cons "providers"
           (or *quota-providers*
               (list (%quota-unavailable "zai") (%quota-unavailable "gpt")))))))

(defun start-quota-collector ()
  (unless (and *quota-thread* (bt:thread-alive-p *quota-thread*))
    (setf *quota-stop* nil)
    (setf *quota-thread*
          (bt:make-thread
           (lambda ()
             (loop until *quota-stop*
                   do (%refresh-quotas)
                      (loop repeat +quota-interval-seconds+
                            until *quota-stop* do (sleep 1))))
           :name "llm-log-quota-collector")))
  *quota-thread*)

(defun stop-quota-collector ()
  (setf *quota-stop* t)
  (when *quota-thread*
    (ignore-errors (bt:join-thread *quota-thread*))
    (setf *quota-thread* nil))
  t)

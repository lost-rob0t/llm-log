(in-package #:llm-log-expert-integration-test)

(defun %corpus-event (id input output)
  (jsown:new-js
    ("event_id" id)
    ("provider" "openrouter")
    ("upstream" "https://openrouter.ai")
    ("method" "POST")
    ("path" "/api/v1/chat/completions")
    ("query" "")
    ("started_at" "2026-09-15T01:00:00Z")
    ("completed_at" "2026-09-15T01:00:01Z")
    ("latency_ms" 1000)
    ("model" "fixture-model")
    ("request_sha256" (make-string 64 :initial-element #\a))
    ("response_sha256" (make-string 64 :initial-element #\b))
    ("response_status" 200)
    ("transport" "http")
    ("input_tokens" input)
    ("output_tokens" output)
    ("total_tokens" (+ input output))
    ("request_body"
      (jsown:new-js
        ("encoding" "utf-8")
        ("text" (format nil
                         "{\"messages\":[{\"role\":\"user\",\"content\":\"fix code ~A\"}]}"
                         id))))
    ("response_body"
      (jsown:new-js ("encoding" "utf-8") ("text" "{}")))))

(defun %append-corpus-event (path event)
  (with-open-file (stream path :direction :output :if-exists :append
                               :if-does-not-exist :create
                               :external-format :utf-8)
    (write-line (jsown:to-json event) stream)))

(rove:deftest common-lisp-bulk-load-and-infill
  (let* ((root (uiop:ensure-directory-pathname
                (merge-pathnames
                 (format nil "llm-log-cl-corpus-~A/" (gensym))
                 (uiop:temporary-directory))))
         (source (merge-pathnames #P"events.jsonl" root))
         (data-dir (merge-pathnames #P"expert/" root))
         (host nil))
    (unwind-protect
         (progn
           (ensure-directories-exist source)
           (%append-corpus-event source (%corpus-event "evt-bulk-1" 10 2))
           (%append-corpus-event source (%corpus-event "evt-bulk-2" 20 4))

           (setf host (llm-log-expert:start-expert-host data-dir))
           (let ((result
                   (llm-log-expert:import-capture-corpus
                    host source :from-start t :checkpoint-every 1)))
             (rove:ok (= 2 (jsown:val-safe result "replayed")))
             (rove:ok (= 2 (jsown:val-safe result "classified")))
             (rove:ok (= 2 (jsown:val-safe result "usage_projected"))))

           (let ((summary (llm-log-expert:query-analytics-summary host)))
             (rove:ok (= 2 (jsown:val-safe summary "request_count")))
             (rove:ok (= 30 (jsown:val-safe summary "input_tokens")))
             (rove:ok (= 6 (jsown:val-safe summary "output_tokens"))))

           ;; Restart proves the checkpoint and analytics are durable, not
           ;; merely process-local counters.
           (llm-log-expert:stop-expert-host host)
           (setf host (llm-log-expert:start-expert-host data-dir))

           (let ((empty-infill
                   (llm-log-expert:import-capture-corpus
                    host source :require-checkpoint t :checkpoint-every 1)))
             (rove:ok (= 0 (jsown:val-safe empty-infill "replayed"))))

           (%append-corpus-event source (%corpus-event "evt-infill-3" 7 3))
           (let ((infill
                   (llm-log-expert:import-capture-corpus
                    host source :require-checkpoint t :checkpoint-every 1)))
             (rove:ok (= 1 (jsown:val-safe infill "replayed")))
             (rove:ok (= 1 (jsown:val-safe infill "classified"))))

           (let ((summary (llm-log-expert:query-analytics-summary host)))
             (rove:ok (= 3 (jsown:val-safe summary "request_count")))
             (rove:ok (= 37 (jsown:val-safe summary "input_tokens")))
             (rove:ok (= 9 (jsown:val-safe summary "output_tokens")))))
      (when host (ignore-errors (llm-log-expert:stop-expert-host host)))
      (ignore-errors
        (uiop:delete-directory-tree root :validate t :if-does-not-exist :ignore)))))

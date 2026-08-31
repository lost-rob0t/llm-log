(in-package #:llm-log-expert-integration-test)

(defun classifier-time-request (event-id user-message-id request-id started-at)
  (let ((request
          (classifier-query-request
           event-id user-message-id request-id
           "Explain the classifier timestamp query contract."
           "openrouter" "fixture/time-model")))
    (setf (jsown:val (jsown:val request "payload") "started_at") started-at)
    request))

(rove:deftest request-classifier-time-range-red-contract
  (let* ((data-dir (uiop:ensure-directory-pathname
                    (merge-pathnames
                     (format nil "llm-log-classifier-time-red-~A/" (gensym))
                     (uiop:temporary-directory))))
         (host (llm-log-expert:start-expert-host data-dir)))
    (unwind-protect
         (progn
           (dolist (request
                    (list
                     (classifier-time-request
                      "evt-time-10" "msg-time-10" "req-time-10"
                      "2026-08-31T10:00:00Z")
                     (classifier-time-request
                      "evt-time-11" "msg-time-11" "req-time-11"
                      "2026-08-31T11:00:00Z")
                     (classifier-time-request
                      "evt-time-12" "msg-time-12" "req-time-12"
                      "2026-08-31T12:00:00Z")))
             (rove:ok
              (equal "ok"
                     (jsown:val-safe
                      (llm-log-expert:dispatch-expert-request host request)
                      "status"))))

           ;; Timestamp indexes must remain usable after the expert host restarts.
           (llm-log-expert:stop-expert-host host)
           (setf host (llm-log-expert:start-expert-host data-dir))

           (let* ((query
                    (jsown:new-js
                      ("version" 1)
                      ("operation" "query_classification_history")
                      ("event_id" "evt-time-history")
                      ("payload"
                       (jsown:new-js
                         ("started_at_gte" "2026-08-31T10:30:00Z")
                         ("started_at_lt" "2026-08-31T12:00:00Z")
                         ("limit" 32)))))
                  (reply (llm-log-expert:dispatch-expert-request host query))
                  (result (jsown:val-safe reply "result"))
                  (assertions (and result (jsown:val-safe result "assertions"))))
             (rove:ok (equal "ok" (jsown:val-safe reply "status")))
             (rove:ok (plusp (length assertions)))
             (rove:ok
              (every (lambda (assertion)
                       (equal "req-time-11"
                              (jsown:val-safe assertion "request_id")))
                     assertions))
             (rove:ok
              (every (lambda (assertion)
                       (equal "2026-08-31T11:00:00Z"
                              (jsown:val-safe assertion "started_at")))
                     assertions))
             (rove:ok
              (notany (lambda (assertion)
                        (equal "req-time-12"
                               (jsown:val-safe assertion "request_id")))
                      assertions)))

           ;; An inverted timestamp range is invalid and must never trigger a scan.
           (let* ((bad-query
                    (jsown:new-js
                      ("version" 1)
                      ("operation" "query_classification_history")
                      ("event_id" "evt-time-inverted")
                      ("payload"
                       (jsown:new-js
                         ("started_at_gte" "2026-08-31T12:00:00Z")
                         ("started_at_lt" "2026-08-31T10:00:00Z")
                         ("limit" 8)))))
                  (reply (llm-log-expert:dispatch-expert-request host bad-query)))
             (rove:ok (equal "error" (jsown:val-safe reply "status")))))
      (ignore-errors (llm-log-expert:stop-expert-host host))
      (ignore-errors
        (uiop:delete-directory-tree data-dir :validate t :if-does-not-exist :ignore)))))

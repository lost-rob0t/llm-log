(in-package #:llm-log-expert-integration-test)

(defun classifier-task-request (event-id user-message-id request-id task-id message)
  (jsown:new-js
    ("version" 1)
    ("operation" "query_classification")
    ("event_id" event-id)
    ("payload"
     (jsown:new-js
       ("user_message_id" user-message-id)
       ("request_id" request-id)
       ("task_id" task-id)
       ("message" message)
       ("provider" "openrouter")
       ("model" "fixture/model-task")
       ("client" "gptel")))))

(defun classification-task-history-request (event-id task-id limit)
  (jsown:new-js
    ("version" 1)
    ("operation" "query_classification_history")
    ("event_id" event-id)
    ("payload"
     (jsown:new-js
       ("task_id" task-id)
       ("limit" limit)))))

(rove:deftest request-classifier-task-id-history-red-contract
  (let* ((data-dir (uiop:ensure-directory-pathname
                    (merge-pathnames
                     (format nil "llm-log-classifier-task-red-~A/" (gensym))
                     (uiop:temporary-directory))))
         (host (llm-log-expert:start-expert-host data-dir)))
    (unwind-protect
         (progn
           (dolist (request
                    (list
                     (classifier-task-request
                      "evt-task-a1" "msg-task-a1" "req-task-a1" "task-alpha"
                      "Fix the classifier task query and add tests.")
                     (classifier-task-request
                      "evt-task-a2" "msg-task-a2" "req-task-a2" "task-alpha"
                      "Explain the classifier task query behavior.")
                     (classifier-task-request
                      "evt-task-b1" "msg-task-b1" "req-task-b1" "task-beta"
                      "Review an unrelated classifier request.")))
             (rove:ok
              (equal "ok"
                     (jsown:val-safe
                      (llm-log-expert:dispatch-expert-request host request)
                      "status"))))

           (llm-log-expert:stop-expert-host host)
           (setf host (llm-log-expert:start-expert-host data-dir))

           (let* ((reply
                    (llm-log-expert:dispatch-expert-request
                     host
                     (classification-task-history-request
                      "evt-task-history-alpha" "task-alpha" 32)))
                  (result (jsown:val-safe reply "result"))
                  (assertions (and result (jsown:val-safe result "assertions"))))
             ;; RED on baseline: task_id is not a declared bounded selector.
             (rove:ok (equal "ok" (jsown:val-safe reply "status")))
             (rove:ok (listp assertions))
             (rove:ok (plusp (length assertions)))
             (rove:ok (<= (length assertions) 32))
             (dolist (assertion assertions)
               (rove:ok (equal "task-alpha"
                               (jsown:val-safe assertion "task_id")))
               (rove:ok
                (member (jsown:val-safe assertion "request_id")
                        '("req-task-a1" "req-task-a2")
                        :test #'equal))
               (rove:ok
                (not (equal "req-task-b1"
                            (jsown:val-safe assertion "request_id"))))
               (rove:ok (stringp (jsown:val-safe assertion "event_id")))
               (rove:ok (stringp (jsown:val-safe assertion "user_message_id")))
               (rove:ok (stringp (jsown:val-safe assertion "rule_id")))
               (rove:ok (jsown:val-safe assertion "rule_version"))
               (rove:ok (consp (jsown:val-safe assertion "evidence_ids")))
               (rove:ok (jsown:val-safe assertion "expert_version"))))

           (let* ((reply
                    (llm-log-expert:dispatch-expert-request
                     host
                     (classification-task-history-request
                      "evt-task-history-missing" "task-does-not-exist" 8)))
                  (result (jsown:val-safe reply "result"))
                  (assertions (and result (jsown:val-safe result "assertions"))))
             (rove:ok (equal "ok" (jsown:val-safe reply "status")))
             (rove:ok (null assertions))))
      (ignore-errors (llm-log-expert:stop-expert-host host))
      (ignore-errors
        (uiop:delete-directory-tree data-dir :validate t :if-does-not-exist :ignore)))))

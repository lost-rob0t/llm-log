(in-package #:llm-log-expert-integration-test)

(defun %pagination-evidence (index)
  (jsown:new-js
    ("evidence_id" (format nil "page-evidence-~3,'0D" index))
    ("observed_at" "2026-09-14T02:00:00Z")
    ("evidence_type" "test_result")
    ("authority" "authoritative")
    ("observed_value" "success")
    ("source_id" (format nil "page-source-~3,'0D" index))))

(defun %pagination-record (index)
  (jsown:new-js
    ("version" 1)
    ("operation" "record_outcome_evidence")
    ("event_id" (format nil "page-event-~3,'0D" index))
    ("payload"
     (jsown:new-js
       ("scope" "request")
       ("scope_id" (format nil "page-request-~3,'0D" index))
       ("evidence" (list (%pagination-evidence index)))))))

(defun %pagination-query (cursor)
  (let ((payload
          (jsown:new-js
            ("outcome" "success")
            ("scope" "request")
            ("limit" 64))))
    (when cursor
      (jsown:extend-js payload ("cursor" cursor)))
    (jsown:new-js
      ("version" 1)
      ("operation" "query_outcome_dataset")
      ("event_id" "page-query")
      ("payload" payload))))

(rove:deftest outcome-dataset-pagination-red-contract
  (let* ((data-dir
           (uiop:ensure-directory-pathname
            (merge-pathnames
             (format nil "llm-log-outcome-page-red-~A/" (gensym))
             (uiop:temporary-directory))))
         (host (llm-log-expert:start-expert-host data-dir)))
    (unwind-protect
         (progn
           ;; Cross the historical 256-candidate ceiling so this proves real
           ;; corpus traversal rather than cosmetic next_cursor metadata.
           (loop for index from 0 below 257
                 do (rove:ok
                     (equal "ok"
                            (jsown:val-safe
                             (llm-log-expert:dispatch-expert-request
                              host (%pagination-record index))
                             "status"))))

           (let ((cursor nil)
                 (seen (make-hash-table :test #'equal))
                 (count 0)
                 (pages 0)
                 (done nil))
             (loop until done
                   do (incf pages)
                      (let* ((reply
                               (llm-log-expert:dispatch-expert-request
                                host (%pagination-query cursor)))
                             (result (jsown:val-safe reply "result"))
                             (examples (jsown:val-safe result "examples"))
                             (truncated (jsown:val-safe result "truncated"))
                             (next (jsown:val-safe result "next_cursor")))
                        (rove:ok (equal "ok" (jsown:val-safe reply "status")))
                        (rove:ok (<= (length examples) 64))
                        (dolist (example examples)
                          (let ((id (jsown:val-safe example "assertion_id")))
                            (rove:ok (not (gethash id seen)))
                            (setf (gethash id seen) t)
                            (incf count)))
                        (if truncated
                            (progn
                              (rove:ok (and (stringp next) (plusp (length next))))
                              (rove:ok (not (equal cursor next)))
                              (setf cursor next))
                            (setf done t))))
             (rove:ok (> pages 4))
             (rove:ok (= 257 count))))
      (ignore-errors (llm-log-expert:stop-expert-host host))
      (ignore-errors
        (uiop:delete-directory-tree
         data-dir :validate t :if-does-not-exist :ignore)))))

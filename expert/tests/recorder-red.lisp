(in-package #:llm-log-expert-test)

(defun %expert-function (name)
  (let ((symbol (find-symbol name :llm-log-expert)))
    (and symbol (fboundp symbol) (symbol-function symbol))))

(defun %expert-variable-symbol (name)
  (let ((symbol (find-symbol name :llm-log-expert)))
    (and symbol (boundp symbol) symbol)))

(defun %required-expert-function (name)
  (or (%expert-function name)
      (error "Missing recorder contract function LLM-LOG-EXPERT::~A" name)))

(defun %event-directories (data-directory)
  (let ((root (merge-pathnames #P"capture/http/events/"
                               (uiop:ensure-directory-pathname data-directory))))
    (if (probe-file root)
        (uiop:subdirectories root)
        '())))

(defun %event-metadata (event-directory)
  (uiop:read-file-string (merge-pathnames #P"metadata.json" event-directory)))

(defun %event-body (event-directory name)
  (%slurp-binary-file (merge-pathnames name event-directory)))

(defun %record-fixture-event (record recorder index)
  (funcall record recorder
           :metadata (format nil "{\"schema\":\"llm-log.capture/http-v1\",\"fixture\":~D}" index)
           :request-body (%octets index 0 (+ index 1))
           :response-body (%octets (+ index 100) 0 (+ index 101))))

(defun run-recorder-concurrency-contract ()
  (let* ((start (%required-expert-function "START-RECORDER"))
         (stop (%required-expert-function "STOP-RECORDER"))
         (record (%required-expert-function "RECORD-HTTP-CAPTURE"))
         (data-directory
           (merge-pathnames
            (format nil "llm-log-recorder-red-~A/" (gensym))
            (uiop:temporary-directory)))
         (recorder nil)
         (producer-errors '())
         (producer-error-lock (sb-thread:make-mutex :name "recorder-red-errors")))
    (unwind-protect
         (progn
           (setf recorder (funcall start data-directory))
           (let ((threads
                   (loop for index below 12
                         collect
                         (sb-thread:make-thread
                          (lambda ()
                            (handler-case
                                (%record-fixture-event record recorder index)
                              (error (condition)
                                (sb-thread:with-mutex (producer-error-lock)
                                  (push condition producer-errors)))))
                          :name (format nil "llm-log-recorder-producer-~D" index)))))
             (dolist (thread threads)
               (sb-thread:join-thread thread)))
           (ok (null producer-errors)
               "all concurrent recorder submissions must succeed")
           (let ((events (%event-directories data-directory)))
             (ok (= 12 (length events))
                 "twelve acknowledged submissions must publish exactly twelve committed events")
             (dolist (event events)
               (ok (probe-file (merge-pathnames #P"metadata.json" event)))
               (ok (probe-file (merge-pathnames #P"request-body.bin" event)))
               (ok (probe-file (merge-pathnames #P"response-body.bin" event)))
               (let* ((metadata (%event-metadata event))
                      (marker-start (search "\"fixture\":" metadata)))
                 (ok marker-start "every committed event must retain its fixture identity")
                 (when marker-start
                   (let* ((number-start (+ marker-start (length "\"fixture\":")))
                          (index (parse-integer metadata :start number-start :junk-allowed t)))
                     (ok (equalp (%octets index 0 (+ index 1))
                                 (%event-body event #P"request-body.bin"))
                         "request bytes may not cross-contaminate between concurrent events")
                     (ok (equalp (%octets (+ index 100) 0 (+ index 101))
                                 (%event-body event #P"response-body.bin"))
                         "response bytes may not cross-contaminate between concurrent events")))))))
      (when recorder (ignore-errors (funcall stop recorder)))
      (ignore-errors (uiop:delete-directory-tree data-directory :validate t)))))

(defun run-recorder-crash-contract ()
  (let* ((start (%required-expert-function "START-RECORDER"))
         (stop (%required-expert-function "STOP-RECORDER"))
         (record (%required-expert-function "RECORD-HTTP-CAPTURE"))
         (fault-symbol (%expert-variable-symbol "*RECORDER-TEST-FAIL-BEFORE-PUBLISH*"))
         (data-directory
           (merge-pathnames
            (format nil "llm-log-recorder-crash-red-~A/" (gensym))
            (uiop:temporary-directory)))
         (recorder nil))
    (unless fault-symbol
      (error "Missing deterministic recorder fault-injection hook LLM-LOG-EXPERT::*RECORDER-TEST-FAIL-BEFORE-PUBLISH*"))
    (unwind-protect
         (progn
           (setf recorder (funcall start data-directory))
           (%record-fixture-event record recorder 1)
           (let ((before (length (%event-directories data-directory))))
             (ok (= 1 before) "control event must commit before injected failure")
             (let ((failed nil))
               (let ((old-value (symbol-value fault-symbol)))
                 (unwind-protect
                      (progn
                        (setf (symbol-value fault-symbol) t)
                        (handler-case
                            (%record-fixture-event record recorder 2)
                          (error () (setf failed t))))
                   (setf (symbol-value fault-symbol) old-value)))
               (ok failed "failure-before-publish must return failure, never false success")
               (ok (= before (length (%event-directories data-directory)))
                   "failed staging work must not create a partial committed event"))
             (%record-fixture-event record recorder 3)
             (ok (= 2 (length (%event-directories data-directory)))
                 "recorder must accept a later command after bounded failure recovery")))
      (when recorder (ignore-errors (funcall stop recorder)))
      (ignore-errors (uiop:delete-directory-tree data-directory :validate t)))))

(deftest common-lisp-recorder-concurrency-red
  (testing "single-writer recorder serializes concurrent immutable event publication"
    (run-recorder-concurrency-contract)))

(deftest common-lisp-recorder-crash-red
  (testing "failure before publish cannot expose a partial committed event"
    (run-recorder-crash-contract)))

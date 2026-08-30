(in-package #:llm-log-expert)

(defvar *recorder-test-fail-before-publish* nil)

(defstruct (capture-recorder (:constructor %make-capture-recorder))
  system
  actor
  data-directory)

(defun %recorder-root (data-directory name)
  (merge-pathnames
   (make-pathname :directory `(:relative "capture" "http" ,name))
   (uiop:ensure-directory-pathname data-directory)))

(defun %fresh-recorder-event-id ()
  (format nil "~36R-~36R"
          (get-universal-time)
          (random most-positive-fixnum)))

(defun %write-recorder-binary (path octets)
  (with-open-file (stream path
                          :direction :output
                          :element-type '(unsigned-byte 8)
                          :if-does-not-exist :create
                          :if-exists :error)
    (write-sequence octets stream)
    (finish-output stream)))

(defun %write-recorder-text (path text)
  (with-open-file (stream path
                          :direction :output
                          :external-format :utf-8
                          :if-does-not-exist :create
                          :if-exists :error)
    (write-string text stream)
    (terpri stream)
    (finish-output stream)))

(defun %publish-recorder-event (data-directory metadata request-body response-body)
  (let* ((event-id (%fresh-recorder-event-id))
         (staging-root (%recorder-root data-directory "staging"))
         (events-root (%recorder-root data-directory "events"))
         (staging (merge-pathnames
                   (make-pathname :directory `(:relative ,event-id))
                   staging-root))
         (committed (merge-pathnames
                     (make-pathname :directory `(:relative ,event-id))
                     events-root)))
    (ensure-directories-exist (merge-pathnames #P".keep" staging-root))
    (ensure-directories-exist (merge-pathnames #P".keep" events-root))
    (ensure-directories-exist (merge-pathnames #P"metadata.json" staging))
    (unwind-protect
         (progn
           (%write-recorder-text (merge-pathnames #P"metadata.json" staging) metadata)
           (%write-recorder-binary (merge-pathnames #P"request-body.bin" staging) request-body)
           (%write-recorder-binary (merge-pathnames #P"response-body.bin" staging) response-body)
           (when *recorder-test-fail-before-publish*
             (error "Injected recorder failure before publish"))
           (rename-file staging committed)
           (setf staging nil)
           event-id)
      (when (and staging (probe-file staging))
        (ignore-errors
          (uiop:delete-directory-tree staging :validate t))))))

(defun %recorder-receive (data-directory message)
  (handler-case
      (case (first message)
        (:record
         (list :ok
               (%publish-recorder-event data-directory
                                        (getf (rest message) :metadata)
                                        (getf (rest message) :request-body)
                                        (getf (rest message) :response-body))))
        (otherwise
         (list :error (format nil "Unknown recorder command: ~S" (first message)))))
    (error (condition)
      (list :error (princ-to-string condition)))))

(defun start-recorder (data-directory)
  (let* ((directory (uiop:ensure-directory-pathname data-directory))
         (system (asys:make-actor-system))
         (actor (ac:actor-of
                 system
                 :name "llm-log-recorder"
                 :receive (lambda (message)
                            (%recorder-receive directory message)))))
    (%make-capture-recorder :system system
                            :actor actor
                            :data-directory directory)))

(defun stop-recorder (recorder)
  (check-type recorder capture-recorder)
  (ac:shutdown (capture-recorder-system recorder) :wait t)
  t)

(defun record-http-capture (recorder &key metadata request-body response-body)
  (check-type recorder capture-recorder)
  (check-type metadata string)
  (let ((result
          (act:ask-s
           (capture-recorder-actor recorder)
           (list :record
                 :metadata metadata
                 :request-body request-body
                 :response-body response-body))))
    (if (and (consp result) (eq :ok (first result)))
        (second result)
        (error "Recorder command failed: ~A"
               (if (and (consp result) (eq :error (first result)))
                   (second result)
                   result)))))
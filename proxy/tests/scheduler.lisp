(in-package #:llm-log/tests)

(defun %wait-until (predicate &key (timeout 2.0d0))
  (loop with deadline = (+ (/ (get-internal-real-time)
                              (float internal-time-units-per-second 1d0))
                           timeout)
        when (funcall predicate)
          return t
        when (>= (/ (get-internal-real-time)
                    (float internal-time-units-per-second 1d0))
                 deadline)
          return nil
        do (sleep 0.01d0)))

(defun %scheduler-toml (&rest lines)
  (with-output-to-string (stream)
    (write-line "[scheduler]" stream)
    (dolist (line lines)
      (write-line line stream))))

(deftest scheduler-defaults-are-bounded
  (let* ((config (resolve-config :config-file nil))
         (scheduler (runtime-config-scheduler config)))
    (ok (eql (scheduler-config-max-active scheduler) 4))
    (ok (eql (scheduler-config-max-queue-depth scheduler) 32))
    (ok (eql (scheduler-config-queue-timeout-seconds scheduler) 30))
    (ok (eql (scheduler-config-retry-after-seconds scheduler) 1))))

(deftest scheduler-toml-overrides-defaults
  (let* ((config
           (parse-toml-config
            (%scheduler-toml
             "max_active = 2"
             "max_queue_depth = 7"
             "queue_timeout_seconds = 9"
             "retry_after_seconds = 3")))
         (scheduler (runtime-config-scheduler config)))
    (ok (eql (scheduler-config-max-active scheduler) 2))
    (ok (eql (scheduler-config-max-queue-depth scheduler) 7))
    (ok (eql (scheduler-config-queue-timeout-seconds scheduler) 9))
    (ok (eql (scheduler-config-retry-after-seconds scheduler) 3))))

(deftest scheduler-toml-rejects-invalid-values
  (ok (signals
       (parse-toml-config (%scheduler-toml "max_active = 0"))
       'invalid-configuration))
  (ok (signals
       (parse-toml-config (%scheduler-toml "max_queue_depth = -1"))
       'invalid-configuration))
  (ok (signals
       (parse-toml-config (%scheduler-toml "queue_timeout_seconds = -1"))
       'invalid-configuration))
  (ok (signals
       (parse-toml-config (%scheduler-toml "retry_after_seconds = 0"))
       'invalid-configuration))
  (ok (signals
       (parse-toml-config (%scheduler-toml "wat = 1"))
       'invalid-configuration)))

(deftest scheduler-admits-up-to-provider-limit
  (let ((scheduler
          (make-request-scheduler
           (make-scheduler-config :max-active 2
                                  :max-queue-depth 0
                                  :queue-timeout-seconds 0
                                  :retry-after-seconds 1))))
    (multiple-value-bind (first first-reason)
        (acquire-provider-slot scheduler "alpha")
      (ok first)
      (ok (eq first-reason :admitted)))
    (multiple-value-bind (second second-reason)
        (acquire-provider-slot scheduler "alpha")
      (ok second)
      (ok (eq second-reason :admitted)))
    (ok (eql (scheduler-provider-active-count scheduler "alpha") 2))
    (multiple-value-bind (third third-reason)
        (acquire-provider-slot scheduler "alpha")
      (ok (null third))
      (ok (eq third-reason :queue-full)))
    (release-provider-slot scheduler "alpha")
    (release-provider-slot scheduler "alpha")
    (ok (zerop (scheduler-provider-active-count scheduler "alpha")))))

(deftest scheduler-isolates-providers
  (let ((scheduler
          (make-request-scheduler
           (make-scheduler-config :max-active 1
                                  :max-queue-depth 0
                                  :queue-timeout-seconds 0
                                  :retry-after-seconds 1))))
    (ok (acquire-provider-slot scheduler "alpha"))
    (ok (acquire-provider-slot scheduler "beta"))
    (ok (eql (scheduler-provider-active-count scheduler "alpha") 1))
    (ok (eql (scheduler-provider-active-count scheduler "beta") 1))
    (release-provider-slot scheduler "alpha")
    (release-provider-slot scheduler "beta")))

(deftest scheduler-queue-full-rejects-without-consuming-slot
  (let* ((scheduler
           (make-request-scheduler
            (make-scheduler-config :max-active 1
                                   :max-queue-depth 1
                                   :queue-timeout-seconds 2
                                   :retry-after-seconds 1)))
         (waiter-result nil)
         (waiter nil))
    (ok (acquire-provider-slot scheduler "alpha"))
    (setf waiter
          (bt:make-thread
           (lambda ()
             (multiple-value-bind (admitted reason)
                 (acquire-provider-slot scheduler "alpha")
               (setf waiter-result (list admitted reason))
               (when admitted
                 (release-provider-slot scheduler "alpha"))))
           :name "llm-log-test-queued-waiter"))
    (ok (%wait-until
         (lambda ()
           (eql (scheduler-provider-queued-count scheduler "alpha") 1))))
    (multiple-value-bind (admitted reason)
        (acquire-provider-slot scheduler "alpha")
      (ok (null admitted))
      (ok (eq reason :queue-full)))
    (release-provider-slot scheduler "alpha")
    (bt:join-thread waiter)
    (ok (equal waiter-result '(t :admitted)))
    (ok (zerop (scheduler-provider-active-count scheduler "alpha")))
    (ok (zerop (scheduler-provider-queued-count scheduler "alpha")))))

(deftest scheduler-queue-timeout-does-not-start-request
  (let ((scheduler
          (make-request-scheduler
           (make-scheduler-config :max-active 1
                                  :max-queue-depth 1
                                  :queue-timeout-seconds 0
                                  :retry-after-seconds 1))))
    (ok (acquire-provider-slot scheduler "alpha"))
    (multiple-value-bind (admitted reason)
        (acquire-provider-slot scheduler "alpha")
      (ok (null admitted))
      (ok (eq reason :queue-timeout)))
    (ok (eql (scheduler-provider-active-count scheduler "alpha") 1))
    (ok (zerop (scheduler-provider-queued-count scheduler "alpha")))
    (release-provider-slot scheduler "alpha")))

(deftest scheduler-fifo-order-survives-contention
  (let* ((scheduler
           (make-request-scheduler
            (make-scheduler-config :max-active 1
                                   :max-queue-depth 2
                                   :queue-timeout-seconds 2
                                   :retry-after-seconds 1)))
         (order '())
         (order-lock (bt:make-lock "llm-log-test-order"))
         (second nil)
         (third nil))
    (labels ((run-waiter (id)
               (multiple-value-bind (admitted reason)
                   (acquire-provider-slot scheduler "alpha")
                 (declare (ignore reason))
                 (when admitted
                   (bt:with-lock-held (order-lock)
                     (setf order (append order (list id))))
                   (sleep 0.03d0)
                   (release-provider-slot scheduler "alpha")))))
      (ok (acquire-provider-slot scheduler "alpha"))
      (setf second
            (bt:make-thread (lambda () (run-waiter :second))
                            :name "llm-log-test-second"))
      (ok (%wait-until
           (lambda ()
             (eql (scheduler-provider-queued-count scheduler "alpha") 1))))
      (setf third
            (bt:make-thread (lambda () (run-waiter :third))
                            :name "llm-log-test-third"))
      (ok (%wait-until
           (lambda ()
             (eql (scheduler-provider-queued-count scheduler "alpha") 2))))
      (release-provider-slot scheduler "alpha")
      (bt:join-thread second)
      (bt:join-thread third)
      (ok (equal order '(:second :third)))
      (ok (zerop (scheduler-provider-active-count scheduler "alpha")))
      (ok (zerop (scheduler-provider-queued-count scheduler "alpha"))))))

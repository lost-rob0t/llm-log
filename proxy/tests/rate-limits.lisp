(in-package #:llm-log/tests)

(defmacro with-rate-clock ((scheduler now &rest config-arguments) &body body)
  `(let* ((,now 0)
          (,scheduler
            (make-request-scheduler
             (make-scheduler-config ,@config-arguments)
             :clock (lambda () ,now)
             :sleep-function (lambda (seconds) (incf ,now seconds)))))
     ,@body))

(deftest rate-config-is-positive-and-explicit
  (let ((config (runtime-config-scheduler (resolve-config :config-file nil))))
    (ok (= (llm-log::scheduler-config-requests-per-minute config) 60))
    (ok (= (llm-log::scheduler-config-burst config) 4)))
  (let ((config
          (runtime-config-scheduler
           (parse-toml-config
            (%scheduler-toml "requests_per_minute = 10" "burst = 2"
                             "[scheduler.provider_groups]"
                             "agentzero = \"glm\"" "opencode = \"glm\"")))))
    (ok (= (llm-log::scheduler-config-requests-per-minute config) 10))
    (ok (= (llm-log::scheduler-config-burst config) 2))
    (ok (equal (llm-log::scheduler-config-provider-groups config)
               '(("agentzero" . "glm") ("opencode" . "glm")))))
  (dolist (line '("requests_per_minute = 0" "requests_per_minute = -1"
                  "requests_per_minute = 1.5" "requests_per_minute = true"
                  "burst = 0" "burst = -1" "burst = \"2\""
                  "provider_groups = 1"))
    (ok (signals (parse-toml-config (%scheduler-toml line))
                 'invalid-configuration)))
  (ok (signals
       (parse-toml-config
        (%scheduler-toml "[scheduler.provider_groups]" "alpha = \"\""))
       'invalid-configuration)))

(deftest completed-requests-do-not-refund-quota
  (with-rate-clock (scheduler now :max-active 4 :max-queue-depth 0
                                 :requests-per-minute 10 :burst 2)
    (dotimes (index 2)
      (declare (ignorable index))
      (ok (acquire-provider-slot scheduler "alpha"))
      (release-provider-slot scheduler "alpha"))
    (ok (zerop (scheduler-provider-active-count scheduler "alpha")))
    (multiple-value-bind (admitted reason retry-after)
        (acquire-provider-slot scheduler "alpha")
      (ok (not admitted))
      (ok (eq reason :rate-limited))
      (ok (= retry-after 6)))
    (ok (zerop now))))

(deftest rate-refills-at-exact-boundary-and-rounds-retry-up
  (with-rate-clock (scheduler now :max-queue-depth 0
                                 :requests-per-minute 30 :burst 1)
    (ok (acquire-provider-slot scheduler "alpha"))
    (release-provider-slot scheduler "alpha")
    (setf now 199/100)
    (multiple-value-bind (admitted reason retry-after)
        (acquire-provider-slot scheduler "alpha")
      (declare (ignore reason))
      (ok (not admitted))
      (ok (= retry-after 1)))
    (setf now 2)
    (ok (acquire-provider-slot scheduler "alpha"))
    (release-provider-slot scheduler "alpha")))

(deftest idle-refill-never-exceeds-burst
  (with-rate-clock (scheduler now :max-queue-depth 0
                                 :requests-per-minute 60 :burst 2)
    (ok (acquire-provider-slot scheduler "alpha"))
    (release-provider-slot scheduler "alpha")
    (setf now 1000)
    (dotimes (index 2)
      (declare (ignorable index))
      (ok (acquire-provider-slot scheduler "alpha"))
      (release-provider-slot scheduler "alpha"))
    (ok (not (acquire-provider-slot scheduler "alpha")))))

(deftest backwards-clock-cannot-mint-quota
  (with-rate-clock (scheduler now :max-queue-depth 0
                                 :requests-per-minute 60 :burst 1)
    (setf now 10)
    (ok (acquire-provider-slot scheduler "alpha"))
    (release-provider-slot scheduler "alpha")
    (setf now 9)
    (ok (not (acquire-provider-slot scheduler "alpha")))
    (setf now 10)
    (ok (not (acquire-provider-slot scheduler "alpha")))
    (setf now 11)
    (ok (acquire-provider-slot scheduler "alpha"))
    (release-provider-slot scheduler "alpha")))

(deftest aliases-share-rate-and-active-limits-not-client-identity
  (with-rate-clock (scheduler now :max-active 1 :max-queue-depth 0
                                 :requests-per-minute 10 :burst 1
                                 :provider-groups
                                 '(("agentzero" . "glm")
                                   ("opencode" . "glm")))
    (ok (acquire-provider-slot scheduler "agentzero"))
    (ok (= (scheduler-provider-active-count scheduler "opencode") 1))
    (ok (not (acquire-provider-slot scheduler "opencode")))
    (release-provider-slot scheduler "agentzero")
    (ok (not (acquire-provider-slot scheduler "opencode")))
    ;; A group label is not an implicit mapping for an unrelated provider.
    (ok (acquire-provider-slot scheduler "glm"))
    (release-provider-slot scheduler "glm")
    (setf now 6)
    (ok (acquire-provider-slot scheduler "opencode"))
    (release-provider-slot scheduler "opencode")))

(deftest scheduler-snapshots-mutable-policy
  (let* ((alias (copy-seq "agentzero"))
         (group (copy-seq "glm"))
         (config (make-scheduler-config
                  :max-queue-depth 0 :requests-per-minute 1 :burst 1
                  :provider-groups (list (cons alias group)
                                        (cons "opencode" "glm"))))
         (scheduler (make-request-scheduler config :clock (lambda () 0))))
    (ok (acquire-provider-slot scheduler "agentzero"))
    (release-provider-slot scheduler "agentzero")
    (setf (llm-log::scheduler-config-burst config) 100
          (char alias 0) #\X
          (char group 0) #\X)
    (ok (not (acquire-provider-slot scheduler "agentzero")))
    (ok (not (acquire-provider-slot scheduler "opencode")))))

(deftest queued-rate-request-admits-on-refill
  (with-rate-clock (scheduler now :max-queue-depth 1 :queue-timeout-seconds 3
                                 :requests-per-minute 60 :burst 1)
    (ok (acquire-provider-slot scheduler "alpha"))
    (release-provider-slot scheduler "alpha")
    (ok (acquire-provider-slot scheduler "alpha"))
    (ok (>= now 1))
    (ok (< now 3))
    (release-provider-slot scheduler "alpha")
    (ok (zerop (scheduler-provider-queued-count scheduler "alpha")))))

(deftest deadline-wins-over-simultaneous-refill
  (with-rate-clock (scheduler now :max-queue-depth 1 :queue-timeout-seconds 1
                                 :requests-per-minute 60 :burst 1)
    (ok (acquire-provider-slot scheduler "alpha"))
    (release-provider-slot scheduler "alpha")
    (multiple-value-bind (admitted reason)
        (acquire-provider-slot scheduler "alpha")
      (ok (not admitted))
      (ok (eq reason :queue-timeout)))
    (ok (zerop (scheduler-provider-active-count scheduler "alpha")))
    (ok (zerop (scheduler-provider-queued-count scheduler "alpha")))
    ;; The expired waiter must not consume the newly refilled token.
    (ok (acquire-provider-slot scheduler "alpha"))
    (release-provider-slot scheduler "alpha")))

(deftest waiter-error-unwinds-without-poisoning-fifo
  (let ((scheduler
          (make-request-scheduler
           (make-scheduler-config :max-active 1 :max-queue-depth 1 :burst 4)
           :clock (lambda () 0)
           :sleep-function (lambda (seconds)
                             (declare (ignore seconds))
                             (error "injected waiter interruption")))))
    (ok (acquire-provider-slot scheduler "alpha"))
    (ok (signals (acquire-provider-slot scheduler "alpha") 'error))
    (ok (zerop (scheduler-provider-queued-count scheduler "alpha")))
    (ok (= (scheduler-provider-active-count scheduler "alpha") 1))
    (release-provider-slot scheduler "alpha")
    (ok (acquire-provider-slot scheduler "alpha"))
    (release-provider-slot scheduler "alpha")))

(deftest cancelled-waiter-does-not-consume-or-block-quota
  (let* ((cancelled nil)
         (scheduler
           (make-request-scheduler
            (make-scheduler-config :max-active 1 :max-queue-depth 1 :burst 2)
            :clock (lambda () 0)
            :sleep-function (lambda (seconds)
                              (declare (ignore seconds))
                              (setf cancelled t)))))
    (ok (acquire-provider-slot scheduler "alpha"))
    (multiple-value-bind (admitted reason)
        (acquire-provider-slot scheduler "alpha"
                               :cancelled-p (lambda () cancelled))
      (ok (not admitted))
      (ok (eq reason :cancelled)))
    (ok (zerop (scheduler-provider-queued-count scheduler "alpha")))
    (release-provider-slot scheduler "alpha")
    (ok (acquire-provider-slot scheduler "alpha"))
    (release-provider-slot scheduler "alpha")))

(deftest simultaneous-callers-cannot-overspend-burst
  (let ((scheduler
          (make-request-scheduler
           (make-scheduler-config :max-active 64 :max-queue-depth 0
                                  :requests-per-minute 60 :burst 8)
           :clock (lambda () 0)))
        (admissions 0)
        (lock (bt:make-lock "rate-test-count")))
    (let ((workers
            (loop repeat 64 collect
              (bt:make-thread
               (lambda ()
                 (when (acquire-provider-slot scheduler "alpha")
                   (unwind-protect
                        (bt:with-lock-held (lock) (incf admissions))
                     (release-provider-slot scheduler "alpha"))))))))
      (dolist (worker workers) (bt:join-thread worker)))
    (ok (= admissions 8))
    (ok (zerop (scheduler-provider-active-count scheduler "alpha")))))


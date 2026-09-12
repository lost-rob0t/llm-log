(in-package #:llm-log)

;; Provider-wide admission control.
;;
;; The scheduler deliberately owns only admission state. Relay threads remain
;; responsible for their client and upstream sockets. A provider gets an
;; independent active counter and FIFO wait queue, so one saturated provider
;; cannot consume another provider's slots.

(defparameter +scheduler-poll-interval-seconds+ 0.01d0)

(defstruct scheduler-waiter
  id)

(defstruct provider-admission-state
  (active 0)
  (queue '())
  (lock (bt:make-lock "llm-log-provider-admission")))

(defstruct (request-scheduler (:constructor %make-request-scheduler))
  config
  (states (make-hash-table :test #'equal))
  (states-lock (bt:make-lock "llm-log-scheduler-states")))

(defun make-request-scheduler (config)
  "Create one scheduler using CONFIG for every provider it observes."
  (%make-request-scheduler :config (validate-scheduler-config config)))

(defun %monotonic-seconds ()
  (/ (get-internal-real-time)
     (float internal-time-units-per-second 1d0)))

(defun %provider-state (scheduler provider)
  (let ((lock (request-scheduler-states-lock scheduler)))
    (bt:with-lock-held (lock)
      (or (gethash provider (request-scheduler-states scheduler))
          (setf (gethash provider (request-scheduler-states scheduler))
                (make-provider-admission-state))))))

(defun %remove-waiter (state waiter)
  (setf (provider-admission-state-queue state)
        (delete waiter
                (provider-admission-state-queue state)
                :test #'eq
                :count 1)))

(defun acquire-provider-slot (scheduler provider)
  "Try to acquire one active slot for PROVIDER.

Returns two values. The first is true when the caller owns a slot. The second
is one of :ADMITTED, :QUEUE-FULL, or :QUEUE-TIMEOUT. A queued request is
admitted strictly from the head of the provider FIFO."
  (let* ((config (request-scheduler-config scheduler))
         (max-active (scheduler-config-max-active config))
         (max-queue-depth (scheduler-config-max-queue-depth config))
         (queue-timeout (scheduler-config-queue-timeout-seconds config))
         (state (%provider-state scheduler provider))
         (waiter (make-scheduler-waiter :id (gensym "WAIT-")))
         (deadline (+ (%monotonic-seconds) queue-timeout))
         (lock (provider-admission-state-lock state)))
    (bt:with-lock-held (lock)
      (when (and (< (provider-admission-state-active state) max-active)
                 (null (provider-admission-state-queue state)))
        (incf (provider-admission-state-active state))
        (return-from acquire-provider-slot (values t :admitted)))
      (when (>= (length (provider-admission-state-queue state))
                max-queue-depth)
        (return-from acquire-provider-slot (values nil :queue-full)))
      (setf (provider-admission-state-queue state)
            (nconc (provider-admission-state-queue state)
                   (list waiter))))
    (loop
      (let ((result nil))
        (bt:with-lock-held (lock)
          (let ((now (%monotonic-seconds)))
            (cond
              ((>= now deadline)
               (%remove-waiter state waiter)
               (setf result :queue-timeout))
              ((and (eq waiter (first (provider-admission-state-queue state)))
                    (< (provider-admission-state-active state) max-active))
               (setf (provider-admission-state-queue state)
                     (rest (provider-admission-state-queue state)))
               (incf (provider-admission-state-active state))
               (setf result :admitted)))))
        (case result
          (:admitted
           (return (values t :admitted)))
          (:queue-timeout
           (return (values nil :queue-timeout)))))
      (sleep +scheduler-poll-interval-seconds+))))

(defun release-provider-slot (scheduler provider)
  "Release one active slot previously acquired for PROVIDER."
  (let* ((state (%provider-state scheduler provider))
         (lock (provider-admission-state-lock state)))
    (bt:with-lock-held (lock)
      (when (plusp (provider-admission-state-active state))
        (decf (provider-admission-state-active state)))))
  scheduler)

(defun scheduler-provider-active-count (scheduler provider)
  "Return the current active request count for PROVIDER."
  (let* ((state (%provider-state scheduler provider))
         (lock (provider-admission-state-lock state)))
    (bt:with-lock-held (lock)
      (provider-admission-state-active state))))

(defun scheduler-provider-queued-count (scheduler provider)
  "Return the current queued request count for PROVIDER."
  (let* ((state (%provider-state scheduler provider))
         (lock (provider-admission-state-lock state)))
    (bt:with-lock-held (lock)
      (length (provider-admission-state-queue state)))))

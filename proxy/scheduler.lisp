(in-package #:llm-log)

;; One in-process admission owner per trusted provider/quota group. The same
;; lock protects rate tokens, active slots and FIFO transitions: checking a
;; bucket separately from reserving a slot would race under concurrent load.
;; Relay threads retain socket ownership; no network I/O runs under this lock.

(defparameter +scheduler-poll-interval-seconds+ 1/100)

(defstruct scheduler-waiter
  deadline)

(defstruct provider-admission-state
  (active 0)
  (queue '())
  tokens
  last-refill
  (lock (bt:make-lock "llm-log-provider-admission")))

(defstruct (request-scheduler (:constructor %make-request-scheduler))
  config
  clock
  sleep-function
  (states (make-hash-table :test #'equal))
  (states-lock (bt:make-lock "llm-log-scheduler-states")))

(defun %monotonic-seconds ()
  ;; Keep exact rational ticks so rounding cannot admit before a deadline.
  (/ (get-internal-real-time) internal-time-units-per-second))

(defun make-request-scheduler
    (config &key (clock #'%monotonic-seconds) (sleep-function #'sleep))
  "Snapshot CONFIG. CLOCK and SLEEP-FUNCTION are injectable for tests.

The bucket starts with BURST request permits and refills at REQUESTS-PER-MINUTE.
This is not a durable account quota or a cross-process limit."
  (check-type clock function)
  (check-type sleep-function function)
  (let ((snapshot (copy-scheduler-config (validate-scheduler-config config))))
    ;; COPY-TREE alone would leave mutable strings shared with the caller.
    (setf (scheduler-config-provider-groups snapshot)
          (mapcar (lambda (entry)
                    (cons (copy-seq (car entry)) (copy-seq (cdr entry))))
                  (scheduler-config-provider-groups config)))
    (%make-request-scheduler :config snapshot :clock clock
                             :sleep-function sleep-function)))

(defun %provider-state (scheduler provider)
  (let* ((group (cdr (assoc provider
                           (scheduler-config-provider-groups
                            (request-scheduler-config scheduler))
                           :test #'equal)))
         ;; Group labels are not implicit aliases for unrelated provider names.
         (key (if group (list :group group) (list :provider provider)))
         (lock (request-scheduler-states-lock scheduler)))
    (bt:with-lock-held (lock)
      (or (gethash key (request-scheduler-states scheduler))
          (setf (gethash key (request-scheduler-states scheduler))
                (make-provider-admission-state))))))

(defun %remove-waiter (state waiter)
  (setf (provider-admission-state-queue state)
        (delete waiter (provider-admission-state-queue state)
                :test #'eq :count 1)))

(defun %refresh-admission-state (state config now)
  "Called with STATE locked. Clamp backwards clocks and evict expired waiters."
  (let* ((last (provider-admission-state-last-refill state))
         (effective-now (if last (max last now) now))
         (burst (scheduler-config-burst config)))
    (setf (provider-admission-state-tokens state)
          (if last
              (min burst
                   (+ (provider-admission-state-tokens state)
                      (* (- effective-now last)
                         (/ (scheduler-config-requests-per-minute config) 60))))
              burst)
          (provider-admission-state-last-refill state) effective-now
          (provider-admission-state-queue state)
          (delete-if (lambda (waiter)
                       (>= now (scheduler-waiter-deadline waiter)))
                     (provider-admission-state-queue state)))))

(defun %rate-wait-seconds (state config &optional (permits 1))
  (/ (* 60 (max 0 (- permits (provider-admission-state-tokens state))))
     (scheduler-config-requests-per-minute config)))

(defun %admission-retry-after (state config)
  "A rounded-up hint, not a promise that active requests finish by this time."
  (max (scheduler-config-retry-after-seconds config)
       (ceiling (%rate-wait-seconds
                 state config (1+ (length (provider-admission-state-queue state)))))))

(defun %admission-capacity-p (state config)
  (and (< (provider-admission-state-active state)
          (scheduler-config-max-active config))
       (>= (provider-admission-state-tokens state) 1)))

(defun %reserve-admission (state)
  (assert (>= (provider-admission-state-tokens state) 1))
  (decf (provider-admission-state-tokens state))
  (incf (provider-admission-state-active state)))

(defun acquire-provider-slot (scheduler provider &key cancelled-p)
  "Return VALUES admitted-p, reason, retry-after-seconds.

A permit costs one request token AND one active slot, atomically. Queued callers
are admitted FIFO before their deadline. Rejections/cancellations cost no token.
CANCELLED-P is an optional cooperative callback; it must not block. The caller
must release an admitted active slot in UNWIND-PROTECT around upstream work."
  (let* ((config (request-scheduler-config scheduler))
         (state (%provider-state scheduler provider))
         (clock (request-scheduler-clock scheduler))
         (deadline (+ (funcall clock)
                      (scheduler-config-queue-timeout-seconds config)))
         (waiter (make-scheduler-waiter :deadline deadline))
         (lock (provider-admission-state-lock state)))
    ;; Install cleanup before inserting the waiter, including error/throw exits.
    (unwind-protect
         (progn
           (when (and cancelled-p (funcall cancelled-p))
             (return-from acquire-provider-slot (values nil :cancelled 0)))
           (bt:with-lock-held (lock)
             (%refresh-admission-state state config (funcall clock))
             (when (and (null (provider-admission-state-queue state))
                        (%admission-capacity-p state config))
               (%reserve-admission state)
               (return-from acquire-provider-slot (values t :admitted 0)))
             (when (>= (length (provider-admission-state-queue state))
                       (scheduler-config-max-queue-depth config))
               (return-from acquire-provider-slot
                 (values nil
                         (if (and (< (provider-admission-state-tokens state) 1)
                                  (< (provider-admission-state-active state)
                                     (scheduler-config-max-active config)))
                             :rate-limited :queue-full)
                         (%admission-retry-after state config))))
             (setf (provider-admission-state-queue state)
                   (nconc (provider-admission-state-queue state) (list waiter))))
           (loop
             (when (and cancelled-p (funcall cancelled-p))
               (return-from acquire-provider-slot (values nil :cancelled 0)))
             (let ((pause +scheduler-poll-interval-seconds+))
               (bt:with-lock-held (lock)
                 (let ((now (funcall clock)))
                   (%refresh-admission-state state config now)
                   ;; Expiry wins even if a rate token/slot just became free.
                   (when (>= now deadline)
                     (return-from acquire-provider-slot
                       (values nil :queue-timeout
                               (%admission-retry-after state config))))
                   (when (and (eq waiter (first (provider-admission-state-queue state)))
                              (%admission-capacity-p state config))
                     (pop (provider-admission-state-queue state))
                     (%reserve-admission state)
                     (return-from acquire-provider-slot (values t :admitted 0)))
                   (setf pause (min pause (- deadline now)))))
               (funcall (request-scheduler-sleep-function scheduler) pause))))
      (bt:with-lock-held (lock)
        (%remove-waiter state waiter)))))

(defun release-provider-slot (scheduler provider)
  "Release an active slot, NOT its request token. Failed attempts still count."
  (let* ((state (%provider-state scheduler provider))
         (lock (provider-admission-state-lock state)))
    (bt:with-lock-held (lock)
      (when (plusp (provider-admission-state-active state))
        (decf (provider-admission-state-active state)))))
  scheduler)

(defun scheduler-provider-active-count (scheduler provider)
  "Return the current active request count for PROVIDER's quota group."
  (let* ((state (%provider-state scheduler provider))
         (lock (provider-admission-state-lock state)))
    (bt:with-lock-held (lock)
      (provider-admission-state-active state))))

(defun scheduler-provider-queued-count (scheduler provider)
  "Return the current queued request count for PROVIDER's quota group."
  (let* ((state (%provider-state scheduler provider))
         (lock (provider-admission-state-lock state)))
    (bt:with-lock-held (lock)
      (length (provider-admission-state-queue state)))))

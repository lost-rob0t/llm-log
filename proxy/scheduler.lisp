(in-package #:llm-log)

;; One mutex serializes all admission state for an upstream origin. Profiles
;; never participate in this key. Network I/O stays outside the critical section.
(defparameter +scheduler-poll-interval-seconds+ 0.01d0)

(defstruct scheduler-waiter id)

(defstruct provider-admission-state
  (active 0)
  (queue '())
  (tokens nil)
  (updated-at 0d0)
  (lock (bt:make-lock "llm-log-provider-admission")))

(defstruct (request-scheduler (:constructor %make-request-scheduler))
  config clock sleeper aliases
  (states (make-hash-table :test #'equal))
  (states-lock (bt:make-lock "llm-log-scheduler-states")))

(defun %monotonic-seconds ()
  (/ (get-internal-real-time)
     (float internal-time-units-per-second 1d0)))

(defun %quota-origin-key (url)
  "Canonical origin, deliberately excluding path, profile and credentials."
  (let* ((uri (quri:uri url))
         (scheme (string-downcase (or (quri:uri-scheme uri) "")))
         (host (quri:uri-host uri)))
    (unless (and host (plusp (length host))
                 (member scheme '("http" "https") :test #'equal))
      (%invalid "scheduler upstream must have an HTTP(S) origin"))
    (list :origin scheme (string-downcase host)
          (or (quri:uri-port uri) (if (equal scheme "https") 443 80)))))

(defun make-request-scheduler
    (config &key upstreams (clock #'%monotonic-seconds) (sleeper #'sleep))
  "Create a process-local scheduler. Aliases of one origin share a budget.
CLOCK and SLEEPER are injectable functions for deterministic contract tests."
  (unless (and (functionp clock) (functionp sleeper))
    (%invalid "scheduler clock and sleeper must be functions"))
  (%make-request-scheduler
   :config (copy-scheduler-config (validate-scheduler-config config))
   :clock clock :sleeper sleeper
   :aliases (mapcar (lambda (entry)
                      (validate-upstream (car entry) (cdr entry))
                      (cons (copy-seq (car entry))
                            (%quota-origin-key (cdr entry))))
                    upstreams)))

(defun %provider-state (scheduler provider)
  (let ((key (or (cdr (assoc provider (request-scheduler-aliases scheduler)
                             :test #'equal))
                 (list :provider provider)))
        (lock (request-scheduler-states-lock scheduler)))
    (bt:with-lock-held (lock)
      (or (gethash key (request-scheduler-states scheduler))
          (setf (gethash key (request-scheduler-states scheduler))
                (make-provider-admission-state))))))

(defun %remove-waiter (state waiter)
  (setf (provider-admission-state-queue state)
        (delete waiter (provider-admission-state-queue state)
                :test #'eq :count 1)))

(defun %rate-delay (config state now)
  "Refill under the state lock; return seconds until one start token exists."
  (let ((rpm (scheduler-config-requests-per-minute config)))
    (when (zerop rpm) (return-from %rate-delay 0))
    (let ((rate (/ rpm 60))
          (burst (scheduler-config-burst config)))
      (if (null (provider-admission-state-tokens state))
          (setf (provider-admission-state-tokens state) burst
                (provider-admission-state-updated-at state) now)
          (let* ((previous (provider-admission-state-updated-at state))
                 (elapsed (max 0 (- now previous))))
            (setf (provider-admission-state-tokens state)
                  (min burst (+ (provider-admission-state-tokens state)
                                (* elapsed rate)))
                  (provider-admission-state-updated-at state)
                  (max previous now))))
      (max 0 (/ (- 1 (provider-admission-state-tokens state)) rate)))))

(defun %admit-slot (config state)
  "Spend a start token and an active slot atomically, with the state locked."
  (assert (< (provider-admission-state-active state)
             (scheduler-config-max-active config)))
  (when (plusp (scheduler-config-requests-per-minute config))
    (assert (>= (provider-admission-state-tokens state) 1))
    (decf (provider-admission-state-tokens state)))
  (incf (provider-admission-state-active state)))

(defun %admission-retry-after (config delay)
  (max (scheduler-config-retry-after-seconds config) (ceiling delay)))

(defun acquire-provider-slot (scheduler provider)
  "Return ADMITTED-P, reason, and integer Retry-After (zero on admission).
An admitted request spends one start token even if its upstream later fails.
Rejected/expired waiters spend no tokens. FIFO and deadline precede admission."
  (let* ((config (request-scheduler-config scheduler))
         (state (%provider-state scheduler provider))
         (clock (request-scheduler-clock scheduler))
         (deadline (+ (funcall clock)
                      (scheduler-config-queue-timeout-seconds config)))
         (waiter (make-scheduler-waiter :id (gensym "WAIT-")))
         (lock (provider-admission-state-lock state)))
    (unwind-protect
         (progn
           (bt:with-lock-held (lock)
             (let ((delay (%rate-delay config state (funcall clock))))
               (when (and (zerop delay)
                          (< (provider-admission-state-active state)
                             (scheduler-config-max-active config))
                          (null (provider-admission-state-queue state)))
                 (%admit-slot config state)
                 (return-from acquire-provider-slot (values t :admitted 0)))
               (when (>= (length (provider-admission-state-queue state))
                         (scheduler-config-max-queue-depth config))
                 (return-from acquire-provider-slot
                   (values nil :queue-full (%admission-retry-after config delay))))
               (setf (provider-admission-state-queue state)
                     (nconc (provider-admission-state-queue state) (list waiter)))))
           (loop
             (bt:with-lock-held (lock)
               (let* ((now (funcall clock))
                      (delay (%rate-delay config state now)))
                 (when (>= now deadline)
                   (return-from acquire-provider-slot
                     (values nil :queue-timeout
                             (%admission-retry-after config delay))))
                 (when (and (eq waiter (first (provider-admission-state-queue state)))
                            (zerop delay)
                            (< (provider-admission-state-active state)
                               (scheduler-config-max-active config)))
                   (pop (provider-admission-state-queue state))
                   (%admit-slot config state)
                   (return-from acquire-provider-slot (values t :admitted 0)))))
             (funcall (request-scheduler-sleeper scheduler)
                      +scheduler-poll-interval-seconds+)))
      ;; Also clean up non-local exits while waiting, not just ordinary timeout.
      (bt:with-lock-held (lock) (%remove-waiter state waiter)))))

(defun release-provider-slot (scheduler provider)
  "Release concurrency only. Completion never refunds the request-start budget."
  (let* ((state (%provider-state scheduler provider))
         (lock (provider-admission-state-lock state)))
    (bt:with-lock-held (lock)
      (when (plusp (provider-admission-state-active state))
        (decf (provider-admission-state-active state)))))
  scheduler)

(defun scheduler-provider-active-count (scheduler provider)
  (let* ((state (%provider-state scheduler provider))
         (lock (provider-admission-state-lock state)))
    (bt:with-lock-held (lock) (provider-admission-state-active state))))

(defun scheduler-provider-queued-count (scheduler provider)
  (let* ((state (%provider-state scheduler provider))
         (lock (provider-admission-state-lock state)))
    (bt:with-lock-held (lock) (length (provider-admission-state-queue state)))))

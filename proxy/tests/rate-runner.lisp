;;; Independent rate gate. The full proxy gate remains mandatory.
;;; Preserve the repository's wrapper-ASDF initialization order.
(let ((asdf-fasl (sb-unix::posix-getenv "ASDF")))
  (when (and asdf-fasl (plusp (length asdf-fasl)))
    (load asdf-fasl)))
(require :asdf)

(in-package #:cl-user)

;; Compile/load the complete test system, but execute only the socket-free
;; rate contracts here. The original runner still executes every test.
(asdf:load-system :llm-log-tests)

(let ((colors (find-symbol "*ENABLE-COLORS*" :rove)))
  (when colors
    (setf (symbol-value colors) nil)))

(let* ((names '("RATE-CONFIG-IS-POSITIVE-AND-EXPLICIT"
                "COMPLETED-REQUESTS-DO-NOT-REFUND-QUOTA"
                "RATE-REFILLS-AT-EXACT-BOUNDARY-AND-ROUNDS-RETRY-UP"
                "IDLE-REFILL-NEVER-EXCEEDS-BURST"
                "BACKWARDS-CLOCK-CANNOT-MINT-QUOTA"
                "ALIASES-SHARE-RATE-AND-ACTIVE-LIMITS-NOT-CLIENT-IDENTITY"
                "SCHEDULER-SNAPSHOTS-MUTABLE-POLICY"
                "QUEUED-RATE-REQUEST-ADMITS-ON-REFILL"
                "DEADLINE-WINS-OVER-SIMULTANEOUS-REFILL"
                "WAITER-ERROR-UNWINDS-WITHOUT-POISONING-FIFO"
                "CANCELLED-WAITER-DOES-NOT-CONSUME-OR-BLOCK-QUOTA"
                "SIMULTANEOUS-CALLERS-CANNOT-OVERSPEND-BURST"))
       (tests (mapcar (lambda (name)
                        (or (find-symbol name :llm-log/tests)
                            (error "Required rate contract missing: ~A" name)))
                      names)))
  (assert (= (length tests) 12))
  ;; Rove RUN-TESTS rejects unregistered names and returns passed-p as its
  ;; first value. Do not turn test failures into a successful process exit.
  (let ((passed (uiop:symbol-call :rove :run-tests tests)))
    (uiop:quit (if passed 0 1))))

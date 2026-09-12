(in-package #:cl-user)

(defvar *policy-cases* '())

(defmacro policy-case (name &body body)
  `(push (cons ,(string-downcase (symbol-name name))
               (lambda () ,@body))
         *policy-cases*))

(defun expect-invalid-policy (thunk)
  (assert (handler-case (progn (funcall thunk) nil)
            (llm-log:invalid-configuration () t))))

(defun rate-config (&key (rate 60) (burst 2) (active 8) (queue 0) (timeout 0))
  (llm-log:runtime-config-scheduler
   (llm-log:parse-toml-config
    (format nil "[scheduler]~%requests_per_minute = ~D~%burst = ~D~%max_active = ~D~%max_queue_depth = ~D~%queue_timeout_seconds = ~D~%"
            rate burst active queue timeout))))

(defun fixture-profile-config ()
  ;; Synthetic identity values, not a capture of a released OpenCode version.
  (llm-log:parse-toml-config
   "[upstreams]
zai = \"https://api.z.ai\"
alias = \"https://API.Z.AI:443\"
[profiles.fixture]
version = \"test-fixture-1\"
user_agent = \"opencode/test-fixture\"
title = \"OpenCode\"
drop_headers = [\"x-session-id\", \"x-stainless-package-version\", \"x-agent-name\", \"traceparent\"]
[provider_profiles]
zai = \"fixture\"
alias = \"fixture\"
"))

(defun harness-headers (name)
  (let ((headers (make-hash-table :test #'equal)))
    (setf (gethash "User-Agent" headers) name
          (gethash "X-Agent-Name" headers) name
          (gethash "X-Session-Id" headers) "local-private-session"
          (gethash "X-Stainless-Package-Version" headers) "fixture-sdk"
          (gethash "Traceparent" headers) "local-trace"
          (gethash "Authorization" headers) "Bearer fake-test-key"
          (gethash "Anthropic-Version" headers) "required-fixture-version"
          (gethash "Content-Type" headers) "application/json")
    headers))

(policy-case existing-active-limit-still-applies
  (let ((scheduler (llm-log:make-request-scheduler
                    (llm-log:make-scheduler-config :max-active 1
                                                   :max-queue-depth 0))))
    (assert (llm-log:acquire-provider-slot scheduler "alpha"))
    (assert (not (llm-log:acquire-provider-slot scheduler "alpha")))
    (llm-log:release-provider-slot scheduler "alpha")
    (assert (zerop (llm-log:scheduler-provider-active-count scheduler "alpha")))))

(policy-case rate-config-is-explicit-and-validated
  (let ((defaults (llm-log:runtime-config-scheduler
                   (llm-log:resolve-config :config-file nil))))
    (assert (zerop (llm-log::scheduler-config-requests-per-minute defaults)))
    (assert (= 1 (llm-log::scheduler-config-burst defaults))))
  (assert (= 120 (llm-log::scheduler-config-requests-per-minute
                  (rate-config :rate 120 :burst 3))))
  (dolist (text '("[scheduler]
requests_per_minute = -1" "[scheduler]
burst = 0" "[scheduler]
requests_per_minute = 0.5" "[scheduler]
burst = 100001"))
    (expect-invalid-policy (lambda () (llm-log:parse-toml-config text)))))

(policy-case release-does-not-refund-request-budget
  (let* ((now 0d0)
         (scheduler (llm-log:make-request-scheduler
                     (rate-config) :clock (lambda () now))))
    (dotimes (index 2)
      (declare (ignore index))
      (assert (llm-log:acquire-provider-slot scheduler "alpha"))
      (llm-log:release-provider-slot scheduler "alpha"))
    (multiple-value-bind (admitted reason retry-after)
        (llm-log:acquire-provider-slot scheduler "alpha")
      (assert (not admitted))
      (assert (eq reason :queue-full))
      (assert (= retry-after 1)))
    (setf now 0.5d0)
    (assert (not (llm-log:acquire-provider-slot scheduler "alpha")))
    (setf now 1d0)
    (assert (llm-log:acquire-provider-slot scheduler "alpha"))
    (llm-log:release-provider-slot scheduler "alpha")
    (assert (not (llm-log:acquire-provider-slot scheduler "alpha")))))

(policy-case rejected-request-does-not-spend-a-token
  (let ((scheduler (llm-log:make-request-scheduler
                    (rate-config :active 1) :clock (constantly 0d0))))
    (assert (llm-log:acquire-provider-slot scheduler "alpha"))
    (assert (not (llm-log:acquire-provider-slot scheduler "alpha")))
    (llm-log:release-provider-slot scheduler "alpha")
    (assert (llm-log:acquire-provider-slot scheduler "alpha"))
    (llm-log:release-provider-slot scheduler "alpha")
    (assert (not (llm-log:acquire-provider-slot scheduler "alpha")))))

(policy-case retry-after-is-ceiling-of-refill-delay
  (let ((scheduler (llm-log:make-request-scheduler
                    (rate-config :rate 20 :burst 1) :clock (constantly 0d0))))
    (assert (llm-log:acquire-provider-slot scheduler "alpha"))
    (llm-log:release-provider-slot scheduler "alpha")
    (multiple-value-bind (admitted reason retry-after)
        (llm-log:acquire-provider-slot scheduler "alpha")
      (declare (ignore reason))
      (assert (not admitted))
      (assert (= retry-after 3)))))

(policy-case aliases-share-one-origin-budget
  (let ((scheduler
          (llm-log:make-request-scheduler
           (rate-config :burst 1) :clock (constantly 0d0)
           :upstreams '(("hermes" . "https://API.Z.AI:443/a")
                        ("agent-zero" . "https://api.z.ai/b")
                        ("other" . "https://independent.example")))))
    (assert (llm-log:acquire-provider-slot scheduler "hermes"))
    (assert (= 1 (llm-log:scheduler-provider-active-count scheduler "agent-zero")))
    (llm-log:release-provider-slot scheduler "hermes")
    (assert (not (llm-log:acquire-provider-slot scheduler "agent-zero")))
    (assert (llm-log:acquire-provider-slot scheduler "other"))
    (llm-log:release-provider-slot scheduler "other")))

(policy-case rate-wait-expires-before-refill
  (let* ((now 0d0)
         (scheduler
           (llm-log:make-request-scheduler
            (rate-config :rate 1 :burst 1 :queue 1 :timeout 1)
            :clock (lambda () now)
            :sleeper (lambda (delay) (incf now delay)))))
    (assert (llm-log:acquire-provider-slot scheduler "alpha"))
    (llm-log:release-provider-slot scheduler "alpha")
    (multiple-value-bind (admitted reason)
        (llm-log:acquire-provider-slot scheduler "alpha")
      (assert (not admitted))
      (assert (eq reason :queue-timeout)))
    (assert (zerop (llm-log:scheduler-provider-active-count scheduler "alpha")))
    (assert (zerop (llm-log:scheduler-provider-queued-count scheduler "alpha")))))

(policy-case abandoned-waiter-is-removed
  (let ((scheduler
          (llm-log:make-request-scheduler
           (rate-config :burst 1 :queue 1 :timeout 10)
           :clock (constantly 0d0)
           :sleeper (lambda (delay) (declare (ignore delay)) (error "test cancellation")))))
    (assert (llm-log:acquire-provider-slot scheduler "alpha"))
    (llm-log:release-provider-slot scheduler "alpha")
    (assert (handler-case
                (progn (llm-log:acquire-provider-slot scheduler "alpha") nil)
              (error () t)))
    (assert (zerop (llm-log:scheduler-provider-queued-count scheduler "alpha")))
    (assert (zerop (llm-log:scheduler-provider-active-count scheduler "alpha")))))

(policy-case backwards-clock-cannot-mint-tokens
  (let* ((now 10d0)
         (scheduler (llm-log:make-request-scheduler
                     (rate-config :burst 1) :clock (lambda () now))))
    (assert (llm-log:acquire-provider-slot scheduler "alpha"))
    (llm-log:release-provider-slot scheduler "alpha")
    (setf now 9d0)
    (assert (not (llm-log:acquire-provider-slot scheduler "alpha")))
    (setf now 10d0)
    (assert (not (llm-log:acquire-provider-slot scheduler "alpha")))
    (setf now 11d0)
    (assert (llm-log:acquire-provider-slot scheduler "alpha"))
    (llm-log:release-provider-slot scheduler "alpha")))

(policy-case simultaneous-clients-cannot-overspend-burst
  (let* ((scheduler (llm-log:make-request-scheduler
                     (rate-config :burst 7 :active 64) :clock (constantly 0d0)))
         (wins 0)
         (lock (bt:make-lock "policy-test-results"))
         (threads
           (loop repeat 32 collect
             (bt:make-thread
              (lambda ()
                (when (llm-log:acquire-provider-slot scheduler "alpha")
                  (unwind-protect
                       (bt:with-lock-held (lock) (incf wins))
                    (llm-log:release-provider-slot scheduler "alpha"))))))))
    (mapc #'bt:join-thread threads)
    (assert (= wins 7))
    (assert (zerop (llm-log:scheduler-provider-active-count scheduler "alpha")))))

(policy-case profiles-normalize-harness-identity-without-changing-auth
  (let ((config (fixture-profile-config)))
    (dolist (name '("Hermes/fixture" "OpenClaw/fixture" "AgentZero/fixture"))
      (let* ((original (harness-headers name))
             (outbound (llm-log::apply-outbound-profile config "zai" original)))
        (assert (equal (gethash "user-agent" outbound) "opencode/test-fixture"))
        (assert (equal (gethash "x-title" outbound) "OpenCode"))
        (assert (equal (gethash "authorization" outbound) "Bearer fake-test-key"))
        (assert (equal (gethash "anthropic-version" outbound) "required-fixture-version"))
        (assert (equal (gethash "content-type" outbound) "application/json"))
        (dolist (key '("x-session-id" "x-stainless-package-version" "x-agent-name" "traceparent"))
          (assert (not (gethash key outbound))))
        (assert (equal (gethash "User-Agent" original) name))
        (assert (gethash "X-Session-Id" original))))))

(policy-case unmapped-provider-stays-transparent
  (let* ((headers (harness-headers "AgentZero/fixture"))
         (outbound (llm-log::apply-outbound-profile
                    (fixture-profile-config) "unmapped" headers)))
    (assert (equalp headers outbound))))

(policy-case invalid-profiles-fail-closed
  (dolist (text '("[provider_profiles]
zai = \"missing\"" "[profiles.bad]
user_agent = \"missing-version\"" "[profiles.bad]
version = \"1\"
drop_headers = [\"authorization\"]" "[profiles.bad]
version = \"1\"
drop_headers = [\"host\"]" "[profiles.bad]
version = \"1\"
unknown = \"field\"" "[profiles.bad]
version = \"1\"
user_agent = \"ok\\r\\nInjected: yes\""))
    (expect-invalid-policy (lambda () (llm-log:parse-toml-config text)))))

(policy-case relay-applies-profile-and-rejects-before-upstream
  ;; Exercise the real relay/admission boundary, substituting ONLY the final
  ;; network operation. This is not a live Woo/SSE/provider conformance test.
  (let* ((config (fixture-profile-config))
         (policy (rate-config :rate 20 :burst 1))
         (scheduler (llm-log:make-request-scheduler
                     policy :clock (constantly 0d0)
                     :upstreams (llm-log:runtime-config-upstreams config)))
         (body #(123 125))
         (seen nil)
         (calls 0)
         (original (symbol-function 'llm-log::%relay-admitted-request))
         (path (merge-pathnames
                (make-pathname :name (format nil "llm-log-policy-~A" (gensym)))
                (uiop:temporary-directory))))
    (setf (llm-log:runtime-config-scheduler config) policy)
    (unwind-protect
         (progn
           (setf (symbol-function 'llm-log::%relay-admitted-request)
                 (lambda (client method headers octets target upstream)
                   (declare (ignore client method target upstream))
                   (incf calls)
                   (setf seen (list headers octets))))
           (with-open-file (stream path :direction :io :if-exists :error
                                        :if-does-not-exist :create
                                        :element-type '(unsigned-byte 8))
             (llm-log::%relay-request stream config scheduler :post
                                     "/zai/v1/chat/completions"
                                     (harness-headers "AgentZero/fixture") body)
             (assert (= calls 1))
             (assert (equal (gethash "user-agent" (first seen)) "opencode/test-fixture"))
             (assert (eq (second seen) body))
             (llm-log::%relay-request stream config scheduler :post
                                     "/alias/v1/chat/completions"
                                     (harness-headers "Hermes/fixture") body)
             (assert (= calls 1))
             (force-output stream)
             (file-position stream 0)
             (let ((bytes (make-array (file-length stream) :element-type '(unsigned-byte 8))))
               (read-sequence bytes stream)
               (let ((text (trivial-utf-8:utf-8-bytes-to-string bytes)))
                 (assert (search "429 Too Many Requests" text))
                 (assert (search "Retry-After: 3" text))))))
      (setf (symbol-function 'llm-log::%relay-admitted-request) original)
      (when (probe-file path) (delete-file path)))))

(let ((failed 0)
      (total (length *policy-cases*)))
  (dolist (entry (reverse *policy-cases*))
    (handler-case
        (progn (funcall (cdr entry)) (format t "PASS ~A~%" (car entry)))
      (error (condition)
        (incf failed)
        (format t "FAIL ~A: ~A~%" (car entry) condition))))
  (format t "Policy contracts: ~D passed, ~D failed, ~D total.~%"
          (- total failed) failed total)
  (uiop:quit (if (zerop failed) 0 1)))

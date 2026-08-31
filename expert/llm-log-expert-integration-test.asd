(asdf:defsystem #:llm-log-expert-integration-test
  :description "RED-first Common Lisp/Tek9/SWI-Prolog integration contracts for llm-log expert plane."
  :author "lost-rob0t"
  :license "MIT"
  :depends-on (#:llm-log-expert #:rove)
  :serial t
  :components ((:module "tests"
                :serial t
                :components ((:file "integration-package")
                             (:file "expert-roundtrip-red")
                             (:file "reasoner-failure-red")
                             (:file "request-classifier-red")
                             (:file "request-classifier-query-red")
                             (:file "request-classifier-time-range-red")
                             (:file "request-classifier-task-id-red")
                             (:file "task-accounting-red"))))
  :perform (asdf:test-op (op c)
             (declare (ignore op c))
             (unless (uiop:symbol-call :rove :run :llm-log-expert-integration-test)
               (error "llm-log-expert-integration-test failed"))))

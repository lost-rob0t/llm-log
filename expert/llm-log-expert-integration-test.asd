(asdf:defsystem #:llm-log-expert-integration-test
  :description "RED-first Common Lisp/Tek9/SWI-Prolog integration contract for llm-log #10."
  :author "lost-rob0t"
  :license "MIT"
  :depends-on (#:llm-log-expert #:rove)
  :serial t
  :components ((:module "tests"
                :serial t
                :components ((:file "integration-package")
                             (:file "expert-roundtrip-red"))))
  :perform (asdf:test-op (op c)
             (declare (ignore op c))
             (unless (uiop:symbol-call :rove :run :llm-log-expert-integration-test)
               (error "llm-log-expert-integration-test failed"))))

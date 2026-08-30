(asdf:defsystem #:llm-log-expert-test
  :description "Common Lisp RED contracts for llm-log runtime and transport."
  :author "lost-rob0t"
  :license "MIT"
  :depends-on (#:llm-log-expert #:rove)
  :serial t
  :components ((:module "tests"
                :serial t
                :components ((:file "package")
                             (:file "transport-red")
                             (:file "capture-red"))))
  :perform (asdf:test-op (op c)
             (declare (ignore op c))
             (unless (uiop:symbol-call :rove :run :llm-log-expert-test)
               (error "llm-log-expert-test failed"))))

(asdf:defsystem #:llm-log-tests
  :description "Rove contracts for the Common Lisp llm-log runtime."
  :author "lost-rob0t"
  :license "MIT"
  :serial t
  :depends-on (#:llm-log #:rove)
  :components ((:module "tests"
                :components ((:file "package")
                             (:file "config"))))
  :perform (asdf:test-op (operation component)
             (uiop:symbol-call :rove :run-system component)))

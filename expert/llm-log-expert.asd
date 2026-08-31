(asdf:defsystem #:llm-log-expert
  :description "Common Lisp llm-log runtime and expert-system host."
  :author "lost-rob0t"
  :license "MIT"
  :version "0.1.0"
  :serial t
  :depends-on (#:tek9 #:jsown #:uiop)
  :components ((:file "package")
               (:file "config")
               (:file "transport")
               (:file "host")
               (:file "prolog-supervisor")
               (:file "storage")
               (:file "classification-history")
               (:file "task-accounting")
               (:file "retry-accounting")
               (:file "service")
               (:file "task-dispatch")
               (:static-file "prolog/worker.pl")))

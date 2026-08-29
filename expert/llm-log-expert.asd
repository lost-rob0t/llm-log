(asdf:defsystem #:llm-log-expert
  :description "Common Lisp expert-system host for llm-log."
  :author "lost-rob0t"
  :license "MIT"
  :version "0.1.0"
  :serial t
  :depends-on (#:tek9 #:jsown #:uiop)
  :components ((:file "package")
               (:file "host")
               (:file "prolog-supervisor")
               (:file "storage")
               (:file "service")
               (:static-file "prolog/worker.pl")))

(asdf:defsystem #:llm-log
  :description "Common Lisp llm-log runtime: configuration, scheduling, CLI, and transparent transport."
  :author "lost-rob0t"
  :license "MIT"
  :version "0.1.0"
  :serial t
  :depends-on (#:uiop #:clop #:woo #:usocket #:quri #:cl+ssl
                     #:bordeaux-threads #:trivial-utf-8)
  :components ((:file "package")
               (:file "config")
               (:file "scheduler")
               (:file "transport")
               (:file "cli")))

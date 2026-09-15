(asdf:defsystem #:llm-log
  :description "All-Common-Lisp transparent LLM capture, analytics, quota, and expert runtime."
  :author "lost-rob0t"
  :license "MIT"
  :version "0.2.0"
  :serial t
  :depends-on (#:uiop #:clop #:woo #:usocket #:quri #:cl+ssl
               #:bordeaux-threads #:trivial-utf-8 #:jsown #:ironclad
               #:llm-log-expert)
  :components ((:file "package")
               (:file "config")
               (:file "transport")
               (:file "recorder")
               (:file "capture-transport")
               (:file "quotas")
               (:file "api")
               (:file "runtime-services")
               (:file "cli")))

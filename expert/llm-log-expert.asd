(asdf:defsystem #:llm-log-expert
  :description "Common Lisp llm-log expert, corpus ingestion, analytics, and remote service runtime."
  :author "lost-rob0t"
  :license "MIT"
  :version "0.2.0"
  :serial t
  :depends-on (#:tek9 #:jsown #:uiop #:woo #:bordeaux-threads
               #:trivial-utf-8 #:ironclad)
  :components ((:file "package")
               (:file "config")
               (:file "transport")
               (:file "host")
               (:file "prolog-supervisor")
               (:file "storage")
               (:file "classification-history")
               (:file "task-accounting")
               (:file "retry-accounting")
               (:file "task-breakdowns")
               (:file "outcome")
               (:file "outcome-dataset")
               (:file "outcome-dataset-pagination")
               (:file "outcome-breakdowns")
               (:file "retry-economics")
               (:file "service")
               (:file "task-dispatch")
               (:file "outcome-dispatch")
               (:file "capture-usage")
               (:file "capture-import")
               (:file "analytics")
               (:file "capture-analytics-hook")
               (:file "analytics-dispatch")
               (:file "corpus")
               (:file "http")
               (:file "commands")
               (:static-file "prolog/worker.pl")))

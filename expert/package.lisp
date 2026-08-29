(in-package #:cl-user)

(uiop:define-package #:llm-log-expert
  (:use #:cl)
  (:import-from #:tek9
                #:new-database
                #:open-database
                #:close-database
                #:db-is-open-p)
  (:export
   #:expert-host
   #:expert-host-data-directory
   #:expert-host-database
   #:expert-host-prolog-process
   #:expert-host-prolog-session-id
   #:start-expert-host
   #:stop-expert-host
   #:expert-host-open-p))

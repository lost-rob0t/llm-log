(in-package #:cl-user)

(uiop:define-package #:llm-log-expert
  (:use #:cl)
  (:import-from #:tek9
                #:new-database
                #:open-database
                #:close-database
                #:db-is-open-p
                #:fetch*
                #:put*
                #:with-write-transaction)
  (:export
   #:expert-host
   #:expert-host-data-directory
   #:expert-host-database
   #:expert-host-prolog-process
   #:expert-host-prolog-session-id
   #:expert-host-prolog-request-sequence
   #:start-expert-host
   #:stop-expert-host
   #:expert-host-open-p
   #:current-kb-revision
   #:project-request-event
   #:fetch-request-event
   #:derive-event-transport
   #:dispatch-expert-request
   #:serve-stdio
   #:main))

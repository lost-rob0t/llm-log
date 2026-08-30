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
   #:runtime-config
   #:runtime-config-data-directory
   #:runtime-config-listen-address
   #:runtime-config-port
   #:runtime-config-upstreams
   #:default-data-directory
   #:make-runtime-config
   #:make-default-runtime-config
   #:http-proxy
   #:start-http-proxy
   #:stop-http-proxy
   #:proxy-listen-port
   #:expert-host
   #:expert-host-data-directory
   #:expert-host-database
   #:expert-host-prolog-process
   #:expert-host-prolog-session-id
   #:expert-host-prolog-request-sequence
   #:start-expert-host
   #:stop-expert-host
   #:expert-host-open-p
   #:reasoner-failure
   #:reasoner-failure-kind
   #:reasoner-failure-message
   #:current-kb-revision
   #:project-request-event
   #:fetch-request-event
   #:assertion-conflict
   #:assertion-conflict-assertion-id
   #:persist-derived-assertion
   #:fetch-derived-assertion
   #:derive-event-transport
   #:dispatch-expert-request
   #:serve-stdio
   #:main))

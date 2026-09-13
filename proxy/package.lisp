(in-package #:cl-user)

(uiop:define-package #:llm-log
  (:use #:cl)
  (:export
   ;; configuration surface (zero-Python rewrite slice 1)
   #:runtime-config
   #:runtime-config-data-directory
   #:runtime-config-listen-address
   #:runtime-config-port
   #:runtime-config-upstreams
   #:runtime-config-scheduler
   #:scheduler-config
   #:make-scheduler-config
   #:scheduler-config-max-active
   #:scheduler-config-max-queue-depth
   #:scheduler-config-queue-timeout-seconds
   #:scheduler-config-retry-after-seconds
   #:validate-scheduler-config
   #:invalid-configuration
   #:invalid-configuration-detail
   #:default-upstreams
   #:default-data-directory
   #:default-config-file
   #:normalize-upstream-url
   #:validate-upstream
   #:upstream-base-url
   #:parse-toml-config
   #:load-config-file
   #:resolve-config
   #:parse-serve-arguments
   ;; scheduler surface
   #:request-scheduler
   #:make-request-scheduler
   #:acquire-provider-slot
   #:release-provider-slot
   #:scheduler-provider-active-count
   #:scheduler-provider-queued-count
   ;; transport surface (zero-Python rewrite slice 2)
   #:start-proxy
   #:stop-proxy
   #:proxy-server
   #:proxy-server-thread
   #:proxy-server-config
   #:proxy-server-scheduler
   ;; CLI entry point
   #:main))

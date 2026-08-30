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
   ;; transport surface (zero-Python rewrite slice 2)
   #:start-proxy
   #:stop-proxy
   #:proxy-server
   #:proxy-server-thread
   #:proxy-server-config
   ;; CLI entry point
   #:main))

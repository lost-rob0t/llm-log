(in-package #:llm-log/tests)

(defmacro with-scheduled-fixture-proxy
    ((proxy-var upstream-var) scheduler-config &body body)
  (let ((upstream-port (gensym "UPSTREAM-PORT"))
        (proxy-port (gensym "PROXY-PORT")))
    `(multiple-value-bind (,upstream-port ,proxy-port) (%next-fixture-ports)
       (let ((+fixture-upstream-port+ ,upstream-port)
             (+fixture-proxy-port+ ,proxy-port)
             (,proxy-var nil)
             (,upstream-var nil))
         (unwind-protect
              (locally
                (setf ,upstream-var (start-fixture-upstream))
                (setf ,proxy-var
                      (start-proxy
                       (resolve-config
                        :config-file nil
                        :port +fixture-proxy-port+
                        :scheduler ,scheduler-config
                        :upstreams
                        (list
                         (validate-upstream
                          "fixture"
                          (format nil "http://127.0.0.1:~A"
                                  +fixture-upstream-port+))))))
                (unless (%wait-for-port +fixture-proxy-port+)
                  (error "llm-log proxy did not start"))
                ,@body)
           (when ,proxy-var
             (stop-proxy ,proxy-var))
           (when ,upstream-var
             (stop-fixture-upstream ,upstream-var)))))))

(defun %slow-fixture-response ()
  (list :status 200
        :headers '(("Content-Type" . "text/event-stream"))
        :body-mode
        (list :chunked
              0.5
              (%ascii-octets
               (format nil "data: done~C~C" #\Return #\Linefeed)))))

(deftest queue-full-returns-429-with-retry-after
  (with-scheduled-fixture-proxy
      (proxy upstream)
      (make-scheduler-config :max-active 1
                             :max-queue-depth 0
                             :queue-timeout-seconds 5
                             :retry-after-seconds 7)
    (setf (fixture-server-response-spec upstream) (%slow-fixture-response))
    (let ((first-status nil)
          (first-thread nil))
      (setf first-thread
            (bt:make-thread
             (lambda ()
               (setf first-status
                     (nth-value
                      0
                      (%client-request +fixture-proxy-port+
                                       "GET" "/fixture/v1/first"))))
             :name "llm-log-test-first-active"))
      (ok (%wait-until
           (lambda ()
             (eql (scheduler-provider-active-count
                   (proxy-server-scheduler proxy) "fixture")
                  1))))
      (multiple-value-bind (status headers body)
          (%client-request +fixture-proxy-port+ "GET" "/fixture/v1/second")
        (ok (eql status 429))
        (ok (equal (%head-header
                    (mapcar (lambda (entry)
                              (format nil "~A: ~A" (car entry) (cdr entry)))
                            headers)
                    "Retry-After")
                   "7"))
        (ok (search "queue is full" (%octets-to-string body))))
      (bt:join-thread first-thread)
      (ok (eql first-status 200))
      (ok (eql (length (fixture-server-requests upstream)) 1))
      (ok (zerop
           (scheduler-provider-active-count
            (proxy-server-scheduler proxy) "fixture"))))))

(deftest queue-timeout-returns-429-without-opening-upstream
  (with-scheduled-fixture-proxy
      (proxy upstream)
      (make-scheduler-config :max-active 1
                             :max-queue-depth 1
                             :queue-timeout-seconds 0
                             :retry-after-seconds 2)
    (setf (fixture-server-response-spec upstream) (%slow-fixture-response))
    (let ((first-thread
            (bt:make-thread
             (lambda ()
               (%client-request +fixture-proxy-port+
                                "GET" "/fixture/v1/first"))
             :name "llm-log-test-timeout-active")))
      (ok (%wait-until
           (lambda ()
             (eql (scheduler-provider-active-count
                   (proxy-server-scheduler proxy) "fixture")
                  1))))
      (multiple-value-bind (status headers body)
          (%client-request +fixture-proxy-port+ "GET" "/fixture/v1/queued")
        (ok (eql status 429))
        (ok (equal (%head-header
                    (mapcar (lambda (entry)
                              (format nil "~A: ~A" (car entry) (cdr entry)))
                            headers)
                    "Retry-After")
                   "2"))
        (ok (search "queue wait expired" (%octets-to-string body))))
      (bt:join-thread first-thread)
      (ok (eql (length (fixture-server-requests upstream)) 1))
      (ok (zerop
           (scheduler-provider-queued-count
            (proxy-server-scheduler proxy) "fixture"))))))

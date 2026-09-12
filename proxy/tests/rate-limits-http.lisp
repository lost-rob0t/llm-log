(in-package #:llm-log/tests)

(deftest http-rate-limit-rejects-after-completion-without-upstream-work
  (with-scheduled-fixture-proxy
      (proxy upstream)
      (make-scheduler-config :max-active 4 :max-queue-depth 0
                             :requests-per-minute 1 :burst 1)
    (ok (= (nth-value 0 (%client-request +fixture-proxy-port+
                                        "GET" "/fixture/v1/first")) 200))
    (ok (%wait-until
         (lambda ()
           (zerop (scheduler-provider-active-count
                   (proxy-server-scheduler proxy) "fixture")))))
    (multiple-value-bind (status headers body)
        (%client-request +fixture-proxy-port+ "GET" "/fixture/v1/second")
      (ok (= status 429))
      (ok (search "rate limit" (%octets-to-string body)))
      (let ((lines (mapcar (lambda (entry)
                            (format nil "~A: ~A" (car entry) (cdr entry)))
                          headers)))
        (ok (equal (%head-header lines "Cache-Control") "no-store"))
        (let ((retry (parse-integer (%head-header lines "Retry-After"))))
          (ok (<= 1 retry 60)))))
    (ok (= (length (fixture-server-requests upstream)) 1))))

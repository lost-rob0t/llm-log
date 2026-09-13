(in-package #:llm-log/tests)

;; A relay must detach Woo's watchers/registry in the event-loop thread before
;; its descriptor can be closed or recycled by a worker. Reuse the real socket
;; fixtures; neither the acceptor nor the upstream relay is substituted.
(deftest repeated-relays-and-server-restarts-retain-socket-ownership
  (dotimes (cycle 3)
    (with-fixture-proxy (proxy upstream)
      (dotimes (request 12)
        (let ((payload (%ascii-octets (format nil "cycle-~D-request-~D" cycle request))))
          (setf (fixture-server-response-spec upstream)
                (list :status 200
                      :headers '(("Content-Type" . "application/octet-stream"))
                      :body-mode (list :fixed payload)))
          (multiple-value-bind (status headers body)
              (%client-request +fixture-proxy-port+ "POST" "/fixture/reuse" :body payload)
            (declare (ignore headers))
            (ok (eql status 200))
            (ok (equalp payload body)))))
      (ok (= (length (fixture-server-requests upstream)) 12)))))

(in-package #:llm-log/tests)

;; Exercise real sockets, descriptor reuse, and repeated event-loop teardown.
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

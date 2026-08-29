(in-package #:llm-log-expert-test)

(defun runtime-symbol-function-p (name)
  (multiple-value-bind (symbol status)
      (find-symbol name :llm-log-expert)
    (and status (fboundp symbol))))

(deftest common-lisp-http-proxy-surface-red
  (testing "zero-Python runtime must own the HTTP proxy lifecycle"
    (ok (runtime-symbol-function-p "START-HTTP-PROXY")
        "Common Lisp must expose START-HTTP-PROXY before transport implementation can be accepted")
    (ok (runtime-symbol-function-p "STOP-HTTP-PROXY")
        "Common Lisp must expose STOP-HTTP-PROXY before transport implementation can be accepted")
    (ok (runtime-symbol-function-p "PROXY-LISTEN-PORT")
        "Common Lisp must expose PROXY-LISTEN-PORT so black-box tests can bind an ephemeral port")))

(deftest common-lisp-runtime-config-surface
  (testing "runtime configuration is owned by Common Lisp"
    (let ((config (llm-log-expert:make-default-runtime-config)))
      (ok (pathnamep (llm-log-expert:runtime-config-data-directory config)))
      (ok (string= "127.0.0.1"
                   (llm-log-expert:runtime-config-listen-address config)))
      (ok (= 8787 (llm-log-expert:runtime-config-port config))))))

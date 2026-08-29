;;; Common Lisp test runner for llm-log Nix checks.
;;;
;;; Required pattern for every Nix-run SBCL entrypoint in this repository:
;;; load the wrapper-provided ASDF before anything else, otherwise the
;;; prebuilt nixpkgs fasls are treated as stale and ASDF tries to recompile
;;; them into the read-only store. See
;;; research/LLM-LOG-RESEARCH-012-cl-runtime-slice-1.org.
;;;
;;; Rove symbols are resolved at runtime (find-symbol/symbol-call) because
;;; SBCL reads a whole --load file before executing any form, and the ROVE
;;; package only exists after ASDF loads the test system.

(let ((asdf-fasl (sb-unix::posix-getenv "ASDF")))
  (when (and asdf-fasl (plusp (length asdf-fasl)))
    (load asdf-fasl)))
(require :asdf)

(in-package #:cl-user)

(asdf:load-system :llm-log-tests)

(let ((colors (find-symbol "*ENABLE-COLORS*" :rove)))
  (when colors
    (setf (symbol-value colors) nil)))

(let* ((results (uiop:symbol-call :rove :run-system :llm-log-tests))
       (all-passed
        (and (listp results)
             (plusp (length results))
             (every (lambda (result)
                      (member (class-name (class-of result))
                              '(passed pending)))
                    results))))
  (uiop:quit (if all-passed 0 1)))

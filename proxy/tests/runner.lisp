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

;; run-system-tests returns (values passed-p results) from call-with-suite.
(let ((passed (uiop:symbol-call :rove/core/suite :run-system-tests
                                :llm-log-tests)))
  (uiop:quit (if passed 0 1)))

;;; Common Lisp test runner for llm-log Nix checks.
;;;
;;; Required pattern for every Nix-run SBCL entrypoint in this repository:
;;; load the wrapper-provided ASDF before anything else, otherwise the
;;; prebuilt nixpkgs fasls are treated as stale and ASDF tries to recompile
;;; them into the read-only store. See
;;; research/LLM-LOG-RESEARCH-012-cl-runtime-slice-1.org.

(let ((asdf-fasl (sb-unix::posix-getenv "ASDF")))
  (when (and asdf-fasl (plusp (length asdf-fasl)))
    (load asdf-fasl)))
(require :asdf)

(in-package #:cl-user)

(asdf:load-system :llm-log/tests)

(setf rove:*enable-colors* nil)
(rove:run-system :llm-log/tests)
(uiop:quit (if (rove:failed-p) 1 0))

;;; Common Lisp runtime entrypoint for the packaged llm-log executable.
;;;
;;; Required pattern for every Nix-run SBCL entrypoint in this repository:
;;; load the wrapper-provided ASDF before anything else, otherwise the
;;; prebuilt nixpkgs fasls are treated as stale and ASDF tries to recompile
;;; them into the read-only store. See
;;; research/LLM-LOG-RESEARCH-012-cl-runtime-slice-1.org.
;;; --script implies --no-sysinit/--no-userinit/--disable-debugger, so a
;;; user's ~/.sbclrc (Quicklisp) cannot hijack the packaged closure.

(let ((asdf-fasl (sb-unix::posix-getenv "ASDF")))
  (when (and asdf-fasl (plusp (length asdf-fasl)))
    (load asdf-fasl)))
(require :asdf)
(asdf:load-system :llm-log)
(uiop:quit (or (llm-log:main) 0))

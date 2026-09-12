;;; Isolated policy contracts: no live provider, sockets or transport shutdown.
(let ((asdf-fasl (sb-unix::posix-getenv "ASDF")))
  (when (and asdf-fasl (plusp (length asdf-fasl)))
    (load asdf-fasl)))
(require :asdf)
(asdf:load-system :llm-log)
(load (merge-pathnames #P"policy-contracts.lisp" *load-truename*))

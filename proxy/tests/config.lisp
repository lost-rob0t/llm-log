(in-package #:llm-log/tests)

;; Common Lisp runtime configuration contract (zero-Python rewrite slice 1).
;; Precedence: built-in defaults < config file < environment (not yet
;; supported) < CLI arguments. Invalid configuration is rejected, never
;; warned about. See research/LLM-LOG-RESEARCH-012-cl-runtime-slice-1.org.

(deftest default-data-directory-is-home-llm-proxy
  (ok (uiop:pathname-equal
       (default-data-directory)
       (merge-pathnames #P".llm-proxy/"
                        (uiop:ensure-directory-pathname
                         (user-homedir-pathname))))))

(deftest default-config-file-lives-under-default-data-directory
  (ok (uiop:pathname-equal
       (default-config-file)
       (merge-pathnames #P".llm-proxy/config.toml"
                        (uiop:ensure-directory-pathname
                         (user-homedir-pathname))))))

(deftest default-upstreams-match-established-registry
  (let ((config (resolve-config :config-file nil)))
    (ok (equal (upstream-base-url config "openai") "https://api.openai.com"))
    (ok (equal (upstream-base-url config "openrouter") "https://openrouter.ai"))
    (ok (equal (upstream-base-url config "anthropic") "https://api.anthropic.com"))))

(deftest defaults-are-home-localhost-8787
  (let ((config (resolve-config :config-file nil)))
    (ok (uiop:pathname-equal (runtime-config-data-directory config)
                             (default-data-directory)))
    (ok (equal (runtime-config-listen-address config) "127.0.0.1"))
    (ok (eql (runtime-config-port config) 8787))))

(deftest toml-config-parses-all-optional-sections
  (let ((config (parse-toml-config
                 "data_dir = \"/tmp/proxy\"
listen = \"0.0.0.0\"
port = 9999

[upstreams]
ollama = \"http://127.0.0.1:11434\"
")))
    (ok (uiop:pathname-equal (runtime-config-data-directory config)
                             #P"/tmp/proxy/"))
    (ok (equal (runtime-config-listen-address config) "0.0.0.0"))
    (ok (eql (runtime-config-port config) 9999))
    (ok (equal (upstream-base-url config "ollama") "http://127.0.0.1:11434"))))

(deftest toml-config-allows-omitted-keys
  (let ((config (parse-toml-config "port = 7001")))
    (ok (null (runtime-config-data-directory config)))
    (ok (null (runtime-config-listen-address config)))
    (ok (eql (runtime-config-port config) 7001))
    (ok (null (runtime-config-upstreams config)))))

(deftest toml-config-upstream-trailing-slash-is-normalized
  (let ((config (parse-toml-config
                 "[upstreams]
openai = \"https://api.openai.com/\"
")))
    (ok (equal (upstream-base-url config "openai") "https://api.openai.com"))))

(deftest toml-config-rejects-unknown-keys
  (ok (signals (parse-toml-config "log_level = \"debug\"")
        'invalid-configuration)))

(deftest toml-config-rejects-non-integer-port
  (ok (signals (parse-toml-config "port = \"abc\"")
        'invalid-configuration)))

(deftest toml-config-rejects-out-of-range-port
  (ok (signals (parse-toml-config "port = 70000")
        'invalid-configuration))
  (ok (signals (parse-toml-config "port = 0")
        'invalid-configuration)))

(deftest toml-config-rejects-non-string-listen
  (ok (signals (parse-toml-config "listen = 42")
        'invalid-configuration)))

(deftest toml-config-rejects-non-string-data-dir
  (ok (signals (parse-toml-config "data_dir = 5")
        'invalid-configuration)))

(deftest toml-config-rejects-non-string-upstream-value
  (ok (signals (parse-toml-config "[upstreams]
openai = 5
")
        'invalid-configuration)))

(deftest toml-config-rejects-empty-upstream-name
  (ok (signals (parse-toml-config "[upstreams]
\"\" = \"http://127.0.0.1:11434\"
")
        'invalid-configuration)))

(deftest toml-config-rejects-empty-upstream-value
  (ok (signals (parse-toml-config "[upstreams]
openai = \"\"
")
        'invalid-configuration)))

(deftest toml-config-rejects-non-http-scheme
  (ok (signals (parse-toml-config "[upstreams]
local = \"ftp://127.0.0.1:21\"
")
        'invalid-configuration)))

(deftest toml-config-rejects-invalid-toml-syntax
  (ok (signals (parse-toml-config "port =")
        'invalid-configuration)))

(deftest config-file-missing-explicit-path-is-an-error
  (ok (signals (load-config-file #P"/nonexistent/llm-log/config.toml")
        'invalid-configuration)))

(deftest config-file-layer-overrides-defaults
  (uiop:with-temporary-file (:pathname config :suffix ".toml")
    (with-open-file (out config :direction :output :if-exists :supersede)
      (write-string "listen = \"0.0.0.0\"
port = 9001

[upstreams]
ollama = \"http://127.0.0.1:11434\"
" out))
    (let ((config (resolve-config :config-file config)))
      (ok (equal (runtime-config-listen-address config) "0.0.0.0"))
      (ok (eql (runtime-config-port config) 9001))
      (ok (uiop:pathname-equal (runtime-config-data-directory config)
                               (default-data-directory)))
      (ok (equal (upstream-base-url config "ollama") "http://127.0.0.1:11434"))
      (ok (equal (upstream-base-url config "openai") "https://api.openai.com")))))

(deftest cli-arguments-override-config-file-overrides-defaults
  (uiop:with-temporary-file (:pathname config :suffix ".toml")
    (with-open-file (out config :direction :output :if-exists :supersede)
      (write-string "data_dir = \"/tmp/file-data\"
port = 9001
" out))
    (let ((config (resolve-config :config-file config
                                  :data-directory #P"/tmp/cli-data/"
                                  :port 9100)))
      (ok (uiop:pathname-equal (runtime-config-data-directory config)
                               #P"/tmp/cli-data/"))
      (ok (equal (runtime-config-listen-address config) "127.0.0.1"))
      (ok (eql (runtime-config-port config) 9100)))))

(deftest cli-upstream-replaces-same-named-entry-and-adds-new-ones
  (let ((config (resolve-config
                 :config-file nil
                 :upstreams (list (validate-upstream
                                   "openai" "https://openai.example")
                                  (validate-upstream
                                   "ollama" "http://127.0.0.1:11434/")))))
    (ok (equal (upstream-base-url config "openai") "https://openai.example"))
    (ok (equal (upstream-base-url config "ollama") "http://127.0.0.1:11434"))
    (ok (equal (upstream-base-url config "anthropic") "https://api.anthropic.com"))))

(deftest serve-arguments-parse-to-resolved-config
  (uiop:with-temporary-file (:pathname config :suffix ".toml")
    (with-open-file (out config :direction :output :if-exists :supersede)
      (write-string "port = 9001
" out))
    (let ((config (parse-serve-arguments
                   (list "serve" "--config" (namestring config)
                         "--data-dir" "/tmp/cli"
                         "--listen" "0.0.0.0"
                         "--port" "9999"
                         "--upstream" "ollama=http://127.0.0.1:11434/"
                         "--upstream" "openai=https://api.openai.com"))))
      (ok (uiop:pathname-equal (runtime-config-data-directory config)
                               #P"/tmp/cli/"))
      (ok (equal (runtime-config-listen-address config) "0.0.0.0"))
      (ok (eql (runtime-config-port config) 9999))
      (ok (equal (upstream-base-url config "ollama") "http://127.0.0.1:11434"))
      (ok (equal (upstream-base-url config "openai") "https://api.openai.com"))
      (ok (equal (upstream-base-url config "anthropic") "https://api.anthropic.com")))))

(deftest serve-arguments-accept-port-boundaries
  (ok (eql (runtime-config-port
            (parse-serve-arguments (list "serve" "--port" "65535")))
           65535))
  (ok (eql (runtime-config-port
            (parse-serve-arguments (list "serve" "--port" "1")))
           1)))

(deftest serve-arguments-reject-missing-port-value
  (ok (signals (parse-serve-arguments (list "serve" "--port"))
        'invalid-configuration)))

(deftest serve-arguments-reject-non-integer-port
  (ok (signals (parse-serve-arguments (list "serve" "--port" "abc"))
        'invalid-configuration)))

(deftest serve-arguments-reject-out-of-range-port
  (ok (signals (parse-serve-arguments (list "serve" "--port" "65536"))
        'invalid-configuration)))

(deftest serve-arguments-reject-unknown-argument
  (ok (signals (parse-serve-arguments (list "serve" "--wat"))
        'invalid-configuration)))

(deftest serve-arguments-reject-unknown-command
  (ok (signals (parse-serve-arguments (list "run"))
        'invalid-configuration)))

(deftest serve-arguments-reject-empty-invocation
  (ok (signals (parse-serve-arguments nil)
        'invalid-configuration)))

(deftest serve-arguments-reject-upstream-without-value-separator
  (ok (signals (parse-serve-arguments (list "serve" "--upstream" "openai"))
        'invalid-configuration)))

(deftest serve-arguments-reject-upstream-with-empty-name
  (ok (signals (parse-serve-arguments
                (list "serve" "--upstream" "=https://api.openai.com"))
        'invalid-configuration)))

(deftest serve-arguments-reject-upstream-with-empty-url
  (ok (signals (parse-serve-arguments (list "serve" "--upstream" "openai="))
        'invalid-configuration)))

(deftest serve-arguments-reject-explicit-missing-config-file
  (ok (signals (parse-serve-arguments
                (list "serve" "--config" "/nonexistent/llm-log/config.toml"))
        'invalid-configuration)))

{
  description = "All-Common-Lisp transparent LLM capture, analytics, and symbolic expert runtime";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    tek9 = {
      url = "github:lost-rob0t/tek9/a9f5b595f5d965163d2b7c518c72a2efd9be13fe";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, tek9 }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      eachSystem = f: nixpkgs.lib.genAttrs systems (system: f system);
    in
    {
      packages = eachSystem (system:
        let
          pkgs = import nixpkgs { inherit system; };
          cl = pkgs.sbcl.pkgs;
          tek9Package = tek9.packages.${system}.tek9;

          expertLib = pkgs.sbcl.buildASDFSystem {
            pname = "llm-log-expert";
            version = "0.2.0";
            src = ./expert;
            systems = [ "llm-log-expert" ];
            lispLibs = [
              tek9Package
              cl.jsown
              cl.woo
              cl.bordeaux-threads
              cl.trivial-utf-8
              cl.ironclad
            ];
          };

          sbclWithExpert = pkgs.sbcl.withPackages (_: [ expertLib ]);
          expertCore = pkgs.runCommand "llm-log-expert-core" {
            nativeBuildInputs = [ sbclWithExpert ];
          } ''
            export HOME="$TMPDIR/home"
            mkdir -p "$HOME" "$out/lib"
            sbcl --noinform --non-interactive \
              --eval '(require :asdf)' \
              --eval '(asdf:load-system :llm-log-expert)' \
              --eval "(sb-ext:save-lisp-and-die \"$out/lib/llm-log-expert.core\")"
          '';

          expertService = pkgs.writeShellApplication {
            name = "llm-log-expert";
            runtimeInputs = [ sbclWithExpert pkgs.swi-prolog ];
            text = ''
              : "''${LLM_LOG_PROLOG_WORKER:=${./expert/prolog/worker.pl}}"
              export LLM_LOG_PROLOG_WORKER
              exec sbcl --noinform --core ${expertCore}/lib/llm-log-expert.core \
                --no-sysinit --no-userinit --non-interactive \
                --eval '(uiop:quit (llm-log-expert:main (uiop:command-line-arguments)))' \
                "$@"
            '';
          };

          llmLogLib = pkgs.sbcl.buildASDFSystem {
            pname = "llm-log";
            version = "0.2.0";
            src = ./proxy;
            systems = [ "llm-log" ];
            lispLibs = [
              expertLib
              cl.clop
              cl.woo
              cl.usocket
              cl.quri
              cl.cl_plus_ssl
              cl.bordeaux-threads
              cl.trivial-utf-8
              cl.jsown
              cl.ironclad
            ];
          };

          llmLogTests = pkgs.sbcl.buildASDFSystem {
            pname = "llm-log-tests";
            version = "0.2.0";
            src = ./proxy;
            systems = [ "llm-log-tests" ];
            lispLibs = [ llmLogLib cl.rove cl.bordeaux-threads cl.usocket ];
          };

          sbclWithRuntime = pkgs.sbcl.withPackages (_: [ llmLogLib ]);
          sbclWithTests = pkgs.sbcl.withPackages (_: [ llmLogTests ]);

          llmLog = pkgs.writeShellApplication {
            name = "llm-log";
            runtimeInputs = [ sbclWithRuntime pkgs.swi-prolog ];
            text = ''
              : "''${LLM_LOG_PROLOG_WORKER:=${./expert/prolog/worker.pl}}"
              export LLM_LOG_PROLOG_WORKER
              exec sbcl --noinform --no-userinit --no-sysinit --non-interactive \
                --load ${./proxy/entrypoint.lisp} "$@"
            '';
          };
        in
        {
          default = llmLog;
          llm-log = llmLog;
          llm-log-lib = llmLogLib;
          llm-log-tests = llmLogTests;
          llm-log-sbcl = sbclWithTests;
          llm-log-expert-lib = expertLib;
          llm-log-expert = expertService;
        });

      homeManagerModules = {
        default = import ./nix/home-manager.nix { inherit self; };
        llm-log = self.homeManagerModules.default;
      };

      devShells = eachSystem (system:
        let pkgs = import nixpkgs { inherit system; };
        in {
          default = pkgs.mkShell {
            packages = [
              pkgs.sbcl
              pkgs.swi-prolog
              tek9.packages.${system}.tek9
              self.packages.${system}.llm-log
              self.packages.${system}.llm-log-expert
            ];
          };
        });

      checks = eachSystem (system:
        let
          pkgs = import nixpkgs { inherit system; };
          cl = pkgs.sbcl.pkgs;
          expertLib = self.packages.${system}.llm-log-expert-lib;
          transportTestSbcl = pkgs.sbcl.withPackages (_: [ expertLib cl.rove ]);
        in
        {
          package = self.packages.${system}.default;
          expert-lib = expertLib;

          source-language-contract = pkgs.runCommand "llm-log-source-language-contract" { } ''
            bad="$(find ${self} -type f \( -name '*.py' -o -name 'pyproject.toml' \) -print -quit)"
            if [ -n "$bad" ]; then
              echo "Python source is forbidden in llm-log: $bad" >&2
              exit 1
            fi
            touch "$out"
          '';

          common-lisp-transport-contract = pkgs.runCommand "llm-log-common-lisp-transport-contract" {
            nativeBuildInputs = [ transportTestSbcl ];
          } ''
            export HOME="$TMPDIR/home"
            mkdir -p "$HOME"
            sbcl --noinform --non-interactive \
              --eval '(require :asdf)' \
              --eval '(asdf:load-asd #P"${self}/expert/llm-log-expert-test.asd")' \
              --eval '(asdf:test-system "llm-log-expert-test")'
            touch "$out"
          '';

          common-lisp-expert-integration-contract = pkgs.runCommand "llm-log-common-lisp-expert-integration-contract" {
            nativeBuildInputs = [ transportTestSbcl pkgs.swi-prolog ];
          } ''
            export HOME="$TMPDIR/home"
            unset LLM_LOG_PROLOG_WORKER || true
            mkdir -p "$HOME"
            sbcl --noinform --non-interactive \
              --eval '(require :asdf)' \
              --eval '(asdf:load-asd #P"${self}/expert/llm-log-expert-integration-test.asd")' \
              --eval '(asdf:test-system "llm-log-expert-integration-test")'
            touch "$out"
          '';

          llm-log-runtime-contract = pkgs.runCommand "llm-log-runtime-contract" {
            nativeBuildInputs = [ self.packages.${system}.llm-log-sbcl pkgs.swi-prolog ];
          } ''
            export HOME="$TMPDIR/home"
            mkdir -p "$HOME"
            sbcl --noinform --no-userinit --no-sysinit --non-interactive \
              --load ${./proxy/tests/runner.lisp}
            touch "$out"
          '';
        });
    };
}

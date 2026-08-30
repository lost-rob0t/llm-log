{
  description = "Transparent LLM capture proxy with a Prolog sidecar KB";

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
          llmLog = pkgs.callPackage ./nix/package.nix { };
          tek9Package = tek9.packages.${system}.tek9;
          expertLib = pkgs.sbcl.buildASDFSystem {
            pname = "llm-log-expert";
            version = "0.1.0";
            src = ./expert;
            systems = [ "llm-log-expert" ];
            lispLibs = [ tek9Package cl.jsown ];
          };
          sbclWithExpert = pkgs.sbcl.withPackages (_: [ expertLib ]);
          expertService = pkgs.writeShellApplication {
            name = "llm-log-expert";
            runtimeInputs = [ sbclWithExpert pkgs.swi-prolog ];
            text = ''
              export LLM_LOG_PROLOG_WORKER="${./expert/prolog/worker.pl}"
              exec sbcl --noinform --script ${./expert/entrypoint.lisp} "$@"
            '';
          };
          # Common Lisp runtime (zero-Python rewrite, research 012). The
          # wrapper-provided ASDF must be loaded before require :asdf in every
          # Nix-run entrypoint, see research/LLM-LOG-RESEARCH-012.
          llmLogClLib = pkgs.sbcl.buildASDFSystem {
            pname = "llm-log";
            version = "0.1.0";
            src = ./proxy;
            systems = [ "llm-log" ];
            lispLibs = with cl; [ clop woo usocket quri cl_plus_ssl
                                  bordeaux-threads trivial-utf-8 ];
          };
          llmLogClTests = pkgs.sbcl.buildASDFSystem {
            pname = "llm-log-tests";
            version = "0.1.0";
            src = ./proxy;
            systems = [ "llm-log-tests" ];
            lispLibs = [ llmLogClLib cl.rove cl.bordeaux-threads cl.usocket ];
          };
          sbclWithClTests = pkgs.sbcl.withPackages (_: [ llmLogClTests ]);
          sbclWithClRuntime = pkgs.sbcl.withPackages (_: [ llmLogClLib ]);
          llmLogCl = pkgs.writeShellApplication {
            name = "llm-log";
            runtimeInputs = [ sbclWithClRuntime ];
            text = ''
              exec sbcl --noinform --no-userinit --no-sysinit --non-interactive \
                --load ${./proxy/entrypoint.lisp} "$@"
            '';
          };
        in
        {
          default = llmLog;
          llm-log = llmLog;
          llm-log-expert-lib = expertLib;
          llm-log-expert = expertService;
          llm-log-cl-lib = llmLogClLib;
          llm-log-cl-tests = llmLogClTests;
          llm-log-cl = llmLogCl;
          # Standalone SBCL closure carrying the Common Lisp runtime system;
          # also the interpreter used by the CL contract checks.
          llm-log-cl-sbcl = sbclWithClTests;
        });

      homeManagerModules = {
        default = import ./nix/home-manager.nix { inherit self; };
        llm-log = self.homeManagerModules.default;
      };

      devShells = eachSystem (system:
        let
          pkgs = import nixpkgs { inherit system; };
          python = pkgs.python312.withPackages (ps: [ ps.aiohttp ]);
        in
        {
          default = pkgs.mkShell {
            packages = [
              python
              pkgs.sbcl
              pkgs.swi-prolog
              tek9.packages.${system}.tek9
              self.packages.${system}.llm-log-expert
              self.packages.${system}.llm-log-cl-lib
            ];
          };
        });

      checks = eachSystem (system:
        let
          pkgs = import nixpkgs { inherit system; };
          cl = pkgs.sbcl.pkgs;
          python = pkgs.python312.withPackages (ps: [ ps.aiohttp ]);
          expertLib = self.packages.${system}.llm-log-expert-lib;
          expertService = self.packages.${system}.llm-log-expert;
          transportTestSbcl = pkgs.sbcl.withPackages (_: [ expertLib cl.rove ]);
        in
        {
          package = self.packages.${system}.default;
          expert-lib = expertLib;

          # Migration-only historical evidence.  This remains Python-backed
          # until equivalent CL/Prolog black-box contracts replace it.
          expert-service-contract = pkgs.runCommand "llm-log-expert-service-contract" {
            nativeBuildInputs = [ python expertService ];
          } ''
            export HOME="$TMPDIR/home"
            export LLM_LOG_EXPERT_BIN="${expertService}/bin/llm-log-expert"
            mkdir -p "$HOME"
            cd ${self}
            python -m unittest \
              tests.test_expert_service_red \
              tests.test_reasoner_result_validation_red \
              -v
            touch "$out"
          '';

          # Authoritative zero-Python transport RED/GREEN gate.
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

          llm-log-config-contract = pkgs.runCommand "llm-log-config-contract" {
            nativeBuildInputs = [ self.packages.${system}.llm-log-cl-sbcl ];
          } ''
            export HOME="$TMPDIR/home"
            mkdir -p "$HOME"
            sbcl --noinform --no-userinit --no-sysinit --non-interactive \
              --load ${./proxy/tests/runner.lisp}
            touch "$out"
          '';

          # RESEARCH-019 RED: this must fail on the untouched dependency
          # closure until Sento/cl-gserver and every transitive Lisp system
          # are explicitly pinned and packaged. Ambient Quicklisp is forbidden.
          common-lisp-recorder-deps = pkgs.runCommand "llm-log-common-lisp-recorder-deps" {
            nativeBuildInputs = [ transportTestSbcl ];
          } ''
            export HOME="$TMPDIR/home"
            mkdir -p "$HOME"
            sbcl --noinform --non-interactive \
              --eval '(require :asdf)' \
              --eval '(asdf:load-system :sento)'
            touch "$out"
          '';
        });
    };
}

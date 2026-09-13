{ self, pkgs, lib }:

let
  proxyPackage = pkgs.writeShellScriptBin "llm-log" ''
    exit 0
  '';

  expertPackage = pkgs.writeShellScriptBin "llm-log-expert" ''
    exit 0
  '';

  quotaPackage = pkgs.writeShellScriptBin "llm-log-quotas" "exit 0";
  codexPackage = pkgs.writeShellScriptBin "codex" "exit 0";

  evaluated = lib.evalModules {
    specialArgs = { inherit pkgs; };
    modules = [
      ({ lib, ... }: {
        options = {
          home.homeDirectory = lib.mkOption { type = lib.types.str; };
          home.packages = lib.mkOption {
            type = lib.types.listOf lib.types.package;
            default = [ ];
          };
          home.sessionVariables = lib.mkOption { type = lib.types.attrsOf lib.types.str; default = { }; };
          assertions = lib.mkOption { type = lib.types.listOf lib.types.attrs; default = [ ]; };
          xdg.dataHome = lib.mkOption { type = lib.types.str; };
          xdg.cacheHome = lib.mkOption { type = lib.types.str; };
          systemd.user.services = lib.mkOption {
            # Like Home Manager, evaluate each service as a module definition.
            # An opaque attrs stub leaves nested mkIf values unevaluated.
            type = lib.types.attrsOf (lib.types.submodule {
              options = {
                Unit = lib.mkOption { type = lib.types.attrs; default = { }; };
                Service = lib.mkOption { type = lib.types.attrs; default = { }; };
                Install = lib.mkOption { type = lib.types.attrs; default = { }; };
              };
            });
            default = { };
          };
        };
      })

      (import ../home-manager.nix { inherit self; })

      {
        home.homeDirectory = "/home/test";
        xdg.dataHome = "/home/test/.local/share";
        xdg.cacheHome = "/home/test/.cache";

        services.llm-log = {
          enable = true;
          package = proxyPackage;
          dataDir = "/home/test/Documents/AI/proxy";
          quotas = {
            enable = true;
            package = quotaPackage;
            inherit codexPackage;
            zaiKeyFile = "/home/test/.config/llm-log/zai.key";
            environmentFile = "/home/test/.config/llm-log/quotas.env";
            codexHome = "/home/test/.codex";
          };
          expert = {
            enable = true;
            package = expertPackage;
            dataDir = "/home/test/.llm-proxy/expert";
            require = true;
          };
        };
      }
    ];
  };

  services = evaluated.config.systemd.user.services;
  command = services.llm-log.Service.ExecStart;
  quota = services.llm-log-quotas.Service;
  # Test needles carry no dependency semantics. Nix's regex matcher rejects
  # store contexts; preserve context on the actual command under test instead.
  quotaExecutable = builtins.unsafeDiscardStringContext "${quotaPackage}/bin/llm-log-quotas";
  codexExecutable = builtins.unsafeDiscardStringContext "${codexPackage}/bin/codex";
in
assert lib.hasInfix "--log-dir" command;
assert lib.hasInfix "/home/test/Documents/AI/proxy" command;
assert lib.hasInfix "--expert-service-bin" command;
assert lib.hasInfix "--expert-data-dir" command;
assert lib.hasInfix "/home/test/.llm-proxy/expert" command;
assert lib.hasInfix "--require-expert-plane" command;
assert !(builtins.hasAttr "llm-log-expert" services);
assert lib.all (entry: entry.assertion) evaluated.config.assertions;
assert lib.hasInfix quotaExecutable quota.ExecStart;
assert lib.hasInfix codexExecutable quota.ExecStart;
assert lib.hasInfix "/home/test/.cache/llm-log/quotas.json" quota.ExecStart;
assert lib.hasInfix "--zai-key-file" quota.ExecStart;
assert quota.EnvironmentFile == [ "-/home/test/.config/llm-log/quotas.env" ];
assert quota.Environment == [ "CODEX_HOME=/home/test/.codex" ];
assert quota.UMask == "0077";
assert evaluated.config.home.sessionVariables.LLM_LOG_QUOTA_CACHE == "/home/test/.cache/llm-log/quotas.json";
pkgs.runCommand "llm-log-home-manager-expert-contract" { } ''
  touch "$out"
''

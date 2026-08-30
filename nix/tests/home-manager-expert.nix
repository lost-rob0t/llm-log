{ self, pkgs, lib }:

let
  proxyPackage = pkgs.writeShellScriptBin "llm-log" ''
    exit 0
  '';

  expertPackage = pkgs.writeShellScriptBin "llm-log-expert" ''
    exit 0
  '';

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
          xdg.dataHome = lib.mkOption { type = lib.types.str; };
          systemd.user.services = lib.mkOption {
            type = lib.types.attrs;
            default = { };
          };
        };
      })

      (import ../home-manager.nix { inherit self; })

      {
        home.homeDirectory = "/home/test";
        xdg.dataHome = "/home/test/.local/share";

        services.llm-log = {
          enable = true;
          package = proxyPackage;
          dataDir = "/home/test/Documents/AI/proxy";
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

  service = evaluated.config.systemd.user.services.llm-log;
  command = service.Service.ExecStart;
in
assert lib.hasInfix "${proxyPackage}/bin/llm-log" command;
assert lib.hasInfix "--log-dir" command;
assert lib.hasInfix "/home/test/Documents/AI/proxy" command;
assert lib.hasInfix "--expert-service-bin" command;
assert lib.hasInfix "${expertPackage}/bin/llm-log-expert" command;
assert lib.hasInfix "--expert-data-dir" command;
assert lib.hasInfix "/home/test/.llm-proxy/expert" command;
assert lib.hasInfix "--require-expert-plane" command;
assert !(builtins.hasAttr "llm-log-expert" evaluated.config.systemd.user.services);
pkgs.runCommand "llm-log-home-manager-expert-contract" { } ''
  touch "$out"
''

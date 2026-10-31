{ self, pkgs, lib }:
let
  package = pkgs.writeShellScriptBin "llm-log-quotas" "exit 0";
  codexPackage = pkgs.writeShellScriptBin "codex" "exit 0";
  evaluate = proxyEnabled: quotaEnabled: quotaPort: lib.evalModules {
    specialArgs = { inherit pkgs; };
    modules = [
      ({ lib, ... }: {
        options = {
          home.homeDirectory = lib.mkOption { type = lib.types.str; };
          home.packages = lib.mkOption { type = lib.types.listOf lib.types.package; default = [ ]; };
          xdg.dataHome = lib.mkOption { type = lib.types.str; };
          assertions = lib.mkOption { type = lib.types.listOf lib.types.attrs; default = [ ]; };
          systemd.user.services = lib.mkOption {
            type = lib.types.attrsOf (lib.types.submodule { freeformType = lib.types.attrs; });
            default = { };
          };
        };
      })
      (import ../home-manager.nix { inherit self; })
      {
        home.homeDirectory = "/home/test";
        xdg.dataHome = "/home/test/.local/share";
        services.llm-log = {
          enable = proxyEnabled;
          inherit package;
          quotas = {
            enable = quotaEnabled;
            inherit package codexPackage;
            port = quotaPort;
            zaiKeyFile = "/run/user/1000/zai-key";
            environmentFile = "/run/user/1000/quota.env";
            codexHome = "/home/test/.codex";
          };
        };
      }
    ];
  };
  quotaOnly = (evaluate false true 8788).config;
  disabled = (evaluate false false 8788).config;
  collision = (evaluate true true 8787).config;
  command = quotaOnly.systemd.user.services.llm-log-quotas.Service.ExecStart;
  contains = value: lib.hasInfix value (builtins.unsafeDiscardStringContext command);
in
assert builtins.attrNames quotaOnly.systemd.user.services == [ "llm-log-quotas" ];
assert builtins.attrNames disabled.systemd.user.services == [ ];
assert lib.all (item: item.assertion) quotaOnly.assertions;
assert !(lib.all (item: item.assertion) collision.assertions);
assert contains "/bin/llm-log-quotas";
assert contains "--codex";
assert contains "--cache";
assert contains "/run/user/1000/zai-key";
assert quotaOnly.systemd.user.services.llm-log-quotas.Service.UMask == "0077";
assert quotaOnly.systemd.user.services.llm-log-quotas.Service.Environment == [ "CODEX_HOME=/home/test/.codex" ];
assert quotaOnly.systemd.user.services.llm-log-quotas.Service.EnvironmentFile == [ "-/run/user/1000/quota.env" ];
pkgs.runCommand "llm-log-home-manager-quota-contract" { } ''
  touch "$out"
''

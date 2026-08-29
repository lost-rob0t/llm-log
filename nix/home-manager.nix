{ self }:
{ config, lib, pkgs, ... }:

let
  cfg = config.services.llm-log;
  inherit (lib) concatMapStringsSep escapeShellArg mkEnableOption mkIf mkOption optionalString types;
  system = pkgs.stdenv.hostPlatform.system;
  upstreamNames = builtins.sort builtins.lessThan (builtins.attrNames cfg.upstreams);
  upstreamArgs = concatMapStringsSep " "
    (name: "--upstream ${escapeShellArg "${name}=${cfg.upstreams.${name}}"}")
    upstreamNames;
  extraArgs = concatMapStringsSep " " escapeShellArg cfg.extraArgs;
  classifierArg = optionalString (!cfg.enablePrologClassifier) "--no-prolog-classifier";
  command = concatMapStringsSep " " (value: value) (builtins.filter (value: value != "") [
    "${cfg.package}/bin/llm-log"
    "serve"
    "--listen ${escapeShellArg cfg.listenAddress}"
    "--port ${toString cfg.port}"
    "--log-dir ${escapeShellArg cfg.dataDir}"
    upstreamArgs
    classifierArg
    extraArgs
  ]);
in
{
  options.services.llm-log = {
    enable = mkEnableOption "transparent llm-log capture proxy";

    package = mkOption {
      type = types.package;
      default = self.packages.${system}.default;
      defaultText = lib.literalExpression "inputs.llm-log.packages.${pkgs.stdenv.hostPlatform.system}.default";
      description = "llm-log package to run.";
    };

    listenAddress = mkOption {
      type = types.str;
      default = "127.0.0.1";
      description = "Address for the local proxy listener.";
    };

    port = mkOption {
      type = types.port;
      default = 8787;
      description = "Port for the local proxy listener.";
    };

    dataDir = mkOption {
      type = types.str;
      default = "${config.xdg.dataHome}/llm-log";
      defaultText = lib.literalExpression ''"${config.xdg.dataHome}/llm-log"'';
      description = "Append-only capture directory. Defaults under XDG_DATA_HOME.";
    };

    enablePrologClassifier = mkOption {
      type = types.bool;
      default = true;
      description = "Classify captured requests with the bundled SWI-Prolog classifier.";
    };

    upstreams = mkOption {
      type = types.attrsOf types.str;
      default = {
        openai = "https://api.openai.com";
        openrouter = "https://openrouter.ai";
        anthropic = "https://api.anthropic.com";
        chatgpt = "https://chatgpt.com";
      };
      description = "Provider-prefix to upstream base URL mapping.";
    };

    extraArgs = mkOption {
      type = types.listOf types.str;
      default = [ ];
      description = "Additional command-line arguments passed to llm-log serve.";
    };
  };

  config = mkIf cfg.enable {
    home.packages = [ cfg.package ];

    systemd.user.services.llm-log = {
      Unit = {
        Description = "Transparent LLM capture proxy";
        After = [ "network-online.target" ];
        Wants = [ "network-online.target" ];
      };

      Service = {
        ExecStart = command;
        Restart = "on-failure";
        RestartSec = 2;
      };

      Install.WantedBy = [ "default.target" ];
    };
  };
}

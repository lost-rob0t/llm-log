{ self }:
{ config, lib, pkgs, ... }:

let
  cfg = config.services.llm-log;
  inherit (lib) concatMapStringsSep escapeShellArg mkEnableOption mkIf mkOption types;
  system = pkgs.stdenv.hostPlatform.system;
  upstreamNames = builtins.sort builtins.lessThan (builtins.attrNames cfg.upstreams);
  upstreamArgs = concatMapStringsSep " "
    (name: "--upstream ${escapeShellArg "${name}=${cfg.upstreams.${name}}"}")
    upstreamNames;
  extraArgs = concatMapStringsSep " " escapeShellArg cfg.extraArgs;
  command = concatMapStringsSep " " (value: value) [
    "${cfg.package}/bin/llm-log"
    "serve"
    "--listen ${escapeShellArg cfg.listenAddress}"
    "--port ${toString cfg.port}"
    "--data-dir ${escapeShellArg cfg.dataDir}"
    "--expert-data-dir ${escapeShellArg cfg.expert.dataDir}"
    upstreamArgs
    extraArgs
  ];
in
{
  options.services.llm-log = {
    enable = mkEnableOption "all-Common-Lisp transparent llm-log capture proxy";

    package = mkOption {
      type = types.package;
      default = self.packages.${system}.default;
      defaultText = lib.literalExpression "inputs.llm-log.packages.${pkgs.stdenv.hostPlatform.system}.default";
      description = "All-Common-Lisp llm-log runtime package.";
    };

    listenAddress = mkOption {
      type = types.str;
      default = "127.0.0.1";
      description = "Address for the local proxy/API listener.";
    };

    port = mkOption {
      type = types.port;
      default = 8787;
      description = "Port for the local proxy/API listener.";
    };

    dataDir = mkOption {
      type = types.str;
      default = "${config.xdg.dataHome}/llm-log";
      defaultText = lib.literalExpression ''"${config.xdg.dataHome}/llm-log"'';
      description = "Append-only raw capture directory.";
    };

    # Compatibility option retained for existing consumers. Classification is
    # always owned by the in-process CL/Tek9/SWI expert.
    enablePrologClassifier = mkOption {
      type = types.bool;
      default = true;
      description = "Compatibility flag; classification is an in-process Common Lisp expert feature.";
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

    expert = {
      # The expert is now a mandatory in-process runtime component. Keep this
      # option for consumer compatibility, but default it on and do not use it
      # to fork a child process or disable the embedded expert.
      enable = mkOption {
        type = types.bool;
        default = true;
        description = "Compatibility flag; the Common Lisp expert is embedded in llm-log.";
      };

      package = mkOption {
        type = types.package;
        default = self.packages.${system}.llm-log-expert;
        defaultText = lib.literalExpression "inputs.llm-log.packages.${pkgs.stdenv.hostPlatform.system}.llm-log-expert";
        description = "Standalone CL expert package for bulk administration or remote deployment.";
      };

      dataDir = mkOption {
        type = types.str;
        default = "${config.xdg.dataHome}/llm-log/expert";
        defaultText = lib.literalExpression ''"${config.xdg.dataHome}/llm-log/expert"'';
        description = "Mutable Tek9/SWI expert state used directly by the CL runtime.";
      };

      require = mkOption {
        type = types.bool;
        default = false;
        description = "Compatibility option retained for existing configurations; the embedded expert is always present.";
      };
    };

    extraArgs = mkOption {
      type = types.listOf types.str;
      default = [ ];
      description = "Additional command-line arguments passed to llm-log serve.";
    };
  };

  config = mkIf cfg.enable {
    home.packages = [ cfg.package cfg.expert.package ];

    systemd.user.services.llm-log = {
      Unit = {
        Description = "All-Common-Lisp LLM capture and expert runtime";
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

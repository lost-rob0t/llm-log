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
  expertServiceArg = optionalString cfg.expert.enable
    "--expert-service-bin ${escapeShellArg "${cfg.expert.package}/bin/llm-log-expert"}";
  expertDataDirArg = optionalString cfg.expert.enable
    "--expert-data-dir ${escapeShellArg cfg.expert.dataDir}";
  requireExpertArg = optionalString (cfg.expert.enable && cfg.expert.require)
    "--require-expert-plane";
  command = concatMapStringsSep " " (value: value) (builtins.filter (value: value != "") [
    "${cfg.package}/bin/llm-log"
    "serve"
    "--listen ${escapeShellArg cfg.listenAddress}"
    "--port ${toString cfg.port}"
    "--log-dir ${escapeShellArg cfg.dataDir}"
    upstreamArgs
    classifierArg
    expertServiceArg
    expertDataDirArg
    requireExpertArg
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

    expert = {
      enable = mkEnableOption "Common Lisp llm-log expert plane";

      package = mkOption {
        type = types.package;
        default = self.packages.${system}.llm-log-expert;
        defaultText = lib.literalExpression "inputs.llm-log.packages.${pkgs.stdenv.hostPlatform.system}.llm-log-expert";
        description = "Packaged Common Lisp expert service launched as a child of llm-log.";
      };

      dataDir = mkOption {
        type = types.str;
        default = "${config.home.homeDirectory}/.llm-proxy/expert";
        defaultText = lib.literalExpression ''"${config.home.homeDirectory}/.llm-proxy/expert"'';
        description = "Mutable Tek9/expert-plane state directory, separate from append-only capture evidence.";
      };

      require = mkOption {
        type = types.bool;
        default = false;
        description = "Fail closed before upstream contact when the configured expert plane is unavailable.";
      };
    };

    extraArgs = mkOption {
      type = types.listOf types.str;
      default = [ ];
      description = "Additional command-line arguments passed to llm-log serve.";
    };

    quotas = {
      enable = mkEnableOption "independent provider-reported subscription quota telemetry";
      package = mkOption {
        type = types.package;
        default = self.packages.${system}.llm-log;
        description = "Python package providing llm-log-quotas, independent of the proxy package.";
      };
      codexPackage = mkOption {
        type = types.package;
        default = pkgs.codex;
        description = "Codex App Server executable using an existing ChatGPT login.";
      };
      port = mkOption { type = types.port; default = 8788; };
      refreshSeconds = mkOption { type = types.ints.between 30 3600; default = 60; };
      cacheFile = mkOption {
        type = types.str;
        default = "${config.home.homeDirectory}/.cache/llm-log/quotas.json";
        description = "Atomic sanitized snapshot path, containing no credentials.";
      };
      zaiKeyFile = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = "Runtime owner-only z.AI key file; use an absolute string, never a Nix path or inline secret.";
      };
      environmentFile = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = "Optional private runtime environment file; never copied into the Nix store.";
      };
      codexHome = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = "Existing Codex configuration directory; null preserves defaults.";
      };
    };
  };

  config = lib.mkMerge [ (mkIf cfg.enable {
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
  }) (mkIf cfg.quotas.enable {
    home.packages = [ cfg.quotas.package ];
    assertions = [ {
      assertion = cfg.quotas.port >= 1024 && (!cfg.enable || cfg.quotas.port != cfg.port);
      message = "llm-log quota port must be unprivileged and distinct from the enabled proxy port.";
    } ];
    systemd.user.services.llm-log-quotas = {
      Unit = {
        Description = "Provider-reported z.AI and GPT Codex subscription quotas";
        After = [ "network-online.target" ];
        Wants = [ "network-online.target" ];
      };
      Service = {
        ExecStart = lib.escapeShellArgs (
          [ "${cfg.quotas.package}/bin/llm-log-quotas"
            "--port" (toString cfg.quotas.port)
            "--refresh" (toString cfg.quotas.refreshSeconds)
            "--cache" cfg.quotas.cacheFile
            "--codex" "${cfg.quotas.codexPackage}/bin/codex" ]
          ++ lib.optionals (cfg.quotas.zaiKeyFile != null) [ "--zai-key-file" cfg.quotas.zaiKeyFile ]
        );
        Environment = lib.optional (cfg.quotas.codexHome != null) "CODEX_HOME=${cfg.quotas.codexHome}";
        EnvironmentFile = lib.optional (cfg.quotas.environmentFile != null) "-${cfg.quotas.environmentFile}";
        Restart = "on-failure";
        RestartSec = 5;
        UMask = "0077";
        NoNewPrivileges = true;
      };
      Install.WantedBy = [ "default.target" ];
    };
  }) ];
}

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
  admissionGroupNames = builtins.sort builtins.lessThan (builtins.attrNames cfg.admission.providerGroups);
  admissionGroupArgs = concatMapStringsSep " "
    (name: "--admission-provider-group ${escapeShellArg "${name}=${cfg.admission.providerGroups.${name}}"}")
    admissionGroupNames;
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
    "--admission-max-active ${toString cfg.admission.maxActive}"
    "--admission-max-queue-depth ${toString cfg.admission.maxQueueDepth}"
    "--admission-queue-timeout-seconds ${toString cfg.admission.queueTimeoutSeconds}"
    "--admission-requests-per-minute ${toString cfg.admission.requestsPerMinute}"
    "--admission-burst ${toString cfg.admission.burst}"
    "--admission-retry-after-seconds ${toString cfg.admission.retryAfterSeconds}"
    admissionGroupArgs
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

    admission = {
      maxActive = mkOption {
        type = types.ints.positive;
        default = 4;
        description = "Maximum active upstream requests per admission group.";
      };

      maxQueueDepth = mkOption {
        type = types.ints.unsigned;
        default = 32;
        description = "Maximum FIFO waiters per admission group.";
      };

      queueTimeoutSeconds = mkOption {
        type = types.ints.positive;
        default = 10;
        description = "Maximum transparent queue wait before a local HTTP 429.";
      };

      requestsPerMinute = mkOption {
        type = types.ints.unsigned;
        default = 60;
        description = "Process-local request-start token rate; 0 disables the rate bucket.";
      };

      burst = mkOption {
        type = types.ints.positive;
        default = 4;
        description = "Maximum request-start token burst per admission group.";
      };

      retryAfterSeconds = mkOption {
        type = types.ints.positive;
        default = 1;
        description = "Minimum Retry-After value for local admission 429 responses.";
      };

      providerGroups = mkOption {
        type = types.attrsOf types.str;
        default = { };
        description = "Trusted provider-prefix aliases that share one admission/quota group.";
      };
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

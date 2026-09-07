{ self }:
{ config, lib, pkgs, ... }:

let
  cfg = config.services.llm-log;
  inherit (lib) concatMapStringsSep concatStringsSep escapeShellArg mkEnableOption mkIf mkOption optionalString types;
  system = pkgs.stdenv.hostPlatform.system;
  upstreamNames = builtins.sort builtins.lessThan (builtins.attrNames cfg.upstreams);
  upstreamArgs = concatMapStringsSep " "
    (name: "--upstream ${escapeShellArg "${name}=${cfg.upstreams.${name}}"}")
    upstreamNames;
  quantizationPresetNames = builtins.sort builtins.lessThan (builtins.attrNames cfg.quantizationPresets);
  quantizationPresetArgs = concatMapStringsSep " "
    (name:
      "--quantization-preset ${escapeShellArg "${name}=${concatStringsSep "," cfg.quantizationPresets.${name}}"}")
    quantizationPresetNames;
  selectedQuantizationPresetArg =
    "--openrouter-quantization-preset ${escapeShellArg cfg.openrouterQuantizationPreset}";
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
    quantizationPresetArgs
    selectedQuantizationPresetArg
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

    quantizationPresets = mkOption {
      type = types.attrsOf (types.listOf (types.enum [
        "fp32"
        "fp16"
        "bf16"
        "fp8"
        "int8"
        "fp6"
        "fp4"
        "int4"
        "unknown"
      ]));
      default = {
        high-precision = [ "fp32" "fp16" "bf16" "fp8" ];
        balanced = [ "fp32" "fp16" "bf16" "fp8" "int8" "fp6" ];
        all-known = [ "fp32" "fp16" "bf16" "fp8" "int8" "fp6" "fp4" "int4" ];
      };
      description = ''
        Named OpenRouter quantization allowlists. The defaults deliberately omit
        "unknown" so endpoints without disclosed precision are not eligible.
      '';
    };

    openrouterQuantizationPreset = mkOption {
      type = types.str;
      default = "high-precision";
      description = "Quantization preset enforced for OpenRouter inference requests.";
    };

    extraArgs = mkOption {
      type = types.listOf types.str;
      default = [ ];
      description = "Additional command-line arguments passed to llm-log serve.";
    };
  };

  config = mkIf cfg.enable {
    assertions = [
      {
        assertion = builtins.hasAttr cfg.openrouterQuantizationPreset cfg.quantizationPresets;
        message = "services.llm-log.openrouterQuantizationPreset must name a configured quantizationPresets entry";
      }
    ];

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

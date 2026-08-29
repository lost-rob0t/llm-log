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
        in
        {
          default = llmLog;
          llm-log = llmLog;
          llm-log-expert-lib = expertLib;
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
            ];
          };
        });

      checks = eachSystem (system: {
        package = self.packages.${system}.default;
        expert-lib = self.packages.${system}.llm-log-expert-lib;
      });
    };
}

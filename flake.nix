{
  description = "Transparent LLM capture proxy with a Prolog sidecar KB";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      eachSystem = f: nixpkgs.lib.genAttrs systems (system: f system);
    in
    {
      packages = eachSystem (system:
        let
          pkgs = import nixpkgs { inherit system; };
          llmLog = pkgs.callPackage ./nix/package.nix { };
        in
        {
          default = llmLog;
          llm-log = llmLog;
        });

      apps = eachSystem (system:
        let
          app = {
            type = "app";
            program = "${self.packages.${system}.default}/bin/llm-log";
          };
        in
        {
          default = app;
          llm-log = app;
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
            packages = [ python pkgs.swi-prolog ];
          };
        });

      checks = eachSystem (system: {
        package = self.packages.${system}.default;
      });
    };
}

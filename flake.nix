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
          pythonPackages = pkgs.python312Packages;
        in
        rec {
          default = llm-log;
          llm-log = pythonPackages.buildPythonApplication {
            pname = "llm-log";
            version = "0.1.0";
            src = self;
            pyproject = true;

            build-system = [ pythonPackages.setuptools ];
            dependencies = [ pythonPackages.aiohttp ];
            nativeBuildInputs = [ pkgs.makeWrapper ];
            nativeCheckInputs = [ pkgs.swiProlog ];

            checkPhase = ''
              runHook preCheck
              ${pkgs.python312.interpreter} -m unittest discover -s tests -v
              runHook postCheck
            '';

            postInstall = ''
              wrapProgram "$out/bin/llm-log" \
                --prefix PATH : ${pkgs.lib.makeBinPath [ pkgs.swiProlog ]}
            '';
          };
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
            packages = [ python pkgs.swiProlog ];
          };
        });

      checks = eachSystem (system: {
        package = self.packages.${system}.default;
      });
    };
}

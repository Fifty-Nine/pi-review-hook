{
  description = "Agentic AI code review pre-commit hook using pi";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = {
    self,
    nixpkgs,
  }: let
      systems = ["x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin"];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (s: f nixpkgs.legacyPackages.${s});
    in {
      packages = forAllSystems (pkgs: {
        default = pkgs.python3Packages.buildPythonApplication {
          pname = "pi-review-precommit";
          version = "0.1.0";
          src = self;
          pyproject = true;

          build-system = [
            pkgs.python3Packages.hatchling
          ];

          # No runtime dependencies — standard library only.
        };
      });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [pkgs.python3 pkgs.uv];
        };
      });
    };
}
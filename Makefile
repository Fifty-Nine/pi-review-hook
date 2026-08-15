# Dev shortcuts.
# NixOS notes:
# - uv downloads its own Python which doesn't work on NixOS, so wrap with
#   `nix shell nixpkgs#python3 nixpkgs#uv`.
# - The PyPI ruff binary can't execute on NixOS (stub-ld), so lint/fmt use
#   nixpkgs#ruff directly instead of `uv run ruff`.

.PHONY: test lint fmt build wheel-check try-repo

test:
	nix shell nixpkgs#python3 nixpkgs#uv -c uv run pytest

lint:
	nix shell nixpkgs#ruff -c ruff check src tests

fmt:
	nix shell nixpkgs#ruff -c ruff format src tests

build:
	nix shell nixpkgs#python3 nixpkgs#uv -c uv build

wheel-check: build
	@echo "=== extension.ts in wheel? ==="
	nix shell nixpkgs#unzip -c unzip -l dist/*.whl | grep extension.ts

try-repo:
	@echo "Run from a consumer repo:"
	@echo "  pre-commit try-repo <path-to-this-repo> pi-review --verbose"

# Dev shortcuts. NixOS note: uv downloads its own Python which doesn't work
# on NixOS, so wrap with `nix shell nixpkgs#python3 nixpkgs#uv`.

.PHONY: test lint fmt try-repo build wheel-check

test:
	nix shell nixpkgs#python3 nixpkgs#uv -c uv run pytest

lint:
	nix shell nixpkgs#python3 nixpkgs#uv -c uv run ruff check src tests

fmt:
	nix shell nixpkgs#python3 nixpkgs#uv -c uv run ruff format src tests

build:
	nix shell nixpkgs#python3 nixpkgs#uv -c uv build

wheel-check: build
	@echo "=== extension.ts in wheel? ==="
	unzip -l dist/*.whl | grep extension.ts

try-repo:
	@echo "Run from a consumer repo:"
	@echo "  pre-commit try-repo <path-to-this-repo> pi-review --verbose"

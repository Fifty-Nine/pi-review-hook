# Review Guidelines

These criteria override the default reviewer instructions for this repository.
A **no-go** is required when any of the following apply to the staged changes:

- The change breaks the documented failure-mode matrix (fail-open when pi is
  missing; fail-closed on pi errors, non-compliance, or unrecognized
  decisions) without an explicit, documented reason.
- Behavior changes are not covered by unit tests, or the suite is not green
  (`nix shell nixpkgs#python3 nixpkgs#uv -c uv run pytest`).
- The `extension.ts` tool schema changes without updating the NDJSON parsing
  expectations in `pi_runner.py` and the tests.
- The wheel no longer bundles `extension.ts` (verify with `make wheel-check`).
- The change introduces external Python dependencies (v1 constraint:
  standard library only).
- The change breaks NixOS compatibility (e.g. reverting to a compiled build
  backend such as `uv_build`).
- Documentation (README.md, AGENTS.md, docs/) becomes inconsistent with the
  implementation.
- Secrets, credentials, or API keys are committed.
- The dogfooding configuration (`.pre-commit-config.yaml`) is changed without
  keeping the pinned `rev` in sync with a pushed commit.

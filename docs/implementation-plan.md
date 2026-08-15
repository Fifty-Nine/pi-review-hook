# Implementation Plan: pi-review pre-commit hook

## Overview

A standalone, pre-commit-framework-compatible hook that uses `pi` to perform
agentic code review with context retention across multiple rejected rounds.
The hook gives a go/no-go decision via its exit code. Developed in Python/uv,
packaged as a pip-installable pre-commit hook, pushed to
`git@github.com:Fifty-Nine/pi-review-precommit`.

---

## 1. Repository structure

```
pi-review-precommit/
├── .pre-commit-hooks.yaml          # Hook definition for pre-commit framework
├── .pre-commit-config.yaml         # Self-hosted hooks (linting, formatting)
├── pyproject.toml                  # Python package config (uv / pip)
├── README.md
├── src/
│   └── pi_review_precommit/
│       ├── __init__.py
│       ├── hook.py                 # Main entry point + hook flow
│       ├── config.py               # Argparse + env var configuration
│       ├── state.py                # .git/pi-reviewer/ state management
│       ├── pi_runner.py            # pi invocation + NDJSON parsing
│       ├── prompts.py              # System prompt + per-round prompt construction
│       └── extension.ts            # Bundled pi extension (submit_review_decision tool)
├── tests/
│   ├── test_state.py
│   ├── test_config.py
│   ├── test_pi_runner.py
│   └── test_hook.py
└── Makefile                        # dev shortcuts (lint, test, try-repo)
```

### Key design notes

- **`extension.ts`** is a TypeScript file bundled as Python package data. At
  runtime the hook resolves its path via `importlib.resources` and passes it
  to `pi --extension <path>`.
- **No external Python dependencies** for v1. The hook uses only the standard
  library (`json`, `subprocess`, `argparse`, `pathlib`, `os`, `uuid`,
  `importlib.resources`, `shutil`). This keeps the pre-commit venv minimal
  and avoids dependency issues.
- **`uv`** is used for development workflow (lockfile, venv, running tests).
  The package itself is plain pip-installable for pre-commit compatibility.

---

## 2. `pyproject.toml`

```toml
[project]
name = "pi-review-precommit"
version = "0.1.0"
description = "Agentic AI code review pre-commit hook using pi"
license = "MIT"
requires-python = ">=3.11"
dependencies = []

[project.scripts]
pi-review = "pi_review_precommit.hook:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/pi_review_precommit"]

[tool.hatch.build.targets.wheel.force-include]
"src/pi_review_precommit/extension.ts" = "pi_review_precommit/extension.ts"

[tool.uv]
dev-dependencies = [
    "pytest>=8.0",
    "pytest-cov",
    "ruff",
]
```

### Notes

- `requires-python = ">=3.11"` for `importlib.resources.files()` support and
  modern typing.
- `hatchling` build backend with `force-include` ensures `extension.ts` is
  packaged in the wheel.
- `[project.scripts]` defines the `pi-review` console script that pre-commit's
  `entry` references.
- Empty `dependencies = []` — standard library only for v1.

---

## 3. `.pre-commit-hooks.yaml`

```yaml
- id: pi-review
  name: pi-review
  description: Agentic AI code review using pi with context retention across rounds.
  entry: pi-review
  language: python
  pass_filenames: false
  always_run: true
  require_serial: true
  stages: [pre-commit]
```

### Notes

- `always_run: true` — the hook runs on every commit regardless of which file
  types are staged. The hook computes its own diff internally.
- `pass_filenames: false` — the hook doesn't receive filename arguments; it
  uses `git diff --cached` and `git write-tree` directly.
- `require_serial: true` — the hook is stateful (manages session state in
  `.git/`); must not run in parallel.
- `stages: [pre-commit]` — only runs at the pre-commit stage.
- Consumers can pass `args` (e.g., `args: ["--model", "glm-5.2"]`) and these
  are forwarded before any filenames (which are absent due to
  `pass_filenames: false`).

---

## 4. TypeScript extension file

**File: `src/pi_review_precommit/extension.ts`**

```typescript
import { Type } from "@earendil-works/pi-ai";
import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";

const reviewDecisionTool = defineTool({
  name: "submit_review_decision",
  label: "Submit Review Decision",
  description:
    "Submit your go/no-go review decision. " +
    "You MUST call this tool at the end of your review turn. " +
    "Provide 'go' if the changes are acceptable, 'no-go' if they have blocking issues.",
  parameters: Type.Object({
    // Only required field — keeps schema minimal to avoid tool-call failures.
    decision: Type.String({
      description: "Your decision: 'go' or 'no-go'",
    }),

    // Optional but encouraged fields.
    issues: Type.Optional(
      Type.Array(
        Type.Object({
          severity: Type.Optional(
            Type.String({ description: "critical, major, or minor" })
          ),
          description: Type.Optional(Type.String()),
          file: Type.Optional(Type.String()),
          line: Type.Optional(Type.Number()),
        })
      )
    ),

    summary: Type.Optional(
      Type.String({
        description: "Brief overall summary of the review",
      })
    ),

    suggestions: Type.Optional(
      Type.Array(Type.String())
    ),
  }),

  async execute(_toolCallId, params, _signal, _onUpdate, _ctx) {
    return {
      content: [
        {
          type: "text",
          text: `Review decision recorded: ${params.decision}`,
        },
      ],
      details: params,
    };
  },
});

export default function (pi: ExtensionAPI) {
  pi.registerTool(reviewDecisionTool);
}
```

### Schema design rationale

- **`decision` is the only required field.** If the agent produces a bare
  `{"decision": "no-go"}`, the tool call succeeds. Complex required schemas
  cause repeated tool-call failures → fail-closed on every review.
- **Optional fields** (`issues`, `summary`, `suggestions`) encourage detail
  without risking compliance. The hook extracts them if present and surfaces
  them to the user on rejection.
- **Field ordering**: `issues` before `summary` nudges the agent to think
  through issues first, then summarize (less impactful with thinking models
  but still helpful).

---

## 5. Python hook implementation

### 5a. `config.py` — Configuration

```python
"""Configuration: CLI args > env vars > defaults."""

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Config:
    model: str
    pi_binary: str
    system_prompt: str
    session_dir: str  # relative to repo root, e.g. ".git/pi-reviewer/sessions"
    system_prompt_file: str | None = None


DEFAULT_MODEL = "glm-5.2"
DEFAULT_PI_BINARY = "pi"
DEFAULT_SESSION_DIR = ".git/pi-reviewer/sessions"


def _env_or_default(env_key: str, default: str) -> str:
    return os.environ.get(env_key, default)


def parse_args(argv: list[str] | None = None) -> Config:
    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(
        prog="pi-review",
        description="Agentic AI code review pre-commit hook using pi.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help=f"Model pattern for pi (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--pi-binary",
        default=None,
        help=f"pi binary name or path (default: {DEFAULT_PI_BINARY})",
    )
    parser.add_argument(
        "--system-prompt",
        default=None,
        help="System prompt for the reviewer (overrides built-in default).",
    )
    parser.add_argument(
        "--system-prompt-file",
        default=None,
        help="Path to a file containing the system prompt.",
    )
    parser.add_argument(
        "--session-dir",
        default=None,
        help=f"Session directory (default: {DEFAULT_SESSION_DIR})",
    )

    args = parser.parse_args(argv)

    # Precedence: CLI args > env vars > defaults
    model = args.model or _env_or_default("PI_REVIEW_MODEL", DEFAULT_MODEL)
    pi_binary = args.pi_binary or _env_or_default(
        "PI_REVIEW_PI_BINARY", DEFAULT_PI_BINARY
    )
    session_dir = args.session_dir or _env_or_default(
        "PI_REVIEW_SESSION_DIR", DEFAULT_SESSION_DIR
    )

    # System prompt: --system-prompt-file > --system-prompt > env > built-in default
    system_prompt = None
    if args.system_prompt_file:
        system_prompt = Path(args.system_prompt_file).read_text()
    elif args.system_prompt:
        system_prompt = args.system_prompt
    elif env_sp := os.environ.get("PI_REVIEW_SYSTEM_PROMPT"):
        system_prompt = env_sp

    return Config(
        model=model,
        pi_binary=pi_binary,
        system_prompt=system_prompt or "",  # empty = use built-in default in prompts.py
        session_dir=session_dir,
        system_prompt_file=args.system_prompt_file,
    )
```

### 5b. `state.py` — State management

```python
"""State management for review sessions under .git/pi-reviewer/."""

import json
from pathlib import Path


STATE_DIR = Path(".git") / "pi-reviewer"
STATE_FILE = STATE_DIR / "state.json"
SESSIONS_SUBDIR = "sessions"


def state_path() -> Path:
    return STATE_FILE


def sessions_path(session_dir: str = ".git/pi-reviewer/sessions") -> Path:
    return Path(session_dir)


def load_state() -> dict | None:
    """Load hook coordination state, or None if no state exists."""
    p = state_path()
    if not p.exists():
        return None
    return json.loads(p.read_text())


def save_state(state: dict) -> None:
    """Save hook coordination state."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_path().write_text(json.dumps(state, indent=2))


def clear_state(session_dir: str = ".git/pi-reviewer/sessions") -> None:
    """Clear all state: hook state file + pi session directory."""
    p = state_path()
    if p.exists():
        p.unlink()

    sdir = sessions_path(session_dir)
    if sdir.exists():
        import shutil

        shutil.rmtree(sdir)


def record_rejection(session_id: str, tree_hash: str, issues: list | None) -> None:
    """Record a rejected tree hash and optional issues in state."""
    state = load_state() or {
        "session_id": session_id,
        "rejected_trees": [],
        "round": 0,
    }
    rejected = state.setdefault("rejected_trees", [])
    rejected.append({"tree_hash": tree_hash, "issues": issues})
    state["round"] = state.get("round", 0) + 1
    save_state(state)


def is_tree_rejected(tree_hash: str) -> bool:
    """Check if a tree hash was previously rejected."""
    state = load_state()
    if not state:
        return False
    return any(
        entry["tree_hash"] == tree_hash
        for entry in state.get("rejected_trees", [])
    )


def get_session_id() -> str | None:
    """Get the current session ID, or None if no active session."""
    state = load_state()
    if not state:
        return None
    return state.get("session_id")


def get_round_number() -> int:
    """Get the current round number (0 = first review)."""
    state = load_state()
    if not state:
        return 0
    return state.get("round", 0)
```

### 5c. `pi_runner.py` — pi invocation + NDJSON parsing

```python
"""Invoke pi and parse NDJSON output for the decision tool call."""

import json
import shutil
import subprocess
import sys
from importlib.resources import files
from pathlib import Path


def find_pi(pi_binary: str) -> str | None:
    """Find the pi binary in PATH. Returns path or None."""
    return shutil.which(pi_binary)


def get_extension_path() -> str:
    """Get the path to the bundled extension.ts file."""
    # importlib.resources.files() works with installed packages
    return str(files("pi_review_precommit") / "extension.ts")


def run_review(
    pi_binary: str,
    model: str,
    session_id: str,
    session_dir: str,
    system_prompt: str,
    user_prompt: str,
) -> dict | None:
    """
    Invoke pi in JSON mode and parse the NDJSON stream for the
    submit_review_decision tool call.

    Returns the tool call args dict if found, or None if pi ran
    but didn't call the decision tool.

    Raises subprocess.CalledProcessError if pi itself fails (non-zero
    exit, crash, etc.).
    """
    extension_path = get_extension_path()

    cmd = [
        pi_binary,
        "--mode", "json",
        "--session-id", session_id,
        "--model", model,
        "--session-dir", session_dir,
        "--no-extensions",
        "--extension", extension_path,
        "--system-prompt", system_prompt,
        # Allowlist: read-only exploration tools + our decision tool.
        # This excludes bash, edit, write by default.
        "--tools", "read,grep,find,ls,submit_review_decision",
        "--print",  # non-interactive: process prompt and exit
        user_prompt,
    ]

    # Ensure session dir exists
    Path(session_dir).mkdir(parents=True, exist_ok=True)

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
    )

    # pi exits 0 on success even if the review decision is "no-go".
    # Non-zero exit means pi itself errored (network, crash, etc.).
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode, cmd, proc.stdout, proc.stderr
        )

    # Parse NDJSON stream line by line, looking for tool_execution_start
    # with toolName === "submit_review_decision".
    for line in proc.stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        if (
            event.get("type") == "tool_execution_start"
            and event.get("toolName") == "submit_review_decision"
        ):
            return event.get("args")

    return None
```

### 5d. `prompts.py` — Prompt construction

```python
"""System prompt and per-round user prompt construction."""

import subprocess

DEFAULT_SYSTEM_PROMPT = """\
You are an automated code reviewer for a pre-commit hook. Your job is to
review staged code changes and decide whether they are acceptable to commit.

## Your role

- Review the staged diff provided to you.
- Use the read, grep, find, and ls tools to explore surrounding code for
  context when needed, but focus primarily on the staged changes.
- Focus on issues that existing linters and formatters cannot catch:
  logic errors, security misconfigurations, incorrect patterns, missing
  edge cases, and architectural concerns.
- Do NOT flag trivial style issues — those are handled by other hooks.

## Your decision

At the end of your review, you MUST call the `submit_review_decision` tool
with your decision:
- "go" — the changes are acceptable to commit.
- "no-go" — the changes have blocking issues that should be fixed first.

When calling submit_review_decision, include:
- `issues`: a list of issues found, each with severity (critical/major/minor),
  description, file, and line number.
- `summary`: a brief overall summary of the review.
- `suggestions`: actionable suggestions for fixing issues (if any).

These fields are optional but encouraged — provide them when you have
findings to report.

## Review criteria

- Flag issues that meaningfully impact correctness, security, performance,
  or maintainability.
- Flag issues introduced by the changes being reviewed, not pre-existing bugs.
- Be proportional — don't demand rigor inconsistent with the rest of the
  codebase.
- The author would likely fix the issue if aware of it.

## Multi-round reviews

If this is a subsequent review round (you will be told), the user has had
an opportunity to address your previous feedback. Be proportionally lenient
on previously raised issues that appear resolved, but maintain standards on
new problems.
"""


def get_system_prompt(config_system_prompt: str) -> str:
    """Return the system prompt: config override or built-in default."""
    if config_system_prompt:
        return config_system_prompt
    return DEFAULT_SYSTEM_PROMPT


def get_staged_diff() -> str:
    """Get the staged diff via git diff --cached."""
    result = subprocess.run(
        ["git", "diff", "--cached"],
        capture_output=True,
        text=True,
    )
    return result.stdout


def get_staged_files() -> list[str]:
    """Get list of staged file paths."""
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        capture_output=True,
        text=True,
    )
    return [
        f for f in result.stdout.strip().splitlines() if f.strip()
    ]


def get_staged_tree_hash() -> str:
    """Compute the staged tree hash via git write-tree."""
    result = subprocess.run(
        ["git", "write-tree"],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def build_first_round_prompt(diff: str, files: list[str]) -> str:
    """Build the prompt for the first review round."""
    files_str = "\n".join(f"  - {f}" for f in files)
    return f"""\
Review the following staged changes and decide whether they are acceptable
to commit.

## Staged files

{files_str}

## Staged diff

```diff
{diff}
```

Perform your review and call submit_review_decision with your decision.
"""


def build_followup_prompt(
    diff: str,
    files: list[str],
    round_number: int,
    previous_issues: list | None,
) -> str:
    """Build the prompt for a subsequent review round."""
    files_str = "\n".join(f"  - {f}" for f in files)

    issues_str = ""
    if previous_issues:
        issues_str = "\n\n## Issues raised in previous rounds\n\n"
        for i, issue in enumerate(previous_issues, 1):
            severity = issue.get("severity", "unknown")
            desc = issue.get("description", "")
            issues_str += f"{i}. [{severity}] {desc}\n"

    return f"""\
The user has run `git commit` again. This may be a continuation of the
previous review (the user amended their changes) or a new change entirely;
we cannot determine which. Review the current staged changes and make your
decision.

This is review round {round_number + 1}. Be proportionally lenient on
previously raised issues that appear resolved, but maintain standards on
new problems.

## Staged files

{files_str}
{issues_str}

## Staged diff

```diff
{diff}
```

Perform your review and call submit_review_decision with your decision.
"""


def get_previous_issues() -> list | None:
    """Extract issues from the most recent rejection in state."""
    from pi_review_precommit.state import load_state

    state = load_state()
    if not state:
        return None
    rejected = state.get("rejected_trees", [])
    if not rejected:
        return None
    return rejected[-1].get("issues")
```

### 5e. `hook.py` — Main entry point

```python
"""Main hook entry point. Called by pre-commit as `pi-review`."""

import sys
import uuid

from pi_review_precommit.config import parse_args
from pi_review_precommit.pi_runner import find_pi, run_review
from pi_review_precommit.prompts import (
    build_first_round_prompt,
    build_followup_prompt,
    get_previous_issues,
    get_staged_diff,
    get_staged_files,
    get_staged_tree_hash,
    get_system_prompt,
)
from pi_review_precommit.state import (
    clear_state,
    get_round_number,
    get_session_id,
    is_tree_rejected,
    record_rejection,
    save_state,
)


def main() -> int:
    config = parse_args()

    # 1. Check if pi is available
    pi_path = find_pi(config.pi_binary)
    if pi_path is None:
        print(
            f"pi-review: '{config.pi_binary}' not found in PATH, "
            "skipping review.",
            file=sys.stderr,
        )
        return 0  # fail-open: pi not installed

    # 2. Compute staged tree hash
    tree_hash = get_staged_tree_hash()

    # 3. Check if this tree was already rejected
    if is_tree_rejected(tree_hash):
        print(
            "pi-review: These changes are identical to a previously "
            "rejected review. Amend your changes or use "
            "SKIP=pi-review to bypass.",
            file=sys.stderr,
        )
        return 1  # auto-reject, no pi call

    # 4. Get or create session
    session_id = get_session_id()
    round_number = get_round_number()

    if session_id is None:
        # First review — create new session
        session_id = f"pi-review-{uuid.uuid4().hex[:12]}"
        save_state({
            "session_id": session_id,
            "rejected_trees": [],
            "round": 0,
        })
        round_number = 0

    # 5. Construct prompt
    diff = get_staged_diff()
    files = get_staged_files()

    if not diff.strip():
        # No staged changes — nothing to review
        return 0

    system_prompt = get_system_prompt(config.system_prompt)

    if round_number == 0:
        user_prompt = build_first_round_prompt(diff, files)
    else:
        previous_issues = get_previous_issues()
        user_prompt = build_followup_prompt(
            diff, files, round_number, previous_issues
        )

    # 6. Invoke pi
    try:
        decision_args = run_review(
            pi_binary=config.pi_binary,
            model=config.model,
            session_id=session_id,
            session_dir=config.session_dir,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
    except Exception as e:
        print(f"pi-review: pi invocation failed: {e}", file=sys.stderr)
        return 1  # fail-closed: infra error

    # 7. Check for decision tool call
    if decision_args is None:
        print(
            "pi-review: Reviewer did not produce a decision "
            "(no submit_review_decision tool call).",
            file=sys.stderr,
        )
        return 1  # fail-closed: non-compliance

    decision = decision_args.get("decision", "").lower().strip()

    # 8. Handle decision
    if decision == "go":
        clear_state(config.session_dir)
        print("pi-review: Changes approved.", file=sys.stderr)
        return 0
    elif decision == "no-go":
        issues = decision_args.get("issues")
        summary = decision_args.get("summary", "")
        record_rejection(session_id, tree_hash, issues)

        # Surface issues to the user
        print("pi-review: Changes rejected.", file=sys.stderr)
        if summary:
            print(f"  Summary: {summary}", file=sys.stderr)
        if issues:
            print("  Issues:", file=sys.stderr)
            for i, issue in enumerate(issues, 1):
                severity = issue.get("severity", "?")
                desc = issue.get("description", "")
                file = issue.get("file", "")
                line = issue.get("line", "")
                loc = f"{file}:{line}" if file else ""
                print(f"    {i}. [{severity}] {desc} ({loc})", file=sys.stderr)

        print(
            "  Amend your changes and try again, or use "
            "SKIP=pi-review to bypass.",
            file=sys.stderr,
        )
        return 1
    else:
        print(
            f"pi-review: Unrecognized decision '{decision}'. "
            "Expected 'go' or 'no-go'.",
            file=sys.stderr,
        )
        return 1  # fail-closed: unparseable decision


if __name__ == "__main__":
    sys.exit(main())
```

---

## 6. Consumer integration

### 6a. Standard pre-commit (`.pre-commit-config.yaml`)

```yaml
repos:
  - repo: git@github.com:Fifty-Nine/pi-review-precommit
    rev: v0.1.0
    hooks:
      - id: pi-review
        args: ["--model", "glm-5.2"]
```

### 6b. git-hooks.nix (NixOS flake)

In `flake.nix`, add the hook repo as a flake input and reference it:

```nix
{
  inputs = {
    # ... existing inputs ...
    pi-review-precommit = {
      url = "github:Fifty-Nine/pi-review-precommit";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, git-hooks, pi-review-precommit, ... } @ inputs: let
    system = "x86_64-linux";
    pkgs = nixpkgs.legacyPackages.${system};

    pi-review-pkg = pi-review-precommit.packages.${system}.default;

    pre-commit-check = git-hooks.lib.${system}.run {
      src = ./.;
      hooks = {
        # ... existing hooks ...

        pi-review = {
          enable = true;
          name = "pi-review";
          description = "Agentic AI code review using pi.";
          entry = "${pi-review-pkg}/bin/pi-review --model glm-5.2";
          pass_filenames = false;
          always_run = true;
          stages = ["pre-commit"];
        };
      };
    };
  in {
    # ... rest of outputs ...
  };
}
```

> **Note**: The exact git-hooks.nix integration depends on whether
> `pi-review-precommit` exposes a Nix flake output. If not, the package can
> be built via `pkgs.python3.pkgs.buildPythonApplication` or installed via
> `pkgs.python3Packages.callPackage` in the consumer's flake. Alternatively,
> the hook repo can be consumed via standard pre-commit (not git-hooks.nix)
> by using a `.pre-commit-config.yaml` directly.

### 6c. Escape hatch

```bash
# Skip only pi-review, keep all other hooks:
SKIP=pi-review git commit -m " ..."

# Or skip all hooks (not recommended):
git commit --no-verify -m "..."
```

---

## 7. Default system prompt

The default system prompt (in `prompts.py`) is adapted from the
[pi-review extension](https://github.com/earendil-works/pi-review) rubric,
simplified for automated pre-commit use. Key elements:

- Reviewer persona: automated code reviewer for pre-commit.
- Focus on issues linters can't catch (logic, security, patterns).
- Must call `submit_review_decision` at end.
- Multi-round leniency instructions.
- Proportional severity assessment.

Consumers can override via `--system-prompt` or `--system-prompt-file`.

---

## 8. Testing approach

### Unit tests

- **`test_state.py`**: state load/save/clear, tree hash rejection checking,
  session ID management. Use `tmp_path` fixture for `.git/` isolation.
- **`test_config.py`**: arg parsing, env var precedence (CLI > env > default),
  defaults.
- **`test_pi_runner.py`**: mock `subprocess.run` to return canned NDJSON
  streams. Verify tool call extraction from `tool_execution_start` events.
  Test missing tool call → None. Test pi failure → exception.
- **`test_hook.py`**: integration test mocking `pi_runner.run_review` and
  git commands. Test full flow: first round go, first round no-go, same-tree
  auto-reject, follow-up round, pi-not-found fail-open, pi-error fail-closed,
  non-compliance fail-closed.

### Manual testing

```bash
# Try the hook on a local repo without installing:
pre-commit try-repo ../pi-review-precommit pi-review --verbose

# Or with specific args:
pre-commit try-repo ../pi-review-precommit pi-review --verbose -- --model glm-5.2
```

### Edge cases to test

- Empty staged diff (no changes) → exit 0.
- pi not in PATH → exit 0 with message.
- pi crashes / network error → exit 1 with error.
- pi runs but doesn't call decision tool → exit 1.
- Same tree hash as previous rejection → exit 1, no pi call.
- Go decision → state cleared, exit 0.
- No-go decision → state recorded, exit 1, issues printed.
- Unrecognized decision value → exit 1.
- Multiple rounds: session resumes, leniency prompt includes previous issues.

---

## 9. Implementation sequence

1. **Scaffold repo**: `pyproject.toml`, `.pre-commit-hooks.yaml`, package
   structure, `README.md`.
2. **Write `extension.ts`**: the `submit_review_decision` tool definition.
   Test manually: `pi --mode json --no-extensions -e ./src/pi_review_precommit/extension.ts --tools submit_review_decision -p "Call submit_review_decision with decision go"`.
3. **Write `config.py`**: argparse + env var parsing. Test: verify precedence.
4. **Write `state.py`**: state management. Test: load/save/clear/reject.
5. **Write `prompts.py`**: prompt construction. Test: first round vs follow-up.
6. **Write `pi_runner.py`**: pi invocation + NDJSON parsing. Test: mock NDJSON.
7. **Write `hook.py`**: main flow wiring everything together. Test: integration.
8. **Test end-to-end**: `pre-commit try-repo` on a test repo with staged changes.
9. **Push to GitHub**: `git@github.com:Fifty-Nine/pi-review-precommit`.
10. **Tag release**: `v0.1.0`.
11. **Integrate into aedificium-nixos**: add flake input + hook config.
12. **Iterate**: test with real commits, tune system prompt, adjust leniency.

---

## 10. Open implementation notes

- **Nix flake support**: The hook repo could optionally expose a flake output
  (`packages.${system}.default`) for direct git-hooks.nix integration. This
  requires a `flake.nix` with `buildPythonApplication`. Not required for v1
  but nice to have.
- **`extension.ts` runtime resolution**: `importlib.resources.files()` works
  with installed packages. Verify it resolves correctly in pre-commit's
  venv. Fallback: `Path(__file__).parent / "extension.ts"`.
- **pi-jailed vs pi**: The hook defaults to `pi`. NixOS users can override
  with `--pi-binary pi-jailed` or `PI_REVIEW_PI_BINARY=pi-jailed`.
- **Large diffs**: Very large staged diffs may exceed the model's context
  window. Consider truncating or summarizing the diff if it exceeds a
  threshold. Not handled in v1.
- **Session dir visibility in pi-jailed**: If using `pi-jailed`, the session
  dir under `.git/` must be inside the mounted cwd. Since `.git/` is inside
  the repo root (which is mounted), this should work. Verify.
# AGENTS.md

> **Agents: update this file.** As you implement features, fix bugs, discover
> gotchas, or change architecture, update the relevant sections below. This
> file is the living context for anyone (human or agent) working on this
> project. Keep it accurate — stale docs are worse than no docs.

## Project overview

`pi-review-precommit` is a [pre-commit](https://pre-commit.com) hook that uses
[pi](https://pi.dev) (an AI coding agent) to perform automated code review on
staged changes before a commit is allowed to proceed. The hook retains review
context across multiple rejected rounds so pi can give proportionally lenient
feedback as the user iterates.

**Repo**: `git@github.com:Fifty-Nine/pi-review-precommit`
**Language**: Python (standard library only for v1 — no external deps)
**Extension file**: TypeScript (bundled in the Python package)

## Design documents

- **[docs/implementation-plan.md](docs/implementation-plan.md)** — Full
  implementation plan with code sketches for every module. This is the
  primary reference for implementation. Read it before starting.
- **[docs/adr.md](docs/adr.md)** — Architecture Decision Record documenting 10
  key design decisions with context, options, choices, and consequences.
  Read this to understand *why* things are designed the way they are.

## Architecture summary

### Flow (per `git commit` attempt)

```
git commit
  │
  ├─ pre-commit runs pi-review hook
  ├─ check: pi binary in PATH? → no: exit 0 (skip)
  ├─ compute staged tree hash: `git write-tree`
  ├─ tree hash matches prior rejection? → yes: exit 1 (auto-reject, no pi call)
  ├─ load session state from .git/pi-reviewer/state.json
  ├─ construct prompt (first round vs follow-up with leniency note,
  │  current date injected so the model doesn't guess the date,
  │  REVIEW_GUIDELINES.md appended if present)
  ├─ invoke: pi --mode json --session-id <id> --model <model> --session-dir <dir>
  │         -e <bundled extension.ts>
  │         --system-prompt "<reviewer prompt>"
  │         --tools read,grep,find,ls,submit_review_decision
  │         -p "<user prompt>"
  │
  │   (No --no-extensions: provider extensions like ollama register the
  │   models consumers rely on. Tool safety is enforced by --tools.)
  ├─ parse NDJSON stdout, scan for tool_execution_start with
  │  toolName == "submit_review_decision"
  ├─ no tool call found? → exit 1 (fail-closed: non-compliance)
  ├─ pi process crashed/errored? → exit 1 (fail-closed: infra error)
  ├─ decision == "go"? → log approving comments to .git/pi-reviewer/reviews/,
  │                     print summary/suggestions, clear all state
  │                     (archive session to .jsonl.gz if enabled), exit 0
  └─ decision == "no-go"? → record rejected tree hash + issues, print issues, exit 1

(commit created)
  │
  └─ post-commit runs pi-review-notes hook (if enabled)
     ├─ resolve HEAD commit + tree hash
     ├─ review log matches tree? → attach review note to refs/notes/pi-review,
     │                             delete consumed log (cleanup failure → stderr)
     ├─ no match? → attach brief "no review" audit note
     └─ note attach fails? → exit 1 (fail-open but visible: log kept for retry)
```

### State model

```
.git/pi-reviewer/
├── state.json              # Hook coordination state
│   {
│     "session_id": "pi-review-abc123def456",
│     "rejected_trees": [
│       {"tree_hash": "a1b2c3...", "issues": [...]},
│       {"tree_hash": "d4e5f6...", "issues": [...]}
│     ],
│     "round": 2
│   }
├── sessions/               # pi session store (managed by pi itself)
│   └── <timestamp>_<session-id>.jsonl   # one jsonl file per session
├── reviews/                # finished (go) review logs, consumed by post-commit
│   └── <timestamp>-<tree-hash>.json
└── archive/                # opt-in session archives (gzipped jsonl)
    └── session-<id>.jsonl.gz
```

- **Session accumulates** across rounds until a "go" decision clears it.
  No implicit keying by HEAD, branch, or file set.
- **Same-tree auto-reject**: if `git write-tree` hash matches a prior rejection,
  block without invoking pi. User must amend or `SKIP=pi-review`.
- **No reset mechanism for v1.** Rely on go-clears + `SKIP=pi-review`.
- **Go → clear all state** (rm state.json + sessions/ dir; archive first if
  `--archive-sessions`).
- **Post-commit notes**: the `pi-review-notes` hook attaches a review note (or
  a "no review" audit note) to every commit under `refs/notes/pi-review` and
  deletes the consumed review log. See `notes.py`.

### The custom pi extension

`src/pi_review_precommit/extension.ts` defines a single custom tool:
`submit_review_decision`. The agent is instructed (via system prompt) to call
this tool at the end of every review turn. The hook detects the call in the
NDJSON stream via `tool_execution_start` events.

**Schema design**: only `decision` (string: "go" | "no-go") is required. All
other fields (`issues`, `summary`, `suggestions`) are optional. This is
intentional — complex required schemas cause tool-call failures, which would
trigger fail-closed on every review.

### Configuration precedence

```
CLI args (pre-commit `args`) > env vars (PI_REVIEW_*) > built-in defaults
```

| Setting | CLI flag | Env var | Default |
|---|---|---|---|
| Model | `--model` | `PI_REVIEW_MODEL` | `glm-5.2` |
| pi binary | `--pi-binary` | `PI_REVIEW_PI_BINARY` | `pi` |
| System prompt | `--system-prompt` / `--system-prompt-file` | `PI_REVIEW_SYSTEM_PROMPT` | Built-in reviewer prompt |
| Session dir | `--session-dir` | `PI_REVIEW_SESSION_DIR` | `.git/pi-reviewer/sessions` |
| Archive sessions | `--archive-sessions` | `PI_REVIEW_ARCHIVE_SESSIONS` | off (delete on go) |
| Review guidelines | `--no-review-guidelines` | `PI_REVIEW_NO_REVIEW_GUIDELINES` | on (auto-discover `REVIEW_GUIDELINES.md`) |

**Note**: default binary is `pi`, not `pi-jailed`. The `pi-jailed` variant is
NixOS-specific (bubblewrap-sandboxed). NixOS consumers override with
`--pi-binary pi-jailed`.

The optional **`pi-review-notes`** hook id (post-commit stage) takes no
arguments; it attaches review/audit git notes under `refs/notes/pi-review`
and is skipped with `SKIP=pi-review-notes`.

## Module layout (target)

```
src/pi_review_precommit/
├── __init__.py          # Package init
├── hook.py              # Main entry point (console_scripts: pi-review)
├── notes.py             # Post-commit hook (console_scripts: pi-review-notes)
├── config.py            # Argparse + env var configuration
├── state.py             # .git/pi-reviewer/ state management
├── pi_runner.py         # pi invocation + NDJSON parsing
├── prompts.py           # System prompt + per-round prompt construction
└── extension.ts         # Bundled pi extension (submit_review_decision tool)
```

The implementation plan in `docs/implementation-plan.md` contains full code
sketches for each module. Use them as a starting point, but adapt as needed.

## pi CLI reference

Key flags used by the hook:

| Flag | Purpose |
|---|---|
| `--mode json` | NDJSON event stream output |
| `--session-id <id>` | Resume/create a session by exact ID |
| `--session-dir <dir>` | Custom session storage directory |
| `--model <pattern>` | Model pattern (e.g. `glm-5.2`, `openai/gpt-4o`) |
| `--extension, -e <path>` | Load a custom extension file (can repeat) |
| `--no-extensions` | Disable extension auto-discovery (only `-e` paths load) |
| `--tools, -t <names>` | Comma-separated tool allowlist |
| `--system-prompt <text>` | Replace the default system prompt |
| `--print, -p` | Non-interactive mode: process prompt and exit |

Built-in tools: `read`, `bash`, `edit`, `write`, `grep` (off by default),
`find` (off by default), `ls` (off by default).

The hook uses `--tools read,grep,find,ls,submit_review_decision` to allowlist
read-only exploration + the decision tool, excluding `bash`, `edit`, `write`.
The allowlist applies to built-in, extension, and custom tools alike, so it
remains deterministic even though the hook does **not** pass `--no-extensions`
(provider extensions like ollama register the models consumers rely on; see
the note in `pi_runner.py`).

## NDJSON event reference

pi in JSON mode outputs one JSON object per line. Key events:

| Event type | Key fields | Hook use |
|---|---|---|
| `session` | `id`, `cwd`, `timestamp` | Session metadata |
| `agent_start` | — | Agent run started |
| `turn_start` | `turnIndex` | Turn started |
| `message_start` | `message` (role, content) | Message begin |
| `message_update` | `assistantMessageEvent` (deltas) | Streaming deltas |
| `message_end` | `message` (final content blocks) | Final message |
| `tool_execution_start` | `toolCallId`, `toolName`, `args` | **Decision extraction** |
| `tool_execution_end` | `toolCallId`, `toolName`, `result`, `isError` | Tool completed |
| `turn_end` | `message`, `toolResults` | Turn finished |
| `agent_end` | `messages` (full conversation) | Agent run ended |
| `agent_settled` | — | Terminal — no more auto-retry/compaction |

**The hook scans for `tool_execution_start` with
`toolName == "submit_review_decision"` and reads `args` for the decision.**

Assistant message content blocks include `thinking` (model's internal
reasoning) and `text` (the actual reply). The hook ignores both — it only
cares about the tool call.

## pi extension format

Extensions are TypeScript modules loaded via `--extension, -e <path>`. They
export a default factory function that receives `ExtensionAPI`:

```typescript
import { Type } from "@earendil-works/pi-ai";
import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function (pi: ExtensionAPI) {
  pi.registerTool({
    name: "my_tool",
    description: "...",
    parameters: Type.Object({ /* TypeBox schema */ }),
    async execute(toolCallId, params, signal, onUpdate, ctx) {
      return { content: [{ type: "text", text: "..." }], details: {} };
    },
  });
}
```

- **TypeBox** (`@earendil-works/pi-ai` or `typebox` package) is used for
  parameter schemas: `Type.String()`, `Type.Optional()`, `Type.Array()`,
  `Type.Object()`, `Type.Number()`.
- Extensions are loaded via `jiti` (TypeScript without compilation).
- See [pi extensions docs](https://pi.dev/docs/latest/extensions) for full API.
- See [pi-review](https://github.com/earendil-works/pi-review) for a
  real-world review extension (interactive, not for automated hooks — but
  its rubric and approach are reference material).

## pre-commit framework notes

- **`.pre-commit-hooks.yaml`** in the hook repo defines the hook for consumers.
- **`language: python`** — pre-commit creates a venv and pip-installs the
  package. The `entry` (`pi-review`) must match a `console_scripts` entry
  point in `pyproject.toml`.
- **`pi` is NOT in the venv** — it must be in the system PATH. The hook
  checks for it and fails open (exit 0) if not found.
- **`SKIP=pi-review`** — pre-commit's built-in escape hatch. Skips only this
  hook while running all other hooks. This is the recommended way to bypass
  the review (e.g., during API outages).
- **`always_run: true`** — hook runs on every commit regardless of staged
  file types (it computes its own diff).
- **`pass_filenames: false`** — hook doesn't receive filename arguments.
- **`require_serial: true`** — hook is stateful, must not run in parallel.
- **`PRE_COMMIT=1`** — pre-commit sets this env var during hook execution.

## Testing

```bash
# Run unit tests
nix shell nixpkgs#python3 nixpkgs#uv -c uv run pytest

# Try the hook on a local repo without installing:
cd /some/other/repo
pre-commit try-repo ~/pi-review-hook pi-review --verbose

# Try with custom args:
pre-commit try-repo ~/pi-review-hook pi-review --verbose -- --model glm-5.2
```

### Edge cases to test

- Empty staged diff → exit 0 (nothing to review)
- pi not in PATH → exit 0 with message (fail-open)
- pi crashes / network error → exit 1 (fail-closed)
- pi runs but no `submit_review_decision` tool call → exit 1 (fail-closed)
- Same tree hash as previous rejection → exit 1, no pi call (auto-reject)
- Go decision → state cleared, exit 0
- No-go decision → state recorded, issues printed, exit 1
- Unrecognized decision value → exit 1 (fail-closed)
- Multiple rounds: session resumes, leniency prompt includes previous issues

## NixOS / uv gotchas

- **uv downloads its own Python**, which uses dynamic linking and doesn't
  work on NixOS. Use `nix shell nixpkgs#python3 nixpkgs#uv -c uv ...` to get
  a NixOS-compatible Python + uv.
- **pi-jailed** is a bubblewrap-sandboxed variant of pi, specific to the
  `aedificium-nixos` repo. It mounts only the cwd (network + mount-cwd
  permissions). The session dir under `.git/` is inside cwd, so it should
  be visible inside the jail. The hook itself runs outside the jail (it's
  the hook script that invokes pi-jailed).
- **`importlib.resources.files()`** is used to find the bundled `extension.ts`
  at runtime. Verify it resolves correctly in pre-commit's venv. Fallback:
  `Path(__file__).parent / "extension.ts"`.

## Build system

The project uses `hatchling` as the build backend. `uv_build` was tried
first but its compiled `uv-build` binary cannot run on NixOS inside
pre-commit's pip build env (stub-ld), which breaks `pre-commit try-repo`
and any pip install on NixOS. `hatchling` is pure Python and works
anywhere. `extension.ts` is force-included into the wheel; verified
present in `dist/*.whl`.

## Implementation status

> **Update this section as you implement.** Check off items and add notes
> about what's done, what's in progress, and what's blocked.

- [x] `extension.ts` — submit_review_decision tool definition (StringEnum
      for decision, verified loading under real pi 0.84.1 with ollama)
- [x] `config.py` — argparse + env var configuration
- [x] `state.py` — .git/pi-reviewer/ state management
- [x] `prompts.py` — system prompt + per-round prompt construction
- [x] `pi_runner.py` — pi invocation + NDJSON parsing (no `--no-extensions`;
      see note above)
- [x] `hook.py` — main entry point, full flow (empty-diff check runs before
      same-tree rejection so a stale rejection can't block an empty index)
- [x] `.pre-commit-hooks.yaml` — created (pi-review + pi-review-notes)
- [x] Unit tests (89 passing: config, state, pi_runner, hook, notes)
- [x] Wheel build verified — `extension.ts` bundled via hatchling
- [x] Extension integration verified under real pi (tool call + NDJSON)
- [x] Go-decision review log: comments persisted to `.git/pi-reviewer/reviews/`
      and printed at commit time
- [x] Current date injected into review prompts (fixes model date-guessing)
- [x] REVIEW_GUIDELINES.md support: auto-discovered at repo root, appended to
      the review prompt, overrides default criteria (opt-out via
      `--no-review-guidelines`); this repo dogfoods its own REVIEW_GUIDELINES.md
- [x] Opt-in session archiving: `--archive-sessions` / `PI_REVIEW_ARCHIVE_SESSIONS`
      gzips the session to `.git/pi-reviewer/archive/session-<id>.jsonl.gz`
      instead of deleting on go; `record_approval` records the archive path
- [x] `pi-review-notes` post-commit hook: attaches review printout (or "no
      review" audit note) to every commit under `refs/notes/pi-review`;
      deletes the consumed review log; fail-open but visible on note failure
- [x] README/AGENTS docs for git notes + jsonl.gz archives
- [x] End-to-end manual testing via `pre-commit try-repo` (scratch-repo E2E:
      normal review flow, no-match audit note, amend-after-go, note-attach
      failure + retry, archiving off — all passed)
- [ ] Nix flake output (optional — for direct git-hooks.nix integration)
- [ ] First release tag (v0.1.0)

## Known issues / TODOs

> **Update this section as you discover issues.**

- Large staged diffs may exceed the model's context window. Consider
  truncating or summarizing if the diff exceeds a threshold. Not handled in v1.
- The `requires-python` in pyproject.toml is `>=3.11` but `.python-version`
  says `3.14` (uv default). This is fine for development; the actual
  constraint is `>=3.11`.
- No reset mechanism for v1. If a user abandons a change and starts a
  completely different one, the session context may be polluted. The
  follow-up prompt tells pi "this may be new or continuation." Reset
  strategy to be guided by implementation experience.
- Amend-after-go: the review log is consumed by the original commit, so an
  amended commit (same tree) gets a "no review" audit note. Accepted
  consequence of the cleanup decision.
- The `refs/notes/pi-review` notes ref is not pushed by default; sharing
  notes requires `git push origin refs/notes/pi-review`.

## Reference links

- [pi documentation](https://pi.dev/docs/latest)
- [pi extensions](https://pi.dev/docs/latest/extensions)
- [pi JSON event stream mode](https://pi.dev/docs/latest/json)
- [pi structured output issue #1086](https://github.com/earendil-works/pi/issues/1086)
- [pi-review extension (reference)](https://github.com/earendil-works/pi-review)
- [pre-commit — creating new hooks](https://pre-commit.com/#new-hooks)
- [pi.nix flake input](https://github.com/lukasl-dev/pi.nix)
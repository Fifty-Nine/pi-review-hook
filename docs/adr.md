# ADR: pi-review pre-commit hook

**Status**: Accepted
**Date**: 2026-08-15

## Context

We want to introduce an agentic AI code review step into the pre-commit
workflow. The hook should use [pi](https://pi.dev) (an AI coding agent) to
review staged changes, retain context across multiple rejected review rounds
(so the reviewer remembers previous feedback when the user amends and retries),
and give a go/no-go decision via its exit code.

The hook must be reusable across repos, not tightly coupled to any single
project. It should follow the [pre-commit framework](https://pre-commit.com)
conventions so it can be imported by any pre-commit consumer.

### Key constraints

- pi has native session management (`--session-id`, `--session-dir`) that
  retains conversation history across invocations.
- pi has a JSON event stream mode (`--mode json`) that outputs structured
  NDJSON events.
- pi supports custom tools via TypeScript extensions loaded with
  `--extension, -e <path>`.
- The pre-commit framework provides a `SKIP=<hook-id>` env var to bypass
  individual hooks.
- Pre-commit hooks run before a commit is created — there is no commit SHA
  yet, only staged changes in the index.

---

## Decision 1: Invocation mode — JSON event stream

### Context

pi supports three output modes: `text` (default TUI), `json` (NDJSON event
stream), and `rpc` (programmatic stdin/stdout protocol). The hook needs to
parse pi's output programmatically to extract the review decision.

### Options considered

- **`-p` (print) mode with text output**: Simplest, but the output is
  free-form prose with no structured fields. The hook would need to
  sentinel-parse the text, which is fragile.
- **JSON mode (`--mode json`)**: Outputs a stream of typed NDJSON events
  (`session`, `agent_start`, `turn_start`, `message_start`,
  `message_update`, `message_end`, `turn_end`, `agent_end`,
  `agent_settled`, `tool_execution_start`, etc.). Structured and
  parseable.
- **RPC mode**: Long-lived pi process with programmatic stdin/stdout
  protocol. Most powerful but requires lifecycle management (starting,
  keeping alive, crash recovery). Overkill for a pre-commit hook that
  runs once per commit attempt.

### Decision

**JSON mode (`--mode json`)** with `--print` (non-interactive, process
prompt and exit).

### Consequences

- The hook receives a stream of typed JSON events that can be parsed
  line-by-line.
- No long-lived process to manage — each hook invocation is a fresh
  `pi --print` call.
- Session continuity is handled by pi's native `--session-id` mechanism,
  not by keeping a process alive.

---

## Decision 2: Decision extraction — custom tool call

### Context

pi's JSON mode gives structured *transport* (typed events), but the
assistant's text reply is free-form prose. There is no native `decision`
field. The hook needs a reliable way to extract a go/no-go decision.

### Options considered

- **Sentinel line**: instruct pi to end with `DECISION: GO` or
  `DECISION: NO-GO`, grep the text. Fragile — models may not comply.
- **Fenced JSON block**: instruct pi to end with a JSON block, regex
  extract. Better but still depends on model compliance.
- **Tool call**: expose a custom tool (`submit_review_decision`) via a
  pi extension that the agent must call at end of turn. The hook scans
  the NDJSON stream for `tool_execution_start` events. Most robust.
- **Secondary classifier**: run a second model call to classify the
  review text. Adds latency, cost, and another failure point.

### Decision

**Tool call via custom pi extension.** The pi developer (badlogic)
explicitly recommends this approach in
[issue #1086](https://github.com/earendil-works/pi/issues/1086):

> "Instead, I propose you expose a tool to the automation agent via an
> automation specific extension, it must call at the end of its turn,
> which records the JSON. This is more reliable and supported by all
> providers."

The hook scans for `tool_execution_start` events with
`toolName === "submit_review_decision"` and reads the `args` field.

### Consequences

- The extension is a TypeScript file (`extension.ts`) bundled in the
  Python package, loaded via `--extension <path>`.
- The tool schema has only one required field (`decision: "go" | "no-go"`)
  to minimize schema compliance failures. Optional fields (`issues`,
  `summary`, `suggestions`) encourage detail without risking compliance.
- **Non-compliance (no tool call) → fail-closed** (block the commit).
  Can be enhanced later with alternative failure modes.

---

## Decision 3: Session model — accumulate until go

### Context

The hook must retain review context across multiple rejected rounds: user
attempts commit → pi reviews → no-go → user amends → retries → pi resumes
with context from the previous round. The question is how to key the
session across rounds.

### Options considered

- **Key by HEAD**: all staged-change iterations on the same HEAD are the
  same session. Go → commit lands → HEAD advances → old session orphaned.
  But can't distinguish "user amended same change" from "user abandoned
  and started a different change on the same HEAD."
- **Key by HEAD + staged file set**: finer-grained, catches completely
  different file sets. But can't distinguish overlapping changes.
- **Explicit session ID with manual reset**: hook generates a random
  session ID, stores it, reuses until manual reset or go. Predictable but
  puts reset burden on the user.
- **No implicit keying — accumulate until go**: the session persists across
  all rounds until a go decision clears it. The follow-up prompt tells
  the agent "the user ran git commit again; this may be a continuation
  or a new change; we cannot determine which." Simplest to implement.

### Decision

**No implicit keying. Session accumulates until a go decision clears it.**

The follow-up prompt explicitly informs the agent of the ambiguity. The
agent must determine from the diff and prior context whether it's a
continuation or a new change.

### Consequences

- Simplest implementation — no HEAD tracking, no file-set hashing, no
  branch detection.
- Risk of context pollution if the user abandons a change and starts a
  completely different one. Accepted for v1.
- No reset mechanism for v1. Rely on go-clears + `SKIP=pi-review`.
  Reset strategy can be guided by implementation experience.

---

## Decision 4: Same-tree auto-reject

### Context

If the user retries `git commit` without amending anything, the staged
tree hash is identical to a previously rejected review. Re-invoking pi
is wasteful — the model will likely produce the same decision.

### Decision

**If the staged tree hash (`git write-tree`) matches a previously rejected
tree hash, auto-reject (exit 1) without invoking pi.** The message tells
the user to amend their changes or use `SKIP=pi-review`.

### Consequences

- Saves API calls and latency on no-op retry.
- The user must actually change something to get a re-review.
- `SKIP=pi-review` is the escape hatch for users who want to force the
  same content through.

---

## Decision 5: Review input — diff + read-only exploration

### Context

The reviewer needs enough context to assess whether staged changes are
safe. A diff in isolation may not show whether a change is correct in the
context of surrounding code. But giving the agent full freedom to explore
could lead to unfocused reviews.

### Options considered

- **Diff-only, no tools**: fastest, but blind to context. Bad for repos
  where module relationships matter (e.g., NixOS configs).
- **Diff + read-only tools** (`read`, `grep`, `find`, `ls`): agent can
  explore surrounding code for context. No `bash`, `edit`, `write` —
  the reviewer cannot modify code or run commands.
- **Diff + full staged file contents, no tools**: more context than
  diff-only without freedom to wander. But large changes = large prompt,
  and can't look at non-staged files.

### Decision

**Diff + read-only exploration tools.** The hook provides
`git diff --cached` + list of staged file paths. pi gets `read`, `grep`,
`find`, `ls` tools (via `--tools read,grep,find,ls,submit_review_decision`).
No `bash`, `edit`, or `write`.

### Consequences

- Reviews can leverage surrounding code context, important for
  interdependent codebases.
- The reviewer cannot modify code or execute commands — safety by
  construction.
- Risk: the agent may wander or consume more tokens exploring. Mitigated
  by the system prompt instructing focus on the staged diff.

---

## Decision 6: Strictness — lenient on subsequent rounds

### Context

In a multi-round review, the user has had an opportunity to address
previous feedback. Re-applying the same strictness to issues the user
already attempted to fix creates a frustrating loop.

### Decision

**The reviewer is instructed to be more lenient on subsequent rounds.**
The follow-up prompt notes the round number, lists previous issues, and
instructs: "Be proportionally lenient on previously raised issues that
appear resolved, but maintain standards on new problems."

### Consequences

- The review becomes a dialogue: strict first pass, progressively
  lenient as the user addresses feedback.
- This leverages the stateful session — the agent remembers prior
  context and can assess what's been addressed.
- Risk: the agent may become too lenient over many rounds. Can be
  tuned via the system prompt.

---

## Decision 7: Failure-mode matrix

### Context

The hook can fail in several ways: pi not installed, pi invocation fails
(network/API/crash), pi runs but doesn't call the decision tool
(non-compliance).

### Decision

| Scenario | Behavior | Exit code |
|---|---|---|
| pi not in PATH | Skip (fail-open) | 0 |
| pi invocation fails (network/API/crash) | Fail-closed | 1 |
| Non-compliance (no decision tool call) | Fail-closed | 1 |
| Unrecognized decision value | Fail-closed | 1 |
| Decision = "go" | Clear state, pass | 0 |
| Decision = "no-go" | Record rejection, block | 1 |
| Same tree as previous rejection | Auto-reject, no pi call | 1 |

**Escape hatch**: `SKIP=pi-review git commit` (pre-commit built-in env var)
bypasses only this hook while running all other hooks. Unlike
`--no-verify` which skips everything.

### Consequences

- Commits are blocked on any review failure except pi not being installed.
- API outages block commits — users must use `SKIP=pi-review` to bypass.
  This is intentional: the hook should be conservative. The escape hatch
  is targeted (only this hook, not all hooks).
- pi-not-installed is fail-open so the hook is a no-op on systems without
  pi, making it safe to include in shared configs.

---

## Decision 8: Standalone pre-commit hook repo in Python

### Context

The hook was initially planned as a Nix-specific script tightly coupled to
the `aedificium-nixos` repo. The user decided it should be a reusable,
standalone pre-commit hook that can be imported by any repo.

### Options considered

- **Nix-specific script** (pkgs.writeShellScript in flake.nix): tightly
  coupled, not reusable outside NixOS.
- **Standalone pre-commit hook repo, `language: python`**: pip-installable,
  follows pre-commit.com conventions, importable by any pre-commit consumer.
- **Standalone repo, `language: unsupported` (system)**: requires the
  package to be pre-installed system-wide. No dependency management.

### Decision

**Standalone pre-commit hook repo, `language: python`, developed with
uv.** Pushed to `git@github.com:Fifty-Nine/pi-review-precommit`.

### Consequences

- The hook is reusable across any repo that uses pre-commit.
- Python provides better tooling for state management, JSON parsing, and
  subprocess handling than bash.
- The TypeScript extension file is bundled as Python package data and
  resolved at runtime via `importlib.resources`.
- pre-commit manages the Python venv; `pi` must be in the system PATH
  (not in the venv).
- No external Python dependencies for v1 (standard library only) — keeps
  the pre-commit venv minimal.

---

## Decision 9: Configuration — CLI args + env var overrides

### Context

Consumers need to configure the hook (model, pi binary name, system
prompt, session directory) with sensible defaults.

### Options considered

- **CLI args only** (via pre-commit `args`): standard pre-commit way, but
  can't handle per-user overrides.
- **Env vars only**: simpler hook code, but consumers can't use the
  standard `args` mechanism.
- **Config file + env vars**: good for complex configs, but adds a file.
- **CLI args + env vars + config file**: most flexible, most complex.

### Decision

**CLI args (via pre-commit `args`) as primary, with env var overrides
for environment-specific settings. Precedence: CLI args > env vars >
built-in defaults.**

| Setting | CLI arg | Env var | Default |
|---|---|---|---|
| Model | `--model` | `PI_REVIEW_MODEL` | `glm-5.2` |
| pi binary | `--pi-binary` | `PI_REVIEW_PI_BINARY` | `pi` |
| System prompt | `--system-prompt` / `--system-prompt-file` | `PI_REVIEW_SYSTEM_PROMPT` | Built-in reviewer prompt |
| Session dir | `--session-dir` | `PI_REVIEW_SESSION_DIR` | `.git/pi-reviewer/sessions` |

### Consequences

- Default binary is `pi` (not `pi-jailed`) — `pi-jailed` is NixOS-specific.
  NixOS users override with `--pi-binary pi-jailed`.
- The system prompt defaults to a built-in reviewer prompt. For long
  custom prompts, `--system-prompt-file` reads from a file.
- The extension file is not user-configurable — it's the hook's tool
  definition, bundled in the package.

---

## Decision 10: Tool schema — minimal required fields

### Context

The `submit_review_decision` tool's argument schema determines how
reliably the agent can call it. Complex required schemas cause tool-call
failures (the agent can't produce valid arguments), leading to fail-closed
on every review.

### Decision

**Only `decision` is required.** All other fields (`issues`, `summary`,
`suggestions`) are optional but encouraged.

### Consequences

- The agent can always successfully call the tool with a bare
  `{"decision": "no-go"}`.
- Optional fields encourage detail without risking compliance failures.
- The hook extracts optional fields if present and surfaces them to the
  user on rejection.
- Field ordering in the schema: `issues` before `summary` to nudge the
  agent to think through issues first (less impactful with thinking
  models but still helpful).

---

## References

- [pi documentation](https://pi.dev/docs/latest)
- [pi extensions](https://pi.dev/docs/latest/extensions)
- [pi JSON event stream mode](https://pi.dev/docs/latest/json)
- [pi structured output issue #1086](https://github.com/earendil-works/pi/issues/1086)
- [pi-review extension](https://github.com/earendil-works/pi-review)
- [pre-commit framework — creating new hooks](https://pre-commit.com/#new-hooks)
- [pi.nix flake input](https://github.com/lukasl-dev/pi.nix)
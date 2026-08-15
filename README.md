# pi-review-precommit

A [pre-commit](https://pre-commit.com) hook that runs an agentic AI code
review on your staged changes using [pi](https://pi.dev), then lets the commit
proceed or blocks it based on a go/no-go decision. The reviewer keeps context
across rejected rounds, so when you amend and retry it remembers what it
already raised and is proportionally more lenient on resolved issues.

> **⚠️ Vibe-coded, experimental, pre-release.** This project was written in an
> AI-assisted session (pi) from a design doc, then checked by a separate
> design-agent review, and its own commits were reviewed by this very hook
> ("dogfooded"). There is **no stable release yet** — pin to a commit SHA, not
> a tag. Treat it as a fun experiment, not a hardened production gate. See
> [Pitfalls & caveats](#pitfalls--caveats) before adopting.

---

## What it does

On `git commit`, the hook:

1. Computes your staged diff and the staged tree hash.
2. Asks pi to review the diff and call a custom `submit_review_decision` tool
   with `"go"` or `"no-go"`.
3. **go** → the commit proceeds (and the reviewer's approving comments are
   saved under `.git/pi-reviewer/reviews/`).
4. **no-go** → the commit is blocked, the issues are printed, and the rejection
   is recorded. Amending and retrying resumes the *same* pi session, so the
   reviewer sees its previous feedback and eases up on things you've fixed.

It is **not** a linter or formatter. It targets issues those tools miss:
logic errors, security misconfigurations, broken patterns, missing edge
cases, architectural concerns. Trivial style is left to your other hooks.

## How pi is used in the loop

The hook shells out to the `pi` CLI once per commit attempt:

```bash
pi --mode json --print \
   --session-id <id> --session-dir <dir> \
   --model <model> --system-prompt "<reviewer prompt>" \
   --extension <bundled extension.ts> \
   --tools read,grep,find,ls,submit_review_decision \
   "<user prompt with the staged diff>"
```

- **JSON mode (`--mode json --print`)**: pi runs non-interactively and emits a
  stream of typed NDJSON events. The hook scans the stream for a
  `tool_execution_start` event whose `toolName` is `submit_review_decision`
  and reads the decision from its `args`. It does **not** parse the assistant's
  prose — the decision comes only from the structured tool call.
- **Session continuity (`--session-id`, `--session-dir`)**: pi's native session
  store holds the conversation across rounds. The hook generates one session
  id per review cycle and reuses it until a `go` clears it.
- **A custom tool (`submit_review_decision`)**: defined by a small TypeScript
  extension bundled inside the Python package and loaded with `-e`. Only
  `decision` is a required argument; `issues`, `summary`, and `suggestions`
  are optional but encouraged. A minimal review can always call
  `{"decision": "no-go"}`, which avoids schema-compliance failures.
- **Read-only exploration only**: the `--tools` allowlist is
  `read,grep,find,ls,submit_review_decision`. The reviewer can look at
  surrounding code for context but **cannot run `bash`, `edit`, or `write`**.
  Safety is enforced by the allowlist, not by a sandbox.
- **No `--no-extensions`**: provider extensions (e.g. ollama) register the
  models you actually want to use, so disabling all extensions would break
  model resolution. The `--tools` allowlist still disables any tools those
  extensions register; only provider registration (and extension startup
  code) runs. See the caveat on extension code below.

## Requirements

- **[pre-commit](https://pre-commit.com)** installed and managing your repo's
  hooks.
- **[pi](https://pi.dev)** installed and on your `PATH`. If pi isn't found, the
  hook **fails open** (exit 0, no-op) so shared configs stay safe on machines
  without pi.
- **A model pi can resolve**, backed by a provider whose credentials you have
  (see [Provider / model notes](#provider--model-notes)). Every commit makes a
  real model call, so this implies network access and, for hosted models, cost.
- **Python ≥3.11** in pre-commit's venv (the hook has no Python dependencies —
  standard library only).

## Integration

Add the hook to your repo's `.pre-commit-config.yaml`. Because there's no
release tag yet, pin to a commit SHA from
[github.com/Fifty-Nine/pi-review-hook](https://github.com/Fifty-Nine/pi-review-hook):

```yaml
repos:
  - repo: https://github.com/Fifty-Nine/pi-review-hook
    rev: 0b823df  # pin to a real commit SHA; replace before adopting
    hooks:
      - id: pi-review
        args: ["--model", "ollama-cloud/glm-5.2"]
```

Then `pre-commit install` as usual. The hook is `always_run: true`,
`pass_filenames: false`, and `require_serial: true` (it computes its own diff
and is stateful), so it runs on every commit regardless of staged file types.

**NixOS note:** the build backend is `hatchling` (pure Python) specifically
because `uv_build`'s compiled binary can't run on NixOS inside pre-commit's pip
build env. If you're on NixOS, point `--pi-binary` at your sandboxed pi if you
use one (e.g. `--pi-binary pi-jailed`).

## Configuration

Precedence: **CLI args (pre-commit `args`) > env vars (`PI_REVIEW_*`) > defaults.**

| Setting | CLI arg | Env var | Default |
|---|---|---|---|
| Model | `--model` | `PI_REVIEW_MODEL` | `glm-5.2` |
| pi binary | `--pi-binary` | `PI_REVIEW_PI_BINARY` | `pi` |
| System prompt | `--system-prompt` / `--system-prompt-file` | `PI_REVIEW_SYSTEM_PROMPT` | built-in reviewer prompt |
| Session dir | `--session-dir` | `PI_REVIEW_SESSION_DIR` | `.git/pi-reviewer/sessions` |
| Archive sessions | `--archive-sessions` | `PI_REVIEW_ARCHIVE_SESSIONS` | off (delete on go) |
| Review guidelines | `--no-review-guidelines` | `PI_REVIEW_NO_REVIEW_GUIDELINES` | on (auto-discover `REVIEW_GUIDELINES.md`) |

Examples:

```yaml
hooks:
  - id: pi-review
    args:
      - "--model"
      - "ollama-cloud/glm-5.2"
      - "--archive-sessions"     # keep a tar.gz of each reviewed session
```

```bash
# Or configure via env (e.g. in CI, a direnv, or a shell wrapper)
export PI_REVIEW_MODEL=ollama-cloud/glm-5.2
export PI_REVIEW_ARCHIVE_SESSIONS=1
```

## Provider / model notes

- The `--model` value must resolve against a provider pi can see. Built-in
  providers (e.g. google) work out of the box; extension-registered providers
  such as `ollama` / `ollama-cloud` require that provider extension to be
  installed in pi. Use the `provider/model` form to be explicit, e.g.
  `ollama-cloud/glm-5.2` or `openai/gpt-4o`.
- Run `pi --list-models` to see what's available. A pattern that doesn't
  resolve makes pi exit non-zero, which the hook treats as an infrastructure
  failure and **fails closed** (blocks the commit) — so a stale model id can
  block all commits until fixed or bypassed.
- The tool allowlist is `read,grep,find,ls,submit_review_decision`. The reviewer
  can explore surrounding code read-only but cannot run `bash`, edit, or
  write. Note: user-installed pi extensions still load their *code* at startup
  (providers, event handlers); only their *tools* are disabled by the allowlist.

## The review loop & multi-round behavior

- **First round**: a fresh pi session reviews your diff with the built-in
  (strict) reviewer prompt.
- **Rejection**: the rejected tree hash and issues are recorded; the session is
  kept.
- **Same-tree auto-reject**: if you retry `git commit` without changing
  anything, the staged tree hash matches a prior rejection and the hook blocks
  immediately **without calling pi** (fast, no model cost).
- **Follow-up round**: when you amend and retry, pi resumes the session. The
  prompt tells it the round number, lists the previous issues, and instructs it
  to be **proportionally lenient on previously raised issues that appear
  resolved** while holding the line on new problems.
- **Approval (`go`)**: all review state is cleared. The approving comments
  (summary/suggestions) are printed at commit time and persisted to
  `.git/pi-reviewer/reviews/`. If `--archive-sessions` is on, the pi session is
  compressed to `.git/pi-reviewer/archives/` instead of deleted, so you can
  resurrect it later for interrogation:

  ```bash
  tar -xzf .git/pi-reviewer/archives/<id>-<ts>.tar.gz -C /tmp
  pi --session <session-id> --session-dir /tmp/sessions
  ```

## Custom review criteria (REVIEW_GUIDELINES.md)

Place a `REVIEW_GUIDELINES.md` at the root of your repo to give the reviewer
project-specific criteria — for example, what constitutes a blocking
("no-go") finding. Its contents are appended to the review prompt and
**override the built-in criteria** (the system prompt says so explicitly), so
the agent can be more confident about rejecting commits that violate your
rules. You can put anything there; it's plain Markdown.

```markdown
# Review Guidelines
A "no-go" is required when:
- Secrets or credentials are committed.
- The change breaks the documented failure-mode matrix.
- Behavior changes are not covered by tests.
```

The file is auto-discovered on every review. Disable with
`--no-review-guidelines` or `PI_REVIEW_NO_REVIEW_GUIDELINES=1`.

## Escape hatch

```bash
# Skip only pi-review, keep all your other hooks running:
SKIP=pi-review git commit -m "..."

# (Not recommended) skip every hook:
git commit --no-verify -m "..."
```

Use `SKIP=pi-review` during model/provider outages, when iterating fast, or
when the reviewer is wrong and you've judged the change safe.

## Pitfalls & caveats

This project is experimental and was largely vibe-coded. Please read these
before relying on it:

- **It calls a model on every commit.** That means latency (seconds to tens of
  seconds) and, for hosted models, real cost per commit. The same-tree
  auto-reject avoids re-reviewing no-op retries, but every *changed* commit
  pays. Consider this for busy repos.
- **Review quality == model quality.** The reviewer can produce **false
  positives** (blocking a legitimate commit) or **misses** (approving flawed
  code). In our own dogfooding it caught a genuine ordering bug in the
  archiving code — and it also produced false positives on a correctly
  parameterized SQL query. It is an aid, not an oracle.
- **Model patterns drift.** Provider catalogs change (we hit an ollama catalog
  change mid-development that made our pinned model id stop resolving). A stale
  `--model` makes pi fail, which the hook treats as an infra failure and
  **fails closed** — blocking commits. Keep the model id current, or `SKIP`.
- **No session reset for v1.** If you abandon a change mid-review and start a
  different one, the old session context may carry over. The follow-up prompt
  explicitly tells pi this might be a new change; in practice, rely on
  `go`-clears and `SKIP=pi-review` to reset.
- **Large diffs are not handled.** Very large staged changes may exceed the
  model's context window. Not truncated or summarized in v1.
- **State lives under `.git/pi-reviewer/`** (not committed): `state.json`,
  `sessions/`, `reviews/`, and `archives/`. It is per-repo and per-working-copy.
- **pre-commit hides passed-hook output.** On a successful review the printed
  summary/suggestions aren't shown unless you run with `--verbose`. The
  authoritative record is the JSON in `.git/pi-reviewer/reviews/`.
- **Rev pinning is manual.** Pin `rev:` to a commit SHA in your
  `.pre-commit-config.yaml` and bump it deliberately after upstream changes.
  There is no autoupdate story yet.
- **Extension code runs at startup.** Because the hook does not pass
  `--no-extensions`, any pi extension you have installed will load its code
  (providers, event handlers). Their *tools* are disabled by the `--tools`
  allowlist, but the extension code itself executes — be aware if you run
  untrusted extensions.
- **NixOS specifics.** The default binary is `pi` (not the bubblewrap-sandboxed
  `pi-jailed`); override with `--pi-binary pi-jailed` if you use that variant.
  The build backend is `hatchling` precisely because `uv_build`'s binary can't
  run on NixOS inside pre-commit's build env.

## How it fails

| Scenario | Behavior | Exit |
|---|---|---|
| pi not on `PATH` | Skip (fail-open) | 0 |
| pi fails / model not found / network error | Fail-closed | 1 |
| pi runs but never calls the decision tool | Fail-closed | 1 |
| Unrecognized decision value | Fail-closed | 1 |
| Empty staged diff | Nothing to review | 0 |
| Same tree as a prior rejection | Auto-reject, **no pi call** | 1 |
| Decision `go` | Clear state (archive if enabled), proceed | 0 |
| Decision `no-go` | Record rejection, print issues, block | 1 |

When pi itself fails, the hook now includes the tail of pi's stderr in its
message so the cause (e.g. "Model ... not found") is diagnosable.

## Development

This repo reviews itself with the pi-review hook (see
`.pre-commit-config.yaml`). Lint/format use `make lint` / `make fmt`
(`nixpkgs#ruff`; the PyPI `ruff` binary can't run on NixOS). Tests:

```bash
nix shell nixpkgs#python3 nixpkgs#uv -c uv run pytest   # or: pytest -v
```

Further reading:

- [Implementation plan](docs/implementation-plan.md)
- [Architecture Decision Record](docs/adr.md)
- [AGENTS.md](AGENTS.md) — living context for anyone working on the project

## License

MIT.
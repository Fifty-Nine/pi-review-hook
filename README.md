# pi-review-precommit

A [pre-commit](https://pre-commit.com) hook that uses [pi](https://pi.dev) to
perform agentic AI code review with context retention across multiple rejected
rounds.

## How it works

- On `git commit`, the hook reviews your staged changes using pi.
- pi gives a go/no-go decision via a custom tool call.
- If no-go, the commit is blocked and issues are printed.
- If you amend and retry, pi resumes the review session with context from
  previous rounds — it knows what was already raised and is proportionally
  lenient on resolved issues.
- Once a go decision is reached, the review context is cleared.

## Installation

```yaml
# .pre-commit-config.yaml
repos:
  - repo: git@github.com:Fifty-Nine/pi-review-precommit
    rev: v0.1.0
    hooks:
      - id: pi-review
        args: ["--model", "glm-5.2"]
```

## Configuration

| Setting | CLI arg | Env var | Default |
|---|---|---|---|
| Model | `--model` | `PI_REVIEW_MODEL` | `glm-5.2` |
| pi binary | `--pi-binary` | `PI_REVIEW_PI_BINARY` | `pi` |
| System prompt | `--system-prompt` | `PI_REVIEW_SYSTEM_PROMPT` | Built-in |
| Session dir | `--session-dir` | `PI_REVIEW_SESSION_DIR` | `.git/pi-reviewer/sessions` |

Precedence: CLI args > env vars > defaults.

## Escape hatch

```bash
# Skip only pi-review, keep all other hooks:
SKIP=pi-review git commit -m "..."
```

## Documentation

- [Implementation plan](docs/implementation-plan.md)
- [Architecture Decision Record](docs/adr.md)
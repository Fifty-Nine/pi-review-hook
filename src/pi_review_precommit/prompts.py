"""System prompt and per-round user prompt construction.

The system prompt sets the reviewer persona (adapted from the pi-review
rubric, simplified for automated use). The user prompt carries the staged
diff plus round-specific context: first round is a fresh review, follow-up
rounds tell the agent about the ambiguity of the retry (continuation vs.
new change — see ADR Decision 3) and list previously raised issues with a
leniency instruction (ADR Decision 6).
"""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

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
- If the repository contains a REVIEW_GUIDELINES.md file, its contents
  override these general instructions — follow them when deciding go vs
  no-go.

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


def _run_git(args: list[str]) -> str:
    """Run a git command, returning stdout. Raises RuntimeError on failure."""
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {stderr}")
    return result.stdout


def get_staged_diff() -> str:
    """Get the staged diff via git diff --cached."""
    return _run_git(["diff", "--cached"])


def get_staged_files() -> list[str]:
    """Get list of staged file paths."""
    out = _run_git(["diff", "--cached", "--name-only"])
    return [f for f in out.strip().splitlines() if f.strip()]


def get_staged_tree_hash() -> str:
    """Compute the staged tree hash via git write-tree."""
    return _run_git(["write-tree"]).strip()


def _today() -> str:
    """Current local date, e.g. 2026-08-14.

    Injected into the review prompts so the model doesn't guess the date
    (it previously suggested a wrong copyright year because it had no
    notion of the current date).
    """
    return datetime.now().strftime("%Y-%m-%d")


REVIEW_GUIDELINES_FILE = "REVIEW_GUIDELINES.md"

# The well-known empty tree hash; used as the base for root commits
# (no HEAD / no parent) so `git diff` still works.
EMPTY_TREE_HASH = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def get_review_guidelines() -> str | None:
    """Read REVIEW_GUIDELINES.md from the repo root, if present.

    Follows the pi-review extension convention: a REVIEW_GUIDELINES.md file
    in the repo (pre-commit runs at the repo root) is appended to the review
    prompt and overrides the default criteria. Returns None if absent,
    empty, or unreadable.
    """
    path = Path(REVIEW_GUIDELINES_FILE)
    try:
        if not path.is_file():
            return None
        content = path.read_text().strip()
    except OSError:
        return None
    return content or None


def _guidelines_section(guidelines: str | None) -> str:
    """Render the project review guidelines section, or empty string."""
    if not guidelines:
        return ""
    return (
        "\n\n## Project review guidelines (override the default criteria)\n\n"
        f"{guidelines}"
    )


def get_full_change_set_diff(base_tree: str, staged_tree: str) -> str:
    """Get the full change set diff: base_tree -> staged_tree.

    This is the diff the amended commit will contain (the parent of the
    approved commit to the staged tree), as opposed to the delta-only
    ``git diff --cached`` the hook computes for fresh reviews.
    """
    return _run_git(["diff", base_tree, staged_tree])


def get_head_tree() -> str:
    """The tree of HEAD (the parent of a fresh commit). Empty tree if no HEAD."""
    try:
        return _run_git(["rev-parse", "HEAD^{tree}"]).strip()
    except RuntimeError:
        return EMPTY_TREE_HASH


def get_parent_tree() -> str:
    """The tree of HEAD~1 (the parent of an amended commit). Empty tree if none."""
    try:
        return _run_git(["rev-parse", "HEAD~1^{tree}"]).strip()
    except RuntimeError:
        return EMPTY_TREE_HASH


def build_amend_prompt(
    diff: str,
    files: list[str],
    round_number: int,
    review_guidelines: str | None = None,
) -> str:
    """Build the prompt for an amend follow-up review.

    The user amended a previously approved commit. The reviewer sees the
    FULL change set the amended commit will contain (base -> staged), not
    just the delta, and is asked to verify the previous feedback was
    addressed and re-evaluate the whole change.

    Framing by round: round 0 (after a go) -> verify suggestions; round > 0
    (after a no-go on an amend) -> verify issues + leniency. The previous
    feedback lives in the resumed session, so it is not injected here.
    """
    files_str = "\n".join(f"  - {f}" for f in files)

    if round_number == 0:
        framing = (
            "You previously approved this change. The user has amended the "
            "commit to address your suggestions. Verify your suggestions "
            "were addressed and re-evaluate the FULL amended change set "
            "below (you may raise new issues anywhere)."
        )
    else:
        framing = (
            "You previously rejected this change with issues. The user has "
            "amended the commit again. Verify the previously raised issues "
            "were resolved and re-evaluate the FULL amended change set "
            "below. Be proportionally lenient on previously raised issues "
            "that appear resolved, but maintain standards on new problems."
        )

    return f"""\
The user has run `git commit --amend` on a previously reviewed commit.

{framing}

Today's date is {_today()}.

This is review round {round_number + 1}.

## Staged files

{files_str}

## Full amended change set (base -> staged)

```diff
{diff}
```
{_guidelines_section(review_guidelines)}

Perform your review and call submit_review_decision with your decision.
"""


def build_first_round_prompt(
    diff: str, files: list[str], review_guidelines: str | None = None
) -> str:
    """Build the prompt for the first review round."""
    files_str = "\n".join(f"  - {f}" for f in files)
    return f"""\
Review the following staged changes and decide whether they are acceptable
to commit.

Today's date is {_today()}.

## Staged files

{files_str}

## Staged diff

```diff
{diff}
```
{_guidelines_section(review_guidelines)}

Perform your review and call submit_review_decision with your decision.
"""


def build_followup_prompt(
    diff: str,
    files: list[str],
    round_number: int,
    previous_issues: list | None,
    review_guidelines: str | None = None,
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

Today's date is {_today()}.

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
{_guidelines_section(review_guidelines)}

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

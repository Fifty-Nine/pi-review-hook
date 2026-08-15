"""Post-commit hook: attach pi-review notes to commits.

Runs at post-commit stage (hook id ``pi-review-notes``). For each commit it
finds the just-created commit whose tree matches a finished (go) review log
under ``.git/pi-reviewer/reviews/`` and attaches a human-readable printout
of the review as a git note under ``refs/notes/pi-review``. Commits with no
matching review get a brief "no review" audit note, so every commit is
annotated.

Failure semantics (ADR: fail-open but visible): a post-commit hook cannot
block a commit (it already happened), so a note-attach failure exits
non-zero and leaves the review log in place for a retry; cleanup failures
are logged to stderr only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from pi_review_precommit.state import REVIEWS_DIR

NOTES_REF = "refs/notes/pi-review"
AUDIT_NOTE_TEXT = (
    "No pi-review associated with this commit "
    "(skipped, bypassed, or tree mismatch)."
)


def build_note_text(review_log: dict) -> str:
    """Render a review log as the human-readable git note text.

    ``Decision`` is always present; ``Summary`` / ``Suggestions`` /
    ``Issues`` sections are omitted when empty. The ``Session archive:``
    line is included only when the review log records an archive path
    (i.e. archiving was enabled).
    """
    lines = [f"Decision: {review_log.get('decision', 'go')}"]

    summary = review_log.get("summary")
    if summary:
        lines.append(f"Summary: {summary}")

    suggestions = review_log.get("suggestions")
    if suggestions:
        lines.append("Suggestions:")
        for s in suggestions:
            lines.append(f"- {s}")

    issues = review_log.get("issues")
    if issues:
        lines.append("Issues:")
        for issue in issues:
            severity = issue.get("severity", "?")
            desc = issue.get("description", "")
            file = issue.get("file", "")
            line = issue.get("line", "")
            loc = f"{file}:{line}" if file else ""
            lines.append(f"- [{severity}] {desc} ({loc})")

    archive_path = review_log.get("archive_path")
    if archive_path:
        if not os.path.isabs(archive_path):
            archive_path = f"./{archive_path}"
        lines.append(f"Session archive: {archive_path}")

    return "\n".join(lines)


def build_audit_note_text() -> str:
    """The brief note attached to commits with no associated review."""
    return AUDIT_NOTE_TEXT


def find_review_log_for_tree(tree_hash: str) -> Path | None:
    """Find the review log whose filename embeds the given tree hash.

    Review log filenames are ``<timestamp>-<tree-hash>.json``; the tree
    hash is the matching key between a staged review and the commit that
    lands it.
    """
    if not REVIEWS_DIR.exists():
        return None
    for p in sorted(REVIEWS_DIR.glob("*.json")):
        if tree_hash in p.name:
            return p
    return None


def attach_note(commit_sha: str, note_text: str) -> None:
    """Attach ``note_text`` to ``commit_sha`` under ``refs/notes/pi-review``.

    Raises RuntimeError if git fails (e.g. the notes ref is corrupt).
    """
    proc = subprocess.run(
        ["git", "notes", "--ref", NOTES_REF, "add", "-m", note_text, commit_sha],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise RuntimeError(f"git notes attach failed: {detail}")


def _git_rev_parse(rev: str) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", rev], capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git rev-parse {rev} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    """Post-commit entry point (console script ``pi-review-notes``).

    ``argv`` is accepted for testability; the hook takes no arguments.
    """
    try:
        commit = _git_rev_parse("HEAD")
        tree = _git_rev_parse("HEAD^{tree}")
    except RuntimeError as e:
        print(f"pi-review-notes: {e}", file=sys.stderr)
        return 1

    log = find_review_log_for_tree(tree)
    if log is not None:
        try:
            review_log = json.loads(log.read_text())
        except (OSError, json.JSONDecodeError) as e:
            print(
                f"pi-review-notes: cannot read review log {log}: {e}",
                file=sys.stderr,
            )
            return 1
        try:
            attach_note(commit, build_note_text(review_log))
        except RuntimeError as e:
            print(f"pi-review-notes: {e}", file=sys.stderr)
            return 1  # fail-open but visible: log kept for a retry
        # Cleanup: delete the consumed log. Failure is a warning only —
        # the note is attached, and an orphaned log is harmless.
        try:
            log.unlink()
        except OSError as e:
            print(
                f"pi-review-notes: warning: could not remove review log "
                f"{log}: {e}",
                file=sys.stderr,
            )
        return 0

    # No matching review: audit note so every commit is annotated.
    try:
        attach_note(commit, build_audit_note_text())
    except RuntimeError as e:
        print(f"pi-review-notes: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

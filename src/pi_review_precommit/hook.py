"""Main hook entry point. Called by pre-commit as `pi-review`.

Implements the full flow described in AGENTS.md and the implementation
plan, including the failure-mode matrix (ADR Decision 7):

| Scenario                          | Behavior                          | Exit |
|-----------------------------------|-----------------------------------|------|
| pi not in PATH                    | Skip (fail-open)                  | 0    |
| pi invocation fails (crash/API)   | Fail-closed                       | 1    |
| Non-compliance (no decision tool) | Fail-closed                       | 1    |
| Unrecognized decision value       | Fail-closed                       | 1    |
| Decision = "go"                   | Keep state (lazy clear), pass     | 0    |
| Decision = "no-go"                | Record rejection, block           | 1    |
| Same tree as previous rejection   | Auto-reject, no pi call           | 1    |
| Empty staged diff                 | Nothing to review                 | 0    |

Amend flow: on a detected ``git commit --amend`` (process hierarchy on
Linux, see amend_detect.py) with a previously approved tree, the existing
session is resumed and the reviewer sees the FULL change set (base ->
staged) instead of the delta, verifying the previous feedback was
addressed. After a go the session is kept (lazy clear) so a subsequent
amend can resume it; it is cleared on the next non-amend commit.
"""

from __future__ import annotations

import subprocess
import sys
import uuid

from pi_review_precommit.amend_detect import AMEND, detect_amend
from pi_review_precommit.config import parse_args
from pi_review_precommit.pi_runner import find_pi, run_review
from pi_review_precommit.prompts import (
    EMPTY_TREE_HASH,
    build_amend_prompt,
    build_first_round_prompt,
    build_followup_prompt,
    get_full_change_set_diff,
    get_head_tree,
    get_parent_tree,
    get_previous_issues,
    get_review_guidelines,
    get_staged_diff,
    get_staged_files,
    get_staged_tree_hash,
    get_system_prompt,
)
from pi_review_precommit.state import (
    archive_sessions,
    clear_state,
    get_approved_tree,
    get_base_tree,
    get_round_number,
    get_session_id,
    is_tree_rejected,
    record_approval,
    record_go_state,
    record_rejection,
    save_state,
)


def main(argv: list[str] | None = None) -> int:
    config = parse_args(argv)

    # 1. Check if pi is available
    pi_path = find_pi(config.pi_binary)
    if pi_path is None:
        print(
            f"pi-review: '{config.pi_binary}' not found in PATH, skipping review.",
            file=sys.stderr,
        )
        return 0  # fail-open: pi not installed

    # 2. Compute staged tree hash + diff (git operations)
    try:
        tree_hash = get_staged_tree_hash()
        diff = get_staged_diff()
    except RuntimeError as e:
        print(f"pi-review: {e}", file=sys.stderr)
        return 1  # fail-closed: cannot determine what to review

    # 3. Empty staged diff — nothing to review (checked before tree
    #    rejection so a stale rejected tree can't block an empty index).
    if not diff.strip():
        return 0

    # 4. Check if this tree was already rejected
    if is_tree_rejected(tree_hash):
        print(
            "pi-review: These changes are identical to a previously "
            "rejected review. Amend your changes or use "
            "SKIP=pi-review to bypass.",
            file=sys.stderr,
        )
        return 1  # auto-reject, no pi call

    # 5. Get or create session
    session_id = get_session_id()
    round_number = get_round_number()
    approved_tree = get_approved_tree()
    base_tree = get_base_tree()

    # 6. Detect amend (process hierarchy + detached-HEAD guard)
    amend = detect_amend()

    # 7. Select the review path
    files = get_staged_files()
    system_prompt = get_system_prompt(config.system_prompt)
    review_guidelines = get_review_guidelines() if config.review_guidelines else None

    if amend == AMEND and approved_tree is not None:
        # Amend follow-up: resume the session and review the FULL change
        # set (base -> staged), verifying the previous feedback was
        # addressed and re-evaluating the whole change.
        if session_id is None:
            # Defensive: approved_tree implies a session, but never crash.
            session_id = f"pi-review-{uuid.uuid4().hex[:12]}"
        full_diff = get_full_change_set_diff(base_tree or EMPTY_TREE_HASH, tree_hash)
        user_prompt = build_amend_prompt(
            full_diff, files, round_number, review_guidelines
        )
    elif round_number > 0:
        # Existing no-go follow-up: resume session, delta + leniency.
        previous_issues = get_previous_issues()
        user_prompt = build_followup_prompt(
            diff, files, round_number, previous_issues, review_guidelines
        )
    else:
        # Fresh review: clear any kept-after-go state, new session, delta.
        if session_id is not None:
            clear_state(
                config.session_dir,
                session_id=session_id,
                archive=config.archive_sessions,
            )
        session_id = f"pi-review-{uuid.uuid4().hex[:12]}"
        save_state(
            {
                "session_id": session_id,
                "rejected_trees": [],
                "round": 0,
            }
        )
        round_number = 0
        user_prompt = build_first_round_prompt(diff, files, review_guidelines)

    # 7. Invoke pi
    try:
        decision_args = run_review(
            pi_binary=config.pi_binary,
            model=config.model,
            session_id=session_id,
            session_dir=config.session_dir,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
    except subprocess.CalledProcessError as e:
        # Include pi's stderr (e.g. "Model ... not found") so the cause is
        # diagnosable; the exception repr alone omits it.
        detail = f": {e.stderr.strip()[-500:]}" if e.stderr else ""
        print(f"pi-review: pi invocation failed: {e}{detail}", file=sys.stderr)
        return 1  # fail-closed: infra error
    except Exception as e:
        print(f"pi-review: pi invocation failed: {e}", file=sys.stderr)
        return 1  # fail-closed: infra error

    # 8. Check for decision tool call
    if decision_args is None:
        print(
            "pi-review: Reviewer did not produce a decision "
            "(no submit_review_decision tool call).",
            file=sys.stderr,
        )
        return 1  # fail-closed: non-compliance

    decision = decision_args.get("decision", "").lower().strip()

    # 9. Handle decision
    if decision == "go":
        summary = decision_args.get("summary")
        suggestions = decision_args.get("suggestions")
        # Archive the session first (fail-closed on error) so the archive
        # path can be recorded in the review log for the post-commit hook.
        archive_path = None
        if config.archive_sessions:
            archive_path = archive_sessions(config.session_dir, session_id)
        # Persist the approving comments before the state clear removes
        # the session, and surface them at commit time.
        record_approval(
            session_id,
            tree_hash,
            round_number,
            decision_args,
            archive_path=archive_path,
        )
        # Compute the base tree (parent of the just-approved commit):
        # amend -> HEAD~1 (amend keeps the parent); fresh -> HEAD.
        if amend == AMEND:
            base_tree = get_parent_tree()
        else:
            base_tree = get_head_tree()
        # Keep the session for a potential amend (lazy clear): the session
        # dir + state stay; cleared on the next non-amend commit.
        record_go_state(session_id, tree_hash, base_tree)
        print("pi-review: Changes approved.", file=sys.stderr)
        if summary:
            print(f"  Summary: {summary}", file=sys.stderr)
        if suggestions:
            print("  Suggestions:", file=sys.stderr)
            for s in suggestions:
                print(f"    - {s}", file=sys.stderr)
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
            "  Amend your changes and try again, or use SKIP=pi-review to bypass.",
            file=sys.stderr,
        )
        return 1
    else:
        print(
            f"pi-review: Unrecognized decision '{decision}'. Expected 'go' or 'no-go'.",
            file=sys.stderr,
        )
        return 1  # fail-closed: unparseable decision


if __name__ == "__main__":
    sys.exit(main())

"""Main hook entry point. Called by pre-commit as `pi-review`.

Implements the full flow described in AGENTS.md and the implementation
plan, including the failure-mode matrix (ADR Decision 7):

| Scenario                          | Behavior                          | Exit |
|-----------------------------------|-----------------------------------|------|
| pi not in PATH                    | Skip (fail-open)                  | 0    |
| pi invocation fails (crash/API)   | Fail-closed                       | 1    |
| Non-compliance (no decision tool) | Fail-closed                       | 1    |
| Unrecognized decision value       | Fail-closed                       | 1    |
| Decision = "go"                   | Clear state, pass                 | 0    |
| Decision = "no-go"                | Record rejection, block           | 1    |
| Same tree as previous rejection   | Auto-reject, no pi call           | 1    |
| Empty staged diff                 | Nothing to review                 | 0    |
"""

from __future__ import annotations

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

    if session_id is None:
        # First review — create new session
        session_id = f"pi-review-{uuid.uuid4().hex[:12]}"
        save_state(
            {
                "session_id": session_id,
                "rejected_trees": [],
                "round": 0,
            }
        )
        round_number = 0

    # 6. Construct prompt
    files = get_staged_files()
    system_prompt = get_system_prompt(config.system_prompt)

    if round_number == 0:
        user_prompt = build_first_round_prompt(diff, files)
    else:
        previous_issues = get_previous_issues()
        user_prompt = build_followup_prompt(diff, files, round_number, previous_issues)

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

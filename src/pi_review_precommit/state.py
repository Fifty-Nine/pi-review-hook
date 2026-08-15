"""State management for review sessions under .git/pi-reviewer/.

Holds the hook coordination state (session id, rejected tree hashes, round
counter) plus the persistent review log. The pi conversation history itself
lives in the pi session store under ``<session-dir>/<session-id>`` and is
managed by pi, not by this module.

See ADR Decision 3 (accumulate until go) and Decision 4 (same-tree
auto-reject).
"""

from __future__ import annotations

import gzip
import json
import shutil
from datetime import datetime
from pathlib import Path

STATE_DIR = Path(".git") / "pi-reviewer"
STATE_FILE = STATE_DIR / "state.json"
SESSIONS_SUBDIR = "sessions"
REVIEWS_DIR = STATE_DIR / "reviews"


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


def clear_state(
    session_dir: str = ".git/pi-reviewer/sessions",
    archive: bool = False,
    session_id: str | None = None,
) -> None:
    """Clear all state: hook state file + pi session directory.

    If ``archive`` is set, the session is archived to
    ``<session-dir-parent>/archive/session-<id>.jsonl.gz`` first (opt-in, so
    prior sessions can be resurrected for interrogation) instead of being
    deleted outright. A missing session file (stale state) is tolerated —
    there is nothing to archive, so the directory is cleared anyway.
    """
    sdir = sessions_path(session_dir)
    if sdir.exists():
        if archive:
            try:
                archive_sessions(session_dir, session_id)
            except FileNotFoundError:
                # Nothing to archive (e.g. stale state with no session
                # file); clear the directory anyway.
                pass
        shutil.rmtree(sdir)

    # Unlink the state file last: if archiving (or anything above) fails,
    # the hook fails closed but state.json is still intact for a retry.
    p = state_path()
    if p.exists():
        p.unlink()


def archive_sessions(
    session_dir: str = ".git/pi-reviewer/sessions",
    session_id: str | None = None,
) -> Path:
    """Archive the pi session as a gzipped jsonl file.

    The pi session store keeps one jsonl file per session directly in the
    session dir, named ``<timestamp>_<session-id>.jsonl``. The archive is a
    gzip of that file: ``<parent>/archive/session-<id>.jsonl.gz``.

    Returns the archive path. Raises FileNotFoundError if no session file
    matches ``session_id``.
    """
    if session_id is None:
        raise ValueError("session_id is required to archive a session")
    sdir = sessions_path(session_dir)
    matches = sorted(sdir.glob(f"*{session_id}*.jsonl"))
    if not matches:
        raise FileNotFoundError(
            f"no session file for '{session_id}' under {sdir}"
        )
    archive_dir = sdir.parent / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    path = archive_dir / f"session-{session_id}.jsonl.gz"
    with matches[0].open("rb") as fin, gzip.open(path, "wb") as fout:
        shutil.copyfileobj(fin, fout)
    return path


def record_rejection(session_id: str, tree_hash: str, issues: list | None) -> None:
    """Record a rejected tree hash and optional issues in state.

    Preserves ``approved_tree`` / ``base_tree`` (a no-go on an amend keeps
    the last go's base so the next amend can resume against it).
    """
    state = load_state() or {
        "session_id": session_id,
        "rejected_trees": [],
        "round": 0,
    }
    rejected = state.setdefault("rejected_trees", [])
    rejected.append({"tree_hash": tree_hash, "issues": issues})
    state["round"] = state.get("round", 0) + 1
    save_state(state)


def record_go_state(session_id: str, approved_tree: str, base_tree: str) -> None:
    """Record the go state: keep the session for a potential amend.

    After a go the session is NOT cleared (lazy clear): the session id,
    the approved tree, and the base tree (the parent of the approved
    commit) are kept so a subsequent ``git commit --amend`` can resume the
    session and re-review the full change set. Cleared on the next
    non-amend commit (round 0) via ``clear_state``.
    """
    save_state(
        {
            "session_id": session_id,
            "rejected_trees": [],
            "round": 0,
            "approved_tree": approved_tree,
            "base_tree": base_tree,
        }
    )


def get_approved_tree() -> str | None:
    """The tree hash of the last approved (go) commit, or None."""
    state = load_state()
    if not state:
        return None
    return state.get("approved_tree")


def get_base_tree() -> str | None:
    """The base tree (parent of the approved commit) for amend diffs."""
    state = load_state()
    if not state:
        return None
    return state.get("base_tree")


def is_tree_rejected(tree_hash: str) -> bool:
    """Check if a tree hash was previously rejected."""
    state = load_state()
    if not state:
        return False
    return any(
        entry["tree_hash"] == tree_hash for entry in state.get("rejected_trees", [])
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


def record_approval(
    session_id: str,
    tree_hash: str,
    round_number: int,
    decision_args: dict,
    archive_path: str | None = None,
) -> Path:
    """Persist the final (go) review round as a review log.

    The approving comments (summary, suggestions, issues) would otherwise
    be lost when the session is eventually cleared (lazy clear — the
    session is kept after a go for a potential amend); this keeps them for
    later reference under ``.git/pi-reviewer/reviews/``. ``archive_path``
    (when archiving is enabled) lets the post-commit hook link the session
    archive from the git note.
    """
    now = datetime.now()
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    path = REVIEWS_DIR / f"{now.strftime('%Y%m%dT%H%M%S%f')}-{tree_hash}.json"
    payload = {
        "timestamp": now.isoformat(timespec="seconds"),
        "session_id": session_id,
        "tree_hash": tree_hash,
        "round": round_number,
        "decision": decision_args.get("decision", "go"),
        "summary": decision_args.get("summary"),
        "suggestions": decision_args.get("suggestions"),
        "issues": decision_args.get("issues"),
        "archive_path": str(archive_path) if archive_path else None,
    }
    path.write_text(json.dumps(payload, indent=2))
    return path

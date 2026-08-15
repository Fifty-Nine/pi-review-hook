"""State management for review sessions under .git/pi-reviewer/.

Holds the hook coordination state (session id, rejected tree hashes, round
counter). The pi conversation history itself lives in the pi session store
under ``<session-dir>/<session-id>`` and is managed by pi, not by this module.

See ADR Decision 3 (accumulate until go) and Decision 4 (same-tree
auto-reject).
"""

from __future__ import annotations

import json
import shutil
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

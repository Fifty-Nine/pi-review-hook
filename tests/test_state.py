"""Tests for state.py: state management under .git/pi-reviewer/."""

import pytest

from pi_review_precommit import state


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """State functions use CWD-relative .git/ paths; run in a temp repo."""
    monkeypatch.chdir(tmp_path)


def test_load_state_none_when_missing() -> None:
    assert state.load_state() is None
    assert state.get_session_id() is None
    assert state.get_round_number() == 0
    assert state.is_tree_rejected("abc") is False


def test_save_and_load_roundtrip() -> None:
    expected = {"session_id": "s1", "rejected_trees": [], "round": 0}
    state.save_state(expected)
    assert state.load_state() == expected
    assert state.state_path().exists()
    assert state.get_session_id() == "s1"
    assert state.get_round_number() == 0


def test_record_rejection_creates_state_and_increments_round() -> None:
    state.record_rejection("s1", "tree1", [{"description": "bug"}])
    assert state.get_session_id() == "s1"
    assert state.get_round_number() == 1
    assert state.is_tree_rejected("tree1") is True
    assert state.is_tree_rejected("tree2") is False

    # Second rejection on a different tree accumulates
    state.record_rejection("s1", "tree2", None)
    loaded = state.load_state()
    assert loaded is not None
    assert len(loaded["rejected_trees"]) == 2
    assert state.get_round_number() == 2
    assert state.is_tree_rejected("tree2") is True


def test_record_rejection_preserves_session_across_rounds() -> None:
    state.save_state({"session_id": "s1", "rejected_trees": [], "round": 0})
    state.record_rejection("s1", "tree1", None)
    assert state.get_session_id() == "s1"  # unchanged
    assert state.get_round_number() == 1


def test_clear_state_removes_state_and_sessions(tmp_path) -> None:
    state.record_rejection("s1", "tree1", None)
    # Simulate a pi-created session dir
    sessions = tmp_path / ".git" / "pi-reviewer" / "sessions" / "s1"
    sessions.mkdir(parents=True)
    (sessions / "session.json").write_text("{}")

    state.clear_state()
    assert not state.state_path().exists()
    assert not state.load_state()
    assert not sessions.exists()


def test_clear_state_custom_session_dir(tmp_path) -> None:
    state.record_rejection("s1", "tree1", None)
    custom = tmp_path / "custom-sessions"
    (custom / "s1").mkdir(parents=True)

    state.clear_state(session_dir=str(custom))
    assert not state.state_path().exists()
    assert not custom.exists()

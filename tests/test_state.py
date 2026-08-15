"""Tests for state.py: state management under .git/pi-reviewer/."""

import json

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
    # default: no archive is produced
    assert not (tmp_path / ".git" / "pi-reviewer" / "archives").exists()


def test_clear_state_custom_session_dir(tmp_path) -> None:
    state.record_rejection("s1", "tree1", None)
    custom = tmp_path / "custom-sessions"
    (custom / "s1").mkdir(parents=True)

    state.clear_state(session_dir=str(custom))
    assert not state.state_path().exists()
    assert not custom.exists()


def test_record_approval_writes_review_log() -> None:
    result = state.record_approval(
        "s1",
        "tree123",
        2,
        {
            "decision": "go",
            "summary": "looks good",
            "suggestions": ["consider adding a test"],
            "issues": None,
        },
    )
    assert result.exists()
    payload = json.loads(result.read_text())
    assert payload["session_id"] == "s1"
    assert payload["tree_hash"] == "tree123"
    assert payload["round"] == 2
    assert payload["decision"] == "go"
    assert payload["summary"] == "looks good"
    assert payload["suggestions"] == ["consider adding a test"]


def test_record_approval_defaults_on_bare_go() -> None:
    result = state.record_approval("s1", "tree1", 0, {"decision": "go"})
    payload = json.loads(result.read_text())
    assert payload["summary"] is None
    assert payload["suggestions"] is None
    assert payload["issues"] is None


def test_clear_state_archive_creates_tarball(tmp_path) -> None:
    import tarfile

    state.record_rejection("s1", "tree1", None)
    sessions = tmp_path / ".git" / "pi-reviewer" / "sessions" / "s1"
    sessions.mkdir(parents=True)
    (sessions / "session.json").write_text('{"id": "s1"}')

    state.clear_state(session_id="s1", archive=True)

    # state file + session dir gone
    assert not state.state_path().exists()
    assert not sessions.exists()
    # tarball exists and contains the session files
    archives = tmp_path / ".git" / "pi-reviewer" / "archives"
    tarballs = list(archives.glob("s1-*.tar.gz"))
    assert len(tarballs) == 1
    with tarfile.open(tarballs[0], "r:gz") as tar:
        names = tar.getnames()
    assert any(n.endswith("s1/session.json") for n in names)


def test_archive_sessions_returns_path(tmp_path) -> None:
    sessions = tmp_path / ".git" / "pi-reviewer" / "sessions" / "s1"
    sessions.mkdir(parents=True)
    (sessions / "session.json").write_text("{}")

    path = state.archive_sessions(session_id="s1")
    assert path.exists()
    assert path.name.startswith("s1-")
    assert path.suffix == ".gz"

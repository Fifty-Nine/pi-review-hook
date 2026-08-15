"""Tests for notes.py: the post-commit hook that attaches pi-review notes."""

import json
import subprocess

import pytest

from pi_review_precommit import notes
from pi_review_precommit.state import REVIEWS_DIR

TREE = "a1b2c3d4e5f6"
COMMIT = "c0ffee42"


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """State functions use CWD-relative .git/ paths; run in a temp repo."""
    monkeypatch.chdir(tmp_path)


def _write_review_log(tree_hash: str, **overrides) -> None:
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": "2026-08-15T08:26:09",
        "session_id": "pi-review-abc",
        "tree_hash": tree_hash,
        "round": 1,
        "decision": "go",
        "summary": "LGTM",
        "suggestions": ["add a test"],
        "issues": None,
        "archive_path": None,
    }
    payload.update(overrides)
    (REVIEWS_DIR / f"20260815T082609-{tree_hash}.json").write_text(
        json.dumps(payload)
    )


# --- Note text builder -----------------------------------------------------


def test_build_note_text_full_fields() -> None:
    text = notes.build_note_text(
        {
            "decision": "go",
            "summary": "LGTM with minor notes",
            "suggestions": ["add a test", "rename x"],
            "issues": [
                {"severity": "major", "description": "bug", "file": "a.py", "line": 3}
            ],
            "archive_path": ".git/pi-reviewer/archive/session-s1.jsonl.gz",
        }
    )
    assert text.splitlines() == [
        "Decision: go",
        "Summary: LGTM with minor notes",
        "Suggestions:",
        "- add a test",
        "- rename x",
        "Issues:",
        "- [major] bug (a.py:3)",
        "Session archive: ./.git/pi-reviewer/archive/session-s1.jsonl.gz",
    ]


def test_build_note_text_omits_empty_sections() -> None:
    text = notes.build_note_text({"decision": "go"})
    assert text == "Decision: go"
    assert "Summary" not in text
    assert "Suggestions" not in text
    assert "Issues" not in text
    assert "Session archive" not in text


def test_build_note_text_archive_link_conditional() -> None:
    with_archive = notes.build_note_text(
        {"decision": "go", "archive_path": ".git/pi-reviewer/archive/s.jsonl.gz"}
    )
    assert "Session archive: ./.git/pi-reviewer/archive/s.jsonl.gz" in with_archive

    without = notes.build_note_text({"decision": "go", "archive_path": None})
    assert "Session archive" not in without


def test_build_note_text_absolute_archive_path_not_prefixed() -> None:
    text = notes.build_note_text(
        {"decision": "go", "archive_path": "/tmp/sessions/archive/s.jsonl.gz"}
    )
    assert "Session archive: /tmp/sessions/archive/s.jsonl.gz" in text
    assert "Session archive: .//tmp" not in text


def test_build_audit_note_text() -> None:
    assert "No pi-review" in notes.build_audit_note_text()


# --- Review log lookup -----------------------------------------------------


def test_find_review_log_for_tree_matches_filename() -> None:
    _write_review_log(TREE)
    found = notes.find_review_log_for_tree(TREE)
    assert found is not None
    assert TREE in found.name


def test_find_review_log_for_tree_no_match() -> None:
    _write_review_log(TREE)
    assert notes.find_review_log_for_tree("zzzz") is None


def test_find_review_log_for_tree_missing_dir() -> None:
    assert notes.find_review_log_for_tree(TREE) is None


# --- attach_note -----------------------------------------------------------


def test_attach_note_builds_git_notes_command(monkeypatch) -> None:
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(notes.subprocess, "run", fake_run)
    notes.attach_note(COMMIT, "Decision: go")
    assert captured["cmd"] == [
        "git",
        "notes",
        "--ref",
        "refs/notes/pi-review",
        "add",
        "-m",
        "Decision: go",
        COMMIT,
    ]


def test_attach_note_raises_on_failure(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "fatal: notes ref corrupt")

    monkeypatch.setattr(notes.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="notes ref corrupt"):
        notes.attach_note(COMMIT, "Decision: go")


# --- main flow -------------------------------------------------------------


@pytest.fixture
def mock_git(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_rev_parse(rev: str) -> str:
        return COMMIT if rev == "HEAD" else TREE

    monkeypatch.setattr(notes, "_git_rev_parse", fake_rev_parse)


def test_main_match_attaches_note_and_cleans_log(
    mock_git, monkeypatch, capsys
) -> None:
    _write_review_log(
        TREE, summary="LGTM", archive_path=".git/pi-reviewer/archive/s.jsonl.gz"
    )
    attached: list[tuple[str, str]] = []

    def fake_attach(commit, text):
        attached.append((commit, text))

    monkeypatch.setattr(notes, "attach_note", fake_attach)

    assert notes.main([]) == 0
    assert attached == [
        (
            COMMIT,
            "Decision: go\nSummary: LGTM\nSuggestions:\n- add a test\n"
            "Session archive: ./.git/pi-reviewer/archive/s.jsonl.gz",
        )
    ]
    # consumed log deleted
    assert not list(REVIEWS_DIR.glob("*.json"))
    assert capsys.readouterr().err == ""


def test_main_no_match_attaches_audit_note(mock_git, monkeypatch) -> None:
    attached: list[tuple[str, str]] = []

    def fake_attach(commit, text):
        attached.append((commit, text))

    monkeypatch.setattr(notes, "attach_note", fake_attach)

    assert notes.main([]) == 0
    assert len(attached) == 1
    assert attached[0][0] == COMMIT
    assert "No pi-review" in attached[0][1]


def test_main_note_failure_keeps_log_and_exits_nonzero(
    mock_git, monkeypatch, capsys
) -> None:
    _write_review_log(TREE)

    def boom(commit, text):
        raise RuntimeError("git notes attach failed: boom")

    monkeypatch.setattr(notes, "attach_note", boom)

    assert notes.main([]) == 1
    # log kept for a retry
    assert len(list(REVIEWS_DIR.glob("*.json"))) == 1
    assert "boom" in capsys.readouterr().err


def test_main_cleanup_failure_warns_but_exits_zero(
    mock_git, monkeypatch, capsys
) -> None:
    _write_review_log(TREE)
    monkeypatch.setattr(notes, "attach_note", lambda commit, text: None)

    def boom(path):
        raise OSError("permission denied")

    monkeypatch.setattr(notes.Path, "unlink", boom)

    assert notes.main([]) == 0
    err = capsys.readouterr().err
    assert "warning" in err
    assert "permission denied" in err


def test_main_git_plumbing_failure_exits_nonzero(monkeypatch, capsys) -> None:
    def boom(rev):
        raise RuntimeError("git rev-parse HEAD failed: not a git repository")

    monkeypatch.setattr(notes, "_git_rev_parse", boom)

    assert notes.main([]) == 1
    assert "not a git repository" in capsys.readouterr().err


def test_main_unreadable_review_log_exits_nonzero(
    mock_git, monkeypatch, capsys
) -> None:
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    (REVIEWS_DIR / f"20260815T082609-{TREE}.json").write_text("not json{")

    assert notes.main([]) == 1
    assert "cannot read review log" in capsys.readouterr().err

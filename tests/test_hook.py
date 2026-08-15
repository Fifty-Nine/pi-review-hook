"""Integration tests for hook.py: full hook flow with mocked pi + git."""

import json
import subprocess

import pytest

from pi_review_precommit import hook

DIFF = "diff --git a/a.py b/a.py\n+print('hi')\n"
FILES = ["a.py"]
TREE = "a1b2c3d4"


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def mock_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hook, "get_staged_tree_hash", lambda: TREE)
    monkeypatch.setattr(hook, "get_staged_diff", lambda: DIFF)
    monkeypatch.setattr(hook, "get_staged_files", lambda: FILES)


@pytest.fixture
def mock_pi_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hook, "find_pi", lambda binary: f"/usr/bin/{binary}")


def _capture_run_review(monkeypatch: pytest.MonkeyPatch, decision_args: dict | None):
    """Install a fake run_review that records its user_prompt and returns
    the given decision args (or raises / returns None per caller needs)."""
    captured: dict = {}

    def fake_run_review(**kwargs):
        captured.update(kwargs)
        if isinstance(decision_args, Exception):
            raise decision_args
        return decision_args

    monkeypatch.setattr(hook, "run_review", fake_run_review)
    return captured


# --- First round: go -------------------------------------------------------


def test_first_round_go_passes(mock_git, mock_pi_available, monkeypatch):
    captured = _capture_run_review(monkeypatch, {"decision": "go"})

    assert hook.main([]) == 0

    # First-round prompt used
    assert "Staged files" in captured["user_prompt"]
    assert "review round" not in captured["user_prompt"]


def test_go_keeps_state_for_amend(mock_git, mock_pi_available, monkeypatch):
    from pi_review_precommit.state import load_state, record_rejection

    _capture_run_review(monkeypatch, {"decision": "go"})
    record_rejection("pi-review-old", "oldtree", None)
    monkeypatch.setattr(hook, "get_head_tree", lambda: "head123")

    assert hook.main([]) == 0  # go keeps state (lazy clear)
    state = load_state()
    assert state is not None
    assert state["approved_tree"] == TREE
    assert state["base_tree"] == "head123"
    assert state["round"] == 0


def test_go_prints_summary_and_suggestions_and_logs(
    mock_git, mock_pi_available, monkeypatch, capsys
):
    from pi_review_precommit.state import REVIEWS_DIR

    _capture_run_review(
        monkeypatch,
        {
            "decision": "go",
            "summary": "LGTM with minor notes",
            "suggestions": ["add a test", "rename x"],
        },
    )

    assert hook.main([]) == 0
    err = capsys.readouterr().err
    assert "Changes approved" in err
    assert "LGTM with minor notes" in err
    assert "add a test" in err

    logs = list(REVIEWS_DIR.glob("*.json"))
    assert len(logs) == 1
    payload = json.loads(logs[0].read_text())
    assert payload["summary"] == "LGTM with minor notes"
    assert payload["suggestions"] == ["add a test", "rename x"]
    assert payload["archive_path"] is None


# --- First round: no-go ----------------------------------------------------


def test_first_round_nogo_blocks_and_records(
    mock_git, mock_pi_available, monkeypatch, capsys
):
    decision_args = {
        "decision": "no-go",
        "summary": "Needs fixes",
        "issues": [
            {"severity": "major", "description": "bug", "file": "a.py", "line": 3},
        ],
    }
    _capture_run_review(monkeypatch, decision_args)

    assert hook.main([]) == 1
    err = capsys.readouterr().err
    assert "Changes rejected" in err
    assert "Needs fixes" in err
    assert "bug" in err

    from pi_review_precommit.state import (
        is_tree_rejected,
        load_state,
    )

    assert is_tree_rejected(TREE)
    state = load_state()
    assert state is not None
    assert state["session_id"].startswith("pi-review-")
    assert state["round"] == 1


# --- Same-tree auto-reject -------------------------------------------------


def test_same_tree_auto_rejects_without_pi(
    mock_git, mock_pi_available, monkeypatch, capsys
):
    from pi_review_precommit.state import record_rejection

    record_rejection("pi-review-abc", TREE, [{"description": "x"}])

    called = {"n": 0}

    def fake_run_review(**kwargs):
        called["n"] += 1
        return {"decision": "go"}

    monkeypatch.setattr(hook, "run_review", fake_run_review)

    assert hook.main([]) == 1
    assert called["n"] == 0  # pi never invoked
    assert "identical to a previously rejected review" in capsys.readouterr().err


# --- Follow-up round -------------------------------------------------------


def test_followup_round_resumes_session_with_previous_issues(
    mock_git, mock_pi_available, monkeypatch
):
    from pi_review_precommit.state import record_rejection

    # Previous rejection on a *different* tree (user amended)
    record_rejection(
        "pi-review-abc",
        "oldtree",
        [{"severity": "critical", "description": "old bug"}],
    )

    captured = _capture_run_review(monkeypatch, {"decision": "go"})

    assert hook.main([]) == 0
    assert captured["session_id"] == "pi-review-abc"  # session resumed
    prompt = captured["user_prompt"]
    assert "review round 2" in prompt
    assert "old bug" in prompt
    assert "proportionally lenient" in prompt
    # Follow-up go keeps state (lazy clear) for a potential amend
    from pi_review_precommit.state import load_state

    state = load_state()
    assert state is not None
    assert state["approved_tree"] == TREE


# --- Failure modes ---------------------------------------------------------


def test_pi_not_found_fails_open(mock_git, monkeypatch):
    monkeypatch.setattr(hook, "find_pi", lambda binary: None)
    assert hook.main([]) == 0


def test_pi_invocation_error_fails_closed(
    mock_git, mock_pi_available, monkeypatch, capsys
):
    _capture_run_review(
        monkeypatch,
        subprocess.CalledProcessError(1, ["pi"], "out", "Model not found\n"),
    )
    assert hook.main([]) == 1
    err = capsys.readouterr().err
    assert "pi invocation failed" in err
    # pi's stderr is surfaced so the cause is diagnosable
    assert "Model not found" in err


def test_pi_invocation_error_without_stderr_fails_closed(
    mock_git, mock_pi_available, monkeypatch, capsys
):
    _capture_run_review(
        monkeypatch,
        subprocess.CalledProcessError(1, ["pi"], "out", ""),
    )
    assert hook.main([]) == 1
    assert "pi invocation failed" in capsys.readouterr().err


def test_no_decision_tool_call_fails_closed(
    mock_git, mock_pi_available, monkeypatch, capsys
):
    _capture_run_review(monkeypatch, None)
    assert hook.main([]) == 1
    assert "did not produce a decision" in capsys.readouterr().err


def test_unrecognized_decision_fails_closed(
    mock_git, mock_pi_available, monkeypatch, capsys
):
    _capture_run_review(monkeypatch, {"decision": "maybe"})
    assert hook.main([]) == 1
    assert "Unrecognized decision" in capsys.readouterr().err


# --- Edge cases ------------------------------------------------------------


def test_empty_diff_exits_zero_without_pi(monkeypatch, capsys):
    monkeypatch.setattr(hook, "find_pi", lambda binary: "/usr/bin/pi")
    monkeypatch.setattr(hook, "get_staged_tree_hash", lambda: TREE)
    monkeypatch.setattr(hook, "get_staged_diff", lambda: "  \n")
    monkeypatch.setattr(hook, "get_staged_files", lambda: [])

    called = {"n": 0}

    def fake_run_review(**kwargs):
        called["n"] += 1
        return {"decision": "go"}

    monkeypatch.setattr(hook, "run_review", fake_run_review)

    # Even if the tree was previously rejected, empty diff => exit 0, no pi
    from pi_review_precommit.state import record_rejection

    record_rejection("pi-review-abc", TREE, [{"description": "x"}])

    assert hook.main([]) == 0
    assert called["n"] == 0
    assert capsys.readouterr().out == ""


def test_git_failure_fails_closed(mock_pi_available, monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise RuntimeError("git write-tree failed: not a git repo")

    monkeypatch.setattr(hook, "get_staged_tree_hash", boom)
    assert hook.main([]) == 1
    assert "not a git repo" in capsys.readouterr().err


def test_go_with_archive_flag_keeps_jsonl_archive(
    mock_git, mock_pi_available, monkeypatch
):
    from pathlib import Path

    from pi_review_precommit.state import REVIEWS_DIR, load_state, record_rejection

    _capture_run_review(monkeypatch, {"decision": "go"})
    record_rejection("pi-review-old", "oldtree", None)
    monkeypatch.setattr(hook, "get_head_tree", lambda: "head123")
    # Simulate the pi session file (real layout: <ts>_<session-id>.jsonl)
    sdir = Path(".git") / "pi-reviewer" / "sessions"
    sdir.mkdir(parents=True)
    (sdir / "2026-08-15T00-00-00-000Z_pi-review-old.jsonl").write_text(
        '{"type": "session", "id": "pi-review-old"}\n'
    )

    assert hook.main(["--archive-sessions"]) == 0
    # state kept (lazy clear) for a potential amend
    state = load_state()
    assert state is not None
    assert state["approved_tree"] == TREE
    archives = list(
        (Path(".git") / "pi-reviewer" / "archive").glob(
            "session-pi-review-old.jsonl.gz"
        )
    )
    assert len(archives) == 1
    # review log records the archive path for the post-commit hook
    logs = list(REVIEWS_DIR.glob("*.json"))
    assert len(logs) == 1
    payload = json.loads(logs[0].read_text())
    assert payload["archive_path"] == str(archives[0])


def test_go_without_archive_flag_keeps_session(
    mock_git, mock_pi_available, monkeypatch
):
    from pathlib import Path

    from pi_review_precommit.state import REVIEWS_DIR, record_rejection

    _capture_run_review(monkeypatch, {"decision": "go"})
    record_rejection("pi-review-old", "oldtree", None)
    monkeypatch.setattr(hook, "get_head_tree", lambda: "head123")
    sdir = Path(".git") / "pi-reviewer" / "sessions"
    sdir.mkdir(parents=True)
    (sdir / "2026-08-15T00-00-00-000Z_pi-review-old.jsonl").write_text("{}")

    assert hook.main([]) == 0
    # session dir kept (lazy clear) for a potential amend
    assert sdir.exists()
    assert not (Path(".git") / "pi-reviewer" / "archive").exists()
    # review log has no archive path when archiving is off
    logs = list(REVIEWS_DIR.glob("*.json"))
    assert len(logs) == 1
    payload = json.loads(logs[0].read_text())
    assert payload["archive_path"] is None


def test_guidelines_included_in_prompt_when_file_present(
    mock_git, mock_pi_available, monkeypatch, tmp_path
):
    (tmp_path / "REVIEW_GUIDELINES.md").write_text("No-go if secrets are committed.")
    captured = _capture_run_review(monkeypatch, {"decision": "go"})

    assert hook.main([]) == 0
    assert "No-go if secrets are committed." in captured["user_prompt"]
    assert "Project review guidelines" in captured["user_prompt"]


def test_guidelines_skipped_with_flag(
    mock_git, mock_pi_available, monkeypatch, tmp_path
):
    (tmp_path / "REVIEW_GUIDELINES.md").write_text("No-go if secrets are committed.")
    captured = _capture_run_review(monkeypatch, {"decision": "go"})

    assert hook.main(["--no-review-guidelines"]) == 0
    assert "No-go if secrets are committed." not in captured["user_prompt"]


# --- Amend flow ------------------------------------------------------------


def test_amend_after_go_resumes_session_with_full_change_set(
    mock_git, mock_pi_available, monkeypatch
):
    from pi_review_precommit.state import load_state, record_go_state

    record_go_state("pi-review-abc", "approved123", "base456")
    monkeypatch.setattr(hook, "detect_amend", lambda: "AMEND")
    monkeypatch.setattr(hook, "get_parent_tree", lambda: "parent123")
    captured_full_diff: dict = {}

    def fake_full_diff(base, staged):
        captured_full_diff["base"] = base
        captured_full_diff["staged"] = staged
        return "FULL DIFF"

    monkeypatch.setattr(hook, "get_full_change_set_diff", fake_full_diff)
    captured = _capture_run_review(monkeypatch, {"decision": "go"})

    assert hook.main([]) == 0
    assert captured["session_id"] == "pi-review-abc"  # session resumed
    assert captured_full_diff == {"base": "base456", "staged": TREE}
    prompt = captured["user_prompt"]
    assert "git commit --amend" in prompt
    assert "FULL DIFF" in prompt
    assert "previously approved" in prompt
    # go on amend keeps state (lazy clear) with the new approved tree
    state = load_state()
    assert state is not None
    assert state["approved_tree"] == TREE
    assert state["base_tree"] == "parent123"
    assert state["round"] == 0


def test_amend_after_go_nogo_keeps_approved_and_base(
    mock_git, mock_pi_available, monkeypatch
):
    from pi_review_precommit.state import load_state, record_go_state

    record_go_state("pi-review-abc", "approved123", "base456")
    monkeypatch.setattr(hook, "detect_amend", lambda: "AMEND")
    monkeypatch.setattr(hook, "get_full_change_set_diff", lambda b, s: "FULL DIFF")
    _capture_run_review(
        monkeypatch,
        {"decision": "no-go", "issues": [{"severity": "major", "description": "x"}]},
    )

    assert hook.main([]) == 1
    state = load_state()
    assert state is not None
    assert state["approved_tree"] == "approved123"  # unchanged
    assert state["base_tree"] == "base456"  # unchanged
    assert state["round"] == 1


def test_amend_after_nogo_resumes_with_leniency(
    mock_git, mock_pi_available, monkeypatch
):
    from pi_review_precommit.state import record_go_state, record_rejection

    record_go_state("pi-review-abc", "approved123", "base456")
    record_rejection(
        "pi-review-abc",
        "rejected1",
        [{"severity": "major", "description": "old bug"}],
    )
    monkeypatch.setattr(hook, "detect_amend", lambda: "AMEND")
    monkeypatch.setattr(hook, "get_parent_tree", lambda: "parent123")
    monkeypatch.setattr(hook, "get_full_change_set_diff", lambda b, s: "FULL DIFF")
    captured = _capture_run_review(monkeypatch, {"decision": "go"})

    assert hook.main([]) == 0
    assert captured["session_id"] == "pi-review-abc"
    prompt = captured["user_prompt"]
    assert "previously rejected" in prompt
    assert "proportionally lenient" in prompt
    assert "FULL DIFF" in prompt


def test_new_commit_on_top_clears_kept_state_and_starts_fresh(
    mock_git, mock_pi_available, monkeypatch
):
    from pi_review_precommit.state import load_state, record_go_state

    record_go_state("pi-review-abc", "approved123", "base456")
    monkeypatch.setattr(hook, "detect_amend", lambda: "NOT_AMEND")
    monkeypatch.setattr(hook, "get_head_tree", lambda: "head123")
    captured = _capture_run_review(monkeypatch, {"decision": "go"})

    assert hook.main([]) == 0
    # fresh session, not resumed
    assert captured["session_id"] != "pi-review-abc"
    assert "Staged files" in captured["user_prompt"]  # first-round prompt
    assert "git commit --amend" not in captured["user_prompt"]
    # go records the new approved state
    state = load_state()
    assert state is not None
    assert state["approved_tree"] == TREE
    assert state["base_tree"] == "head123"


def test_unknown_amend_falls_back_to_fresh(
    mock_git, mock_pi_available, monkeypatch
):
    from pi_review_precommit.state import record_go_state

    record_go_state("pi-review-abc", "approved123", "base456")
    monkeypatch.setattr(hook, "detect_amend", lambda: "UNKNOWN")
    monkeypatch.setattr(hook, "get_head_tree", lambda: "head123")
    captured = _capture_run_review(monkeypatch, {"decision": "go"})

    assert hook.main([]) == 0
    assert captured["session_id"] != "pi-review-abc"
    assert "Staged files" in captured["user_prompt"]
    assert "git commit --amend" not in captured["user_prompt"]


def test_detached_head_skips_amend_behavior(
    mock_git, mock_pi_available, monkeypatch
):
    from pi_review_precommit.state import record_go_state

    record_go_state("pi-review-abc", "approved123", "base456")
    # detect_amend returns NOT_AMEND for detached HEAD
    monkeypatch.setattr(hook, "detect_amend", lambda: "NOT_AMEND")
    monkeypatch.setattr(hook, "get_head_tree", lambda: "head123")
    captured = _capture_run_review(monkeypatch, {"decision": "go"})

    assert hook.main([]) == 0
    assert captured["session_id"] != "pi-review-abc"
    assert "git commit --amend" not in captured["user_prompt"]


def test_amend_without_approved_tree_falls_back_to_fresh(
    mock_git, mock_pi_available, monkeypatch
):
    monkeypatch.setattr(hook, "detect_amend", lambda: "AMEND")
    monkeypatch.setattr(hook, "get_head_tree", lambda: "head123")
    captured = _capture_run_review(monkeypatch, {"decision": "go"})

    assert hook.main([]) == 0
    assert "Staged files" in captured["user_prompt"]
    assert "git commit --amend" not in captured["user_prompt"]


def test_amend_uses_empty_tree_base_when_base_missing(
    mock_git, mock_pi_available, monkeypatch
):
    from pi_review_precommit.state import save_state

    # Old-style state: approved_tree set but no base_tree
    save_state(
        {
            "session_id": "pi-review-abc",
            "rejected_trees": [],
            "round": 0,
            "approved_tree": "approved123",
        }
    )
    monkeypatch.setattr(hook, "detect_amend", lambda: "AMEND")
    monkeypatch.setattr(hook, "get_parent_tree", lambda: "parent123")
    captured_full_diff: dict = {}

    def fake_full_diff(base, staged):
        captured_full_diff["base"] = base
        captured_full_diff["staged"] = staged
        return "FULL DIFF"

    monkeypatch.setattr(hook, "get_full_change_set_diff", fake_full_diff)
    _capture_run_review(monkeypatch, {"decision": "go"})

    assert hook.main([]) == 0
    from pi_review_precommit.prompts import EMPTY_TREE_HASH

    assert captured_full_diff["base"] == EMPTY_TREE_HASH
    assert captured_full_diff["staged"] == TREE


def test_fresh_path_after_go_archives_kept_session(
    mock_git, mock_pi_available, monkeypatch
):
    from pathlib import Path

    from pi_review_precommit.state import load_state, record_go_state

    record_go_state("pi-review-abc", "approved123", "base456")
    monkeypatch.setattr(hook, "detect_amend", lambda: "NOT_AMEND")
    monkeypatch.setattr(hook, "get_head_tree", lambda: "head123")
    sdir = Path(".git") / "pi-reviewer" / "sessions"
    sdir.mkdir(parents=True)
    (sdir / "2026-08-15T00-00-00-000Z_pi-review-abc.jsonl").write_text("{}")

    captured: dict = {}

    def fake_run_review(**kwargs):
        captured.update(kwargs)
        # pi would create the session file; simulate it for the new session
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / f"2026-08-15T00-00-00-000Z_{kwargs['session_id']}.jsonl").write_text(
            "{}"
        )
        return {"decision": "go"}

    monkeypatch.setattr(hook, "run_review", fake_run_review)

    assert hook.main(["--archive-sessions"]) == 0
    # kept session archived on the fresh-path clear
    archives = list(
        (Path(".git") / "pi-reviewer" / "archive").glob(
            "session-pi-review-abc.jsonl.gz"
        )
    )
    assert len(archives) == 1
    # old session file cleared; session dir kept (lazy clear) for the new go
    assert sdir.exists()
    assert not list(sdir.glob("*pi-review-abc*.jsonl"))
    state = load_state()
    assert state is not None
    assert state["approved_tree"] == TREE


def test_fresh_path_clear_tolerates_missing_session_file(
    mock_git, mock_pi_available, monkeypatch
):
    from pathlib import Path

    from pi_review_precommit.state import record_go_state

    record_go_state("pi-review-abc", "approved123", "base456")
    monkeypatch.setattr(hook, "detect_amend", lambda: "NOT_AMEND")
    monkeypatch.setattr(hook, "get_head_tree", lambda: "head123")
    # no session file under sessions/ (stale state) — must not crash
    sdir = Path(".git") / "pi-reviewer" / "sessions"
    sdir.mkdir(parents=True)

    def fake_run_review(**kwargs):
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / f"2026-08-15T00-00-00-000Z_{kwargs['session_id']}.jsonl").write_text(
            "{}"
        )
        return {"decision": "go"}

    monkeypatch.setattr(hook, "run_review", fake_run_review)

    assert hook.main(["--archive-sessions"]) == 0

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

    assert hook.main() == 0

    # First-round prompt used
    assert "Staged files" in captured["user_prompt"]
    assert "review round" not in captured["user_prompt"]


def test_go_clears_state(mock_git, mock_pi_available, monkeypatch):
    from pi_review_precommit.state import load_state, record_rejection

    _capture_run_review(monkeypatch, {"decision": "go"})
    record_rejection("pi-review-old", "oldtree", None)

    assert hook.main() == 0  # go clears state
    assert load_state() is None


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

    assert hook.main() == 0
    err = capsys.readouterr().err
    assert "Changes approved" in err
    assert "LGTM with minor notes" in err
    assert "add a test" in err

    logs = list(REVIEWS_DIR.glob("*.json"))
    assert len(logs) == 1
    payload = json.loads(logs[0].read_text())
    assert payload["summary"] == "LGTM with minor notes"
    assert payload["suggestions"] == ["add a test", "rename x"]


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

    assert hook.main() == 1
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

    assert hook.main() == 1
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

    assert hook.main() == 0
    assert captured["session_id"] == "pi-review-abc"  # session resumed
    prompt = captured["user_prompt"]
    assert "review round 2" in prompt
    assert "old bug" in prompt
    assert "proportionally lenient" in prompt
    # Follow-up clears state on go
    from pi_review_precommit.state import load_state

    assert load_state() is None


# --- Failure modes ---------------------------------------------------------


def test_pi_not_found_fails_open(mock_git, monkeypatch):
    monkeypatch.setattr(hook, "find_pi", lambda binary: None)
    assert hook.main() == 0


def test_pi_invocation_error_fails_closed(
    mock_git, mock_pi_available, monkeypatch, capsys
):
    _capture_run_review(
        monkeypatch,
        subprocess.CalledProcessError(1, ["pi"], "out", "boom"),
    )
    assert hook.main() == 1
    assert "pi invocation failed" in capsys.readouterr().err


def test_no_decision_tool_call_fails_closed(
    mock_git, mock_pi_available, monkeypatch, capsys
):
    _capture_run_review(monkeypatch, None)
    assert hook.main() == 1
    assert "did not produce a decision" in capsys.readouterr().err


def test_unrecognized_decision_fails_closed(
    mock_git, mock_pi_available, monkeypatch, capsys
):
    _capture_run_review(monkeypatch, {"decision": "maybe"})
    assert hook.main() == 1
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

    assert hook.main() == 0
    assert called["n"] == 0
    assert capsys.readouterr().out == ""


def test_git_failure_fails_closed(mock_pi_available, monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise RuntimeError("git write-tree failed: not a git repo")

    monkeypatch.setattr(hook, "get_staged_tree_hash", boom)
    assert hook.main() == 1
    assert "not a git repo" in capsys.readouterr().err

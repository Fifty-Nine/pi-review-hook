"""Tests for prompts.py: prompt construction, incl. current-date injection."""

from datetime import datetime

from pi_review_precommit.prompts import (
    build_amend_prompt,
    build_first_round_prompt,
    build_followup_prompt,
    get_review_guidelines,
    get_system_prompt,
)

DIFF = "diff --git a/a.py b/a.py\n+print('hi')\n"
FILES = ["a.py"]


def test_first_round_prompt_contains_today() -> None:
    prompt = build_first_round_prompt(DIFF, FILES)
    today = datetime.now().strftime("%Y-%m-%d")
    assert f"Today's date is {today}." in prompt


def test_followup_prompt_contains_today() -> None:
    prompt = build_followup_prompt(DIFF, FILES, round_number=1, previous_issues=None)
    today = datetime.now().strftime("%Y-%m-%d")
    assert f"Today's date is {today}." in prompt


def test_followup_prompt_keeps_round_and_issues() -> None:
    prompt = build_followup_prompt(
        DIFF,
        FILES,
        round_number=2,
        previous_issues=[{"severity": "major", "description": "old bug"}],
    )
    assert "review round 3" in prompt
    assert "old bug" in prompt
    assert "proportionally lenient" in prompt


def test_system_prompt_default_and_override() -> None:
    assert "submit_review_decision" in get_system_prompt("")
    custom = "custom reviewer"
    assert get_system_prompt(custom) == custom


def test_system_prompt_mentions_guidelines_override() -> None:
    assert "REVIEW_GUIDELINES.md" in get_system_prompt("")


# --- REVIEW_GUIDELINES.md ---------------------------------------------------


def test_get_review_guidelines_none_when_absent(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert get_review_guidelines() is None


def test_get_review_guidelines_reads_file(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "REVIEW_GUIDELINES.md").write_text("No-go if secrets are committed.\n")
    assert get_review_guidelines() == "No-go if secrets are committed."


def test_get_review_guidelines_empty_file(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "REVIEW_GUIDELINES.md").write_text("   \n")
    assert get_review_guidelines() is None


def test_first_round_prompt_includes_guidelines() -> None:
    prompt = build_first_round_prompt(DIFF, FILES, review_guidelines="No-go if X.")
    assert "Project review guidelines" in prompt
    assert "No-go if X." in prompt


def test_first_round_prompt_without_guidelines() -> None:
    prompt = build_first_round_prompt(DIFF, FILES)
    assert "Project review guidelines" not in prompt


def test_followup_prompt_includes_guidelines() -> None:
    prompt = build_followup_prompt(
        DIFF,
        FILES,
        round_number=1,
        previous_issues=None,
        review_guidelines="No-go if X.",
    )
    assert "No-go if X." in prompt


# --- Amend prompt ----------------------------------------------------------


def test_amend_prompt_round_zero_verifies_suggestions() -> None:
    prompt = build_amend_prompt(DIFF, FILES, round_number=0)
    assert "git commit --amend" in prompt
    assert "previously approved" in prompt
    assert "Verify your suggestions" in prompt
    assert "FULL amended change set" in prompt
    assert "proportionally lenient" not in prompt


def test_amend_prompt_round_gt_zero_verifies_issues_with_leniency() -> None:
    prompt = build_amend_prompt(DIFF, FILES, round_number=2)
    assert "previously rejected" in prompt
    assert "proportionally lenient" in prompt
    assert "FULL amended change set" in prompt


def test_amend_prompt_includes_guidelines() -> None:
    prompt = build_amend_prompt(DIFF, FILES, 0, review_guidelines="No-go if X.")
    assert "No-go if X." in prompt


def test_amend_prompt_contains_today() -> None:
    prompt = build_amend_prompt(DIFF, FILES, round_number=0)
    today = datetime.now().strftime("%Y-%m-%d")
    assert f"Today's date is {today}." in prompt


def test_get_full_change_set_diff_uses_base_and_staged(monkeypatch) -> None:
    from pi_review_precommit import prompts

    captured: dict = {}

    def fake_run_git(args):
        captured["args"] = args
        return "diff --git a/a.py b/a.py\n"

    monkeypatch.setattr(prompts, "_run_git", fake_run_git)
    out = prompts.get_full_change_set_diff("base123", "staged456")
    assert out == "diff --git a/a.py b/a.py\n"
    assert captured["args"] == ["diff", "base123", "staged456"]


def test_get_head_tree_returns_empty_tree_on_failure(monkeypatch) -> None:
    from pi_review_precommit import prompts

    def boom(args):
        raise RuntimeError("git rev-parse failed")

    monkeypatch.setattr(prompts, "_run_git", boom)
    assert prompts.get_head_tree() == prompts.EMPTY_TREE_HASH
    assert prompts.get_parent_tree() == prompts.EMPTY_TREE_HASH


def test_get_head_tree_returns_tree(monkeypatch) -> None:
    from pi_review_precommit import prompts

    monkeypatch.setattr(prompts, "_run_git", lambda args: "abc123\n")
    assert prompts.get_head_tree() == "abc123"
    assert prompts.get_parent_tree() == "abc123"

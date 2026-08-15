"""Tests for prompts.py: prompt construction, incl. current-date injection."""

from datetime import datetime

from pi_review_precommit.prompts import (
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

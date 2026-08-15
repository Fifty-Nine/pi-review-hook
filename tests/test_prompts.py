"""Tests for prompts.py: prompt construction, incl. current-date injection."""

from datetime import datetime

from pi_review_precommit.prompts import (
    build_first_round_prompt,
    build_followup_prompt,
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

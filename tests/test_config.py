"""Tests for config.py: CLI args > env vars > defaults precedence."""

import pytest

from pi_review_precommit.config import (
    DEFAULT_MODEL,
    DEFAULT_PI_BINARY,
    DEFAULT_SESSION_DIR,
    Config,
    parse_args,
)


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("PI_REVIEW_MODEL", "PI_REVIEW_PI_BINARY", "PI_REVIEW_SESSION_DIR"):
        monkeypatch.delenv(key, raising=False)
    cfg = parse_args([])
    assert cfg == Config(
        model=DEFAULT_MODEL,
        pi_binary=DEFAULT_PI_BINARY,
        system_prompt="",
        session_dir=DEFAULT_SESSION_DIR,
    )


def test_env_overrides_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PI_REVIEW_MODEL", "env-model")
    monkeypatch.setenv("PI_REVIEW_PI_BINARY", "pi-jailed")
    monkeypatch.setenv("PI_REVIEW_SESSION_DIR", ".git/custom")
    monkeypatch.setenv("PI_REVIEW_SYSTEM_PROMPT", "env prompt")
    cfg = parse_args([])
    assert cfg.model == "env-model"
    assert cfg.pi_binary == "pi-jailed"
    assert cfg.session_dir == ".git/custom"
    assert cfg.system_prompt == "env prompt"


def test_cli_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PI_REVIEW_MODEL", "env-model")
    monkeypatch.setenv("PI_REVIEW_PI_BINARY", "pi-jailed")
    monkeypatch.setenv("PI_REVIEW_SESSION_DIR", ".git/custom")
    cfg = parse_args(
        [
            "--model",
            "cli-model",
            "--pi-binary",
            "pi-other",
            "--session-dir",
            ".git/cli",
        ]
    )
    assert cfg.model == "cli-model"
    assert cfg.pi_binary == "pi-other"
    assert cfg.session_dir == ".git/cli"


def test_archive_sessions_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PI_REVIEW_ARCHIVE_SESSIONS", raising=False)
    assert parse_args([]).archive_sessions is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
def test_archive_sessions_env_on(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("PI_REVIEW_ARCHIVE_SESSIONS", value)
    assert parse_args([]).archive_sessions is True


@pytest.mark.parametrize("value", ["0", "false", "off", ""])
def test_archive_sessions_env_off(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("PI_REVIEW_ARCHIVE_SESSIONS", value)
    assert parse_args([]).archive_sessions is False


def test_archive_sessions_cli_flag_beats_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PI_REVIEW_ARCHIVE_SESSIONS", "0")
    assert parse_args(["--archive-sessions"]).archive_sessions is True


def test_review_guidelines_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PI_REVIEW_NO_REVIEW_GUIDELINES", raising=False)
    assert parse_args([]).review_guidelines is True


def test_review_guidelines_env_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PI_REVIEW_NO_REVIEW_GUIDELINES", "1")
    assert parse_args([]).review_guidelines is False


def test_review_guidelines_cli_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PI_REVIEW_NO_REVIEW_GUIDELINES", raising=False)
    assert parse_args(["--no-review-guidelines"]).review_guidelines is False


def test_system_prompt_cli_beats_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PI_REVIEW_SYSTEM_PROMPT", "env prompt")
    cfg = parse_args(["--system-prompt", "cli prompt"])
    assert cfg.system_prompt == "cli prompt"


def test_system_prompt_file_beats_all(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("file prompt")
    monkeypatch.setenv("PI_REVIEW_SYSTEM_PROMPT", "env prompt")
    cfg = parse_args(
        ["--system-prompt", "cli prompt", "--system-prompt-file", str(prompt_file)]
    )
    assert cfg.system_prompt == "file prompt"
    assert cfg.system_prompt_file == str(prompt_file)


def test_system_prompt_file_missing(tmp_path) -> None:
    with pytest.raises(SystemExit) as exc:
        parse_args(["--system-prompt-file", str(tmp_path / "nope.txt")])
    assert exc.value.code == 2  # argparse error

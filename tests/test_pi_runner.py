"""Tests for pi_runner.py: pi invocation + NDJSON decision extraction."""

import json
import subprocess

import pytest

from pi_review_precommit import pi_runner

BASE_ARGS = dict(
    pi_binary="pi",
    model="glm-5.2",
    session_id="pi-review-abc",
    session_dir=".git/pi-reviewer/sessions",
    system_prompt="system",
    user_prompt="user",
)


def _ndjson(events: list[dict]) -> str:
    return "\n".join(json.dumps(e) for e in events)


def _tool_execution_start(decision: str) -> dict:
    return {
        "type": "tool_execution_start",
        "toolCallId": "call-1",
        "toolName": "submit_review_decision",
        "args": {"decision": decision},
    }


def _fake_run(stream: str, returncode: int = 0):
    class FakeCompleted:
        def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    return FakeCompleted(returncode, stream, "")


def test_extracts_decision_from_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = _ndjson(
        [
            {"type": "session", "id": "x"},
            {"type": "agent_start"},
            {"type": "turn_start"},
            _tool_execution_start("no-go"),
            {"type": "tool_execution_end", "toolName": "submit_review_decision"},
            {"type": "agent_end", "messages": []},
        ]
    )
    monkeypatch.setattr(pi_runner.subprocess, "run", lambda *a, **k: _fake_run(stream))
    result = pi_runner.run_review(**BASE_ARGS)
    assert result == {"decision": "no-go"}


def test_extracts_full_args_with_issues(monkeypatch: pytest.MonkeyPatch) -> None:
    args = {
        "decision": "no-go",
        "issues": [{"severity": "major", "description": "bad", "file": "a.py"}],
        "summary": "Needs work",
    }
    stream = _ndjson(
        [
            {"type": "session", "id": "x"},
            {
                "type": "tool_execution_start",
                "toolName": "submit_review_decision",
                "args": args,
            },
        ]
    )
    monkeypatch.setattr(pi_runner.subprocess, "run", lambda *a, **k: _fake_run(stream))
    assert pi_runner.run_review(**BASE_ARGS) == args


def test_ignores_other_tool_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = _ndjson(
        [
            {"type": "tool_execution_start", "toolName": "read", "args": {"path": "x"}},
            _tool_execution_start("go"),
        ]
    )
    monkeypatch.setattr(pi_runner.subprocess, "run", lambda *a, **k: _fake_run(stream))
    assert pi_runner.run_review(**BASE_ARGS) == {"decision": "go"}


def test_no_decision_tool_call_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = _ndjson(
        [
            {"type": "session", "id": "x"},
            {"type": "tool_execution_start", "toolName": "read", "args": {}},
        ]
    )
    monkeypatch.setattr(pi_runner.subprocess, "run", lambda *a, **k: _fake_run(stream))
    assert pi_runner.run_review(**BASE_ARGS) is None


def test_empty_output_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pi_runner.subprocess, "run", lambda *a, **k: _fake_run(""))
    assert pi_runner.run_review(**BASE_ARGS) is None


def test_noise_lines_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = (
        "[pi-ollama] Extension loaded\n"
        + _ndjson(
            [
                {"type": "session", "id": "x"},
                _tool_execution_start("go"),
            ]
        )
        + "\n"
    )
    monkeypatch.setattr(pi_runner.subprocess, "run", lambda *a, **k: _fake_run(stream))
    assert pi_runner.run_review(**BASE_ARGS) == {"decision": "go"}


def test_nonzero_exit_raises_called_process_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pi_runner.subprocess, "run", lambda *a, **k: _fake_run("", returncode=1)
    )
    with pytest.raises(subprocess.CalledProcessError):
        pi_runner.run_review(**BASE_ARGS)


def test_cmd_includes_expected_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _fake_run("")

    monkeypatch.setattr(pi_runner.subprocess, "run", fake_run)
    pi_runner.run_review(**BASE_ARGS)

    cmd = captured["cmd"]
    assert cmd[0] == "pi"
    assert "--mode" in cmd and cmd[cmd.index("--mode") + 1] == "json"
    assert cmd[cmd.index("--session-id") + 1] == "pi-review-abc"
    assert cmd[cmd.index("--model") + 1] == "glm-5.2"
    assert cmd[cmd.index("--session-dir") + 1] == ".git/pi-reviewer/sessions"
    assert cmd[cmd.index("--tools") + 1] == ("read,grep,find,ls,submit_review_decision")
    assert "--no-extensions" not in cmd
    assert "--print" in cmd
    assert cmd[-1] == "user"
    # Extension path must exist and point at our bundled extension.ts
    ext_idx = cmd.index("--extension")
    assert cmd[ext_idx + 1].endswith("extension.ts")


def test_find_pi() -> None:
    # "sh" is always on PATH on POSIX
    assert pi_runner.find_pi("sh") is not None
    assert pi_runner.find_pi("definitely-not-a-real-binary-xyz") is None


def test_get_extension_path_exists() -> None:
    path = pi_runner.get_extension_path()
    assert path.endswith("extension.ts")
    assert __import__("pathlib").Path(path).exists()

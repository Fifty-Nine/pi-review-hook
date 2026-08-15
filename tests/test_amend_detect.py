"""Tests for amend_detect.py: process-hierarchy amend detection.

The AMEND path cannot fire under pytest (the test runner is not a child of
a `git commit --amend` process), so the /proc ancestor walk is mocked.
"""

import subprocess

from pi_review_precommit import amend_detect


def test_detect_amend_when_git_ancestor_has_amend(monkeypatch) -> None:
    monkeypatch.setattr(amend_detect, "_is_detached_head", lambda: False)
    monkeypatch.setattr(
        amend_detect,
        "_iter_ancestors",
        lambda: [
            (1234, ["/usr/bin/git", "commit", "--amend", "-m", "x"]),
            (100, ["bash", "-c", "git commit --amend"]),
        ],
    )
    assert amend_detect.detect_amend() == amend_detect.AMEND


def test_detect_amend_not_amend_when_git_ancestor_without_amend(
    monkeypatch,
) -> None:
    monkeypatch.setattr(amend_detect, "_is_detached_head", lambda: False)
    monkeypatch.setattr(
        amend_detect,
        "_iter_ancestors",
        lambda: [
            (1234, ["/usr/bin/git", "commit", "-m", "x"]),
            (100, ["bash", "-c", "git commit"]),
        ],
    )
    assert amend_detect.detect_amend() == amend_detect.NOT_AMEND


def test_detect_amend_unknown_when_no_git_ancestor(monkeypatch) -> None:
    monkeypatch.setattr(amend_detect, "_is_detached_head", lambda: False)
    monkeypatch.setattr(
        amend_detect,
        "_iter_ancestors",
        lambda: [(1234, ["python", "pre-commit"]), (100, ["bash"])],
    )
    assert amend_detect.detect_amend() == amend_detect.UNKNOWN


def test_detect_amend_unknown_when_no_proc(monkeypatch) -> None:
    monkeypatch.setattr(amend_detect, "_is_detached_head", lambda: False)
    monkeypatch.setattr(amend_detect, "_iter_ancestors", lambda: [])
    assert amend_detect.detect_amend() == amend_detect.UNKNOWN


def test_detect_amend_detached_head_returns_not_amend(monkeypatch) -> None:
    monkeypatch.setattr(amend_detect, "_is_detached_head", lambda: True)
    # Even with a git --amend ancestor, detached HEAD skips amend behavior.
    monkeypatch.setattr(
        amend_detect,
        "_iter_ancestors",
        lambda: [(1234, ["/usr/bin/git", "commit", "--amend"])],
    )
    assert amend_detect.detect_amend() == amend_detect.NOT_AMEND


def test_detect_amend_nearest_git_ancestor_wins(monkeypatch) -> None:
    monkeypatch.setattr(amend_detect, "_is_detached_head", lambda: False)
    monkeypatch.setattr(
        amend_detect,
        "_iter_ancestors",
        lambda: [
            (1234, ["/usr/bin/git", "commit", "-m", "x"]),  # nearest: no amend
            (100, ["/usr/bin/git", "commit", "--amend"]),  # farther: amend
        ],
    )
    assert amend_detect.detect_amend() == amend_detect.NOT_AMEND


def test_is_detached_head_true_when_symbolic_ref_fails(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "fatal: not a symbolic ref")

    monkeypatch.setattr(amend_detect.subprocess, "run", fake_run)
    assert amend_detect._is_detached_head() is True


def test_is_detached_head_false_when_symbolic_ref_succeeds(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, "refs/heads/master\n", "")

    monkeypatch.setattr(amend_detect.subprocess, "run", fake_run)
    assert amend_detect._is_detached_head() is False


# --- /proc parsing ---------------------------------------------------------


def test_iter_ancestors_parses_proc(tmp_path, monkeypatch) -> None:
    proc = tmp_path / "proc"
    (proc / "100").mkdir(parents=True)
    (proc / "100" / "stat").write_text("100 (git) S 50 100 100 0 -1 4194304 0\n")
    (proc / "100" / "cmdline").write_bytes(b"/usr/bin/git\0commit\0--amend\0")
    monkeypatch.setattr(amend_detect, "PROC_DIR", proc)
    monkeypatch.setattr(amend_detect.os, "getpid", lambda: 100)

    assert amend_detect._iter_ancestors() == [
        (100, ["/usr/bin/git", "commit", "--amend"])
    ]


def test_iter_ancestors_parses_comm_with_parens(tmp_path, monkeypatch) -> None:
    proc = tmp_path / "proc"
    (proc / "100").mkdir(parents=True)
    (proc / "100" / "stat").write_text(
        "100 (git (wrapper)) S 50 100 100 0 -1 4194304 0\n"
    )
    (proc / "100" / "cmdline").write_bytes(b"git\0commit\0")
    monkeypatch.setattr(amend_detect, "PROC_DIR", proc)
    monkeypatch.setattr(amend_detect.os, "getpid", lambda: 100)

    assert amend_detect._iter_ancestors() == [(100, ["git", "commit"])]


def test_iter_ancestors_stops_at_missing_proc(tmp_path, monkeypatch) -> None:
    proc = tmp_path / "proc"
    (proc / "100").mkdir(parents=True)
    (proc / "100" / "stat").write_text("100 (git) S 50 100 100 0 -1 4194304 0\n")
    (proc / "100" / "cmdline").write_bytes(b"git\0commit\0")
    # /proc/50 does not exist -> walk stops after the first entry
    monkeypatch.setattr(amend_detect, "PROC_DIR", proc)
    monkeypatch.setattr(amend_detect.os, "getpid", lambda: 100)

    assert amend_detect._iter_ancestors() == [(100, ["git", "commit"])]


def test_iter_ancestors_empty_without_proc(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(amend_detect, "PROC_DIR", tmp_path / "nope")
    monkeypatch.setattr(amend_detect.os, "getpid", lambda: 100)
    assert amend_detect._iter_ancestors() == []

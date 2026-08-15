"""Amend detection for the pre-commit hook.

The pre-commit hook cannot see the ``--amend`` flag directly, but on Linux
it can infer it from the process hierarchy: ``git commit --amend`` runs the
hook as a descendant of the ``git`` process, so walking /proc ancestors
finds the ``git`` invocation and its argv.

Detection is process-primary with a fresh fallback: if we cannot determine
the answer (non-Linux, no /proc, no git ancestor), we return UNKNOWN and
the hook falls back to the non-amend path (current behavior). A
detached-HEAD guard runs first: during a rebase/cherry-pick (detached
HEAD) the amend behavior is skipped, scoping amend detection to real
branch commits.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

AMEND = "AMEND"
NOT_AMEND = "NOT_AMEND"
UNKNOWN = "UNKNOWN"

PROC_DIR = Path("/proc")


def _is_detached_head() -> bool:
    """True if HEAD is detached (rebase/cherry-pick in progress).

    ``git symbolic-ref -q HEAD`` exits non-zero when HEAD is detached.
    """
    proc = subprocess.run(
        ["git", "symbolic-ref", "-q", "HEAD"],
        capture_output=True,
        text=True,
    )
    return proc.returncode != 0


def _iter_ancestors() -> list[tuple[int, list[str]]]:
    """Walk the process hierarchy from the current process upward.

    Returns a list of ``(pid, argv)`` tuples, nearest ancestor first.
    Uses /proc on Linux; returns [] if /proc is unavailable. The walk
    stops at the first unreadable entry (process exited, permission
    denied, or the top of the tree).
    """
    if not PROC_DIR.is_dir():
        return []
    result: list[tuple[int, list[str]]] = []
    pid = os.getpid()
    seen: set[int] = set()
    while pid > 0 and pid not in seen:
        seen.add(pid)
        stat_path = PROC_DIR / str(pid) / "stat"
        cmdline_path = PROC_DIR / str(pid) / "cmdline"
        try:
            stat = stat_path.read_text()
            cmdline = cmdline_path.read_bytes().split(b"\0")
        except OSError:
            break
        # stat field 4 (1-indexed) is the PPid. The comm field (2) may
        # contain spaces and parens, so parse everything after the last
        # ')' and take the second whitespace-separated token.
        try:
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
        except (IndexError, ValueError):
            break
        argv = [a.decode(errors="replace") for a in cmdline if a]
        result.append((pid, argv))
        pid = ppid
    return result


def _is_git_argv(argv: list[str]) -> bool:
    """True if argv[0] is a git binary (basename 'git' or ends with '/git')."""
    if not argv:
        return False
    name = argv[0]
    return name == "git" or name.endswith("/git")


def detect_amend() -> str:
    """Detect whether the current commit is a ``git commit --amend``.

    Returns AMEND / NOT_AMEND / UNKNOWN:

    - AMEND: a ``git`` ancestor with ``--amend`` in its argv.
    - NOT_AMEND: a ``git`` ancestor without ``--amend``, or a detached
      HEAD (rebase/cherry-pick — amend behavior skipped).
    - UNKNOWN: no git ancestor found, or /proc unavailable (non-Linux).
    """
    if _is_detached_head():
        return NOT_AMEND
    for _pid, argv in _iter_ancestors():
        if _is_git_argv(argv):
            return AMEND if "--amend" in argv else NOT_AMEND
    return UNKNOWN

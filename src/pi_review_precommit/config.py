"""Configuration: CLI args > env vars > defaults.

See ADR Decision 9. The hook is configured through pre-commit's ``args``
mechanism (primary), with ``PI_REVIEW_*`` environment variables for
environment-specific overrides, and built-in defaults as the floor.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Config:
    model: str
    pi_binary: str
    system_prompt: str
    session_dir: str  # relative to repo root, e.g. ".git/pi-reviewer/sessions"
    system_prompt_file: str | None = None
    archive_sessions: bool = False


DEFAULT_MODEL = "glm-5.2"
DEFAULT_PI_BINARY = "pi"
DEFAULT_SESSION_DIR = ".git/pi-reviewer/sessions"


def _env_or_default(env_key: str, default: str) -> str:
    return os.environ.get(env_key, default)


def _env_flag(env_key: str) -> bool:
    """Parse a boolean environment variable (1/true/yes/on)."""
    return os.environ.get(env_key, "").strip().lower() in ("1", "true", "yes", "on")


def parse_args(argv: list[str] | None = None) -> Config:
    """Parse hook arguments with precedence CLI args > env vars > defaults."""
    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(
        prog="pi-review",
        description="Agentic AI code review pre-commit hook using pi.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help=f"Model pattern for pi (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--pi-binary",
        default=None,
        help=f"pi binary name or path (default: {DEFAULT_PI_BINARY})",
    )
    parser.add_argument(
        "--system-prompt",
        default=None,
        help="System prompt for the reviewer (overrides built-in default).",
    )
    parser.add_argument(
        "--system-prompt-file",
        default=None,
        help="Path to a file containing the system prompt.",
    )
    parser.add_argument(
        "--session-dir",
        default=None,
        help=f"Session directory (default: {DEFAULT_SESSION_DIR})",
    )
    parser.add_argument(
        "--archive-sessions",
        action="store_true",
        default=None,
        help="On approval, archive the pi session as .tar.gz instead of "
        "deleting it (env: PI_REVIEW_ARCHIVE_SESSIONS=1).",
    )

    args = parser.parse_args(argv)

    # Precedence: CLI args > env vars > defaults
    model = args.model or _env_or_default("PI_REVIEW_MODEL", DEFAULT_MODEL)
    pi_binary = args.pi_binary or _env_or_default(
        "PI_REVIEW_PI_BINARY", DEFAULT_PI_BINARY
    )
    session_dir = args.session_dir or _env_or_default(
        "PI_REVIEW_SESSION_DIR", DEFAULT_SESSION_DIR
    )

    # System prompt: --system-prompt-file > --system-prompt > env > built-in default
    system_prompt = None
    if args.system_prompt_file:
        try:
            system_prompt = Path(args.system_prompt_file).read_text()
        except OSError as e:
            parser.error(f"cannot read system prompt file: {e}")
    elif args.system_prompt:
        system_prompt = args.system_prompt
    elif env_sp := os.environ.get("PI_REVIEW_SYSTEM_PROMPT"):
        system_prompt = env_sp

    return Config(
        model=model,
        pi_binary=pi_binary,
        system_prompt=system_prompt or "",  # empty = use built-in default in prompts.py
        session_dir=session_dir,
        system_prompt_file=args.system_prompt_file,
        archive_sessions=bool(args.archive_sessions)
        or _env_flag("PI_REVIEW_ARCHIVE_SESSIONS"),
    )

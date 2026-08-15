"""Invoke pi and parse NDJSON output for the decision tool call.

pi is run in JSON event-stream mode (--mode json) with --print so each hook
invocation is a fresh process that processes the prompt and exits (ADR
Decision 1). The hook does not parse the assistant's prose — it scans the
stream for the ``tool_execution_start`` event of the custom
``submit_review_decision`` tool (ADR Decision 2).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from importlib.resources import files
from pathlib import Path


def find_pi(pi_binary: str) -> str | None:
    """Find the pi binary in PATH. Returns path or None."""
    return shutil.which(pi_binary)


def get_extension_path() -> str:
    """Get the path to the bundled extension.ts file.

    Uses importlib.resources when installed as a package (works in
    pre-commit's venv), with a fallback to the source tree layout.
    """
    try:
        resolved = files("pi_review_precommit") / "extension.ts"
        # files() returns a Traversable; in a wheel this is already a path.
        path = Path(str(resolved))
        if path.exists():
            return str(path)
    except (ModuleNotFoundError, FileNotFoundError, TypeError):
        pass

    # Fallback: source checkout layout
    fallback = Path(__file__).parent / "extension.ts"
    if fallback.exists():
        return str(fallback)

    raise FileNotFoundError(
        "pi-review: bundled extension.ts not found; "
        "the package may be installed incorrectly."
    )


def stage_extension(extension_path: str, session_dir: str) -> str:
    """Copy the bundled extension into the mounted working directory.

    pi-jailed runs pi inside a jail.nix bubblewrap sandbox that mounts
    only the current working directory (mount-cwd) plus pi's own runtime
    closure. A bundled extension path (nix store, venv site-packages,
    source checkout) is therefore invisible to pi inside the jail. Staging
    a copy under the session directory — which lives inside the repo, i.e.
    the mounted cwd — keeps the extension reachable regardless of how the
    hook was installed.
    """
    session_path = Path(session_dir)
    session_path.mkdir(parents=True, exist_ok=True)
    staged = session_path.parent / "extension.ts"
    if staged.exists():
        # Overwrite any stale copy. The bundled file is read-only in the
        # nix store, and a previous copy may have inherited that mode, so
        # unlink (needs only directory write permission) rather than
        # open-for-write.
        staged.unlink()
    shutil.copyfile(extension_path, staged)
    return str(staged)


def run_review(
    pi_binary: str,
    model: str,
    session_id: str,
    session_dir: str,
    system_prompt: str,
    user_prompt: str,
) -> dict | None:
    """
    Invoke pi in JSON mode and parse the NDJSON stream for the
    submit_review_decision tool call.

    Returns the tool call args dict if found, or None if pi ran
    but didn't call the decision tool.

    Raises subprocess.CalledProcessError if pi itself fails (non-zero
    exit, crash, etc.).
    """
    extension_path = stage_extension(get_extension_path(), session_dir)

    cmd = [
        pi_binary,
        "--mode",
        "json",
        "--session-id",
        session_id,
        "--model",
        model,
        "--session-dir",
        session_dir,
        "--extension",
        extension_path,
        "--system-prompt",
        system_prompt,
        # Allowlist: read-only exploration tools + our decision tool.
        # This excludes bash, edit, write by default.
        #
        # NOTE: we deliberately do NOT pass --no-extensions. Provider
        # extensions (e.g. ollama) register the models consumers rely on,
        # and --no-extensions would silently break them. Tool safety is
        # already enforced by the --tools allowlist above, which applies
        # to built-in, extension, and custom tools alike.
        "--tools",
        "read,grep,find,ls,submit_review_decision",
        "--print",  # non-interactive: process prompt and exit
        user_prompt,
    ]

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
    )

    # pi exits 0 on success even if the review decision is "no-go".
    # Non-zero exit means pi itself errored (network, crash, etc.).
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode, cmd, proc.stdout, proc.stderr
        )

    # Parse NDJSON stream line by line, looking for tool_execution_start
    # with toolName === "submit_review_decision".
    # Non-JSON lines (startup noise) are skipped defensively.
    for line in proc.stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        if (
            event.get("type") == "tool_execution_start"
            and event.get("toolName") == "submit_review_decision"
        ):
            return event.get("args")

    return None

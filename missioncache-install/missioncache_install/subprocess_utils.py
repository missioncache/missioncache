"""Subprocess runner that surfaces output and failures.

Never swallows stdout/stderr on failure - the user needs to see what broke.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path


def _resolve_windows_executable(cmd: Sequence[str]) -> list[str]:
    """On Windows, resolve a bare-name cmd[0] to its full path via PATH.

    Fixes two Windows-only spawn problems in one place (this is the boundary
    that owns how things get spawned, so claude, codex and any future CLI are
    covered without a per-tool helper):

    1. npm installs `claude`, `codex` and friends as `.cmd` shims. A list-form
       spawn cannot start a `.cmd` by bare name (CreateProcess only appends
       `.exe`); shutil.which consults PATHEXT and returns the shim's full path,
       which CreateProcess does accept.
    2. shutil.which searches the current directory BEFORE PATH on Windows
       (verified in CPython's shutil source: the win32 branch does
       ``path.insert(0, curdir)`` unconditionally). A binary planted in the
       directory the installer happens to run from would otherwise be spawned
       ahead of the real one. We reject a resolution whose parent is the cwd: a
       real console script lives in a scripts dir, never in the invocation dir.

    POSIX is untouched (execvp already resolves bare names via PATH with no
    curdir search), and a bare name that resolves to nothing is left as-is so
    the FileNotFoundError fold below still fires.
    """
    if sys.platform != "win32" or not cmd:
        return list(cmd)
    name = cmd[0]
    if os.sep in name or (os.altsep and os.altsep in name):
        return list(cmd)  # already an explicit path
    found = shutil.which(name)
    if not found:
        return list(cmd)
    if Path(found).resolve().parent == Path.cwd().resolve():
        return list(cmd)  # curdir hit - refuse, let it fail to spawn
    return [found, *cmd[1:]]


class CommandFailed(Exception):
    """Raised when a subprocess exits non-zero and check=True."""

    def __init__(
        self,
        cmd: Sequence[str],
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> None:
        self.cmd = list(cmd)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(
            f"Command failed (exit {returncode}): {' '.join(self.cmd)}\n{stderr}"
        )


def run(
    cmd: Sequence[str],
    *,
    check: bool = True,
    timeout: float | None = None,
    input_: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command, capture output, raise CommandFailed on non-zero exit.

    Args:
        cmd: Command and args.
        check: Raise CommandFailed on non-zero exit (default True).
        timeout: Seconds before SIGKILL.
        input_: Stdin to pipe in.

    Returns:
        CompletedProcess with captured stdout/stderr.
    """
    cmd = _resolve_windows_executable(cmd)
    try:
        result = subprocess.run(
            list(cmd),
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
            input=input_,
            check=False,
        )
    except FileNotFoundError as e:
        # A missing binary raises instead of returning non-zero; callers
        # handle CommandFailed, so fold it in. (_resolve_windows_executable
        # above turns a resolvable .cmd shim into its full path; this still
        # fires for a genuinely absent binary or a cwd-only planted one we
        # deliberately refused to resolve.)
        raise CommandFailed(list(cmd), -1, "", str(e)) from e
    except subprocess.TimeoutExpired as e:
        # TimeoutExpired.stdout/stderr may be bytes even in text mode (typeshed
        # declares them as bytes | str | None). Decode defensively so we can
        # surface partial output to the user.
        raw_out = e.stdout
        raw_err = e.stderr
        stdout = raw_out.decode(errors="replace") if isinstance(raw_out, bytes) else (raw_out or "")
        stderr = raw_err.decode(errors="replace") if isinstance(raw_err, bytes) else (raw_err or f"timed out after {timeout}s")
        raise CommandFailed(list(cmd), -1, stdout, stderr) from e
    if check and result.returncode != 0:
        raise CommandFailed(
            list(cmd), result.returncode, result.stdout, result.stderr
        )
    return result


def run_streaming(cmd: Sequence[str], *, check: bool = True) -> int:
    """Run a command with inherited stdout/stderr (no capture). Returns exit code.

    Use this for long-running commands where live output matters (pipx install,
    claude plugins install). Output goes straight to the user's terminal.
    """
    cmd = _resolve_windows_executable(cmd)
    try:
        result = subprocess.run(list(cmd), check=False)
    except FileNotFoundError as e:
        raise CommandFailed(list(cmd), -1, "", str(e)) from e
    if check and result.returncode != 0:
        raise CommandFailed(list(cmd), result.returncode, "", "")
    return result.returncode

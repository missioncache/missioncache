"""Subprocess runner that surfaces output and failures.

Never swallows stdout/stderr on failure - the user needs to see what broke.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence


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
    try:
        result = subprocess.run(
            list(cmd),
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_,
            check=False,
        )
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
    result = subprocess.run(list(cmd), check=False)
    if check and result.returncode != 0:
        raise CommandFailed(list(cmd), result.returncode, "", "")
    return result.returncode

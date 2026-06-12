"""Tests for orbit_install.subprocess_utils - command runner with error surfacing."""

from __future__ import annotations

import sys

import pytest

from orbit_install.subprocess_utils import CommandFailed, run


def test_run_returns_result_on_zero_exit() -> None:
    """A successful command returns a CompletedProcess with captured stdout."""
    result = run([sys.executable, "-c", "print('hello')"])
    assert "hello" in result.stdout
    assert result.returncode == 0


def test_run_raises_command_failed_on_nonzero_exit() -> None:
    """Non-zero exit raises CommandFailed with the correct returncode."""
    with pytest.raises(CommandFailed) as exc_info:
        run([sys.executable, "-c", "import sys; sys.exit(7)"])
    assert exc_info.value.returncode == 7, \
        f"CommandFailed.returncode should be 7, got {exc_info.value.returncode}"


def test_run_check_false_returns_result_on_failure() -> None:
    """With check=False, callers get the CompletedProcess even on failure."""
    result = run(
        [sys.executable, "-c", "import sys; sys.exit(4)"],
        check=False,
    )
    assert result.returncode == 4


def test_run_captures_stderr_on_failure() -> None:
    """Failure includes stderr so the user sees what broke."""
    with pytest.raises(CommandFailed) as exc_info:
        run([sys.executable, "-c", "import sys; print('oops', file=sys.stderr); sys.exit(1)"])
    assert "oops" in exc_info.value.stderr


def test_run_timeout_raises_with_duration_in_stderr() -> None:
    """Timing out raises CommandFailed with the timeout duration in stderr."""
    with pytest.raises(CommandFailed) as exc_info:
        run(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            timeout=0.2,
        )
    assert exc_info.value.returncode == -1, "Timeout uses returncode -1 sentinel"
    assert "timed out" in exc_info.value.stderr.lower()


def test_run_passes_stdin() -> None:
    """input_ is piped to the child process stdin."""
    result = run(
        [sys.executable, "-c", "import sys; print(sys.stdin.read().strip())"],
        input_="piped-data",
    )
    assert "piped-data" in result.stdout


def test_command_failed_str_includes_command_and_stderr() -> None:
    """CommandFailed str is actionable: mentions the command and the stderr."""
    err = CommandFailed(["echo", "x"], 1, "", "boom")
    rendered = str(err)
    assert "echo x" in rendered
    assert "boom" in rendered

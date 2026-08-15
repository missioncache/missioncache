"""Tests for missioncache_install.subprocess_utils - command runner with error surfacing."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from missioncache_install import subprocess_utils
from missioncache_install.subprocess_utils import (
    CommandFailed,
    _resolve_windows_executable,
    run,
    run_streaming,
)


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


def test_run_missing_binary_folds_into_command_failed() -> None:
    """A nonexistent binary raises FileNotFoundError from subprocess; the
    runner folds it into the CommandFailed callers already handle (-1)."""
    with pytest.raises(CommandFailed) as exc_info:
        run(["missioncache-definitely-not-a-real-binary-xyz"])
    assert exc_info.value.returncode == -1


def test_run_streaming_missing_binary_folds_into_command_failed() -> None:
    with pytest.raises(CommandFailed) as exc_info:
        run_streaming(["missioncache-definitely-not-a-real-binary-xyz"])
    assert exc_info.value.returncode == -1


class TestResolveWindowsExecutable:
    """The bare-name resolver: only fires on win32, resolves .cmd shims, and
    refuses a current-directory resolution (the persistence/env-passing hazard).
    Runs on POSIX by faking sys.platform + shutil.which."""

    def test_posix_is_a_passthrough(self, monkeypatch):
        monkeypatch.setattr(subprocess_utils.sys, "platform", "linux")
        called = []
        monkeypatch.setattr(subprocess_utils.shutil, "which",
                            lambda n: called.append(n) or "/somewhere/x")
        assert _resolve_windows_executable(["claude", "a"]) == ["claude", "a"]
        assert not called  # never even consulted on POSIX

    def test_win32_resolves_bare_name(self, monkeypatch):
        monkeypatch.setattr(subprocess_utils.sys, "platform", "win32")
        monkeypatch.setattr(subprocess_utils.shutil, "which",
                            lambda n: r"C:\npm\claude.cmd")
        # Not under cwd, so it is accepted and substituted.
        monkeypatch.setattr(subprocess_utils.Path, "cwd", classmethod(lambda cls: Path(r"C:\work")))
        out = _resolve_windows_executable(["claude", "--print"])
        assert out == [r"C:\npm\claude.cmd", "--print"]

    def test_win32_explicit_path_is_untouched(self, monkeypatch):
        monkeypatch.setattr(subprocess_utils.sys, "platform", "win32")
        monkeypatch.setattr(subprocess_utils.shutil, "which",
                            lambda n: (_ for _ in ()).throw(AssertionError("which called")))
        assert _resolve_windows_executable([r"C:\x\claude.exe", "a"]) == [r"C:\x\claude.exe", "a"]

    def test_win32_refuses_curdir_resolution(self, monkeypatch, tmp_path):
        """shutil.which searches cwd before PATH on Windows; a resolution whose
        parent is the cwd must be refused (left bare, to fail to spawn)."""
        monkeypatch.setattr(subprocess_utils.sys, "platform", "win32")
        planted = tmp_path / "claude.cmd"
        planted.write_text("x", encoding="utf-8")
        monkeypatch.setattr(subprocess_utils.shutil, "which", lambda n: str(planted))
        monkeypatch.setattr(subprocess_utils.Path, "cwd", classmethod(lambda cls: tmp_path))
        assert _resolve_windows_executable(["claude"]) == ["claude"]

    def test_win32_unresolvable_stays_bare(self, monkeypatch):
        monkeypatch.setattr(subprocess_utils.sys, "platform", "win32")
        monkeypatch.setattr(subprocess_utils.shutil, "which", lambda n: None)
        assert _resolve_windows_executable(["claude"]) == ["claude"]

    def test_empty_cmd_is_safe(self, monkeypatch):
        monkeypatch.setattr(subprocess_utils.sys, "platform", "win32")
        assert _resolve_windows_executable([]) == []

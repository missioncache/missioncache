"""Tests for claude subprocess isolation and termination handling.

The claude child is launched in its own session/process group so a worker
teardown can reap the whole subtree instead of orphaning it (leaked claude
processes keep burning tokens with no supervisor).
"""

import signal
import subprocess
import sys

import pytest

from missioncache_auto import claude_runner as claude_runner_module
from missioncache_auto.claude_runner import (
    ClaudeRunner,
    _install_termination_handlers,
    _kill_process_group,
    _restore_termination_handlers,
)
from missioncache_auto.models import Visibility


class _FakeProcess:
    pid = -1

    def communicate(self, input=None, timeout=None):
        return ("", "")

    def poll(self):
        return 0

    def kill(self):
        pass


def _run_capturing_popen_kwargs(tmp_path, monkeypatch) -> dict:
    captured: dict = {}

    def fake_popen(cmd, **kwargs):
        captured.update(kwargs)
        return _FakeProcess()

    monkeypatch.setattr(claude_runner_module.subprocess, "Popen", fake_popen)
    ClaudeRunner(visibility=Visibility.NONE).run("p", tmp_path, print_output=False)
    return captured


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group semantics")
def test_claude_launched_in_new_session_posix(tmp_path, monkeypatch):
    """Contract: claude is spawned in its own group so teardown reaps the
    subtree. On POSIX that is start_new_session=True; on Windows it is a new
    process group (asserted separately). This pins the mechanism per platform
    so the diff that split it does not silently regress either."""
    captured = _run_capturing_popen_kwargs(tmp_path, monkeypatch)
    assert captured.get("start_new_session") is True
    assert "creationflags" not in captured


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process-group flag")
def test_claude_launched_in_new_process_group_win32(tmp_path, monkeypatch):
    captured = _run_capturing_popen_kwargs(tmp_path, monkeypatch)
    assert captured.get("creationflags", 0) & subprocess.CREATE_NEW_PROCESS_GROUP
    assert "start_new_session" not in captured


class _KillProc:
    def __init__(self, pid=4321, alive_after_taskkill=False):
        self.pid = pid
        self._alive = alive_after_taskkill
        self.killed = False

    def poll(self):
        return None if self._alive else 0

    def kill(self):
        self.killed = True


class TestKillProcessGroupWin32:
    """Windows teardown: taskkill /T /F reaps the tree (no killpg on Windows),
    with process.kill() as the fallback only when the tree survives. Simulated
    by faking sys.platform + subprocess.run."""

    def _fake_win32(self, monkeypatch):
        monkeypatch.setattr(claude_runner_module.sys, "platform", "win32")
        calls = []
        monkeypatch.setattr(
            claude_runner_module.subprocess, "run",
            lambda cmd, **kw: calls.append(cmd) or None,
        )
        return calls

    def test_taskkill_targets_the_pid_tree(self, monkeypatch):
        calls = self._fake_win32(monkeypatch)
        proc = _KillProc(pid=4321, alive_after_taskkill=False)
        _kill_process_group(proc)
        assert ["taskkill", "/T", "/F", "/PID", "4321"] in calls
        assert proc.killed is False  # tree gone, no fallback needed

    def test_kill_fallback_only_when_tree_survives(self, monkeypatch):
        self._fake_win32(monkeypatch)
        proc = _KillProc(alive_after_taskkill=True)
        _kill_process_group(proc)
        assert proc.killed is True

    def test_taskkill_absent_falls_back_to_kill(self, monkeypatch):
        monkeypatch.setattr(claude_runner_module.sys, "platform", "win32")

        def raise_fnf(cmd, **kw):
            raise FileNotFoundError("taskkill")

        monkeypatch.setattr(claude_runner_module.subprocess, "run", raise_fnf)
        proc = _KillProc(alive_after_taskkill=True)
        _kill_process_group(proc)  # must not raise
        assert proc.killed is True


def test_termination_handlers_round_trip():
    """Install replaces SIGTERM/SIGINT handlers; restore puts the originals back."""
    before_term = signal.getsignal(signal.SIGTERM)
    before_int = signal.getsignal(signal.SIGINT)

    previous = _install_termination_handlers(_FakeProcess())
    try:
        assert signal.getsignal(signal.SIGTERM) != before_term
        assert signal.getsignal(signal.SIGINT) != before_int
    finally:
        _restore_termination_handlers(previous)

    assert signal.getsignal(signal.SIGTERM) == before_term
    assert signal.getsignal(signal.SIGINT) == before_int

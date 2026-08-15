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

"""Tests for claude subprocess isolation and termination handling.

The claude child is launched in its own session/process group so a worker
teardown can reap the whole subtree instead of orphaning it (leaked claude
processes keep burning tokens with no supervisor).
"""

import signal

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

    def kill(self):
        pass


def test_claude_launched_in_new_session(tmp_path, monkeypatch):
    """Popen must get start_new_session=True so claude is its own group leader."""
    captured: dict = {}

    def fake_popen(cmd, **kwargs):
        captured.update(kwargs)
        return _FakeProcess()

    monkeypatch.setattr(claude_runner_module.subprocess, "Popen", fake_popen)
    ClaudeRunner(visibility=Visibility.NONE).run("p", tmp_path, print_output=False)

    assert captured.get("start_new_session") is True


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

"""The claude command ClaudeRunner builds: `--effort` only when asked for."""

from missioncache_auto import claude_runner as claude_runner_module
from missioncache_auto.claude_runner import ClaudeRunner
from missioncache_auto.models import Visibility


class _FakeProcess:
    def communicate(self, input=None, timeout=None):
        return ("", "")

    def kill(self):
        pass


def _built_cmd(monkeypatch, tmp_path, **runner_kwargs):
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeProcess()

    monkeypatch.setattr(claude_runner_module.subprocess, "Popen", fake_popen)
    ClaudeRunner(visibility=Visibility.NONE, **runner_kwargs).run("p", tmp_path, print_output=False)
    return captured["cmd"]


def test_effort_is_passed_to_claude(monkeypatch, tmp_path):
    cmd = _built_cmd(monkeypatch, tmp_path, effort="high")
    assert cmd[cmd.index("--effort") + 1] == "high"


def test_no_effort_flag_by_default(monkeypatch, tmp_path):
    """Without --effort the user's own Claude Code setting applies."""
    assert "--effort" not in _built_cmd(monkeypatch, tmp_path)

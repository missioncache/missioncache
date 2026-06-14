"""Tests for environment variable wiring across the claude_runner / cli boundary.

Two env-var literals are load-bearing for the autonomous-execution mode:

- ``MISSIONCACHE_AUTO_MODE`` (claude_runner.py ~line 89): set to ``"1"`` in the
  child env so hooks running inside the spawned Claude CLI process can
  detect missioncache-auto mode and skip interactive prompts. If renamed
  silently, every hook that gates on it stops gating.
- ``MISSIONCACHE_AUTO_VISIBILITY`` (cli.py ~line 79-80): read from
  ``os.environ`` as the default for ``--visibility`` so users can set a
  shell-wide preference. If renamed silently, the env var is read from
  the wrong key and the user's setting is ignored.

Both contracts are pinned here by capturing the env passed to
``subprocess.Popen`` and by parsing argv with a patched environment.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from missioncache_auto import claude_runner as claude_runner_module
from missioncache_auto import cli as cli_module
from missioncache_auto.claude_runner import ClaudeRunner
from missioncache_auto.models import Visibility


class _FakeProcess:
    """Minimal stand-in for the Popen object used by ClaudeRunner.run.

    Mirrors the two attributes ClaudeRunner touches after spawn:
    ``communicate`` (called once with the prompt) and exposing
    nothing else. Returning empty stdout/stderr makes
    ``_process_output`` produce an ExecutionResult with success=False,
    which is fine - the test pins the spawn env, not the result.
    """

    def communicate(self, input=None, timeout=None):
        return ("", "")

    def kill(self):
        pass


class TestClaudeRunnerEnv:
    def test_missioncache_auto_mode_set_to_one_in_child_env(
        self, tmp_path, monkeypatch
    ):
        """The spawned Claude subprocess must receive MISSIONCACHE_AUTO_MODE=1.

        Hooks downstream gate on this exact string. A rename to e.g.
        ``MISSIONCACHE_AUTO_MODE`` here without updating the hooks would
        silently disable the autonomous-mode skip path.
        """
        captured: dict = {}

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env")
            captured["cwd"] = kwargs.get("cwd")
            return _FakeProcess()

        monkeypatch.setattr(claude_runner_module.subprocess, "Popen", fake_popen)

        runner = ClaudeRunner(visibility=Visibility.NONE)
        runner.run("dummy prompt", tmp_path, print_output=False)

        env = captured["env"]
        assert env is not None, "Popen must be called with an explicit env"
        assert env.get("MISSIONCACHE_AUTO_MODE") == "1"

    def test_claude_code_hide_cwd_set_to_one(self, tmp_path, monkeypatch):
        """The companion ``CLAUDE_CODE_HIDE_CWD=1`` env var is set on the
        same line. Pin the literal so the rename sweep doesn't half-do
        it (rename one, leave the other)."""
        captured: dict = {}

        def fake_popen(cmd, **kwargs):
            captured["env"] = kwargs.get("env")
            return _FakeProcess()

        monkeypatch.setattr(claude_runner_module.subprocess, "Popen", fake_popen)

        ClaudeRunner(visibility=Visibility.NONE).run(
            "p", tmp_path, print_output=False
        )

        assert captured["env"].get("CLAUDE_CODE_HIDE_CWD") == "1"

    def test_child_env_inherits_parent_env(self, tmp_path, monkeypatch):
        """The runner uses ``os.environ.copy()`` as the base - the child
        receives the parent's env PLUS the two overrides. If someone
        switches to a bare ``{"MISSIONCACHE_AUTO_MODE": "1", ...}`` dict, PATH
        and friends vanish and the spawned process can't find ``claude``.

        Set a sentinel env var on the parent and assert it survives.
        """
        captured: dict = {}

        def fake_popen(cmd, **kwargs):
            captured["env"] = kwargs.get("env")
            return _FakeProcess()

        monkeypatch.setattr(claude_runner_module.subprocess, "Popen", fake_popen)
        monkeypatch.setenv("MISSIONCACHE_TEST_SENTINEL", "from-parent")

        ClaudeRunner(visibility=Visibility.NONE).run(
            "p", tmp_path, print_output=False
        )

        assert captured["env"].get("MISSIONCACHE_TEST_SENTINEL") == "from-parent"


class TestCliVisibilityEnvVar:
    """``MISSIONCACHE_AUTO_VISIBILITY`` defaults ``--visibility`` from the shell."""

    def _parse_with_argv(self, monkeypatch, argv):
        """Drive ``cli.parse_args`` with a patched sys.argv.

        ``parse_args`` mutates ``sys.argv`` (it may insert ``"run"``),
        so each test gets a fresh copy.
        """
        monkeypatch.setattr(sys, "argv", list(argv))
        return cli_module.parse_args()

    def test_env_var_sets_visibility_default_minimal(self, monkeypatch):
        monkeypatch.setenv("MISSIONCACHE_AUTO_VISIBILITY", "minimal")
        args = self._parse_with_argv(monkeypatch, ["missioncache-auto", "my-task"])
        assert args.visibility == "minimal"

    def test_env_var_sets_visibility_default_none(self, monkeypatch):
        monkeypatch.setenv("MISSIONCACHE_AUTO_VISIBILITY", "none")
        args = self._parse_with_argv(monkeypatch, ["missioncache-auto", "my-task"])
        assert args.visibility == "none"

    def test_default_is_verbose_when_env_unset(self, monkeypatch):
        """No env var -> hard-coded default ``"verbose"``."""
        monkeypatch.delenv("MISSIONCACHE_AUTO_VISIBILITY", raising=False)
        args = self._parse_with_argv(monkeypatch, ["missioncache-auto", "my-task"])
        assert args.visibility == "verbose"

    def test_explicit_flag_overrides_env_var(self, monkeypatch):
        """``--visibility=none`` on the command line wins over the env
        var.

        Locks down that the env var is the DEFAULT, not an override.
        Uses the ``=`` form because cli.parse_args has a pre-processor
        that inserts ``"run"`` based on the first positional; the
        ``--visibility=value`` single-token form sidesteps the
        pre-processor's off-by-one around separated option values.
        """
        monkeypatch.setenv("MISSIONCACHE_AUTO_VISIBILITY", "minimal")
        args = self._parse_with_argv(
            monkeypatch, ["missioncache-auto", "--visibility=none", "my-task"]
        )
        assert args.visibility == "none"

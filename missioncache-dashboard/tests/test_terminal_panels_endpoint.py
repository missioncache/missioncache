"""/api/terminal-panels, behind the iTerm2 card on the Sessions view.

Spec: the phase 2 plan. It reports whether iTerm2 is there (macOS only), and
whether the installer recorded the iterm_panels component. A missing or broken
installer state means not installed, never an error.
"""

import json
import pathlib

import pytest

from missioncache_dashboard import server


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(server.sys, "platform", "darwin")
    monkeypatch.setattr(server, "SYSTEM_APPLICATIONS", tmp_path / "system-apps")  # empty
    (tmp_path / ".claude").mkdir()
    return tmp_path


def _state(home, text: str) -> None:
    (home / ".claude" / "missioncache-install.state.json").write_text(text)


def test_iterm_in_the_user_applications_folder_counts(home):
    (home / "Applications" / "iTerm.app").mkdir(parents=True)
    out = server.get_terminal_panels()
    assert out["iterm"] is True
    assert out["installed"] is False


def test_no_iterm_on_macos_is_reported(home):
    assert server.get_terminal_panels()["iterm"] is False


def test_installed_reads_the_installer_state(home):
    _state(home, json.dumps({"components": {"dashboard": {}, "iterm_panels": {"mode": "pypi"}}}))
    out = server.get_terminal_panels()
    assert (out["installed"], out["shortcuts"]) == (True, False)
    _state(home, json.dumps({"components": {"iterm_panels": {"key_bindings": {"keys": ["k"]}}}}))
    assert server.get_terminal_panels()["shortcuts"] is True
    _state(home, json.dumps({"components": {"dashboard": {}}}))
    assert server.get_terminal_panels()["installed"] is False


def test_a_broken_state_file_is_not_installed(home):
    _state(home, "{not json")
    assert server.get_terminal_panels()["installed"] is False
    _state(home, "[]")
    assert server.get_terminal_panels()["installed"] is False


def test_not_macos_has_no_iterm(home, monkeypatch):
    (home / "Applications" / "iTerm.app").mkdir(parents=True)
    monkeypatch.setattr(server.sys, "platform", "linux")
    out = server.get_terminal_panels()
    assert out == {"iterm": False, "installed": False, "shortcuts": False}

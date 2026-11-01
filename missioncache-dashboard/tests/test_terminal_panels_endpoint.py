"""/api/terminal-panels, behind the terminal panels card on the Sessions view.

Spec: the phase 2 and phase 3 plans. It reports whether iTerm2 (macOS only)
or Windows Terminal (Windows only) is there, and whether the installer
recorded that terminal's panels, with their shortcuts. A missing or broken
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
    assert out == {"iterm": False, "wt": False, "installed": False, "shortcuts": False}


def _windows_terminal(home, monkeypatch) -> None:
    monkeypatch.setattr(server.sys, "platform", "win32")
    apps = home / "local" / "Microsoft" / "WindowsApps" / "Microsoft.WindowsTerminalPreview_8wekyb3d8bbwe"
    apps.mkdir(parents=True)
    (apps / "wt.exe").touch()
    monkeypatch.setenv("LOCALAPPDATA", str(home / "local"))


def test_windows_terminal_and_its_panels(home, monkeypatch):
    _windows_terminal(home, monkeypatch)
    _state(home, json.dumps({"components": {"iterm_panels": {"key_bindings": {"keys": ["k"]}}}}))
    assert server.get_terminal_panels() == {"iterm": False, "wt": True, "installed": False, "shortcuts": False}
    _state(home, json.dumps({"components": {"wt_panels": {"keys": False}}}))
    assert (server.get_terminal_panels()["installed"], server.get_terminal_panels()["shortcuts"]) == (True, False)
    _state(home, json.dumps({"components": {"wt_panels": {"keys": True}}}))
    assert server.get_terminal_panels()["shortcuts"] is True


def test_windows_without_windows_terminal(home, monkeypatch):
    monkeypatch.setattr(server.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(home / "local"))
    assert server.get_terminal_panels()["wt"] is False

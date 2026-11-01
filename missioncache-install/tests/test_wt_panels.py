"""The wt_panels component: CLI selection, install, the keys question, uninstall.

Spec: the phase 3 plan, decisions 4, 5, 7 and 8. A fake schtasks / reg /
powershell / taskkill layer records every command, so no test touches the
real Task Scheduler, registry or Start menu.
"""

from __future__ import annotations

import dataclasses
import os
import sys
from pathlib import Path

import pytest

from missioncache_install import __main__ as cli
from missioncache_install import installers, state, subprocess_utils, wizard, wt_panels


class FakeSystem:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.started: list[list[str]] = []
        self.refuse_schtasks = False
        self.refuse_run_key = False
        self.clear = "clear"
        self.answers = True

    def run(self, cmd, *, check=True, **_):
        cmd = list(cmd)
        self.calls.append(cmd)
        code = 1 if (self.refuse_schtasks and cmd[:2] == ["schtasks", "/Create"]
                     or self.refuse_run_key and cmd[:2] == ["reg", "add"]) else 0
        return subprocess_utils.subprocess.CompletedProcess(cmd, code, "", "Access is denied." if code else "")

    def run_streaming(self, cmd, *, check=True):
        self.calls.append(list(cmd))
        if "venv" in cmd:
            python = Path(cmd[-1]) / "Scripts" / "python.exe"
            python.parent.mkdir(parents=True, exist_ok=True)
            python.touch()
        return 0

    def ran(self, *prefix: str) -> list[list[str]]:
        return [c for c in self.calls if c[: len(prefix)] == list(prefix)]


@pytest.fixture
def system(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeSystem:
    fake = FakeSystem()
    monkeypatch.setattr(wt_panels.subprocess_utils, "run", fake.run)
    monkeypatch.setattr(wt_panels.subprocess_utils, "run_streaming", fake.run_streaming)
    monkeypatch.setattr(wt_panels.shutil, "which", lambda name: f"C:/bin/{name}")
    monkeypatch.setattr(wt_panels.subprocess, "Popen", lambda args, **_: fake.started.append(args))
    monkeypatch.setattr(wt_panels.sys, "platform", "win32")
    monkeypatch.setattr(wt_panels, "_running", lambda: fake.answers)

    def clear(*, roots, **_):
        fake.calls.append(["stop", *roots])
        return (fake.clear, [(9, "missioncache-wt.exe")] if fake.clear == "blocked" else [])

    monkeypatch.setattr(installers, "_clear_dashboard_processes", clear)
    local = tmp_path / "local"
    (local / "Microsoft" / "WindowsApps" / "Microsoft.WindowsTerminal_8wekyb3d8bbwe").mkdir(parents=True)
    (local / "Microsoft" / "WindowsApps" / "Microsoft.WindowsTerminal_8wekyb3d8bbwe" / "wt.exe").touch()
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.delenv("ProgramFiles", raising=False)
    return fake


def _ctx(**kw) -> installers.InstallContext:
    base = installers.InstallContext(
        mode="pypi", repo_root=None, skip_service=False, port=8787, assume_yes=False
    )
    return dataclasses.replace(base, **kw)


def _answer(monkeypatch: pytest.MonkeyPatch, yes: bool) -> list[bool]:
    defaults: list[bool] = []

    def ask(prompt: str, default: bool = True) -> bool:
        defaults.append(default)
        return yes

    monkeypatch.setattr(wt_panels.ui, "ask_yn", ask)
    return defaults


def _never(*_a, **_k):
    raise AssertionError("an update asked about the keys")


# --- selection ---------------------------------------------------------------

def test_all_leaves_the_panels_out(monkeypatch: pytest.MonkeyPatch) -> None:
    picked: list[list[str]] = []
    monkeypatch.setattr(installers, "install_components", lambda c, ctx: picked.append(c) or [])
    monkeypatch.setattr(wt_panels, "detected", lambda: True)
    monkeypatch.setattr(sys, "argv", ["missioncache-install", "--all"])
    cli.main()
    assert "wt_panels" not in picked[0]


def test_the_flag_adds_only_the_panels(monkeypatch: pytest.MonkeyPatch) -> None:
    picked: list[list[str]] = []
    monkeypatch.setattr(installers, "install_components", lambda c, ctx: picked.append(c) or [])
    monkeypatch.setattr(wt_panels, "detected", lambda: True)
    monkeypatch.setattr(sys, "argv", ["missioncache-install", "--wt-panels"])
    cli.main()
    assert picked == [["wt_panels"]]


def test_wizard_hides_the_panels_without_windows_terminal_and_defaults_to_no(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    defaults: dict[str, bool] = {}

    def ask(prompt: str, default: bool = True) -> bool:
        defaults[prompt] = default
        return default

    monkeypatch.setattr(wizard.ui, "ask_yn", ask)
    for name in ("codex", "opencode", "vscode", "iterm_panels", "wt_panels"):
        monkeypatch.setitem(wizard._TOOL_DETECTORS, name, lambda: False)
    assert "wt_panels" not in wizard._select_components()
    assert not any("Windows Terminal" in p for p in defaults)

    monkeypatch.setitem(wizard._TOOL_DETECTORS, "wt_panels", lambda: True)
    selected = wizard._select_components()
    assert defaults["  Install Windows Terminal panels?"] is False
    assert "wt_panels" not in selected


def test_detected_needs_windows_terminal(system: FakeSystem, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert wt_panels.detected()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "empty"))
    assert not wt_panels.detected()


# --- install -----------------------------------------------------------------

def test_install_registers_at_logon_starts_and_adds_the_shortcut(
    system: FakeSystem, monkeypatch: pytest.MonkeyPatch, isolated_home: Path,
) -> None:
    state.record_component("dashboard", {"mode": "pypi", "port": 8787})
    defaults = _answer(monkeypatch, False)
    wt_panels.install(_ctx())

    assert defaults == [False]
    exe = str(isolated_home / ".missioncache" / "wt" / "venv" / "Scripts" / "missioncache-wt.exe")
    [task] = system.ran("schtasks", "/Create")
    assert task[task.index("/SC") + 1] == "ONLOGON"
    # A Run key left from an install that schtasks refused would start a second daemon.
    assert system.ran("reg", "delete", wt_panels.RUN_KEY, "/v", wt_panels.TASK_NAME, "/f")
    assert "--hidden" in task[task.index("/TR") + 1] and "--keys" not in task[task.index("/TR") + 1]
    assert system.started == [[exe, "--hidden"]]
    install = system.ran("C:/bin/uv", "pip", "install")
    assert install and "missioncache-dashboard[wt]" in install[0]
    [shortcut] = system.ran("powershell")
    assert "/panels" in shortcut[-1] and "MissionCache Panels.lnk" in shortcut[-1]
    components = state.load()["components"]
    assert components["dashboard"] == {"mode": "pypi", "port": 8787}
    assert components["wt_panels"] == {"mode": "pypi", "keys": False, "service": "schtasks"}


def test_the_running_daemon_stops_before_its_venv_is_replaced(
    system: FakeSystem, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _answer(monkeypatch, False)
    wt_panels.install(_ctx())
    kinds = [c[0] for c in system.calls]
    assert kinds.index("stop") < kinds.index("C:/bin/uv")
    [stop] = system.ran("stop")
    assert stop[1] == str(wt_panels._data_dir() / "venv") + os.sep


def test_a_daemon_that_will_not_stop_leaves_the_install_alone(
    system: FakeSystem, monkeypatch: pytest.MonkeyPatch,
) -> None:
    system.clear = "blocked"
    _answer(monkeypatch, False)
    wt_panels.install(_ctx())
    assert [c[0] for c in system.calls] == ["stop"]
    assert "wt_panels" not in state.installed_components()


def test_yes_turns_on_the_keys(system: FakeSystem, monkeypatch: pytest.MonkeyPatch) -> None:
    _answer(monkeypatch, True)
    wt_panels.install(_ctx())
    assert "--keys" in system.started[0]
    assert state.load()["components"]["wt_panels"]["keys"] is True


def test_a_ctrl_c_at_the_question_leaves_nothing(system: FakeSystem, monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupt(*_a, **_k):
        raise KeyboardInterrupt

    monkeypatch.setattr(wt_panels.ui, "ask_yn", interrupt)
    with pytest.raises(KeyboardInterrupt):
        wt_panels.install(_ctx())
    assert system.calls == []
    assert not wt_panels._data_dir().exists()
    assert "wt_panels" not in state.installed_components()


def test_update_keeps_the_keys_choice_without_asking(system: FakeSystem, monkeypatch: pytest.MonkeyPatch) -> None:
    _answer(monkeypatch, True)
    wt_panels.install(_ctx())
    monkeypatch.setattr(wt_panels.ui, "ask_yn", _never)
    wt_panels.install(_ctx(updating=True))
    assert "--keys" in system.started[-1]


def test_a_refused_schtasks_falls_back_to_the_run_key(system: FakeSystem, monkeypatch: pytest.MonkeyPatch) -> None:
    system.refuse_schtasks = True
    _answer(monkeypatch, False)
    wt_panels.install(_ctx())
    [run_key] = system.ran("reg", "add")
    assert run_key[run_key.index("/v") + 1] == wt_panels.TASK_NAME
    # A task left from an earlier install would start a second daemon.
    assert system.ran("schtasks", "/Delete", "/TN", wt_panels.TASK_NAME, "/F")
    assert state.load()["components"]["wt_panels"]["service"] == "run_key"


def test_a_refused_run_key_too_installs_without_logon_start(
    system: FakeSystem, monkeypatch: pytest.MonkeyPatch,
) -> None:
    system.refuse_schtasks = system.refuse_run_key = True
    _answer(monkeypatch, False)
    wt_panels.install(_ctx())
    assert state.load()["components"]["wt_panels"]["service"] == "none"
    assert not system.ran("schtasks", "/Delete")
    assert system.started


def test_the_dashboard_port_rides_on_the_command_line(system: FakeSystem, monkeypatch: pytest.MonkeyPatch) -> None:
    state.record_component("dashboard", {"mode": "pypi", "port": 9999})
    _answer(monkeypatch, False)
    wt_panels.install(_ctx())
    assert system.started[0][-2:] == ["--dashboard-port", "9999"]


def test_without_windows_terminal_nothing_is_installed(
    system: FakeSystem, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "empty"))
    wt_panels.install(_ctx())
    assert system.calls == []
    assert "wt_panels" not in state.installed_components()


# --- uninstall ---------------------------------------------------------------

def test_uninstall_removes_what_install_added_and_nothing_else(
    system: FakeSystem, monkeypatch: pytest.MonkeyPatch, isolated_home: Path, tmp_path: Path,
) -> None:
    state.record_component("dashboard", {"mode": "pypi"})
    _answer(monkeypatch, False)
    wt_panels.install(_ctx())
    shortcut = wt_panels._shortcut_path()
    shortcut.touch()  # the fake powershell wrote nothing
    system.calls.clear()

    wt_panels.uninstall(_ctx())

    assert system.ran("stop")
    assert system.ran("schtasks", "/Delete", "/TN", wt_panels.TASK_NAME, "/F")
    assert system.ran("reg", "delete", wt_panels.RUN_KEY, "/v", wt_panels.TASK_NAME, "/f")
    assert not shortcut.exists()
    assert not (isolated_home / ".missioncache" / "wt").exists()
    assert list(state.load()["components"]) == ["dashboard"]


def test_a_daemon_that_will_not_stop_is_not_uninstalled(system: FakeSystem, monkeypatch: pytest.MonkeyPatch) -> None:
    _answer(monkeypatch, False)
    wt_panels.install(_ctx())
    system.calls.clear()
    system.clear = "blocked"
    wt_panels.uninstall(_ctx())
    assert [c[0] for c in system.calls] == ["stop"]
    assert wt_panels._data_dir().exists()
    assert "wt_panels" in state.installed_components()


def test_files_left_behind_keep_the_record_for_another_uninstall(
    system: FakeSystem, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _answer(monkeypatch, False)
    wt_panels.install(_ctx())
    monkeypatch.setattr(wt_panels.shutil, "rmtree", lambda *a, **k: None)  # a file Windows keeps locked
    wt_panels.uninstall(_ctx())
    assert "wt_panels" in state.installed_components()


def test_panels_that_do_not_answer_are_reported(
    system: FakeSystem, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    system.answers = False
    _answer(monkeypatch, False)
    wt_panels.install(_ctx())
    assert "did not answer" in capsys.readouterr().out


def test_a_typographic_quote_in_the_profile_path_stays_inside_the_string(
    system: FakeSystem, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "O\u2019Brien"))
    _answer(monkeypatch, False)
    wt_panels.install(_ctx())
    [shortcut] = system.ran("powershell")
    assert "O\u2019\u2019Brien" in shortcut[-1]


def test_a_uv_in_the_current_directory_is_not_used(
    system: FakeSystem, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wt_panels.shutil, "which", lambda name: str(tmp_path / "uv.exe"))
    _answer(monkeypatch, False)
    wt_panels.install(_ctx())
    assert not any(str(tmp_path / "uv.exe") in c for c in system.calls)

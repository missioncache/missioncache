"""The iterm_panels component: CLI selection, install, key bindings, uninstall.

A fake `defaults` / `launchctl` / installer layer records every command, so no
test touches the real iTerm2 settings or launchd.
"""

from __future__ import annotations

import dataclasses
import plistlib
import sys
from pathlib import Path

import pytest

from missioncache_install import __main__ as cli
from missioncache_install import installers, iterm_panels, state, subprocess_utils, wizard


class FakeSystem:
    """Stands in for `defaults`, `launchctl` and the package install."""

    def __init__(self, prefs: dict) -> None:
        self.prefs = prefs
        self.calls: list[list[str]] = []

    def run(self, cmd, *, check=True, **_):
        cmd = list(cmd)
        self.calls.append(cmd)
        out = ""
        if cmd[:2] == ["defaults", "export"]:
            if cmd[3] == "-":
                out = plistlib.dumps(self.prefs).decode("utf-8")
            else:
                Path(cmd[3]).write_bytes(plistlib.dumps(self.prefs))
        elif cmd[:2] == ["defaults", "write"]:
            self.prefs[cmd[3]] = plistlib.loads(cmd[4].encode("utf-8"))
        elif cmd[:2] == ["defaults", "delete"]:
            self.prefs.pop(cmd[3], None)
        return subprocess_utils.subprocess.CompletedProcess(cmd, 0, out, "")

    def run_streaming(self, cmd, *, check=True):
        self.calls.append(list(cmd))
        if "venv" in cmd:
            python = Path(cmd[-1]) / "bin" / "python"
            python.parent.mkdir(parents=True, exist_ok=True)
            python.touch()
        return 0

    def ran(self, *prefix: str) -> list[list[str]]:
        return [c for c in self.calls if c[: len(prefix)] == list(prefix)]


# iTerm2's own defaults, as its app bundle ships them (two invented entries).
SHIPPED = {"0x19-0x60000": {"Action": 39, "Text": ""}, "0xf702-0x320000": {"Action": 33, "Text": ""}}


@pytest.fixture
def system(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeSystem:
    fake = FakeSystem({"GlobalKeyMap": {"0x9-0x40000-0x30": {"Action": 2, "Text": ""}}})
    monkeypatch.setattr(iterm_panels.subprocess_utils, "run", fake.run)
    monkeypatch.setattr(iterm_panels.subprocess_utils, "run_streaming", fake.run_streaming)
    monkeypatch.setattr(iterm_panels.shutil, "which", lambda name: f"/bin/{name}")
    app = tmp_path / "Applications" / "iTerm.app"
    (app / "Contents" / "Resources").mkdir(parents=True)
    (app / "Contents" / "Resources" / "DefaultGlobalKeyMap.plist").write_bytes(plistlib.dumps(SHIPPED))
    monkeypatch.setattr(iterm_panels, "_iterm_app", lambda: app)
    monkeypatch.setattr(iterm_panels.sys, "platform", "darwin")
    return fake


def _ctx(**kw) -> installers.InstallContext:
    base = installers.InstallContext(
        mode="pypi", repo_root=None, skip_service=False, port=8787, assume_yes=False
    )
    return dataclasses.replace(base, **kw)


def _answer(monkeypatch: pytest.MonkeyPatch, yes: bool) -> None:
    monkeypatch.setattr(iterm_panels.ui, "ask_yn", lambda prompt, default=True: yes)


# --- selection ---------------------------------------------------------------

def test_all_leaves_the_panels_out(monkeypatch: pytest.MonkeyPatch) -> None:
    picked: list[list[str]] = []
    monkeypatch.setattr(installers, "install_components", lambda c, ctx: picked.append(c) or [])
    monkeypatch.setattr(iterm_panels, "detected", lambda: True)
    monkeypatch.setattr(sys, "argv", ["missioncache-install", "--all"])
    cli.main()
    assert "iterm_panels" not in picked[0]
    assert "dashboard" in picked[0]


def test_the_flag_adds_only_the_panels(monkeypatch: pytest.MonkeyPatch) -> None:
    picked: list[list[str]] = []
    monkeypatch.setattr(installers, "install_components", lambda c, ctx: picked.append(c) or [])
    monkeypatch.setattr(iterm_panels, "detected", lambda: True)
    monkeypatch.setattr(sys, "argv", ["missioncache-install", "--iterm-panels"])
    cli.main()
    assert picked == [["iterm_panels"]]


def test_without_iterm2_nothing_is_installed(
    system: FakeSystem, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(iterm_panels, "_iterm_app", lambda: None)
    iterm_panels.install(_ctx())
    assert system.calls == []
    assert "iterm_panels" not in state.load().get("components", {})


def test_wizard_hides_the_panels_without_iterm2_and_defaults_to_no(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    defaults: dict[str, bool] = {}

    def ask(prompt: str, default: bool = True) -> bool:
        defaults[prompt] = default
        return default

    monkeypatch.setattr(wizard.ui, "ask_yn", ask)
    for name in ("codex", "opencode", "vscode"):
        monkeypatch.setitem(wizard._TOOL_DETECTORS, name, lambda: False)
    monkeypatch.setitem(wizard._TOOL_DETECTORS, "iterm_panels", lambda: False)
    assert "iterm_panels" not in wizard._select_components()
    assert not any("iTerm2" in p for p in defaults)

    monkeypatch.setitem(wizard._TOOL_DETECTORS, "iterm_panels", lambda: True)
    selected = wizard._select_components()
    assert defaults["  Install iTerm2 panels?"] is False
    assert "iterm_panels" not in selected
    assert "dashboard" in selected


# --- install -----------------------------------------------------------------

def test_install_writes_the_agent_and_keeps_other_components(
    system: FakeSystem, monkeypatch: pytest.MonkeyPatch, isolated_home: Path,
) -> None:
    state.record_component("dashboard", {"mode": "pypi", "port": 8787})
    _answer(monkeypatch, False)
    iterm_panels.install(_ctx())

    plist = isolated_home / "Library" / "LaunchAgents" / "com.missioncache.iterm.plist"
    agent = plistlib.loads(plist.read_bytes())
    venv_bin = isolated_home / ".missioncache" / "iterm" / "venv" / "bin"
    assert agent["ProgramArguments"] == [str(venv_bin / "missioncache-iterm")]
    assert agent["KeepAlive"] is True
    assert system.ran("launchctl", "load", str(plist))
    install = system.ran("/bin/uv", "pip", "install")
    assert install and "missioncache-dashboard[iterm2]" in install[0]
    components = state.load()["components"]
    assert components["dashboard"] == {"mode": "pypi", "port": 8787}
    assert components["iterm_panels"] == {"mode": "pypi", "service": "launchd"}
    assert system.prefs["GlobalKeyMap"] == {"0x9-0x40000-0x30": {"Action": 2, "Text": ""}}


def test_local_mode_installs_the_clone_editable(
    system: FakeSystem, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _answer(monkeypatch, False)
    iterm_panels.install(_ctx(mode="local", repo_root=tmp_path / "clone"))
    install = system.ran("/bin/uv", "pip", "install")[0]
    assert "-e" in install
    assert f"{tmp_path / 'clone' / 'missioncache-dashboard'}[iterm2]" in install


def test_no_service_skips_launchd(system: FakeSystem, monkeypatch: pytest.MonkeyPatch) -> None:
    _answer(monkeypatch, False)
    iterm_panels.install(_ctx(skip_service=True))
    assert not system.ran("launchctl")
    assert state.load()["components"]["iterm_panels"]["service"] == "none"


def test_the_three_bindings_from_the_plan() -> None:
    # Spec, decision 7: Cmd+Shift+] next, Cmd+Shift+[ previous, Cmd+Shift+\ unseen.
    assert {f for _, f in iterm_panels.KEY_BINDINGS.values()} == {
        "session_next()", "session_prev()", "jump_to_unseen()"}
    assert iterm_panels.KEY_BINDINGS["0x7d-0x120000-0x1e-0x1"][1] == "session_next()"


def test_the_agent_opens_the_dashboard_on_its_recorded_port(
    system: FakeSystem, monkeypatch: pytest.MonkeyPatch, isolated_home: Path,
) -> None:
    state.record_component("dashboard", {"mode": "pypi", "port": 9999})
    _answer(monkeypatch, False)
    iterm_panels.install(_ctx())
    plist = isolated_home / "Library" / "LaunchAgents" / "com.missioncache.iterm.plist"
    env = plistlib.loads(plist.read_bytes())["EnvironmentVariables"]
    assert env["MISSIONCACHE_DASHBOARD_PORT"] == "9999"


def test_a_ctrl_c_at_the_question_still_leaves_it_uninstallable(
    system: FakeSystem, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupt(*_a, **_k):
        raise KeyboardInterrupt

    monkeypatch.setattr(iterm_panels.ui, "ask_yn", interrupt)
    with pytest.raises(KeyboardInterrupt):
        iterm_panels.install(_ctx())
    assert "iterm_panels" in state.installed_components()


def test_a_first_write_keeps_iterm2s_own_defaults(
    system: FakeSystem, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A user who never changed a global binding has no GlobalKeyMap: iTerm2
    # runs on the shipped defaults, and writing three keys alone drops them.
    del system.prefs["GlobalKeyMap"]
    _answer(monkeypatch, True)
    iterm_panels.install(_ctx())
    keymap = system.prefs["GlobalKeyMap"]
    assert {k: keymap[k] for k in SHIPPED} == SHIPPED
    assert len(keymap) == len(SHIPPED) + 3

    iterm_panels.uninstall(_ctx())
    assert "GlobalKeyMap" not in system.prefs  # back to iTerm2's own defaults
    assert system.ran("defaults", "delete", iterm_panels.PREFS_DOMAIN, "GlobalKeyMap")


def test_uninstall_leaves_what_was_there_before_the_install(
    system: FakeSystem, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The layout option was already on, and one key was already bound the
    # same way: the install changed neither, so uninstall keeps both.
    system.prefs["LanguageAgnosticKeyBindings"] = True
    system.prefs["GlobalKeyMap"]["0x7d-0x120000-0x1e-0x1"] = {"Action": 60, "Text": "session_next()"}
    _answer(monkeypatch, True)
    iterm_panels.install(_ctx())
    iterm_panels.uninstall(_ctx())
    assert system.prefs["LanguageAgnosticKeyBindings"] is True
    assert system.prefs["GlobalKeyMap"] == {
        "0x9-0x40000-0x30": {"Action": 2, "Text": ""},
        "0x7d-0x120000-0x1e-0x1": {"Action": 60, "Text": "session_next()"},
    }


def test_a_second_install_adds_to_the_binding_record(
    system: FakeSystem, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _answer(monkeypatch, True)
    iterm_panels.install(_ctx())
    # The user rebinds one key, then runs the install again and says yes.
    system.prefs["GlobalKeyMap"]["0x7c-0x120000-0x2a-0x1"] = {"Action": 12, "Text": "mine"}
    iterm_panels.install(_ctx())
    recorded = state.load()["components"]["iterm_panels"]["key_bindings"]
    assert recorded == {"keys": sorted(iterm_panels.KEY_BINDINGS), "language_agnostic_set": True}


def test_yes_writes_the_bindings_after_a_backup(
    system: FakeSystem, monkeypatch: pytest.MonkeyPatch, isolated_home: Path,
) -> None:
    _answer(monkeypatch, True)
    iterm_panels.install(_ctx())

    keymap = system.prefs["GlobalKeyMap"]
    for key, (_, function) in iterm_panels.KEY_BINDINGS.items():
        assert keymap[key] == {"Action": 60, "Text": function}
    assert keymap["0x9-0x40000-0x30"] == {"Action": 2, "Text": ""}
    assert system.prefs["LanguageAgnosticKeyBindings"] is True
    backups = list((isolated_home / ".missioncache" / "backups").glob("iterm2-prefs-*.plist"))
    assert len(backups) == 1
    assert plistlib.loads(backups[0].read_bytes())["GlobalKeyMap"] == {
        "0x9-0x40000-0x30": {"Action": 2, "Text": ""}
    }
    first_write = next(i for i, c in enumerate(system.calls) if c[:2] == ["defaults", "write"])
    backup_call = next(
        i for i, c in enumerate(system.calls) if c[:2] == ["defaults", "export"] and c[3] != "-"
    )
    assert backup_call < first_write
    recorded = state.load()["components"]["iterm_panels"]["key_bindings"]
    assert recorded == {"keys": sorted(iterm_panels.KEY_BINDINGS), "language_agnostic_set": True}


def test_bindings_already_set_are_not_asked_again(
    system: FakeSystem, monkeypatch: pytest.MonkeyPatch,
) -> None:
    system.prefs["GlobalKeyMap"] = {
        k: {"Action": 60, "Text": f} for k, (_, f) in iterm_panels.KEY_BINDINGS.items()
    }
    system.prefs["LanguageAgnosticKeyBindings"] = True

    def never(*_a, **_k):
        raise AssertionError("asked about bindings that are already there")

    monkeypatch.setattr(iterm_panels.ui, "ask_yn", never)
    iterm_panels.install(_ctx())
    assert not system.ran("defaults", "write")


def _never(*_a, **_k):
    raise AssertionError("an update asked about key bindings")


def test_update_keeps_the_binding_record_without_asking(
    system: FakeSystem, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _answer(monkeypatch, True)
    iterm_panels.install(_ctx())
    first = state.load()["components"]["iterm_panels"]["key_bindings"]

    monkeypatch.setattr(iterm_panels.ui, "ask_yn", _never)
    iterm_panels.install(_ctx(updating=True))
    assert state.load()["components"]["iterm_panels"]["key_bindings"] == first


def test_update_does_not_ask_again_after_a_no(
    system: FakeSystem, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _answer(monkeypatch, False)
    iterm_panels.install(_ctx())

    monkeypatch.setattr(iterm_panels.ui, "ask_yn", _never)
    iterm_panels.install(_ctx(updating=True))
    assert not system.ran("defaults", "write")
    assert "key_bindings" not in state.load()["components"]["iterm_panels"]


# --- uninstall ---------------------------------------------------------------

def test_uninstall_removes_what_install_added_and_nothing_else(
    system: FakeSystem, monkeypatch: pytest.MonkeyPatch, isolated_home: Path,
) -> None:
    state.record_component("dashboard", {"mode": "pypi"})
    _answer(monkeypatch, True)
    iterm_panels.install(_ctx())
    # The user rebinds one of the three to something else after the install.
    system.prefs["GlobalKeyMap"]["0x7c-0x120000-0x2a-0x1"] = {"Action": 12, "Text": "mine"}

    iterm_panels.uninstall(_ctx())

    plist = isolated_home / "Library" / "LaunchAgents" / "com.missioncache.iterm.plist"
    assert system.ran("launchctl", "unload", str(plist))
    assert not plist.exists()
    assert not (isolated_home / ".missioncache" / "iterm").exists()
    assert list((isolated_home / ".missioncache" / "backups").iterdir())
    assert system.prefs["GlobalKeyMap"] == {
        "0x9-0x40000-0x30": {"Action": 2, "Text": ""},
        "0x7c-0x120000-0x2a-0x1": {"Action": 12, "Text": "mine"},
    }
    assert system.prefs["LanguageAgnosticKeyBindings"] is False
    assert list(state.load()["components"]) == ["dashboard"]

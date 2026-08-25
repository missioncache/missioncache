"""Tests for the Windows dashboard-upgrade lock handling.

Contract (CHANGELOG + the 2026-08-24 live Windows failure): Windows cannot
delete a directory holding a running executable, and the dashboard server
runs out of its own tool directory (uv OR pipx layout - _pipx_install
prefers pipx). Before upgrading, stop every process running from those
roots and VERIFY they are gone; when they are not, refuse the upgrade -
uv removes site-packages before failing on Scripts, so proceeding destroys
the venv. A probe that cannot run is "unknown", never "no blockers". A
failed install must warn, say the install is incomplete, and re-raise
CommandFailed so install_components' per-component isolation keeps working.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from missioncache_install import installers, state, subprocess_utils


def _ok(stdout: str = "") -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr="", returncode=0)


def _capture_ui(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    seen: dict[str, list[str]] = {"warn": [], "detail": [], "success": [], "step": []}
    monkeypatch.setattr(installers.ui, "warn", lambda m, **k: seen["warn"].append(m))
    monkeypatch.setattr(installers.ui, "detail", lambda m, **k: seen["detail"].append(m))
    monkeypatch.setattr(installers.ui, "success", lambda m, **k: seen["success"].append(m))
    monkeypatch.setattr(installers.ui, "step", lambda *a, **k: seen["step"].append(a))
    return seen


def _make_ctx(**overrides) -> installers.InstallContext:
    kwargs = dict(
        mode="pypi", repo_root=None, skip_service=True, port=8787,
        assume_yes=True, mcp_success={},
    )
    kwargs.update(overrides)
    return installers.InstallContext(**kwargs)


class TestProbe:
    def test_parses_pid_and_name_and_passes_safety_kwargs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return _ok("81776|missioncache-dashboard.EXE\n66260|python.exe\n")

        monkeypatch.setattr(installers.subprocess_utils, "run", fake_run)
        assert installers._windows_tool_processes() == [
            (81776, "missioncache-dashboard.EXE"),
            (66260, "python.exe"),
        ]
        # check=True so a failing probe raises instead of masquerading as
        # "no processes"; a timeout so a wedged WMI cannot hang the installer.
        assert captured["kwargs"]["check"] is True
        assert captured["kwargs"]["timeout"] is not None

    def test_roots_travel_via_env_never_in_script(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The O'Brien case: the path must never appear in the script text.
        evil = tmp_path / "O'Brien" / "AppData" / "Roaming"
        monkeypatch.setenv("APPDATA", str(evil))
        monkeypatch.delenv("UV_TOOL_DIR", raising=False)
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured["script"] = cmd[-1]
            captured["env"] = kwargs.get("extra_env")
            return _ok()

        monkeypatch.setattr(installers.subprocess_utils, "run", fake_run)
        installers._windows_tool_processes()
        assert "O'Brien" not in captured["script"]
        roots = captured["env"][installers._ROOTS_ENV].split(";")
        assert any("O'Brien" in root for root in roots)
        # Trailing separator on every root: prefix matching must not reach
        # missioncache-dashboard-backup and friends.
        assert all(root.endswith(("\\", "/")) for root in roots)

    def test_roots_cover_uv_and_pipx_layouts(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("UV_TOOL_DIR", str(tmp_path / "custom-uv"))
        monkeypatch.setenv("PIPX_HOME", str(tmp_path / "custom-pipx"))
        roots = installers._dashboard_tool_roots()
        joined = ";".join(roots)
        assert str(tmp_path / "custom-uv" / "missioncache-dashboard") in joined
        assert str(tmp_path / "custom-pipx" / "venvs" / "missioncache-dashboard") in joined

    def test_empty_appdata_falls_back_to_home(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APPDATA", "")
        monkeypatch.delenv("UV_TOOL_DIR", raising=False)
        roots = installers._dashboard_tool_roots()
        assert str(Path.home()) in roots[0]

    def test_probe_failure_reports_unknown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = _capture_ui(monkeypatch)

        def fake_run(cmd, **kwargs):
            raise subprocess_utils.CommandFailed(list(cmd), 1, "", "Access is denied")

        monkeypatch.setattr(installers.subprocess_utils, "run", fake_run)
        assert installers._windows_tool_processes() is None
        assert any("Access is denied" in w for w in seen["warn"])

    def test_no_processes_is_empty_list_not_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(installers.subprocess_utils, "run", lambda cmd, **k: _ok())
        assert installers._windows_tool_processes() == []

    def test_ignores_junk_and_names_unknown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            installers.subprocess_utils, "run",
            lambda cmd, **k: _ok("header junk\n42|ok.exe\n7|\n"),
        )
        assert installers._windows_tool_processes() == [(42, "ok.exe"), (7, "unknown")]


class TestClearProcesses:
    def test_non_windows_is_a_clear_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        called: list = []
        monkeypatch.setattr(
            installers, "_windows_tool_processes", lambda **k: called.append(1)
        )
        assert installers._clear_dashboard_processes() == ("clear", [])
        assert called == []

    def test_unknown_probe_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(installers.sys, "platform", "win32")
        monkeypatch.setattr(installers, "_windows_tool_processes", lambda **k: None)
        assert installers._clear_dashboard_processes() == ("unknown", [])

    def test_kills_without_tree_flag_then_verifies_clear(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(installers.sys, "platform", "win32")
        probes = iter([[(101, "shim.exe"), (202, "python.exe")], []])
        monkeypatch.setattr(
            installers, "_windows_tool_processes", lambda **k: next(probes)
        )
        kills: list = []
        monkeypatch.setattr(
            installers.subprocess_utils, "run",
            lambda cmd, **k: kills.append(cmd) or _ok(),
        )
        result = installers._clear_dashboard_processes(poll_attempts=1, poll_interval=0)
        assert result == ("clear", [(101, "shim.exe"), (202, "python.exe")])
        assert kills == [
            ["taskkill", "/F", "/PID", "101"],
            ["taskkill", "/F", "/PID", "202"],
        ]
        assert all("/T" not in cmd for cmd in kills)

    def test_failed_kill_is_warned_and_survivors_mean_blocked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(installers.sys, "platform", "win32")
        seen = _capture_ui(monkeypatch)
        monkeypatch.setattr(
            installers, "_windows_tool_processes",
            lambda **k: [(101, "system-owned.exe")],
        )
        monkeypatch.setattr(
            installers.subprocess_utils, "run",
            lambda cmd, **k: SimpleNamespace(
                stdout="", stderr="Access is denied.", returncode=1
            ),
        )
        result = installers._clear_dashboard_processes(poll_attempts=1, poll_interval=0)
        assert result == ("blocked", [(101, "system-owned.exe")])
        assert any("Could not stop system-owned.exe" in w for w in seen["warn"])


class TestInstallDashboardWindows:
    def test_stop_runs_before_install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(installers.sys, "platform", "win32")
        _capture_ui(monkeypatch)
        order: list[str] = []
        monkeypatch.setattr(
            installers, "_clear_dashboard_processes",
            lambda *a, **k: order.append("stop") or ("clear", []),
        )
        monkeypatch.setattr(
            installers, "_pipx_install", lambda pkg: order.append("install")
        )
        monkeypatch.setattr(installers, "_register_dashboard_service", lambda port: None)
        monkeypatch.setattr(installers.settings, "ensure_edit_count_hook", lambda: False)
        monkeypatch.setattr(installers.state, "record_component", lambda *a, **k: None)
        installers.install_dashboard(_make_ctx())
        assert order == ["stop", "install"]

    def test_blocked_refuses_the_destructive_install(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(installers.sys, "platform", "win32")
        _capture_ui(monkeypatch)
        monkeypatch.setattr(
            installers, "_clear_dashboard_processes",
            lambda *a, **k: ("blocked", [(9, "stuck.exe")]),
        )
        monkeypatch.setattr(
            installers, "_windows_tool_processes", lambda **k: [(9, "stuck.exe")]
        )
        installed: list = []
        monkeypatch.setattr(
            installers, "_pipx_install", lambda pkg: installed.append(pkg)
        )
        with pytest.raises(subprocess_utils.CommandFailed):
            installers.install_dashboard(_make_ctx())
        assert installed == [], "a blocked upgrade must never reach the installer"

    def test_failed_install_retries_then_warns_and_reraises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(installers.sys, "platform", "win32")
        monkeypatch.setattr(installers.time, "sleep", lambda s: None)
        seen = _capture_ui(monkeypatch)
        clears: list[int] = []
        monkeypatch.setattr(
            installers, "_clear_dashboard_processes",
            lambda *a, **k: clears.append(1) or ("clear", []),
        )
        monkeypatch.setattr(
            installers, "_windows_tool_processes", lambda **k: [(9, "locker.exe")]
        )
        installs: list[int] = []

        def raising_install(pkg):
            installs.append(1)
            raise subprocess_utils.CommandFailed(["uv"], 2, "", "")

        monkeypatch.setattr(installers, "_pipx_install", raising_install)
        with pytest.raises(subprocess_utils.CommandFailed):
            installers.install_dashboard(_make_ctx())
        # 3 attempts on win32, with a re-clear between each (statusline race),
        # plus the initial pre-install clear.
        assert len(installs) == 3
        assert len(clears) == 3
        assert any("locker.exe" in w for w in seen["warn"])
        assert any("incomplete" in d and "down right now" in d for d in seen["detail"])

    def test_transient_locker_cleared_by_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A statusline render holding the venv for one attempt must not fail
        the install - the second attempt succeeds after a re-clear."""
        monkeypatch.setattr(installers.sys, "platform", "win32")
        monkeypatch.setattr(installers.time, "sleep", lambda s: None)
        _capture_ui(monkeypatch)
        monkeypatch.setattr(
            installers, "_clear_dashboard_processes", lambda *a, **k: ("clear", [])
        )
        attempts: list[int] = []

        def flaky_install(pkg):
            attempts.append(1)
            if len(attempts) == 1:
                raise subprocess_utils.CommandFailed(["uv"], 2, "", "")

        monkeypatch.setattr(installers, "_pipx_install", flaky_install)
        monkeypatch.setattr(installers, "_register_dashboard_service", lambda port: None)
        monkeypatch.setattr(installers.settings, "ensure_edit_count_hook", lambda: False)
        monkeypatch.setattr(installers.state, "record_component", lambda *a, **k: None)
        installers.install_dashboard(_make_ctx())
        assert len(attempts) == 2

    def test_posix_install_failure_does_not_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _capture_ui(monkeypatch)
        installs: list[int] = []

        def raising_install(pkg):
            installs.append(1)
            raise subprocess_utils.CommandFailed(["uv"], 2, "", "")

        monkeypatch.setattr(installers, "_pipx_install", raising_install)
        with pytest.raises(subprocess_utils.CommandFailed):
            installers.install_dashboard(_make_ctx())
        assert len(installs) == 1, "POSIX behavior must be unchanged: one attempt"

    def test_failure_isolation_keeps_later_components(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A Windows dashboard failure must not abort install_components -
        the exact regression ui.fail's sys.exit caused in the first draft."""
        monkeypatch.setattr(installers.sys, "platform", "win32")
        monkeypatch.setattr(installers.time, "sleep", lambda s: None)
        _capture_ui(monkeypatch)
        monkeypatch.setattr(
            installers, "_clear_dashboard_processes", lambda *a, **k: ("clear", [])
        )
        monkeypatch.setattr(installers, "_windows_tool_processes", lambda **k: [])

        def raising_install(pkg):
            raise subprocess_utils.CommandFailed(["uv"], 2, "", "")

        monkeypatch.setattr(installers, "_pipx_install", raising_install)
        ran: list[str] = []
        monkeypatch.setitem(
            installers._INSTALLERS, "rules", lambda ctx: ran.append("rules")
        )
        failed = installers.install_components(["dashboard", "rules"], _make_ctx())
        assert failed == ["dashboard"]
        assert ran == ["rules"], "components after a failed dashboard must still run"
        assert state.failed_components() == ["dashboard"]

    def test_skip_service_warns_that_nothing_restarts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(installers.sys, "platform", "win32")
        seen = _capture_ui(monkeypatch)
        monkeypatch.setattr(
            installers, "_clear_dashboard_processes",
            lambda *a, **k: ("clear", [(5, "dash.exe")]),
        )
        monkeypatch.setattr(installers, "_pipx_install", lambda pkg: None)
        monkeypatch.setattr(installers.settings, "ensure_edit_count_hook", lambda: False)
        monkeypatch.setattr(installers.state, "record_component", lambda *a, **k: None)
        installers.install_dashboard(_make_ctx(skip_service=True))
        assert any("not restarted" in w for w in seen["warn"])

    def test_non_windows_never_probes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _capture_ui(monkeypatch)
        probed: list = []
        monkeypatch.setattr(
            installers, "_windows_tool_processes", lambda: probed.append(1)
        )
        monkeypatch.setattr(installers, "_pipx_install", lambda pkg: None)
        monkeypatch.setattr(installers, "_register_dashboard_service", lambda port: None)
        monkeypatch.setattr(installers.settings, "ensure_edit_count_hook", lambda: False)
        monkeypatch.setattr(installers.state, "record_component", lambda *a, **k: None)
        installers.install_dashboard(_make_ctx(skip_service=False))
        assert probed == []


class TestUnknownAndPollPaths:
    def test_unknown_probe_warns_and_proceeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(installers.sys, "platform", "win32")
        seen = _capture_ui(monkeypatch)
        monkeypatch.setattr(
            installers, "_clear_dashboard_processes", lambda *a, **k: ("unknown", [])
        )
        installed: list[str] = []
        monkeypatch.setattr(
            installers, "_pipx_install", lambda pkg: installed.append(pkg)
        )
        monkeypatch.setattr(installers, "_register_dashboard_service", lambda port: None)
        monkeypatch.setattr(installers.settings, "ensure_edit_count_hook", lambda: False)
        monkeypatch.setattr(installers.state, "record_component", lambda *a, **k: None)
        installers.install_dashboard(_make_ctx())
        assert installed == ["missioncache-dashboard"]
        assert any("without the pre-check" in w for w in seen["warn"])

    def test_report_handles_unknown_blockers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = _capture_ui(monkeypatch)
        monkeypatch.setattr(installers, "_windows_tool_processes", lambda **k: None)
        installers._report_windows_dashboard_failure()
        assert any("could not be checked" in w for w in seen["warn"])
        assert any("down right now" in d for d in seen["detail"])

    def test_poll_success_on_later_attempt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(installers.sys, "platform", "win32")
        monkeypatch.setattr(installers.time, "sleep", lambda s: None)
        _capture_ui(monkeypatch)
        probes = iter([
            [(1, "a.exe")],      # initial enumeration
            [(1, "a.exe")],      # poll 1: handle still open
            [],                   # poll 2: released
        ])
        monkeypatch.setattr(
            installers, "_windows_tool_processes", lambda **k: next(probes)
        )
        monkeypatch.setattr(
            installers.subprocess_utils, "run", lambda cmd, **k: _ok()
        )
        result = installers._clear_dashboard_processes(poll_attempts=5, poll_interval=0)
        assert result == ("clear", [(1, "a.exe")])

    def test_probe_dying_mid_poll_is_unknown_not_blocked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(installers.sys, "platform", "win32")
        _capture_ui(monkeypatch)
        probes = iter([[(1, "a.exe")], None])
        monkeypatch.setattr(
            installers, "_windows_tool_processes", lambda **k: next(probes)
        )
        monkeypatch.setattr(
            installers.subprocess_utils, "run", lambda cmd, **k: _ok()
        )
        result = installers._clear_dashboard_processes(poll_attempts=5, poll_interval=0)
        assert result == ("unknown", [(1, "a.exe")])

    def test_retry_clear_coming_back_blocked_refuses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The never-upgrade-while-blocked invariant must hold on retries too."""
        monkeypatch.setattr(installers.sys, "platform", "win32")
        monkeypatch.setattr(installers.time, "sleep", lambda s: None)
        _capture_ui(monkeypatch)
        clears = iter([("clear", []), ("blocked", [(3, "back.exe")])])
        monkeypatch.setattr(
            installers, "_clear_dashboard_processes", lambda *a, **k: next(clears)
        )
        installs: list[int] = []

        def raising_install(pkg):
            installs.append(1)
            raise subprocess_utils.CommandFailed(["uv"], 2, "", "")

        monkeypatch.setattr(installers, "_pipx_install", raising_install)
        with pytest.raises(installers.DashboardUpgradeBlocked):
            installers.install_dashboard(_make_ctx())
        assert len(installs) == 1, "a blocked retry-clear must stop further attempts"


class TestRunExtraEnv:
    def test_extra_env_layers_over_inherited_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The child must see BOTH the extra vars and the inherited ones.

        env={extra_only} would pass a naive echo test while dropping PATH and
        SystemRoot, which breaks PowerShell on Windows outright - so the
        assertion that distinguishes the implementations is the inherited var.
        """
        import sys

        monkeypatch.setenv("MC_INHERITED_Y", "kept")
        result = subprocess_utils.run(
            [
                sys.executable, "-c",
                "import os; print(os.environ['MC_PROBE_X'], os.environ['MC_INHERITED_Y'])",
            ],
            extra_env={"MC_PROBE_X": "value-42"},
        )
        assert result.stdout.split() == ["value-42", "kept"]

    def test_no_extra_env_inherits_everything(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sys

        monkeypatch.setenv("MC_INHERITED_Z", "still-there")
        result = subprocess_utils.run(
            [sys.executable, "-c", "import os; print(os.environ['MC_INHERITED_Z'])"],
        )
        assert result.stdout.strip() == "still-there"

"""Tests for missioncache_dashboard.cli."""

import socket
import subprocess

import pytest

from missioncache_dashboard import cli


# --- Template rendering -------------------------------------------------------


class TestRenderPlist:
    def test_default_port_omits_env_block(self):
        out = cli.render_plist("/usr/local/bin/missioncache-dashboard", cli.DEFAULT_PORT)
        assert "com.missioncache.dashboard" in out
        assert "/usr/local/bin/missioncache-dashboard" in out
        assert "<string>serve</string>" in out
        assert "EnvironmentVariables" not in out

    def test_custom_port_adds_env_block(self):
        out = cli.render_plist("/usr/local/bin/missioncache-dashboard", 9000)
        assert "EnvironmentVariables" in out
        assert "MISSIONCACHE_DASHBOARD_PORT" in out
        assert "<string>9000</string>" in out

    def test_includes_log_paths(self):
        out = cli.render_plist("/bin/missioncache-dashboard", cli.DEFAULT_PORT)
        assert "missioncache-dashboard-stdout.log" in out
        assert "missioncache-dashboard-stderr.log" in out


class TestRenderSystemdUnit:
    def test_default_port_omits_env_line(self):
        out = cli.render_systemd_unit("/usr/local/bin/missioncache-dashboard", cli.DEFAULT_PORT)
        assert "ExecStart=/usr/local/bin/missioncache-dashboard serve" in out
        assert "Environment=" not in out

    def test_custom_port_adds_env_line(self):
        out = cli.render_systemd_unit("/usr/local/bin/missioncache-dashboard", 9000)
        assert "Environment=MISSIONCACHE_DASHBOARD_PORT=9000" in out

    def test_restart_always(self):
        out = cli.render_systemd_unit("/bin/missioncache-dashboard", cli.DEFAULT_PORT)
        assert "Restart=always" in out
        assert "WantedBy=default.target" in out


# --- Port probing -------------------------------------------------------------


class TestPortInUse:
    def test_free_port_returns_false(self):
        # Bind 0 to let the OS give us a port, close it, then test it's free.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            free_port = sock.getsockname()[1]
        assert cli.port_in_use(free_port) is False

    def test_bound_port_returns_true(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            port = sock.getsockname()[1]
            assert cli.port_in_use(port) is True


class TestResolvePort:
    def test_free_port_returned_as_is(self, monkeypatch):
        monkeypatch.setattr(cli, "port_in_use", lambda p: False)
        assert cli.resolve_port(8787) == 8787


# --- Platform dispatch --------------------------------------------------------


class TestWindowsTaskRun:
    """The autostart command line (schtasks /TR and Run-key value alike)."""

    def test_quotes_binary_and_adds_hidden(self):
        cmd = cli.windows_task_run(r"C:\Users\Tomer Brami\Scripts\missioncache-dashboard.exe", cli.DEFAULT_PORT)
        assert cmd == r'"C:\Users\Tomer Brami\Scripts\missioncache-dashboard.exe" serve --hidden'

    def test_non_default_port_rides_on_the_command_line(self):
        """Neither schtasks nor a Run key can set env vars - the flag is the
        only channel for a non-default port."""
        cmd = cli.windows_task_run("d.exe", 9000)
        assert cmd == '"d.exe" serve --hidden --port 9000'


class TestInstallServiceWindows:
    def _capture_runs(self, monkeypatch, schtasks_rc, reg_add_rc=0):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[0] == "schtasks" and "/Create" in cmd:
                rc = schtasks_rc
            elif cmd[:2] == ["reg", "add"]:
                rc = reg_add_rc
            else:
                rc = 0
            # Honor check=True the way subprocess.run does, so a failing reg add
            # on the fallback path surfaces as CalledProcessError in tests just
            # as it would in production - the double-failure case a non-elevated
            # user reaches.
            if rc != 0 and kwargs.get("check"):
                raise subprocess.CalledProcessError(rc, cmd)
            return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="denied" if rc else "")

        monkeypatch.setattr(cli.subprocess, "run", fake_run)
        monkeypatch.setattr(cli, "resolve_binary", lambda: "d.exe")
        # Report the port as busy so install skips the immediate-start Popen.
        monkeypatch.setattr(cli, "port_in_use", lambda p: True)
        return calls

    def test_schtasks_success_creates_onlogon_task(self, monkeypatch, capsys):
        calls = self._capture_runs(monkeypatch, schtasks_rc=0)
        cli.install_windows(cli.DEFAULT_PORT)
        create = next(c for c in calls if c[0] == "schtasks")
        assert "/SC" in create and "ONLOGON" in create
        assert cli.windows_task_run("d.exe", cli.DEFAULT_PORT) in create
        assert "Task Scheduler task created" in capsys.readouterr().out

    def test_schtasks_success_cleans_lingering_run_key(self, monkeypatch):
        """A prior non-elevated install may have left a Run key; the success
        path must delete it so both mechanisms do not fire at logon."""
        calls = self._capture_runs(monkeypatch, schtasks_rc=0)
        cli.install_windows(cli.DEFAULT_PORT)
        assert any(c[:2] == ["reg", "delete"] for c in calls)

    def test_schtasks_refusal_falls_back_to_run_key(self, monkeypatch, capsys):
        """A non-elevated prompt gets Access Denied from schtasks ONLOGON;
        the HKCU Run key needs no elevation and must carry the SAME command."""
        calls = self._capture_runs(monkeypatch, schtasks_rc=1)
        cli.install_windows(cli.DEFAULT_PORT)
        reg_add = next(c for c in calls if c[:2] == ["reg", "add"])
        assert cli.RUN_KEY in reg_add
        assert cli.windows_task_run("d.exe", cli.DEFAULT_PORT) in reg_add
        out = capsys.readouterr().out
        assert "Run-key autostart" in out
        # The fallback must not pass check=True on reg add - it degrades with a
        # message instead of a traceback (verified by the no-raise below).

    def test_double_failure_degrades_with_message_not_traceback(self, monkeypatch, capsys):
        """schtasks refuses AND the Run-key write fails (the locked-down box):
        the install must print guidance, never raise CalledProcessError."""
        self._capture_runs(monkeypatch, schtasks_rc=1, reg_add_rc=1)
        cli.install_windows(cli.DEFAULT_PORT)  # must not raise
        out = capsys.readouterr().out
        assert "Could not register autostart" in out
        assert "serve" in out

    def test_immediate_start_spawns_hidden_serve(self, monkeypatch, tmp_path):
        """When the port is free, install starts the server now: no window,
        output to the log file, port in the env."""
        self._capture_runs(monkeypatch, schtasks_rc=0)
        monkeypatch.setattr(cli, "port_in_use", lambda p: False)
        monkeypatch.setattr(cli, "windows_log_path", lambda: tmp_path / "d.log")
        monkeypatch.setattr(cli, "log_dir", lambda: tmp_path)
        popens = []

        class _FakePopen:
            def __init__(self, cmd, **kw):
                popens.append((cmd, kw))

        monkeypatch.setattr(cli.subprocess, "Popen", _FakePopen)
        cli.install_windows(9000)
        assert len(popens) == 1
        cmd, kw = popens[0]
        assert cmd == ["d.exe", "serve"]
        assert kw["env"]["MISSIONCACHE_DASHBOARD_PORT"] == "9000"

    def test_cmd_install_service_dispatches_win32(self, monkeypatch):
        monkeypatch.setattr(cli.sys, "platform", "win32")
        monkeypatch.setattr(cli, "resolve_port", lambda p: p)
        seen = {}
        monkeypatch.setattr(cli, "install_windows", lambda port: seen.setdefault("port", port))
        args = cli.build_parser().parse_args(["install-service"])
        assert cli.cmd_install_service(args) == 0
        assert seen["port"] == cli.DEFAULT_PORT

    def test_install_service_accepts_port_flag(self, monkeypatch):
        """missioncache-install invokes `install-service --port N` for every
        non-default port; the parser rejecting the flag silently left users
        without autostart (pre-existing, surfaced by the Windows review)."""
        monkeypatch.setattr(cli.sys, "platform", "win32")
        monkeypatch.setattr(cli, "resolve_port", lambda p: p)
        seen = {}
        monkeypatch.setattr(cli, "install_windows", lambda port: seen.setdefault("port", port))
        args = cli.build_parser().parse_args(["install-service", "--port", "9000"])
        assert cli.cmd_install_service(args) == 0
        assert seen["port"] == 9000

    def test_reinstall_service_accepts_port_flag(self):
        args = cli.build_parser().parse_args(["reinstall-service", "--port", "9000"])
        assert args.port == 9000


class TestUninstallServiceWindows:
    def test_removes_whichever_mechanism_is_present(self, monkeypatch, capsys):
        calls = []
        monkeypatch.setattr(
            cli.subprocess, "run",
            lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
        )
        monkeypatch.setattr(cli, "_schtasks_installed", lambda: True)
        monkeypatch.setattr(cli, "_run_key_installed", lambda: True)
        cli.uninstall_windows()
        assert any(c[:2] == ["schtasks", "/Delete"] for c in calls)
        assert any(c[:2] == ["reg", "delete"] for c in calls)
        # Platform parity: launchctl unload / systemctl --now / pkill all stop
        # the running process; the Windows branch must too, or a reinstall
        # keeps serving stale code behind the occupied port.
        assert any(c[0] == "taskkill" for c in calls)

    def test_nothing_installed_does_not_kill(self, monkeypatch, capsys):
        """A no-op uninstall must not taskkill a server it never managed."""
        calls = []
        monkeypatch.setattr(
            cli.subprocess, "run",
            lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
        )
        monkeypatch.setattr(cli, "_schtasks_installed", lambda: False)
        monkeypatch.setattr(cli, "_run_key_installed", lambda: False)
        cli.uninstall_windows()
        assert not any(c[0] == "taskkill" for c in calls)

    def test_nothing_installed_is_a_clean_noop(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_schtasks_installed", lambda: False)
        monkeypatch.setattr(cli, "_run_key_installed", lambda: False)
        cli.uninstall_windows()
        assert "nothing to do" in capsys.readouterr().out

    def test_cmd_uninstall_service_dispatches_win32(self, monkeypatch):
        monkeypatch.setattr(cli.sys, "platform", "win32")
        called = []
        monkeypatch.setattr(cli, "uninstall_windows", lambda: called.append(True))
        args = cli.build_parser().parse_args(["uninstall-service"])
        assert cli.cmd_uninstall_service(args) == 0
        assert called


class TestStatusWindows:
    def test_reports_installed_and_running(self, monkeypatch, capsys):
        monkeypatch.setattr(cli.sys, "platform", "win32")
        monkeypatch.setattr(cli, "_schtasks_installed", lambda: False)
        monkeypatch.setattr(cli, "_run_key_installed", lambda: True)
        monkeypatch.setattr(cli, "port_in_use", lambda p: True)
        args = cli.build_parser().parse_args(["status"])
        rc = cli.cmd_status(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Installed: True" in out
        assert "Running:   True" in out


# --- Binary resolution --------------------------------------------------------


class TestResolveBinary:
    def test_returns_which_result(self, monkeypatch):
        monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/local/bin/missioncache-dashboard")
        assert cli.resolve_binary() == "/usr/local/bin/missioncache-dashboard"

    def test_raises_when_not_on_path(self, monkeypatch):
        monkeypatch.setattr(cli.shutil, "which", lambda name: None)
        with pytest.raises(SystemExit, match="Could not find"):
            cli.resolve_binary()


# --- Profile autostart (systemd-less Linux / WSL) -----------------------------


class TestAutostartBlock:
    def test_render_contains_markers_and_guarded_start(self):
        out = cli.render_autostart_block(8787)
        assert out.startswith(cli.AUTOSTART_BEGIN)
        assert out.rstrip("\n").endswith(cli.AUTOSTART_END)
        assert "pgrep -f 'missioncache-dashboard serve'" in out
        assert "MISSIONCACHE_DASHBOARD_PORT=8787" in out
        assert "nohup missioncache-dashboard serve" in out

    def test_strip_removes_only_managed_block(self):
        text = (
            "export PATH=$PATH:/opt/x\n"
            + cli.render_autostart_block(8787)
            + "alias ll='ls -la'\n"
        )
        out = cli._strip_autostart_block(text)
        assert "export PATH" in out
        assert "alias ll" in out
        assert cli.AUTOSTART_BEGIN not in out
        assert "nohup" not in out

    def test_strip_handles_torn_block(self):
        """A hand-mangled block missing its end marker must not survive as a
        half-managed fragment."""
        text = "keep this\n" + cli.AUTOSTART_BEGIN + "\nnohup something &\n"
        out = cli._strip_autostart_block(text)
        assert "keep this" in out
        assert cli.AUTOSTART_BEGIN not in out
        assert "nohup" not in out

    def test_strip_noop_without_block(self):
        text = "export PATH=$PATH:/opt/x\n"
        assert cli._strip_autostart_block(text) == text


class TestProfileAutostart:
    def _setup(self, tmp_path, monkeypatch, port_busy=True):
        monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(cli, "resolve_binary", lambda: "/fake/bin/missioncache-dashboard")
        # port "in use" -> the immediate background start is skipped, keeping
        # these tests process-free.
        monkeypatch.setattr(cli, "port_in_use", lambda port: port_busy)

    def test_install_writes_block_and_removes_orphan_unit(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        unit = tmp_path / ".config" / "systemd" / "user" / cli.SYSTEMD_UNIT
        unit.parent.mkdir(parents=True)
        unit.write_text("[Unit]")  # leftover from a pre-fix install

        cli.install_profile_autostart(8787)

        profile = tmp_path / ".profile"
        assert cli.AUTOSTART_BEGIN in profile.read_text()
        assert not unit.exists(), "pre-fix orphan unit must be cleaned up"

    def test_install_idempotent_single_block(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        cli.install_profile_autostart(8787)
        cli.install_profile_autostart(9000)

        text = (tmp_path / ".profile").read_text()
        assert text.count(cli.AUTOSTART_BEGIN) == 1
        assert "MISSIONCACHE_DASHBOARD_PORT=9000" in text
        assert "MISSIONCACHE_DASHBOARD_PORT=8787" not in text

    def test_install_preserves_existing_profile_content(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        (tmp_path / ".profile").write_text("export EDITOR=vim\n")

        cli.install_profile_autostart(8787)

        text = (tmp_path / ".profile").read_text()
        assert text.startswith("export EDITOR=vim\n")
        assert cli.AUTOSTART_BEGIN in text

    def test_bash_profile_preferred_when_exists(self, tmp_path, monkeypatch):
        """bash reads ~/.bash_profile INSTEAD of ~/.profile when present -
        writing to .profile there would never execute."""
        self._setup(tmp_path, monkeypatch)
        (tmp_path / ".bash_profile").write_text("# user bash profile\n")

        cli.install_profile_autostart(8787)

        assert cli.AUTOSTART_BEGIN in (tmp_path / ".bash_profile").read_text()
        assert not (tmp_path / ".profile").exists()

    def test_uninstall_removes_block_and_leaves_rest(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        calls = []
        monkeypatch.setattr(
            cli.subprocess, "run", lambda cmd, **kw: calls.append(cmd)
        )
        (tmp_path / ".profile").write_text("export EDITOR=vim\n")
        cli.install_profile_autostart(8787)

        cli.uninstall_profile_autostart()

        text = (tmp_path / ".profile").read_text()
        assert cli.AUTOSTART_BEGIN not in text
        assert "export EDITOR=vim" in text
        assert ["pkill", "-f", "missioncache-dashboard serve"] in calls


class TestInstallLinuxDispatch:
    def test_no_systemd_uses_profile_autostart(self, monkeypatch):
        monkeypatch.setattr(cli, "systemd_available", lambda: False)
        called = []
        monkeypatch.setattr(cli, "install_profile_autostart", lambda port: called.append(port))
        monkeypatch.setattr(
            cli, "install_systemd",
            lambda port: (_ for _ in ()).throw(AssertionError("must not touch systemctl")),
        )

        cli.install_linux(8787)

        assert called == [8787]

    def test_systemctl_failure_falls_back_without_traceback(self, monkeypatch):
        """systemd present but the user session is broken: degrade to the
        profile mechanism instead of crashing mid-install (the WSL bug)."""
        import subprocess as sp

        monkeypatch.setattr(cli, "systemd_available", lambda: True)
        monkeypatch.setattr(
            cli, "install_systemd",
            lambda port: (_ for _ in ()).throw(sp.CalledProcessError(1, ["systemctl"])),
        )
        called = []
        monkeypatch.setattr(cli, "install_profile_autostart", lambda port: called.append(port))

        cli.install_linux(8787)  # must not raise

        assert called == [8787]

    def test_systemd_success_skips_fallback(self, monkeypatch):
        monkeypatch.setattr(cli, "systemd_available", lambda: True)
        monkeypatch.setattr(cli, "install_systemd", lambda port: None)
        monkeypatch.setattr(
            cli, "install_profile_autostart",
            lambda port: (_ for _ in ()).throw(AssertionError("fallback must not run")),
        )

        cli.install_linux(8787)


class TestProfileAutostartImmediateStart:
    def test_install_starts_dashboard_when_port_free(self, tmp_path, monkeypatch):
        """The immediate background start: correct binary argv, port in env,
        detached session - this is the 'works the moment install finishes'
        half of the fallback."""
        monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(cli, "resolve_binary", lambda: "/fake/bin/missioncache-dashboard")
        monkeypatch.setattr(cli, "port_in_use", lambda port: False)
        spawned = {}

        def fake_popen(argv, **kw):
            spawned["argv"] = argv
            spawned["env"] = kw.get("env", {})
            spawned["detached"] = kw.get("start_new_session", False)

        monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)

        cli.install_profile_autostart(9000)

        assert spawned["argv"] == ["/fake/bin/missioncache-dashboard", "serve"]
        assert spawned["env"]["MISSIONCACHE_DASHBOARD_PORT"] == "9000"
        assert spawned["detached"] is True


class TestStatusProfileAutostart:
    def test_status_recognizes_profile_install(self, tmp_path, monkeypatch, capsys):
        """status must not report 'not installed' on the systemd-less machine
        the profile mechanism exists for."""
        monkeypatch.setattr(cli.sys, "platform", "linux")
        monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(cli, "resolve_binary", lambda: "/fake/bin/missioncache-dashboard")
        monkeypatch.setattr(cli, "port_in_use", lambda port: True)
        cli.install_profile_autostart(8787)

        rc = cli.cmd_status(None)

        out = capsys.readouterr().out
        assert rc == 0
        assert "Installed: True" in out
        assert "Running:   True" in out


class TestResolvePortSelfRecognition:
    """resolve_port must not treat our own running dashboard as a conflict -
    that is the normal state during an update on a working machine."""

    def test_own_dashboard_on_port_is_not_a_conflict(self, monkeypatch, capsys):
        from missioncache_dashboard import cli

        monkeypatch.setattr(cli, "port_in_use", lambda port: True)
        monkeypatch.setattr(cli, "_is_missioncache_dashboard", lambda port: True)
        monkeypatch.setattr(
            "builtins.input",
            lambda *a: (_ for _ in ()).throw(AssertionError("must not prompt")),
        )

        assert cli.resolve_port(8787) == 8787
        assert "running MissionCache dashboard" in capsys.readouterr().out

    def test_foreign_occupant_still_prompts(self, monkeypatch):
        from missioncache_dashboard import cli

        monkeypatch.setattr(cli, "port_in_use", lambda port: port == 8787)
        monkeypatch.setattr(cli, "_is_missioncache_dashboard", lambda port: False)
        monkeypatch.setattr("builtins.input", lambda *a: "8790")

        assert cli.resolve_port(8787) == 8790


class TestCmdServePortPrecedence:
    """serve's port comes from --port, then MISSIONCACHE_DASHBOARD_PORT, then
    the default. windows_task_run emits --port only for non-default ports, so
    an inverted precedence would quietly serve the wrong port on Windows only."""

    def _run_and_capture_port(self, monkeypatch, args_list, env=None):
        import sys as _sys
        import types

        captured = {}
        fake_uvicorn = types.ModuleType("uvicorn")
        fake_uvicorn.run = lambda app, host, port: captured.update(port=port)
        monkeypatch.setitem(_sys.modules, "uvicorn", fake_uvicorn)
        if env is None:
            monkeypatch.delenv("MISSIONCACHE_DASHBOARD_PORT", raising=False)
        else:
            monkeypatch.setenv("MISSIONCACHE_DASHBOARD_PORT", env)
        args = cli.build_parser().parse_args(args_list)
        cli.cmd_serve(args)
        return captured["port"]

    def test_flag_wins(self, monkeypatch):
        assert self._run_and_capture_port(monkeypatch, ["serve", "--port", "9001"], env="9002") == 9001

    def test_env_used_when_no_flag(self, monkeypatch):
        assert self._run_and_capture_port(monkeypatch, ["serve"], env="9003") == 9003

    def test_default_when_neither(self, monkeypatch):
        assert self._run_and_capture_port(monkeypatch, ["serve"]) == cli.DEFAULT_PORT

    def test_explicit_port_zero_is_honored(self, monkeypatch):
        """--port 0 (ask OS for a free port) must not be dropped back to env."""
        assert self._run_and_capture_port(monkeypatch, ["serve", "--port", "0"], env="9004") == 0

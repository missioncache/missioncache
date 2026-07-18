"""Tests for missioncache_dashboard.cli."""

import socket

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


class TestInstallServiceWindows:
    def test_exits_zero_with_manual_instructions(self, monkeypatch, capsys):
        monkeypatch.setattr(cli.sys, "platform", "win32")
        monkeypatch.setattr(cli, "resolve_port", lambda p: p)

        # Build args via the real parser so we're not hand-rolling Namespace shape
        args = cli.build_parser().parse_args(["install-service"])
        rc = cli.cmd_install_service(args)
        assert rc == 0
        captured = capsys.readouterr().out
        assert "Windows" in captured
        assert "not yet supported" in captured


class TestUninstallServiceWindows:
    def test_exits_zero_with_message(self, monkeypatch, capsys):
        monkeypatch.setattr(cli.sys, "platform", "win32")
        args = cli.build_parser().parse_args(["uninstall-service"])
        rc = cli.cmd_uninstall_service(args)
        assert rc == 0
        assert "nothing to uninstall" in capsys.readouterr().out


class TestStatusWindows:
    def test_prints_not_supported(self, monkeypatch, capsys):
        monkeypatch.setattr(cli.sys, "platform", "win32")
        args = cli.build_parser().parse_args(["status"])
        rc = cli.cmd_status(args)
        assert rc == 0
        assert "not supported" in capsys.readouterr().out


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

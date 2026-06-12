"""Tests for orbit_dashboard.cli."""

import socket

import pytest

from orbit_dashboard import cli


# --- Template rendering -------------------------------------------------------


class TestRenderPlist:
    def test_default_port_omits_env_block(self):
        out = cli.render_plist("/usr/local/bin/orbit-dashboard", cli.DEFAULT_PORT)
        assert "com.orbit.dashboard" in out
        assert "/usr/local/bin/orbit-dashboard" in out
        assert "<string>serve</string>" in out
        assert "EnvironmentVariables" not in out

    def test_custom_port_adds_env_block(self):
        out = cli.render_plist("/usr/local/bin/orbit-dashboard", 9000)
        assert "EnvironmentVariables" in out
        assert "ORBIT_DASHBOARD_PORT" in out
        assert "<string>9000</string>" in out

    def test_includes_log_paths(self):
        out = cli.render_plist("/bin/orbit-dashboard", cli.DEFAULT_PORT)
        assert "orbit-dashboard-stdout.log" in out
        assert "orbit-dashboard-stderr.log" in out


class TestRenderSystemdUnit:
    def test_default_port_omits_env_line(self):
        out = cli.render_systemd_unit("/usr/local/bin/orbit-dashboard", cli.DEFAULT_PORT)
        assert "ExecStart=/usr/local/bin/orbit-dashboard serve" in out
        assert "Environment=" not in out

    def test_custom_port_adds_env_line(self):
        out = cli.render_systemd_unit("/usr/local/bin/orbit-dashboard", 9000)
        assert "Environment=ORBIT_DASHBOARD_PORT=9000" in out

    def test_restart_always(self):
        out = cli.render_systemd_unit("/bin/orbit-dashboard", cli.DEFAULT_PORT)
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
        monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/local/bin/orbit-dashboard")
        assert cli.resolve_binary() == "/usr/local/bin/orbit-dashboard"

    def test_raises_when_not_on_path(self, monkeypatch):
        monkeypatch.setattr(cli.shutil, "which", lambda name: None)
        with pytest.raises(SystemExit, match="Could not find"):
            cli.resolve_binary()

"""Orbit Dashboard CLI.

Entry point for the `orbit-dashboard` console script. Subcommands:

    orbit-dashboard serve               Run the dashboard (default).
    orbit-dashboard install-service     Register as launchd / systemd service.
    orbit-dashboard uninstall-service   Remove the service.
    orbit-dashboard reinstall-service   Uninstall + install (Python path fix).
    orbit-dashboard status              Show installed / running state.

Platform support: macOS (launchd) and Linux (systemd --user). Windows
prints manual instructions and exits 0 - Task Scheduler support is
deferred.
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

LAUNCHD_LABEL = "com.orbit.dashboard"
SYSTEMD_UNIT = "orbit-dashboard.service"
DEFAULT_PORT = 8787


# =============================================================================
# Paths
# =============================================================================


def launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def systemd_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / SYSTEMD_UNIT


def log_dir() -> Path:
    return Path.home() / ".claude" / "logs"


# =============================================================================
# Templates (pure, testable)
# =============================================================================


def render_plist(binary_path: str, port: int) -> str:
    """Render the launchd plist pointing at the pip-installed binary."""
    logs = log_dir()
    env_block = ""
    if port != DEFAULT_PORT:
        env_block = (
            "    <key>EnvironmentVariables</key>\n"
            "    <dict>\n"
            f"        <key>ORBIT_DASHBOARD_PORT</key>\n"
            f"        <string>{port}</string>\n"
            "    </dict>\n"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "    <key>Label</key>\n"
        f"    <string>{LAUNCHD_LABEL}</string>\n"
        "    <key>ProgramArguments</key>\n"
        "    <array>\n"
        f"        <string>{binary_path}</string>\n"
        "        <string>serve</string>\n"
        "    </array>\n"
        "    <key>RunAtLoad</key>\n"
        "    <true/>\n"
        "    <key>KeepAlive</key>\n"
        "    <true/>\n"
        "    <key>StandardOutPath</key>\n"
        f"    <string>{logs / 'orbit-dashboard-stdout.log'}</string>\n"
        "    <key>StandardErrorPath</key>\n"
        f"    <string>{logs / 'orbit-dashboard-stderr.log'}</string>\n"
        f"{env_block}"
        "</dict>\n"
        "</plist>\n"
    )


def render_systemd_unit(binary_path: str, port: int) -> str:
    """Render the systemd user unit pointing at the pip-installed binary."""
    env_line = f"Environment=ORBIT_DASHBOARD_PORT={port}\n" if port != DEFAULT_PORT else ""
    return (
        "[Unit]\n"
        "Description=Orbit Dashboard\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"{env_line}"
        f"ExecStart={binary_path} serve\n"
        "Restart=always\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


# =============================================================================
# Port probing
# =============================================================================


def port_in_use(port: int) -> bool:
    """Return True if TCP port is bound on 127.0.0.1."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def resolve_port(requested: int) -> int:
    """Return a free port, prompting if the requested one is taken."""
    if not port_in_use(requested):
        return requested
    print(f"  Port {requested} is already in use.")
    while True:
        raw = input("  Enter a different port (or blank to abort): ").strip()
        if not raw:
            raise SystemExit("Aborted.")
        try:
            alt = int(raw)
        except ValueError:
            print("  Not a number, try again.")
            continue
        if port_in_use(alt):
            print(f"  Port {alt} is also in use.")
            continue
        return alt


# =============================================================================
# Binary resolution
# =============================================================================


def resolve_binary() -> str:
    """Return the absolute path of the installed `orbit-dashboard` script."""
    found = shutil.which("orbit-dashboard")
    if not found:
        raise SystemExit(
            "Could not find `orbit-dashboard` on PATH. This command must be "
            "run from the same environment where `orbit-dashboard` is pip-"
            "installed (pipx, uv tool, or a venv)."
        )
    return found


# =============================================================================
# Platform install/uninstall
# =============================================================================


def install_launchd(port: int) -> None:
    binary = resolve_binary()
    plist = launchd_plist_path()
    plist.parent.mkdir(parents=True, exist_ok=True)
    log_dir().mkdir(parents=True, exist_ok=True)

    if plist.exists():
        print(f"  Replacing existing service definition at {plist}")
        subprocess.run(["launchctl", "unload", str(plist)], check=False)

    plist.write_text(render_plist(binary, port))
    subprocess.run(["launchctl", "load", str(plist)], check=True)
    print(f"  launchd service loaded: {LAUNCHD_LABEL}")
    print(f"  Logs: {log_dir()}/orbit-dashboard-{{stdout,stderr}}.log")


def uninstall_launchd() -> None:
    plist = launchd_plist_path()
    if not plist.exists():
        print("  launchd service not installed, nothing to do.")
        return
    subprocess.run(["launchctl", "unload", str(plist)], check=False)
    plist.unlink()
    print(f"  Removed {plist}")


def install_systemd(port: int) -> None:
    binary = resolve_binary()
    unit = systemd_unit_path()
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(render_systemd_unit(binary, port))
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT], check=True)
    print(f"  systemd --user unit enabled: {SYSTEMD_UNIT}")


def uninstall_systemd() -> None:
    unit = systemd_unit_path()
    if not unit.exists():
        print("  systemd user unit not installed, nothing to do.")
        return
    subprocess.run(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT], check=False)
    unit.unlink()
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    print(f"  Removed {unit}")


# =============================================================================
# Subcommand handlers
# =============================================================================


def cmd_serve(_args: argparse.Namespace) -> int:
    """Run the dashboard via uvicorn. Reads ORBIT_DASHBOARD_PORT env var."""
    import uvicorn  # local import: keeps `orbit-dashboard --help` fast

    port = int(os.environ.get("ORBIT_DASHBOARD_PORT", str(DEFAULT_PORT)))
    uvicorn.run("orbit_dashboard.server:app", host="127.0.0.1", port=port)
    return 0


def cmd_install_service(_args: argparse.Namespace) -> int:
    port = int(os.environ.get("ORBIT_DASHBOARD_PORT", str(DEFAULT_PORT)))
    port = resolve_port(port)

    if sys.platform == "darwin":
        install_launchd(port)
    elif sys.platform.startswith("linux"):
        install_systemd(port)
    elif sys.platform == "win32":
        print(
            "Windows service registration is not yet supported.\n"
            "Run 'orbit-dashboard serve' manually, or add your own Task "
            "Scheduler entry. See docs/installation.md#windows."
        )
        return 0
    else:
        print(f"Unsupported platform: {sys.platform}", file=sys.stderr)
        return 1
    return 0


def cmd_uninstall_service(_args: argparse.Namespace) -> int:
    if sys.platform == "darwin":
        uninstall_launchd()
    elif sys.platform.startswith("linux"):
        uninstall_systemd()
    elif sys.platform == "win32":
        print("Windows service was never auto-registered; nothing to uninstall.")
        return 0
    else:
        print(f"Unsupported platform: {sys.platform}", file=sys.stderr)
        return 1
    return 0


def cmd_reinstall_service(args: argparse.Namespace) -> int:
    rc = cmd_uninstall_service(args)
    if rc != 0:
        return rc
    return cmd_install_service(args)


def cmd_status(_args: argparse.Namespace) -> int:
    """Report installed and running state."""
    if sys.platform == "darwin":
        installed = launchd_plist_path().exists()
        running = False
        if installed:
            result = subprocess.run(
                ["launchctl", "list", LAUNCHD_LABEL],
                capture_output=True,
                text=True,
                check=False,
            )
            running = result.returncode == 0
        print(f"  Installed: {installed}")
        print(f"  Running:   {running}")
    elif sys.platform.startswith("linux"):
        installed = systemd_unit_path().exists()
        running = False
        if installed:
            result = subprocess.run(
                ["systemctl", "--user", "is-active", SYSTEMD_UNIT],
                capture_output=True,
                text=True,
                check=False,
            )
            running = result.stdout.strip() == "active"
        print(f"  Installed: {installed}")
        print(f"  Running:   {running}")
    elif sys.platform == "win32":
        print("  Windows: not supported.")
    else:
        print(f"  Unsupported platform: {sys.platform}")
    return 0


# =============================================================================
# argparse wiring
# =============================================================================


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orbit-dashboard",
        description="Orbit Dashboard - task analytics and autonomous execution monitoring.",
    )
    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="Run the dashboard (default)")
    p_serve.set_defaults(func=cmd_serve)

    p_install = sub.add_parser("install-service", help="Register the dashboard as a background service")
    p_install.set_defaults(func=cmd_install_service)

    p_uninstall = sub.add_parser("uninstall-service", help="Remove the background service")
    p_uninstall.set_defaults(func=cmd_uninstall_service)

    p_reinstall = sub.add_parser(
        "reinstall-service",
        help="Uninstall + install the service (Python path change recovery)",
    )
    p_reinstall.set_defaults(func=cmd_reinstall_service)

    p_status = sub.add_parser("status", help="Show service installed and running state")
    p_status.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        return cmd_serve(args)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

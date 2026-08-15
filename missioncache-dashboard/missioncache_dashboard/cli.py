"""MissionCache Dashboard CLI.

Entry point for the `missioncache-dashboard` console script. Subcommands:

    missioncache-dashboard serve               Run the dashboard (default).
    missioncache-dashboard install-service     Register as launchd / systemd service.
    missioncache-dashboard uninstall-service   Remove the service.
    missioncache-dashboard reinstall-service   Uninstall + install (Python path fix).
    missioncache-dashboard status              Show installed / running state.

Platform support: macOS (launchd), Linux (systemd --user, with a
shell-profile autostart fallback on systemd-less machines like default
WSL), and Windows (Task Scheduler ONLOGON task, with an HKCU Run-key
fallback when schtasks refuses from a non-elevated prompt).
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

LAUNCHD_LABEL = "com.missioncache.dashboard"
SYSTEMD_UNIT = "missioncache-dashboard.service"
SCHTASKS_NAME = "MissionCacheDashboard"
RUN_KEY = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"
RUN_KEY_VALUE = "MissionCacheDashboard"
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


def windows_log_path() -> Path:
    """Where a Windows-autostarted dashboard writes stdout/stderr.

    The launchd/systemd/profile branches all have a log destination; Windows
    is the one platform that also hides its console, so without this a startup
    failure would leave no trace anywhere. serve --hidden redirects its own
    output here (the schtasks/Run-key command lines have no shell to redirect
    with), and the immediate start opens the same file.
    """
    return log_dir() / "missioncache-dashboard-windows.log"


def _env_port() -> int:
    """The dashboard port from the environment, or the default."""
    return int(os.environ.get("MISSIONCACHE_DASHBOARD_PORT", str(DEFAULT_PORT)))


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
            f"        <key>MISSIONCACHE_DASHBOARD_PORT</key>\n"
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
        f"    <string>{logs / 'missioncache-dashboard-stdout.log'}</string>\n"
        "    <key>StandardErrorPath</key>\n"
        f"    <string>{logs / 'missioncache-dashboard-stderr.log'}</string>\n"
        f"{env_block}"
        "</dict>\n"
        "</plist>\n"
    )


def render_systemd_unit(binary_path: str, port: int) -> str:
    """Render the systemd user unit pointing at the pip-installed binary."""
    env_line = f"Environment=MISSIONCACHE_DASHBOARD_PORT={port}\n" if port != DEFAULT_PORT else ""
    return (
        "[Unit]\n"
        "Description=MissionCache Dashboard\n"
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


def _is_missioncache_dashboard(port: int) -> bool:
    """True when the process on the port is a MissionCache dashboard."""
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/version", timeout=2
        ) as resp:
            return "version" in json.load(resp)
    except Exception:
        return False


def resolve_port(requested: int) -> int:
    """Return a free port, prompting if the requested one is taken.

    Our own running dashboard on the requested port is NOT a conflict -
    that is the normal state during an update (the autostart/service is
    doing its job). Registration proceeds and the running instance keeps
    serving until its next restart picks up the new version.
    """
    if not port_in_use(requested):
        return requested
    if _is_missioncache_dashboard(requested):
        print(
            f"  Port {requested} is the running MissionCache dashboard - continuing."
        )
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
    """Return the absolute path of the installed `missioncache-dashboard` script.

    On Windows shutil.which searches the current directory BEFORE PATH (the
    win32 branch of CPython's shutil does ``path.insert(0, curdir)``), so a
    ``missioncache-dashboard.cmd`` planted in the directory this command runs
    from would be resolved and then baked into the HKCU Run key / schtasks
    ONLOGON task - durable code execution at every logon from a transient cwd.
    Refuse a resolution whose parent is the cwd: a real console script lives in
    a scripts dir, never in the invocation dir.
    """
    found = shutil.which("missioncache-dashboard")
    if found and Path(found).resolve().parent == Path.cwd().resolve():
        found = None
    if not found:
        raise SystemExit(
            "Could not find `missioncache-dashboard` on PATH. This command must be "
            "run from the same environment where `missioncache-dashboard` is pip-"
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

    plist.write_text(render_plist(binary, port), encoding="utf-8")
    subprocess.run(["launchctl", "load", str(plist)], check=True)
    print(f"  launchd service loaded: {LAUNCHD_LABEL}")
    print(f"  Logs: {log_dir()}/missioncache-dashboard-{{stdout,stderr}}.log")


def uninstall_launchd() -> None:
    plist = launchd_plist_path()
    if not plist.exists():
        print("  launchd service not installed, nothing to do.")
        return
    subprocess.run(["launchctl", "unload", str(plist)], check=False)
    plist.unlink()
    print(f"  Removed {plist}")


def systemd_available() -> bool:
    """True when systemd is this machine's init (PID 1).

    /run/systemd/system exists exactly when systemd is running as the system
    manager - the check systemd's own docs recommend. On WSL it is absent
    unless the user enabled systemd via /etc/wsl.conf, which is why a fresh
    WSL Ubuntu crashed here with "Failed to connect to bus" before this
    check existed.
    """
    return Path("/run/systemd/system").exists()


def install_systemd(port: int) -> None:
    binary = resolve_binary()
    unit = systemd_unit_path()
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(render_systemd_unit(binary, port), encoding="utf-8")
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


# Managed autostart block markers for systemd-less Linux (WSL default).
# Everything between the markers is owned by install-service; uninstall
# removes the whole block and nothing else.
AUTOSTART_BEGIN = "# >>> missioncache-dashboard autostart >>>"
AUTOSTART_END = "# <<< missioncache-dashboard autostart <<<"
# Single source for the process signature the profile guard greps for and
# uninstall kills - the two must stay in lockstep.
AUTOSTART_MATCH = "missioncache-dashboard serve"


def autostart_log_path() -> Path:
    """One log file shared by the login-time redirect and the immediate start."""
    return log_dir() / "missioncache-dashboard-autostart.log"


def autostart_profile_path() -> Path:
    """The shell startup file the autostart block goes into.

    bash reads ~/.bash_profile INSTEAD of ~/.profile when it exists, so
    prefer it; otherwise ~/.profile (sourced by login shells - the shape
    every WSL terminal opens). Assumes bash: a ~/.bash_login-only setup or
    a zsh login shell won't source either file - unlikely on the default
    WSL Ubuntu this fallback exists for.
    """
    bash_profile = Path.home() / ".bash_profile"
    if bash_profile.exists():
        return bash_profile
    return Path.home() / ".profile"


def render_autostart_block(port: int) -> str:
    log_file = autostart_log_path()
    return (
        f"{AUTOSTART_BEGIN}\n"
        "# Managed by `missioncache-dashboard install-service` - do not edit inside\n"
        "# the markers; `uninstall-service` removes the whole block.\n"
        "if command -v missioncache-dashboard >/dev/null 2>&1 && "
        f"! pgrep -f '{AUTOSTART_MATCH}' >/dev/null 2>&1; then\n"
        f"  (MISSIONCACHE_DASHBOARD_PORT={port} nohup missioncache-dashboard serve "
        f">>'{log_file}' 2>&1 &)\n"
        "fi\n"
        f"{AUTOSTART_END}\n"
    )


def _strip_autostart_block(text: str) -> str:
    begin = text.find(AUTOSTART_BEGIN)
    if begin == -1:
        return text
    end = text.find(AUTOSTART_END, begin)
    if end == -1:
        # Torn block (manual edit): remove from begin to end of file rather
        # than leave half a managed block behind.
        return text[:begin].rstrip("\n") + "\n"
    return text[:begin] + text[end + len(AUTOSTART_END):].lstrip("\n")


def install_profile_autostart(port: int) -> None:
    """Autostart for systemd-less Linux: profile block + immediate start."""
    binary = resolve_binary()
    log_dir().mkdir(parents=True, exist_ok=True)

    # A unit file may linger from a pre-fix install that wrote it before
    # discovering systemctl doesn't work here. Remove it so enabling systemd
    # later doesn't surprise-start a second mechanism.
    systemd_unit_path().unlink(missing_ok=True)

    profile = autostart_profile_path()
    existing = profile.read_text(encoding="utf-8") if profile.exists() else ""
    body = _strip_autostart_block(existing)
    if body:
        body = body.rstrip("\n") + "\n\n"
    profile.write_text(body + render_autostart_block(port), encoding="utf-8")
    print(f"  Autostart block written to {profile} (starts on next login shell)")

    if port_in_use(port):
        print(f"  Dashboard already running on port {port}")
        return
    log_file = autostart_log_path()
    env = {**os.environ, "MISSIONCACHE_DASHBOARD_PORT": str(port)}
    with open(log_file, "ab") as log:
        subprocess.Popen(
            [binary, "serve"], env=env, stdout=log, stderr=log,
            start_new_session=True,
        )
    print(f"  Dashboard started in the background (port {port}, log: {log_file})")


def uninstall_profile_autostart() -> None:
    profile = autostart_profile_path()
    if profile.exists():
        text = profile.read_text(encoding="utf-8")
        stripped = _strip_autostart_block(text)
        if stripped != text:
            profile.write_text(stripped, encoding="utf-8")
            print(f"  Removed autostart block from {profile}")
    subprocess.run(["pkill", "-f", AUTOSTART_MATCH], check=False)


def install_linux(port: int) -> None:
    """systemd when it runs here, profile autostart otherwise.

    The fallback also catches systemd-present-but-broken user sessions: a
    failing systemctl degrades to the profile mechanism instead of dumping
    a traceback mid-install (the exact failure a fresh WSL Ubuntu hit).
    """
    if systemd_available():
        try:
            install_systemd(port)
            return
        except subprocess.CalledProcessError as e:
            print(
                f"  systemctl failed (exit {e.returncode}) - falling back to "
                "profile autostart."
            )
    else:
        print(
            "  systemd is not running on this machine (PID 1 is not systemd - "
            "the WSL default) - using profile autostart instead."
        )
    install_profile_autostart(port)


def uninstall_linux() -> None:
    """Remove whichever mechanism is present (both, if a machine has both)."""
    if systemd_unit_path().exists():
        uninstall_systemd()
    uninstall_profile_autostart()


def windows_task_run(binary_path: str, port: int) -> str:
    """The command line the Windows autostart runs (schtasks /TR and Run key alike).

    ``--hidden`` makes serve detach its console window on win32 - both
    mechanisms otherwise leave a console open on the desktop for the whole
    session. ``--port`` rides on the command line because neither schtasks
    nor a Run-key value can set per-task environment variables.
    """
    cmd = f'"{binary_path}" serve --hidden'
    if port != DEFAULT_PORT:
        cmd += f" --port {port}"
    return cmd


def _schtasks_installed() -> bool:
    result = subprocess.run(
        ["schtasks", "/Query", "/TN", SCHTASKS_NAME],
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
        check=False,
    )
    return result.returncode == 0


def _run_key_installed() -> bool:
    result = subprocess.run(
        ["reg", "query", RUN_KEY, "/v", RUN_KEY_VALUE],
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
        check=False,
    )
    return result.returncode == 0


def install_windows(port: int) -> None:
    """Task Scheduler ONLOGON task, Run-key fallback, immediate start.

    schtasks is tried first because a Scheduler task is the closest analog
    of launchd (visible in Task Scheduler, one place to manage). Creating an
    ONLOGON trigger from a non-elevated prompt is refused on stock Windows
    ("Access is denied"), so the HKCU Run key - writable by any user - is
    the fallback rather than an elevation prompt mid-install.
    """
    binary = resolve_binary()
    task_run = windows_task_run(binary, port)

    result = subprocess.run(
        ["schtasks", "/Create", "/SC", "ONLOGON", "/TN", SCHTASKS_NAME,
         "/TR", task_run, "/F"],
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
        check=False,
    )
    if result.returncode == 0:
        # A Run-key entry may linger from an earlier non-elevated install.
        subprocess.run(
            ["reg", "delete", RUN_KEY, "/v", RUN_KEY_VALUE, "/f"],
            capture_output=True, check=False,
        )
        print(f"  Task Scheduler task created: {SCHTASKS_NAME} (runs at logon)")
    else:
        # check=False + capture, matching the schtasks branch: this is the
        # last-resort fallback on a locked-down box, so a failure here must
        # degrade with a message, not dump a traceback out of the install (the
        # same reasoning install_linux applies to a failing systemctl).
        reg = subprocess.run(
            ["reg", "add", RUN_KEY, "/v", RUN_KEY_VALUE, "/t", "REG_SZ",
             "/d", task_run, "/f"],
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            check=False,
        )
        if reg.returncode != 0:
            print(
                "  Could not register autostart: schtasks refused and the "
                f"HKCU Run key write failed ({(reg.stderr or reg.stdout).strip() or 'unknown error'}). "
                "Start the dashboard manually with: missioncache-dashboard serve"
            )
        else:
            # Remove a schtasks task left by an earlier elevated install, so the
            # two mechanisms do not both fire at logon (the loser dies on the
            # port bind). Mirrors the Run-key cleanup on the success path above.
            subprocess.run(
                ["schtasks", "/Delete", "/TN", SCHTASKS_NAME, "/F"],
                capture_output=True, check=False,
            )
            print(
                f"  schtasks refused ({(result.stderr or result.stdout).strip() or 'unknown error'}) - "
                "registered a Run-key autostart instead (HKCU, runs at logon)."
            )

    if port_in_use(port):
        print(f"  Dashboard already running on port {port}")
        return
    env = {**os.environ, "MISSIONCACHE_DASHBOARD_PORT": str(port)}
    # getattr, not the bare attribute: these constants exist only on win32, and
    # install_windows must stay importable/callable (in tests) on POSIX.
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    log_dir().mkdir(parents=True, exist_ok=True)
    log = open(windows_log_path(), "ab")
    try:
        subprocess.Popen(
            [binary, "serve"], env=env,
            stdout=log, stderr=log,
            creationflags=creationflags,
        )
    finally:
        log.close()
    print(f"  Dashboard started in the background (port {port}, log: {windows_log_path()})")


def uninstall_windows() -> None:
    """Remove whichever autostart mechanism is present, then stop the server."""
    removed = False
    if _schtasks_installed():
        subprocess.run(
            ["schtasks", "/Delete", "/TN", SCHTASKS_NAME, "/F"],
            capture_output=True, check=False,
        )
        print(f"  Removed Task Scheduler task {SCHTASKS_NAME}")
        removed = True
    if _run_key_installed():
        subprocess.run(
            ["reg", "delete", RUN_KEY, "/v", RUN_KEY_VALUE, "/f"],
            capture_output=True, check=False,
        )
        print("  Removed Run-key autostart entry")
        removed = True
    if not removed:
        print("  Windows autostart not installed, nothing to do.")
        return
    # Stop the running server too - the platform parity contract (launchctl
    # unload / systemctl --now / pkill all stop the process). Without this, a
    # package uninstall leaves the old process serving, and a reinstall keeps
    # stale code because the occupied port suppresses the fresh start.
    # /IM is the taskkill analog of the Linux branch's pkill -f.
    subprocess.run(
        ["taskkill", "/F", "/IM", "missioncache-dashboard.exe"],
        capture_output=True, check=False,
    )


# =============================================================================
# Subcommand handlers
# =============================================================================


def _hide_own_console_window() -> None:
    """Hide the console window this process was given (win32 only).

    The Windows autostart mechanisms (schtasks ONLOGON task, HKCU Run key)
    both run console programs in a visible window for the whole login
    session. Hiding our own window is the one wrapper-free way to run in
    the background: no pythonw, no vbs launcher, no extra cmd flash.

    Guarded on actually OWNING the console: GetConsoleProcessList reporting a
    single attached pid means this process is the only one on the console, so
    it is a fresh conhost the autostart spawned for us, safe to hide. More than
    one pid means we are sharing the user's shell (someone ran `serve --hidden`
    by hand from cmd.exe), and hiding that would take their terminal with no
    way back.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        arr = (ctypes.c_uint * 4)()
        count = kernel32.GetConsoleProcessList(arr, 4)
        if count != 1:
            return  # sharing a console we do not own - leave it visible
        hwnd = kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:
        pass


def _redirect_output_to_log() -> None:
    """Point this process's stdout/stderr at the Windows autostart log file.

    Called when serving --hidden: the schtasks/Run-key command lines have no
    shell to redirect with, and the console is hidden, so uvicorn's output
    would otherwise go nowhere. Redirect at the fd level so C-level writes are
    captured too. Best-effort; a failure here must not stop the server.

    win32-only, like _hide_own_console_window: on other platforms --hidden is a
    documented no-op, so the redirect must not run there either.
    """
    if sys.platform != "win32":
        return
    try:
        log_dir().mkdir(parents=True, exist_ok=True)
        fd = os.open(str(windows_log_path()), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        os.dup2(fd, 1)
        os.dup2(fd, 2)
        os.close(fd)
    except OSError:
        pass


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the dashboard via uvicorn.

    Port precedence: --port flag, then MISSIONCACHE_DASHBOARD_PORT, then the
    default. The flag exists because the Windows autostart command line is
    the only knob its mechanisms have - neither schtasks nor a Run-key
    value can set per-task environment variables.
    """
    import uvicorn  # local import: keeps `missioncache-dashboard --help` fast

    if getattr(args, "hidden", False):
        _redirect_output_to_log()
        _hide_own_console_window()
    # `is not None`, not `or`: --port 0 (ask the OS for a free port) is a valid
    # explicit choice that `or` would silently drop back to the env/default.
    flag_port = getattr(args, "port", None)
    port = flag_port if flag_port is not None else _env_port()
    uvicorn.run("missioncache_dashboard.server:app", host="127.0.0.1", port=port)
    return 0


def cmd_install_service(args: argparse.Namespace) -> int:
    # --port first: missioncache-install has always passed the flag here (the
    # env var only reaches the subcommand when the user exports it themselves).
    # `is not None` so an explicit --port 0 is honored, not dropped by `or`.
    flag_port = getattr(args, "port", None)
    port = flag_port if flag_port is not None else _env_port()
    port = resolve_port(port)

    if sys.platform == "darwin":
        install_launchd(port)
    elif sys.platform.startswith("linux"):
        install_linux(port)
    elif sys.platform == "win32":
        install_windows(port)
    else:
        print(f"Unsupported platform: {sys.platform}", file=sys.stderr)
        return 1
    return 0


def cmd_uninstall_service(_args: argparse.Namespace) -> int:
    if sys.platform == "darwin":
        uninstall_launchd()
    elif sys.platform.startswith("linux"):
        uninstall_linux()
    elif sys.platform == "win32":
        uninstall_windows()
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
                text=True, encoding="utf-8", errors="replace",
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
                text=True, encoding="utf-8", errors="replace",
                check=False,
            )
            running = result.stdout.strip() == "active"
        else:
            # systemd-less installs (WSL default) register via the profile
            # autostart block; without this branch, status reported "not
            # installed / not running" on the very platform that path serves.
            profile = autostart_profile_path()
            installed = profile.exists() and AUTOSTART_BEGIN in profile.read_text(encoding="utf-8")
            if installed:
                running = port_in_use(_env_port())
        print(f"  Installed: {installed}")
        print(f"  Running:   {running}")
    elif sys.platform == "win32":
        installed = _schtasks_installed() or _run_key_installed()
        running = False
        # No pid to ask on Windows (neither mechanism tracks one); a bound
        # port is the running signal, same as the profile-autostart branch.
        # Guarded on `installed` so a random service on 8787 does not make an
        # uninstalled machine report Running: True (the profile branch does the
        # same). The port install baked into the command line is not readable
        # back from here without parsing schtasks /Query - a known gap for a
        # non-default-port install, tracked for a follow-up.
        if installed:
            running = port_in_use(_env_port())
        print(f"  Installed: {installed}")
        print(f"  Running:   {running}")
    else:
        print(f"  Unsupported platform: {sys.platform}")
    return 0


# =============================================================================
# argparse wiring
# =============================================================================


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="missioncache-dashboard",
        description="MissionCache Dashboard - task analytics and autonomous execution monitoring.",
    )
    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="Run the dashboard (default)")
    p_serve.add_argument(
        "--port", type=int, default=None,
        help="Port to serve on (overrides MISSIONCACHE_DASHBOARD_PORT)",
    )
    p_serve.add_argument(
        "--hidden", action="store_true",
        help="Detach the console window (Windows autostart; no-op elsewhere)",
    )
    p_serve.set_defaults(func=cmd_serve)

    p_install = sub.add_parser("install-service", help="Register the dashboard as a background service")
    p_install.add_argument(
        "--port", type=int, default=None,
        help="Port to register the service on (overrides MISSIONCACHE_DASHBOARD_PORT)",
    )
    p_install.set_defaults(func=cmd_install_service)

    p_uninstall = sub.add_parser("uninstall-service", help="Remove the background service")
    p_uninstall.set_defaults(func=cmd_uninstall_service)

    p_reinstall = sub.add_parser(
        "reinstall-service",
        help="Uninstall + install the service (Python path change recovery)",
    )
    p_reinstall.add_argument(
        "--port", type=int, default=None,
        help="Port to register the service on (overrides MISSIONCACHE_DASHBOARD_PORT)",
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

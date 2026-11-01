"""Windows Terminal panels: the `missioncache-wt` daemon at logon, and a Start
menu shortcut that opens both panels in one window (Windows only).

The daemon ships as the `wt` extra of missioncache-dashboard, in a venv of its
own under ~/.missioncache/wt/venv (see daemon_venv). It starts at logon from a
Task Scheduler task, or from the HKCU Run key when schtasks refuses, the same
way the dashboard does.

The keys are opt-in and default to no: they need a keyboard hook, which sees
every key press while it runs, even though it acts only on three keys and
only while a Windows Terminal window is in front.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from . import daemon_venv, state, subprocess_utils, ui

COMPONENT = "wt_panels"
TASK_NAME = "MissionCacheWtPanels"
RUN_KEY = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"
# The daemon's port and the wt.exe glob are the dashboard's (wt/daemon.py and
# wt/__init__.py). The installer cannot import the dashboard, so change them
# together.
PORT = 8791
START_WAIT_SECONDS = 10
SHORTCUT_NAME = "MissionCache Panels.lnk"
KEYS = (("Ctrl+Shift+]", "the session below"), ("Ctrl+Shift+[", "the session above"),
        ("Ctrl+Shift+\\", "the newest session waiting for you"))


def _windows_apps() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps"


def detected() -> bool:
    """True on Windows with Windows Terminal (stable or Preview) installed."""
    return sys.platform == "win32" and any(
        _windows_apps().glob("Microsoft.WindowsTerminal*_8wekyb3d8bbwe/wt.exe"))


def _data_dir() -> Path:
    # Where the daemon writes its log and state: missioncache_db's data root.
    root = os.environ.get("MISSIONCACHE_ROOT")
    return (Path(root) if root and Path(root).is_absolute() else Path.home() / ".missioncache") / "wt"


def _venv_python() -> Path:
    return _data_dir() / "venv" / "Scripts" / "python.exe"


def _daemon_exe() -> Path:
    return _data_dir() / "venv" / "Scripts" / "missioncache-wt.exe"


def _shortcut_path() -> Path:
    return (Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu"
            / "Programs" / SHORTCUT_NAME)


def daemon_args(keys: bool, dashboard_port: int) -> list[str]:
    """What the logon task runs. Neither schtasks nor a Run-key value can set
    an environment variable, so the dashboard port rides on the command line."""
    args = [str(_daemon_exe()), "--hidden"]
    if keys:
        args.append("--keys")
    if dashboard_port != 8787:
        args += ["--dashboard-port", str(dashboard_port)]
    return args


def _edge() -> Path | None:
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if base and (exe := Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe").is_file():
            return exe
    return None


# PowerShell ends a single-quoted string on any of these, not only on '. The
# same as resume_sessions._ps_literal, which the installer cannot import.
_PS_QUOTES_RE = re.compile("['\u2018\u2019\u201a\u201b]")


def _ps_literal(text: str) -> str:
    return "'" + _PS_QUOTES_RE.sub(lambda m: m.group(0) * 2, text) + "'"


def _write_shortcut() -> None:
    """A Start menu shortcut to the page that shows both panels: an Edge app
    window when Edge is there, else the default browser."""
    url = f"http://127.0.0.1:{PORT}/panels"
    edge = _edge()
    target, arguments = (str(edge), f"--app={url}") if edge else ("explorer.exe", url)
    path = _shortcut_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    script = (
        f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut({_ps_literal(str(path))}); "
        f"$s.TargetPath = {_ps_literal(target)}; $s.Arguments = {_ps_literal(arguments)}; "
        f"$s.Description = 'Recent Sessions and Saved Tabs'; $s.Save()"
    )
    subprocess_utils.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script])
    ui.detail(f"Start menu shortcut: {path.stem}")


def _register(args: list[str]) -> str:
    """Run the daemon at logon. Returns "schtasks", "run_key", or "none" when
    both are refused. The dashboard's `install_windows` does the same for its
    own server."""
    command = subprocess.list2cmdline(args)
    task = subprocess_utils.run(
        ["schtasks", "/Create", "/SC", "ONLOGON", "/TN", TASK_NAME, "/TR", command, "/F"], check=False)
    if task.returncode == 0:
        # An entry may linger from an earlier install that schtasks refused.
        subprocess_utils.run(["reg", "delete", RUN_KEY, "/v", TASK_NAME, "/f"], check=False)
        ui.detail(f"Task Scheduler task created: {TASK_NAME} (runs at logon)")
        return "schtasks"
    # Creating an ONLOGON task is refused from a non-elevated prompt on stock
    # Windows. The Run key is writable by any user.
    reg = subprocess_utils.run(
        ["reg", "add", RUN_KEY, "/v", TASK_NAME, "/t", "REG_SZ", "/d", command, "/f"], check=False)
    if reg.returncode != 0:
        reason = (reg.stderr or reg.stdout or "").strip() or f"exit {reg.returncode}"
        ui.warn(f"Could not start the panels at logon: schtasks refused and the Run key write failed ({reason}).")
        return "none"
    subprocess_utils.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], check=False)
    refused = (task.stderr or task.stdout or "").strip() or f"exit {task.returncode}"
    ui.detail(f"schtasks refused ({refused}), so the panels start from the Run key (HKCU) at logon")
    return "run_key"


def _stop() -> bool:
    """Stop the daemon, and wait until nothing runs from its venv. The running
    daemon holds its own .exe, which the package install and the uninstall
    both need to replace or remove. False when something still runs there."""
    from . import installers

    root = str(_data_dir() / "venv").rstrip("\\/") + os.sep
    kind, still = installers._clear_dashboard_processes(roots=[root])
    if kind == "blocked":
        ui.warn(f"Could not stop {installers._describe_processes(still)}, which runs from {root}")
        return False
    if kind == "unknown":
        ui.warn("Could not confirm that the panels daemon stopped. If a step below fails "
                "with a file in use, close it from Task Manager (missioncache-wt.exe) and run this again.")
    return True


def _start(args: list[str]) -> bool:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        subprocess.Popen(args, creationflags=flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        ui.warn(f"Could not start the panels: {e}")
        return False
    return True


def _running() -> bool:
    """Whether the daemon answers, a few seconds after it was started."""
    deadline = time.monotonic() + START_WAIT_SECONDS
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/lead", timeout=2):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def _ask_keys() -> bool:
    ui.info("Keys for the panels, while a Windows Terminal window is in front:")
    for key, what in KEYS:
        ui.detail(f"  {key:<14} {what}")
    ui.detail("They need a keyboard hook. It acts on these three keys only, and keeps nothing.")
    return ui.ask_yn("Turn on the keys?", default=False)


def install(ctx) -> None:
    """Install the daemon, register it at logon, start it, and add the
    shortcut. An update keeps the key choice from the first install."""
    # installers imports this module for its tables, so import it here.
    from . import installers

    ui.step("w", "Windows Terminal panels")
    if not detected():
        ui.warn("Windows Terminal panels need Windows with Windows Terminal installed. Skipping them.")
        return
    mode = installers._recorded_mode(COMPONENT, ctx)
    components = state.load().get("components", {})
    prior = components.get(COMPONENT, {})
    # Asked before anything is written, so a Ctrl+C here leaves nothing behind.
    keys = bool(prior.get("keys")) if ctx.updating else _ask_keys()
    _data_dir().mkdir(parents=True, exist_ok=True)
    if not _stop():
        ui.warn("Close it and run the install again. The panels were left as they were.")
        return
    daemon_venv.install_packages(_venv_python(), "wt",
                                 installers._require_repo(ctx) if mode == "local" else None)
    args = daemon_args(keys, components.get("dashboard", {}).get("port") or ctx.port)
    if ctx.skip_service:
        ui.detail("Skipping logon registration (--no-service)")
        service = "none"
    else:
        service = _register(args)
    _write_shortcut()
    # Recorded once the panels exist, so a failure above leaves no record of
    # an install that is not there.
    state.record_component(COMPONENT, {"mode": mode, "keys": keys, "service": service})
    if _start(args) and not _running():
        ui.warn(f"The panels did not answer on 127.0.0.1:{PORT}. See {_data_dir() / 'run.log'}.")
    ui.detail("Open the panels from the Start menu: MissionCache Panels.")
    ui.detail("Session states (working, waiting, done) need the MissionCache plugin in Claude Code.")
    if service == "none":
        ui.success("Windows Terminal panels installed. They will not start by themselves at logon")
    else:
        ui.success("Windows Terminal panels installed")


def uninstall(ctx) -> None:
    """Stop the daemon and remove its logon entry, shortcut, venv and state.

    The resume lists in tasks.db stay: the dashboard and CLI show them too.
    """
    if sys.platform == "win32":
        if not _stop():
            ui.warn("The panels are still running, so nothing was removed. "
                    "Close them and run the uninstall again.")
            return
        subprocess_utils.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], check=False)
        subprocess_utils.run(["reg", "delete", RUN_KEY, "/v", TASK_NAME, "/f"], check=False)
    _shortcut_path().unlink(missing_ok=True)
    shutil.rmtree(_data_dir(), ignore_errors=True)
    if _data_dir().exists():
        # Kept in the state, so running the uninstall again finishes it.
        ui.warn(f"Could not remove everything in {_data_dir()}. "
                "Close anything using it and run the uninstall again.")
        return
    state.remove_component(COMPONENT)
    ui.detail("Windows Terminal panels uninstalled")

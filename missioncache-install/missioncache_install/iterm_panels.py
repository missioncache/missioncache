"""iTerm2 panels: the `missioncache-iterm` daemon as a LaunchAgent (macOS only).

The daemon ships as the `iterm2` extra of missioncache-dashboard. It gets a venv
of its own under ~/.missioncache/iterm/venv rather than riding in the
dashboard's tool venv: `--update` reinstalls the dashboard with `--force` and
no extras, which would strip the iterm2 dependency from under a running daemon.

Key bindings are opt-in and default to no, because they change iTerm2's own
settings. They are written in keycode form with LanguageAgnosticKeyBindings on,
so they keep their meaning under a non-Latin keyboard layout (a Hebrew layout
mirrors the bracket keys). iTerm2's settings are backed up before the write,
to ~/.missioncache/backups/, which uninstall leaves alone.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import sys
from datetime import datetime
from pathlib import Path

from . import state, subprocess_utils, ui


COMPONENT = "iterm_panels"
LABEL = "com.missioncache.iterm"
PREFS_DOMAIN = "com.googlecode.iterm2"
# iTerm2's "Invoke Script Function" key action.
INVOKE_FUNCTION = 60
# GlobalKeyMap keys are char-modifiers-keycode-flag. 0x120000 is Cmd+Shift,
# keycodes 0x21, 0x1e and 0x2a are the physical [ ] and \ keys.
KEY_BINDINGS: dict[str, tuple[str, str]] = {
    "0x7b-0x120000-0x21-0x1": ("Cmd+Shift+[", "session_prev()"),
    "0x7d-0x120000-0x1e-0x1": ("Cmd+Shift+]", "session_next()"),
    "0x7c-0x120000-0x2a-0x1": ("Cmd+Shift+\\", "jump_to_unseen()"),
}


def _iterm_app() -> Path | None:
    for base in (Path("/Applications"), Path.home() / "Applications"):
        if (base / "iTerm.app").exists():
            return base / "iTerm.app"
    return None


def detected() -> bool:
    """True on macOS with iTerm2 in /Applications or ~/Applications."""
    return sys.platform == "darwin" and _iterm_app() is not None


def _default_keymap() -> dict:
    """The global key bindings iTerm2 ships with. iTerm2 uses them until the
    user changes one, and from then on only the user's own GlobalKeyMap, so
    a first write has to start from these."""
    app = _iterm_app()
    try:
        with open(app / "Contents" / "Resources" / "DefaultGlobalKeyMap.plist", "rb") as f:
            keymap = plistlib.load(f)
    except (TypeError, OSError, plistlib.InvalidFileException):
        return {}
    return keymap if isinstance(keymap, dict) else {}


def _data_dir() -> Path:
    return Path.home() / ".missioncache" / "iterm"


def _venv_python() -> Path:
    return _data_dir() / "venv" / "bin" / "python"


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def render_plist(dashboard_port: int = 8787) -> bytes:
    data_dir = _data_dir()
    log = str(data_dir / "run.log")
    env = {"PYTHONUNBUFFERED": "1"}
    if dashboard_port != 8787:
        # The logo and Lead buttons open the dashboard on this port.
        env["MISSIONCACHE_DASHBOARD_PORT"] = str(dashboard_port)
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [str(data_dir / "venv" / "bin" / "missioncache-iterm")],
        "EnvironmentVariables": env,
        "WorkingDirectory": str(data_dir),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 5,
        "StandardOutPath": log,
        "StandardErrorPath": log,
    })


def _install_packages(repo_root: Path | None) -> None:
    """Create the venv if missing, then install or upgrade the daemon into it:
    editable from the clone at ``repo_root``, or from PyPI when it is None."""
    python = _venv_python()
    uv = shutil.which("uv")
    if not python.exists():
        venv = str(python.parent.parent)
        if uv:
            cmd = [uv, "venv", "--python", ">=3.11", venv]
        else:
            cmd = [sys.executable, "-m", "venv", venv]
        ui.detail(f"Running: {' '.join(cmd)}")
        subprocess_utils.run_streaming(cmd)
    if repo_root is not None:
        packages = [
            "-e", str(repo_root / "missioncache-db"),
            "-e", f"{repo_root / 'missioncache-dashboard'}[iterm2]",
        ]
    else:
        packages = ["--upgrade", "missioncache-dashboard[iterm2]"]
    if uv:
        cmd = [uv, "pip", "install", "--python", str(python), *packages]
        if repo_root is None:
            cmd.append("--refresh")
    else:
        cmd = [str(python), "-m", "pip", "install", "--quiet", *packages]
    ui.detail(f"Running: {' '.join(cmd)}")
    subprocess_utils.run_streaming(cmd)


def _load_service(dashboard_port: int) -> None:
    """Write the LaunchAgent and (re)load it, which restarts a running daemon."""
    plist = _plist_path()
    plist.parent.mkdir(parents=True, exist_ok=True)
    if plist.exists():
        subprocess_utils.run(["launchctl", "unload", str(plist)], check=False)
    plist.write_bytes(render_plist(dashboard_port))
    subprocess_utils.run(["launchctl", "load", str(plist)])
    ui.detail(f"launchd service loaded: {LABEL}")


def _read_prefs() -> dict:
    result = subprocess_utils.run(["defaults", "export", PREFS_DOMAIN, "-"], check=False)
    if result.returncode != 0 or not result.stdout:
        return {}
    return plistlib.loads(result.stdout.encode("utf-8"))


def _write_pref(key: str, value: dict | bool) -> None:
    subprocess_utils.run(
        ["defaults", "write", PREFS_DOMAIN, key, plistlib.dumps(value).decode("utf-8")]
    )


def _ours(entry: object, function: str) -> bool:
    return (
        isinstance(entry, dict)
        and entry.get("Action") == INVOKE_FUNCTION
        and entry.get("Text") == function
    )


def _all_bound(prefs: dict) -> bool:
    keymap = prefs.get("GlobalKeyMap") or {}
    return all(_ours(keymap.get(k), f) for k, (_, f) in KEY_BINDINGS.items())


def _print_manual_bindings() -> None:
    ui.detail("To bind the shortcuts yourself: iTerm2 Settings > Keys > Key Bindings > +,")
    ui.detail("action \"Invoke Script Function\", and one of:")
    for shortcut, function in KEY_BINDINGS.values():
        ui.detail(f"  {shortcut:<14} {function}")


def _offer_key_bindings(prefs: dict) -> dict | None:
    """Show the binding change, ask (default no), back up and write.

    Returns what was written, for uninstall to undo, or None when nothing was.
    """
    keymap = dict(prefs["GlobalKeyMap"] if "GlobalKeyMap" in prefs else _default_keymap())
    agnostic = bool(prefs.get("LanguageAgnosticKeyBindings"))
    missing = {k: v for k, v in KEY_BINDINGS.items() if not _ours(keymap.get(k), v[1])}
    if not missing and agnostic:
        ui.detail("The three key bindings are already set")
        return None

    ui.info("Key bindings for the panels (written to iTerm2's settings):")
    for key, (shortcut, function) in missing.items():
        current = keymap.get(key)
        note = ""
        if isinstance(current, dict):
            note = f"  (replaces: {current.get('Text') or 'action ' + str(current.get('Action'))})"
        ui.detail(f"  {shortcut:<14} {function}{note}")
    if not agnostic:
        ui.detail("  Turn on \"keys keep their meaning in any keyboard layout\"")
    if not ui.ask_yn("Write these key bindings?", default=False):
        return None

    backup_dir = Path.home() / ".missioncache" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"iterm2-prefs-{datetime.now():%Y%m%d-%H%M%S}.plist"
    subprocess_utils.run(["defaults", "export", PREFS_DOMAIN, str(backup)])
    ui.detail(f"Backed up iTerm2 settings to {backup}")

    for key, (_, function) in missing.items():
        keymap[key] = {"Action": INVOKE_FUNCTION, "Text": function}
    _write_pref("GlobalKeyMap", keymap)
    if not agnostic:
        _write_pref("LanguageAgnosticKeyBindings", True)
    ui.detail("Key bindings written")
    return {"keys": sorted(missing), "language_agnostic_set": not agnostic}


def _print_prerequisites(prefs: dict) -> None:
    if not prefs.get("EnableAPIServer"):
        ui.warn(
            "iTerm2's Python API is off, so the panels cannot connect. Turn it on in "
            "iTerm2 Settings > General > Magic > Enable Python API."
        )
    ui.detail("Open the panels: View > Toolbelt, then tick Recent Sessions and Saved Tabs.")
    ui.detail("Session states (working, waiting, done) need iTerm2 3.7+ and its Claude Code integration.")
    if not _all_bound(prefs):
        _print_manual_bindings()


def install(ctx) -> None:
    """Install the daemon, load its LaunchAgent, and offer the key bindings.

    An update reinstalls the packages and restarts the daemon, and keeps the
    key-binding record from the first install without asking again.
    """
    # installers imports this module for its tables, so import it here.
    from . import installers

    ui.step("i", "iTerm2 panels")
    if not detected():
        ui.warn("iTerm2 panels need macOS with iTerm2 installed. Skipping them.")
        return
    mode = installers._recorded_mode(COMPONENT, ctx)
    data_dir = _data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(data_dir, 0o700)
    _install_packages(installers._require_repo(ctx) if mode == "local" else None)
    components = state.load().get("components", {})
    prior = components.get(COMPONENT, {})
    info = {"mode": mode, "service": installers._service_kind(ctx.skip_service)}
    if prior.get("key_bindings"):
        info["key_bindings"] = prior["key_bindings"]
    if ctx.skip_service:
        ui.detail("Skipping service registration (--no-service)")
    else:
        _load_service(components.get("dashboard", {}).get("port") or ctx.port)
    # Recorded before the question, so a Ctrl+C there still leaves an agent
    # that --uninstall knows about.
    state.record_component(COMPONENT, info)
    prefs = _read_prefs()
    if not ctx.updating and (written := _offer_key_bindings(prefs)):
        before = info.get("key_bindings", {})
        info["key_bindings"] = {
            "keys": sorted(set(before.get("keys", [])) | set(written["keys"])),
            "language_agnostic_set": bool(before.get("language_agnostic_set") or written["language_agnostic_set"]),
        }
        state.record_component(COMPONENT, info)
    # Read again: the bindings may have been written a moment ago.
    _print_prerequisites(_read_prefs())
    ui.success("iTerm2 panels installed")


def _remove_key_bindings(record: dict) -> None:
    """Undo only what install wrote, and only where the entry is still ours."""
    prefs = _read_prefs()
    keymap = dict(prefs.get("GlobalKeyMap") or {})
    removed = [
        key for key in record.get("keys", [])
        if key in KEY_BINDINGS and _ours(keymap.get(key), KEY_BINDINGS[key][1])
    ]
    for key in removed:
        del keymap[key]
    if removed and keymap == _default_keymap():
        # Nothing left but iTerm2's own defaults: hand them back to iTerm2.
        subprocess_utils.run(["defaults", "delete", PREFS_DOMAIN, "GlobalKeyMap"], check=False)
    elif removed:
        _write_pref("GlobalKeyMap", keymap)
    if record.get("language_agnostic_set"):
        _write_pref("LanguageAgnosticKeyBindings", False)
    if removed or record.get("language_agnostic_set"):
        ui.detail("Removed the panels' key bindings from iTerm2")


def uninstall(ctx) -> None:
    """Stop the daemon and remove its LaunchAgent, venv, state and key bindings.

    The resume lists in tasks.db stay: the dashboard and CLI show them too.
    """
    plist = _plist_path()
    if plist.exists():
        subprocess_utils.run(["launchctl", "unload", str(plist)], check=False)
        plist.unlink()
    record = state.load().get("components", {}).get(COMPONENT, {})
    if record.get("key_bindings") and sys.platform == "darwin":
        _remove_key_bindings(record["key_bindings"])
    shutil.rmtree(_data_dir(), ignore_errors=True)
    state.remove_component(COMPONENT)
    ui.detail("iTerm2 panels uninstalled")

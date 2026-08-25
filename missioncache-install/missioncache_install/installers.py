"""Component installers.

Each installer is idempotent and records state so --update and --uninstall
know what to operate on. Installers NEVER overwrite user-owned config
without explicit consent - the statusline installer in particular will
surface any existing statusLine command and ask before replacing it.

Two modes:
- PyPI mode (default): installs from PyPI, copies bundled rules/user-commands
  out of package data.
- Local mode (--local): editable pip installs + symlinks from the clone.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field, replace
from importlib import resources
from pathlib import Path
from typing import Literal

from . import command_clients, mcp_clients, settings, state, subprocess_utils, ui


MARKETPLACE_DIR = Path.home() / ".claude" / "plugins" / "local-marketplace"
PLUGIN_GITHUB_SOURCE = "missioncache/missioncache"
PLUGIN_MARKETPLACE_NAME = "missioncache"
PLUGIN_ID_PYPI = "missioncache@missioncache"
PLUGIN_ID_LOCAL = "missioncache@local"
USER_COMMAND_FILES = ("whats-new.md", "optimize-prompt.md")
# Line-1 marker that makes a copied file MissionCache-managed. Same contract as
# uninstall_rules: no marker means the user took ownership and we never touch it.
MANAGED_MARKER = "missioncache-plugin:managed"


Mode = Literal["pypi", "local"]

# The `uv run` argument prefix the plugin's hooks.json uses to launch every
# hook (before the per-hook script path). The interpreter pre-warm reuses it so
# the two cannot drift - raising the >=3.11 floor in hooks.json without matching
# it here would leave the warm preparing the wrong interpreter. A test asserts
# hooks.json's args start with this prefix.
HOOK_UV_ARGS = ["run", "--no-project", "--python", ">=3.11", "python"]


@dataclass
class InstallContext:
    """Shared options passed to every installer."""

    mode: Mode
    repo_root: Path | None   # populated only in local mode
    skip_service: bool       # --no-service; dashboard installs without launchd/systemd
    port: int                # dashboard port (default 8787)
    assume_yes: bool         # --yes; skip per-file confirmations (still honors component selection)
    # True when running under --update. Update refreshes what MissionCache owns
    # but never re-acquires config the user has since pointed elsewhere (e.g. a
    # statusLine command that is no longer missioncache-statusline).
    updating: bool = False
    # In-memory MCP-registration outcomes for THIS run only. Keys are tool
    # names ("codex", "opencode", "vscode"); True means the parent MCP
    # installer succeeded in this run, False means it ran and failed, missing
    # key means it didn't run. Slash command installers gate on this (not on
    # state.json) so a stale prior-run success can't mask a fresh failure.
    mcp_success: dict[str, bool] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Plugin core (MCP server + commands + hooks + rules-via-plugin)
# ---------------------------------------------------------------------------

def install_plugin(ctx: InstallContext) -> None:
    """Register the MissionCache plugin with Claude Code.

    PyPI mode: adds the upstream marketplace and installs missioncache@missioncache.
    Local mode: creates ~/.claude/plugins/local-marketplace pointing at the
    clone, then installs missioncache@local. Mirrors setup.sh:152-217.
    """
    ui.step("1", "Core plugin")
    if ctx.mode == "local":
        _install_plugin_local(ctx)
    else:
        _install_plugin_pypi(updating=ctx.updating)
    _warm_hook_interpreter()
    state.record_component(
        "plugin",
        {"mode": "marketplace" if ctx.mode == "pypi" else "local"},
    )
    ui.success("Core plugin installed")


def _warm_hook_interpreter() -> None:
    """Pre-resolve the Python that hooks.json's `uv run` launcher will use.

    The plugin's hooks run via `uv run --no-project --python ">=3.11" python`.
    On a machine with no suitable interpreter, uv downloads one on first use -
    a download the UserPromptSubmit hooks' 5s timeout would lose. Warming here
    moves that one-time cost into the install, where waiting is expected.

    Best-effort: a missing uv or a failed warm only means the first hook run
    pays the cost (or the hook times out once and the next one works).
    """
    if not shutil.which("uv"):
        ui.warn("uv not found on PATH - plugin hooks need it. Install: https://docs.astral.sh/uv/")
        return
    ui.detail("Preparing the hook interpreter (uv-managed Python >=3.11, may download once)...")
    try:
        # run_streaming, not run: this can trigger a python-build-standalone
        # download, and the module's own convention is that long-running
        # commands with live output use run_streaming (captured output would
        # sit silent for the whole download).
        subprocess_utils.run_streaming(["uv", *HOOK_UV_ARGS, "-V"])
        ui.detail("Hook interpreter ready")
    except subprocess_utils.CommandFailed as e:
        ui.warn(f"Could not pre-warm the hook interpreter: {e.stderr.strip() or 'unknown error'}")


def _install_plugin_pypi(updating: bool = False) -> None:
    """Add the upstream marketplace and install missioncache@missioncache.

    updating=True additionally refreshes the marketplace and runs `claude
    plugins update`: `marketplace add` does not re-fetch an already-registered
    marketplace and `plugins install` no-ops on an installed plugin, so
    without the explicit refresh a user's plugin (hooks, commands, templates)
    would stay at its install-time version forever.
    """
    if not shutil.which("claude"):
        ui.warn("Claude CLI not found - skipping plugin registration.")
        ui.detail("After installing Claude Code, run: missioncache-install --update")
        return

    ui.detail(f"Adding marketplace {PLUGIN_GITHUB_SOURCE}")
    try:
        subprocess_utils.run(
            ["claude", "plugins", "marketplace", "add", PLUGIN_GITHUB_SOURCE]
        )
    except subprocess_utils.CommandFailed as e:
        combined = (e.stderr + e.stdout).lower()
        if "already" in combined:
            ui.detail("Marketplace already registered")
        else:
            raise

    if updating:
        try:
            subprocess_utils.run(
                ["claude", "plugins", "marketplace", "update", PLUGIN_MARKETPLACE_NAME]
            )
            ui.detail("Marketplace refreshed from source")
        except subprocess_utils.CommandFailed as e:
            # A `plugins update` against stale marketplace metadata would
            # likely no-op "already latest" and record a false success -
            # exactly the stuck-plugin bug this path exists to fix. Propagate
            # instead: the component gets recorded as failed and the next
            # --update retries it.
            ui.warn(f"Marketplace refresh failed: {e.stderr.strip() or 'unknown error'}")
            raise
        try:
            subprocess_utils.run(["claude", "plugins", "update", PLUGIN_ID_PYPI])
            settings.enable_plugin(PLUGIN_ID_PYPI)
            ui.detail(f"Updated {PLUGIN_ID_PYPI} (restart Claude Code sessions to apply)")
            return
        except subprocess_utils.CommandFailed:
            ui.detail("Plugin update failed (not installed yet?) - falling back to install")

    ui.detail(f"Installing {PLUGIN_ID_PYPI}")
    subprocess_utils.run(["claude", "plugins", "install", PLUGIN_ID_PYPI])
    # Enable only AFTER the install succeeds. If the install above raises,
    # settings.json must not carry an enabledPlugins entry for a plugin the
    # CLI never registered.
    settings.enable_plugin(PLUGIN_ID_PYPI)


def _install_plugin_local(ctx: InstallContext) -> None:
    """Create a local marketplace symlinking the clone, install missioncache@local.

    Ports setup.sh:152-217 to Python.
    """
    repo = _require_repo(ctx)
    plugins_dir = MARKETPLACE_DIR / "plugins"
    marketplace_json = MARKETPLACE_DIR / ".claude-plugin" / "marketplace.json"
    plugin_link = plugins_dir / "missioncache"

    plugins_dir.mkdir(parents=True, exist_ok=True)
    marketplace_json.parent.mkdir(parents=True, exist_ok=True)

    _write_local_marketplace_json(marketplace_json)

    def _link_and_report(verb: str) -> None:
        if _symlink_or_copy(plugin_link, repo):
            ui.detail(f"{verb} symlink -> {repo}")
        else:
            ui.detail(f"{verb} copy of {repo} (symlinks need Developer Mode)")

    if plugin_link.is_symlink():
        if _links_to(plugin_link, repo):
            ui.detail("Plugin symlink already correct")
        else:
            plugin_link.unlink()
            _link_and_report("Updated")
    elif plugin_link.is_dir():
        # Steady state under the copy fallback: the previous run left a real
        # directory here, so this is not the alarming "someone put a dir where
        # our symlink goes" case. Only warn when symlinks actually work.
        shutil.rmtree(plugin_link)
        _link_and_report("Created")
    elif plugin_link.exists():
        ui.warn(f"Unexpected file at {plugin_link}; removing")
        plugin_link.unlink()
        _link_and_report("Created")
    else:
        _link_and_report("Created")

    if shutil.which("claude"):
        try:
            subprocess_utils.run(["claude", "plugins", "install", PLUGIN_ID_LOCAL])
            ui.detail(f"Installed {PLUGIN_ID_LOCAL} via Claude CLI")
        except subprocess_utils.CommandFailed as e:
            ui.warn(f"Claude CLI install failed: {e.stderr.strip() or 'unknown error'}")
            ui.detail(f"You can retry with: claude plugins install {PLUGIN_ID_LOCAL}")
            # Propagate so install_plugin does not enable/record/report success
            # for a plugin the CLI never registered.
            raise
        # Enable only after a successful CLI install.
        settings.enable_plugin(PLUGIN_ID_LOCAL)
    else:
        ui.warn(f"Claude CLI not found. Run: claude plugins install {PLUGIN_ID_LOCAL}")


def _write_local_marketplace_json(path: Path) -> None:
    """Create or update marketplace.json to include MissionCache. Idempotent."""
    entry = {
        "name": "missioncache",
        "source": "./plugins/missioncache",
        "description": "Project management with time tracking and autonomous execution",
        "category": "productivity",
    }
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        plugins = data.setdefault("plugins", [])
        if any(p.get("name") == "missioncache" for p in plugins):
            ui.detail("missioncache already in marketplace.json")
            return
        plugins.append(entry)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        ui.detail("Added missioncache to existing marketplace.json")
        return
    path.write_text(json.dumps({
        "name": "local",
        "owner": {"name": "Tomer Brami"},
        "plugins": [entry],
    }, indent=2), encoding="utf-8")
    ui.detail("Created marketplace.json")


def uninstall_plugin(ctx: InstallContext) -> None:
    """Remove the plugin registration. Does not delete project data."""
    info = state.load().get("components", {}).get("plugin", {})
    mode = info.get("mode", "marketplace")
    plugin_id = PLUGIN_ID_LOCAL if mode == "local" else PLUGIN_ID_PYPI
    if shutil.which("claude"):
        try:
            subprocess_utils.run(["claude", "plugins", "uninstall", plugin_id])
            ui.detail(f"Uninstalled {plugin_id}")
        except subprocess_utils.CommandFailed as e:
            ui.warn(f"Plugin uninstall failed: {e.stderr.strip()}")
    else:
        ui.warn(f"Claude CLI not found - remove manually: claude plugins uninstall {plugin_id}")
    settings.disable_plugin(plugin_id)
    state.remove_component("plugin")


# ---------------------------------------------------------------------------
# Dashboard (FastAPI daemon + service registration)
# ---------------------------------------------------------------------------

_DASHBOARD_DIST = "missioncache-dashboard"

_ClearState = Literal["clear", "blocked", "unknown"]

# The env var carrying the candidate tool roots into the PowerShell probe.
# Values are passed through the environment, never interpolated into the
# script text: inside a PowerShell literal the path would need escaping, and
# a legal Windows username with an apostrophe (O'Brien) would break the
# script into the destructive fail-open path.
_ROOTS_ENV = "MISSIONCACHE_TOOL_ROOTS"

# Three properties, all load-bearing:
# - $ErrorActionPreference plus the try/catch: powershell -Command exits 0 on
#   NON-terminating cmdlet errors (Get-CimInstance's default failure mode),
#   which would masquerade as "no processes". Any failure must exit non-zero
#   so the caller reports "could not check" instead of proceeding.
# - StartsWith with an ordinal comparison, not -like: -like treats [ ] ? * as
#   wildcards, so a bracketed profile path would silently match nothing.
# - Single quotes only ('{0}|{1}' -f ...): embedded double quotes depend on
#   list2cmdline escaping that is historically flaky with powershell.exe.
_PROBE_SCRIPT = (
    "$ErrorActionPreference = 'Stop'; "
    "try { "
    "$roots = $env:" + _ROOTS_ENV + " -split ';' | Where-Object { $_ }; "
    "Get-CimInstance Win32_Process | ForEach-Object { "
    "$p = $_.ExecutablePath; "
    "if ($p) { foreach ($r in $roots) { "
    "if ($p.StartsWith($r, [System.StringComparison]::OrdinalIgnoreCase)) "
    "{ '{0}|{1}' -f $_.ProcessId, $_.Name; break } } } } "
    "} catch { exit 1 }"
)


class DashboardUpgradeBlocked(subprocess_utils.CommandFailed):
    """The dashboard upgrade was refused because its files are still in use.

    A CommandFailed subclass so install_components' per-component isolation
    treats it like any other component failure (recorded, retried on the
    next --update, later components still run).
    """

    def __init__(self, detail: str) -> None:
        super().__init__(["dashboard-upgrade"], 1, "", detail)


def _dashboard_tool_roots() -> list[str]:
    """Every directory the dashboard's executables may run from, per installer.

    _pipx_install prefers pipx over uv, so both layouts are candidates: probing
    only the uv layout made the whole guard inert for pipx users. Env overrides
    (UV_TOOL_DIR, PIPX_HOME) are honored, and each root is returned WITH a
    trailing separator so prefix matching cannot reach a sibling directory
    that merely shares the name prefix (missioncache-dashboard-backup).
    """
    appdata = os.environ.get("APPDATA") or str(Path.home())
    uv_dir = os.environ.get("UV_TOOL_DIR") or str(Path(appdata) / "uv" / "tools")
    localappdata = os.environ.get("LOCALAPPDATA") or str(Path.home())
    pipx_home = os.environ.get("PIPX_HOME") or str(Path(localappdata) / "pipx")
    legacy_pipx = str(Path.home() / ".local" / "pipx")
    roots = [
        str(Path(uv_dir) / _DASHBOARD_DIST),
        str(Path(pipx_home) / "venvs" / _DASHBOARD_DIST),
        str(Path(legacy_pipx) / "venvs" / _DASHBOARD_DIST),
    ]
    return [root.rstrip("\\/") + os.sep for root in roots]


def _windows_tool_processes(*, quiet: bool = False) -> list[tuple[int, str]] | None:
    """PIDs whose executable runs from a dashboard tool root, or None if unknown.

    Windows refuses to delete a directory that holds a running executable, so
    these are exactly the processes that make a forced reinstall fail - the
    dashboard's own server tree is the usual occupant. None means the probe
    itself failed; callers MUST treat that differently from an empty list,
    because "could not check" is not "nothing is running". `quiet` suppresses
    the warning for repeated calls (the verify poll), so a persistently
    failing probe warns once instead of once per poll tick.

    Deliberately does NOT go through the package's own CLI: a previously
    failed upgrade can leave the venv unimportable (uv removes site-packages
    before it fails on Scripts), and this must still work there.
    """
    try:
        result = subprocess_utils.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PROBE_SCRIPT],
            check=True,
            timeout=20,
            extra_env={_ROOTS_ENV: ";".join(_dashboard_tool_roots())},
        )
    except subprocess_utils.CommandFailed as e:
        if not quiet:
            detail = (e.stderr or e.stdout or "").strip() or f"exit {e.returncode}"
            ui.warn(
                "Could not check what is running from the dashboard's install "
                f"directory: {detail}"
            )
        return None
    found: list[tuple[int, str]] = []
    for line in (result.stdout or "").splitlines():
        pid, _, name = line.strip().partition("|")
        if pid.isdigit():
            found.append((int(pid), name or "unknown"))
    return found


def _describe_processes(processes: list[tuple[int, str]]) -> str:
    if not processes:
        return "unidentified processes"
    return ", ".join(f"{name} (pid {pid})" for pid, name in processes)


def _clear_dashboard_processes(
    *, poll_attempts: int = 20, poll_interval: float = 0.5
) -> tuple[_ClearState, list[tuple[int, str]]]:
    """Stop everything running from the dashboard tool roots, and verify.

    Returns (state, processes):
      ("clear", stopped)  - nothing runs there any more; `stopped` is what was killed
      ("blocked", still)  - kills issued but processes remain; upgrading now
                            would destroy the venv, so the caller must NOT
      ("unknown", killed) - the probe could not run (before or after the
                            kills); proceed, but say so

    taskkill runs per PID without /T: the enumeration already includes every
    process running from the roots, and a tree kill both double-kills and
    turns PID reuse between iterations into a real cross-process hazard.
    taskkill returns when termination is REQUESTED, not when handles drop, so
    a re-probe poll stands between the kill and any destructive step.
    """
    if sys.platform != "win32":
        return ("clear", [])
    processes = _windows_tool_processes()
    if processes is None:
        return ("unknown", [])
    if not processes:
        return ("clear", [])
    for pid, name in processes:
        result = subprocess_utils.run(
            ["taskkill", "/F", "/PID", str(pid)], check=False, timeout=15
        )
        if result.returncode != 0:
            reason = (result.stderr or result.stdout or "").strip() or f"exit {result.returncode}"
            ui.warn(f"Could not stop {name} (pid {pid}): {reason}")
    remaining: list[tuple[int, str]] | None = processes
    for _ in range(poll_attempts):
        remaining = _windows_tool_processes(quiet=True)
        if remaining is None:
            # We killed everything we could see and can no longer verify.
            return ("unknown", processes)
        if remaining == []:
            return ("clear", processes)
        time.sleep(poll_interval)
    return ("blocked", remaining or [])


def _report_windows_dashboard_failure() -> None:
    """Explain a failed Windows dashboard install honestly, then let it propagate.

    Never exits: install_components' per-component isolation depends on the
    CommandFailed reaching it, and everything after the dashboard (statusline,
    rules, commands, MCP clients) must still install.
    """
    blockers = _windows_tool_processes()
    if blockers is None:
        ui.warn(
            "The dashboard upgrade failed, and what is running from its install "
            "directory could not be checked. Stop the dashboard manually (or "
            "reboot), then re-run: missioncache-install --update"
        )
    elif blockers:
        ui.warn(
            "The dashboard upgrade could not replace its files: these are still "
            f"running from them - {_describe_processes(blockers)}. Stop them (or "
            "reboot) and re-run: missioncache-install --update"
        )
    else:
        roots = ", ".join(_dashboard_tool_roots())
        ui.warn(
            "The dashboard upgrade failed partway (no processes found running "
            f"from {roots}). Re-run: missioncache-install --update"
        )
    ui.detail(
        "Until it succeeds the dashboard install is incomplete: the dashboard "
        "is down right now, the statusline will not render, and autostart at "
        "next logon will not fix it - this is not a case of keeping the "
        "previous version. Re-run the update once the blockers are gone."
    )


def install_dashboard(ctx: InstallContext) -> None:
    """Install missioncache-dashboard and register it as a background service.

    Also wires the PostToolUse edit-count HTTP hook that the statusline needs.

    On Windows the running dashboard is stopped first: it runs out of the very
    tool directory the upgrade must replace, and Windows cannot delete a
    directory holding a running executable. The failure is worse than a no-op
    (uv removes site-packages before failing on Scripts, leaving a broken
    venv), so whenever processes are known to remain the upgrade is refused -
    an intact old venv beats a half-deleted one. The service registration
    right after the install restarts the server, mirroring launchd on macOS.
    """
    ui.step("2", "Dashboard")
    if ctx.mode == "local":
        _pip_install_editable(_require_repo(ctx) / "missioncache-dashboard")
    else:
        state_kind, stopped = _clear_dashboard_processes()
        if stopped and state_kind == "clear":
            ui.detail(
                "Stopped the running dashboard so Windows releases its files: "
                + _describe_processes(stopped)
            )
        elif state_kind == "blocked":
            ui.warn(
                "These are still running from the dashboard's install directory "
                f"after taskkill: {_describe_processes(stopped)}. Upgrading now "
                "would destroy the install, so the dashboard upgrade is skipped. "
                "Stop them (or reboot), then re-run: missioncache-install --update"
            )
            raise DashboardUpgradeBlocked(
                "dashboard executables still running; upgrade refused to protect the venv"
            )
        elif state_kind == "unknown":
            ui.warn(
                "Proceeding with the dashboard upgrade without the pre-check. "
                "If it fails, stop the dashboard yourself and re-run."
            )
        # Windows gets bounded retries: the statusline ships in the same venv
        # and spawns its python on every prompt render, so a short-lived
        # locker can appear between our verify and uv's delete. The retry is
        # not conditioned on the error text - run_streaming inherits stdio,
        # so the exception carries none. A bounded retry on any failure is
        # harmless: a non-lock failure just fails again.
        attempts = 3 if sys.platform == "win32" else 1
        for attempt in range(1, attempts + 1):
            try:
                _pipx_install("missioncache-dashboard")
                break
            except subprocess_utils.CommandFailed:
                if attempt == attempts:
                    if sys.platform == "win32":
                        _report_windows_dashboard_failure()
                    raise
                ui.detail(
                    f"Dashboard install failed (attempt {attempt}/{attempts}) - "
                    "clearing lockers and retrying"
                )
                retry_kind, retry_stopped = _clear_dashboard_processes()
                stopped = stopped + retry_stopped
                if retry_kind == "blocked":
                    # The invariant is "never upgrade while blocked" - it must
                    # hold on retries exactly as it does on the first pass.
                    ui.warn(
                        "Processes reappeared in the dashboard's install directory "
                        f"and would not stop: {_describe_processes(retry_stopped)}. "
                        "The upgrade is skipped to protect the install. Stop them "
                        "(or reboot), then re-run: missioncache-install --update"
                    )
                    raise DashboardUpgradeBlocked(
                        "dashboard executables reappeared mid-upgrade; refused to continue"
                    ) from None
                time.sleep(attempt)
        if stopped and ctx.skip_service:
            ui.warn(
                "The dashboard was stopped for the upgrade and --no-service means it "
                "is not restarted. Start it with: missioncache-dashboard serve"
            )
    if ctx.skip_service:
        ui.detail("Skipping service registration (--no-service)")
    else:
        _register_dashboard_service(ctx.port)
    if settings.ensure_edit_count_hook():
        ui.detail("Wired PostToolUse edit-count HTTP hook")
    state.record_component(
        "dashboard",
        {
            "mode": ctx.mode,
            "service": _service_kind(ctx.skip_service),
            "port": ctx.port,
        },
    )
    ui.success(f"Dashboard installed (port {ctx.port})")


def _register_dashboard_service(port: int) -> None:
    """Delegate to `missioncache-dashboard install-service` (ships with the dashboard pkg)."""
    binary = shutil.which("missioncache-dashboard")
    if not binary:
        ui.warn(
            "missioncache-dashboard not on PATH - restart your shell and run: "
            "missioncache-dashboard install-service"
        )
        return
    cmd = [binary, "install-service"]
    if port != 8787:
        cmd.extend(["--port", str(port)])
    try:
        subprocess_utils.run_streaming(cmd)
    except subprocess_utils.CommandFailed as e:
        ui.warn(f"Service registration failed (exit {e.returncode}).")
        ui.detail(f"You can retry manually: {' '.join(cmd)}")


def uninstall_dashboard(ctx: InstallContext) -> None:
    """Uninstall service, pipx package (unless editable), and edit-count hook."""
    if shutil.which("missioncache-dashboard"):
        try:
            subprocess_utils.run_streaming(["missioncache-dashboard", "uninstall-service"])
        except subprocess_utils.CommandFailed:
            ui.warn("missioncache-dashboard uninstall-service failed (non-fatal)")
    if _recorded_mode("dashboard", ctx) != "local":
        _pipx_uninstall("missioncache-dashboard")
    settings.remove_edit_count_hook()
    state.remove_component("dashboard")
    ui.detail("Dashboard uninstalled")


# ---------------------------------------------------------------------------
# missioncache-auto CLI
# ---------------------------------------------------------------------------

def install_missioncache_auto(ctx: InstallContext) -> None:
    """Install the missioncache-auto CLI via pipx (or editable in local mode)."""
    ui.step("3", "missioncache-auto CLI")
    if ctx.mode == "local":
        _pip_install_editable(_require_repo(ctx) / "missioncache-auto")
    else:
        _pipx_install("missioncache-auto")
    if shutil.which("missioncache-auto"):
        ui.detail(f"missioncache-auto available at {shutil.which('missioncache-auto')}")
    else:
        ui.warn("missioncache-auto not on PATH - restart your shell")
    state.record_component("missioncache_auto", {"mode": ctx.mode})
    ui.success("missioncache-auto installed")


def uninstall_missioncache_auto(ctx: InstallContext) -> None:
    if _recorded_mode("missioncache_auto", ctx) != "local":
        _pipx_uninstall("missioncache-auto")
    state.remove_component("missioncache_auto")
    ui.detail("missioncache-auto uninstalled")


# ---------------------------------------------------------------------------
# Statusline - touches settings.json, so extra-careful about user consent
# ---------------------------------------------------------------------------

def _is_our_statusline(command: object) -> bool:
    """Whether a statusLine.command is MissionCache's own.

    One predicate for all three consent/probe sites so they agree. Matches on
    the resolved basename, not a substring: on Windows we write an absolute
    path (``C:/.../missioncache-statusline.exe``, possibly quoted), which an
    exact ``== "missioncache-statusline"`` misses, while a plain substring test
    would wrongly claim a user wrapper that merely mentions the name (e.g.
    ``my-status --fallback missioncache-statusline``) and silently overwrite
    it - the invariant the module docstring promises never to break. Non-string
    commands (a malformed settings.json) are simply not ours.
    """
    if not isinstance(command, str):
        return False
    s = command.strip()
    if not s:
        return False
    if s.startswith('"'):
        # A quoted executable is one token even if its path holds a space
        # (the Windows form); take up to the closing quote.
        end = s.find('"', 1)
        token = s[1:end] if end != -1 else s[1:]
    else:
        # Unquoted: the executable is the first whitespace-delimited word, so a
        # user wrapper like "my-status --fallback missioncache-statusline" is
        # correctly NOT ours.
        token = s.split()[0]
    stem = Path(token).name
    return stem in ("missioncache-statusline", "missioncache-statusline.exe")


def _statusline_command() -> str:
    """The statusLine command written to settings.json.

    POSIX uses the bare entry-point name (resolved via PATH at render time).
    Windows writes the absolute path with forward slashes: Claude Code may
    run the statusline via Git Bash, which eats backslashes in an unquoted
    command, and the pipx/uv scripts dir is not guaranteed on that shell's
    PATH. Falls back to the bare name when the exe is not resolvable yet
    (dashboard component skipped or shell not restarted).
    """
    if sys.platform != "win32":
        return "missioncache-statusline"
    found = shutil.which("missioncache-statusline")
    if not found:
        # The bare name is what this function exists to avoid on Windows (Git
        # Bash eats backslashes, the scripts dir may not be on that shell's
        # PATH), so returning it is a known-degraded result - say so instead
        # of writing it silently under a green "Statusline wired".
        ui.warn(
            "missioncache-statusline is not on PATH yet - wired the bare name, "
            "which may not resolve in Claude Code's statusline shell. Restart "
            "your shell and re-run: missioncache-install --statusline"
        )
        return "missioncache-statusline"
    # Explicit replace, not Path.as_posix(): PosixPath treats backslash as a
    # filename character and returns the string unchanged, so the conversion
    # would silently depend on which platform class pathlib instantiates.
    # A forward slash is never legal inside a Windows path component, so the
    # blanket replace is safe.
    path = found.replace("\\", "/")
    # A space in the path (C:/Users/Jane Doe/...) splits an unquoted command
    # in every shell. Double quotes fix Git Bash (the default statusline shell
    # when installed); a PowerShell-only machine treats a quoted string as an
    # expression, but the unquoted form is equally broken there, so quoting
    # only when needed strictly improves.
    if any(ch.isspace() for ch in path):
        return f'"{path}"'
    return path


def install_statusline(ctx: InstallContext) -> bool:
    """Wire settings.json statusLine -> `missioncache-statusline`.

    The entry point itself is installed by install_dashboard (missioncache-statusline
    ships in the missioncache-dashboard PyPI package).

    Respects user consent: if an existing statusLine points at something
    non-MissionCache, shows the current command and asks before overwriting. Returns
    True if the statusline was wired, False if the user declined.
    """
    ui.step("4", "Statusline")

    # Legacy: old setup.sh installed a symlink at ~/.claude/scripts/statusline.py.
    # The pip entry point missioncache-statusline supersedes it. Back up or remove cleanly.
    legacy = Path.home() / ".claude" / "scripts" / "statusline.py"
    if legacy.is_symlink():
        legacy.unlink()
        ui.detail("Removed legacy ~/.claude/scripts/statusline.py symlink")
    elif legacy.is_file():
        bak = legacy.with_suffix(".py.bak")
        legacy.rename(bak)
        ui.detail(f"Backed up legacy statusline.py -> {bak}")

    existing = settings.load().get("statusLine")
    current_cmd = None
    if isinstance(existing, dict):
        current_cmd = existing.get("command")

    if ctx.updating and current_cmd and not _is_our_statusline(current_cmd):
        # The user pointed statusLine at something else after installing.
        # An update refreshes MissionCache's own pieces; it must not win the
        # statusline back, and it must not stall a non-interactive update on
        # a consent prompt either.
        ui.info(
            "statusLine in ~/.claude/settings.json points at something else - "
            "leaving it untouched."
        )
        ui.detail(f"  current command: {current_cmd}")
        ui.detail("  To re-wire MissionCache's statusline: missioncache-install --statusline")
        return False

    if current_cmd and not _is_our_statusline(current_cmd):
        ui.warn(f"An existing statusLine is wired in ~/.claude/settings.json:")
        ui.detail(f"  command: {current_cmd}")
        ui.detail("Overwriting will back up the current value to settings.json.bak")
        if not (ctx.assume_yes or ui.ask_yn("Replace it with missioncache-statusline?", default=False)):
            ui.info("Keeping your existing statusline. Skipping.")
            return False

    command = _statusline_command()
    bak = settings.set_statusline(command)
    if bak:
        ui.detail(f"Backed up previous statusLine to {bak}")
    state.record_component(
        "statusline",
        {"command": command, "backup": str(bak) if bak else None},
    )
    ui.success(f"Statusline wired ({command})")
    return True


def uninstall_statusline(ctx: InstallContext) -> None:
    """Remove the statusLine block. Leaves any .bak file alone for manual restore."""
    info = state.load().get("components", {}).get("statusline", {})
    bak_path = info.get("backup")
    if bak_path:
        ui.detail(f"Your previous statusline is preserved at {bak_path}")
        ui.detail("Restore it manually or re-run missioncache-install to wire a new one.")
    settings.unset_statusline()
    state.remove_component("statusline")


# ---------------------------------------------------------------------------
# Rules (~/.claude/rules/)
# ---------------------------------------------------------------------------

def install_rules(ctx: InstallContext) -> None:
    """Install rule files to ~/.claude/rules/.

    PyPI: copy bundled files out of missioncache_install.bundled.rules.
    Local: symlink from <repo>/rules/ so maintainer edits are live.

    Existing files with different content are backed up to .bak; existing
    symlinks are replaced; existing MissionCache-managed files (marker: `<!-- missioncache-plugin:managed -->`
    on line 1) are refreshed in place.
    """
    ui.step("5", "Rules")
    dst = Path.home() / ".claude" / "rules"
    dst.mkdir(parents=True, exist_ok=True)
    if ctx.mode == "local":
        _symlink_md_dir(_require_repo(ctx) / "rules", dst)
    else:
        _copy_bundled_dir(
            "missioncache_install.bundled.rules", dst, ownership="marker"
        )
    state.record_component(
        "rules",
        {"mode": "symlink" if ctx.mode == "local" else "copy"},
    )
    ui.success("Rules installed")


def uninstall_rules(ctx: InstallContext) -> None:
    """Remove MissionCache-managed rule files from ~/.claude/rules/.

    Symlinks pointing into the repo or the bundled package are removed.
    Regular files are removed only if they carry the `<!-- missioncache-plugin:managed -->`
    marker on line 1; unmarked files are treated as user-owned and left alone.
    """
    dst = Path.home() / ".claude" / "rules"
    if not dst.exists():
        state.remove_component("rules")
        return
    removed = 0
    for f in dst.glob("*.md"):
        if f.is_symlink():
            try:
                target = f.resolve(strict=False)
            except OSError:
                continue
            if "missioncache_install/bundled" in str(target) or target.parent.name == "rules":
                f.unlink()
                removed += 1
            continue
        try:
            first = f.read_text(errors="replace").split("\n", 1)[0]
        except OSError:
            continue
        if "missioncache-plugin:managed" in first:
            f.unlink()
            removed += 1
    ui.detail(f"Removed {removed} MissionCache-managed rule file(s)")
    state.remove_component("rules")


# ---------------------------------------------------------------------------
# User-level slash commands (~/.claude/commands/)
# ---------------------------------------------------------------------------

def install_user_commands(ctx: InstallContext) -> None:
    """Install /whats-new and /optimize-prompt into ~/.claude/commands/."""
    ui.step("6", "User commands")
    dst = Path.home() / ".claude" / "commands"
    dst.mkdir(parents=True, exist_ok=True)
    if ctx.mode == "local":
        _symlink_md_dir(_require_repo(ctx) / "user-commands", dst)
    else:
        _copy_bundled_dir(
            "missioncache_install.bundled.user_commands", dst, ownership="filename"
        )
    state.record_component(
        "user_commands",
        {"mode": "symlink" if ctx.mode == "local" else "copy"},
    )
    ui.success("User commands installed")


def uninstall_user_commands(ctx: InstallContext) -> None:
    """Remove /whats-new and /optimize-prompt from ~/.claude/commands/.

    Only removes the specific filenames missioncache-install installs. Any other
    user-level commands (whether existing or added by hand) are untouched.
    """
    dst = Path.home() / ".claude" / "commands"
    if not dst.exists():
        state.remove_component("user_commands")
        return
    removed = 0
    for name in USER_COMMAND_FILES:
        f = dst / name
        if f.is_symlink() or f.exists():
            f.unlink()
            removed += 1
    ui.detail(f"Removed {removed} user command(s)")
    state.remove_component("user_commands")


# ---------------------------------------------------------------------------
# missioncache-db CLI
# ---------------------------------------------------------------------------

def install_missioncache_db(ctx: InstallContext) -> None:
    """Install the missioncache-db CLI as a standalone tool for terminal task management."""
    ui.step("7", "missioncache-db CLI")
    if ctx.mode == "local":
        _pip_install_editable(_require_repo(ctx) / "missioncache-db")
    else:
        _pipx_install("missioncache-db")
    if shutil.which("missioncache-db"):
        ui.detail(f"missioncache-db available at {shutil.which('missioncache-db')}")
    else:
        ui.warn("missioncache-db not on PATH - restart your shell")
    state.record_component("missioncache_db", {"mode": ctx.mode})
    ui.success("missioncache-db installed")


def uninstall_missioncache_db(ctx: InstallContext) -> None:
    if _recorded_mode("missioncache_db", ctx) != "local":
        _pipx_uninstall("missioncache-db")
    state.remove_component("missioncache_db")
    ui.detail("missioncache-db uninstalled")


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------

# Ignore list for the Windows copytree fallback: VCS/build/cache dirs plus the
# file kinds a maintainer clone routinely carries gitignored (secrets, local
# DBs) that must never land under ~/.claude/plugins/ as plugin content.
_COPYTREE_IGNORE = shutil.ignore_patterns(
    ".git", ".venv", "__pycache__", "dist", "build", "node_modules",
    "*.egg-info", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    ".env", ".env.*", ".envrc", "*.pem", "*.key", "*.db", "*.sqlite", "*.sqlite3",
)


def _links_to(link: Path, target: Path) -> bool:
    """Whether an existing symlink already points at target.

    Compares normalized paths rather than raw readlink() output: on Windows
    readlink() returns the extended-length form (\\\\?\\C:\\...) while the
    target is a plain C:\\... path, so a raw == is always False there and the
    installer would tear down and re-create a correct link on every run.
    """
    try:
        # samefile asks the filesystem (device + file index), so it is immune
        # to path spelling entirely - the \\?\ prefix, 8.3 short names, and
        # case all stop mattering. It follows the link, which is exactly the
        # question: does this link land on target?
        return link.exists() and os.path.samefile(link, target)
    except OSError:
        return False


def _symlink_or_copy(link: Path, target: Path) -> bool:
    """symlink_to, with a copy fallback for Windows without Developer Mode.

    Returns True when a real symlink was made, False when it degraded to a
    copy - so callers phrase their own message and record the right mode
    instead of claiming a symlink over a copy.

    Creating a symlink on Windows needs either elevation or Developer Mode;
    without them the OS refuses with WinError 1314 ("A required privilege is
    not held by the client"). Local mode is a maintainer convenience, so
    degrade to a copy - edits to the clone then need a re-run of
    missioncache-install --local to propagate - rather than failing the install.
    """
    try:
        link.symlink_to(target)
        return True
    except OSError as e:
        if getattr(e, "winerror", None) != 1314:
            raise
    if target.is_dir():
        shutil.copytree(target, link, ignore=_COPYTREE_IGNORE)
    else:
        shutil.copy2(target, link)
    ui.warn(
        f"Symlinks need Windows Developer Mode - copied {target.name} instead. "
        "Enable Developer Mode (Settings > System > For developers) and re-run "
        "for live-updating links."
    )
    return False


def _symlink_md_dir(src_dir: Path, dst_dir: Path) -> None:
    """For each *.md in src_dir, symlink into dst_dir. Backs up regular files."""
    if not src_dir.is_dir():
        ui.warn(f"Source not found: {src_dir}")
        return
    for src in sorted(src_dir.glob("*.md")):
        link = dst_dir / src.name
        if link.is_symlink():
            if _links_to(link, src):
                ui.detail(f"Already linked: {src.name}")
                continue
            link.unlink()
        elif link.exists():
            bak = link.with_suffix(link.suffix + ".bak")
            # Never overwrite an existing .bak (the user's real original,
            # preserved on the first run). Without this guard the third
            # Windows-no-Developer-Mode install crashes: the copy fallback
            # leaves a regular file, so every run re-enters this branch, and
            # Path.rename onto an existing target raises FileExistsError on
            # Windows (POSIX silently replaces). Matches _copy_bundled_dir's
            # never-overwrite-.bak contract.
            if bak.exists():
                link.unlink()
            else:
                link.rename(bak)
                ui.detail(f"Backed up existing {src.name} -> {src.name}.bak")
        if _symlink_or_copy(link, src):
            ui.detail(f"Linked {src.name}")
        else:
            ui.detail(f"Copied {src.name}")


def _copy_bundled_dir(
    package_path: str,
    dst_dir: Path,
    *,
    ownership: Literal["marker", "filename"],
) -> None:
    """Copy every *.md file out of the bundled package into dst_dir.

    package_path: dotted path to the bundled resource package, e.g.
    "missioncache_install.bundled.rules".

    ownership decides what may be overwritten (mirroring the two uninstall
    contracts):
    - "marker": a destination file is MissionCache-managed only when line 1
      carries MANAGED_MARKER. Managed files refresh in place; unmarked files
      are user-owned and are never touched.
    - "filename": the bundled filenames belong to MissionCache by contract
      (user commands, where YAML frontmatter must start at line 1 so a marker
      is impossible). A different existing file is backed up to .bak once;
      an existing .bak is never overwritten by a later run.
    """
    try:
        src_files = resources.files(package_path)
    except (ModuleNotFoundError, FileNotFoundError):
        ui.warn(f"Bundled package {package_path} not found - skipping")
        return
    for item in src_files.iterdir():
        if not item.name.endswith(".md"):
            continue
        _install_md_file(item.name, item.read_text(encoding="utf-8"), dst_dir, ownership=ownership)


def _install_md_file(
    name: str,
    new_content: str,
    dst_dir: Path,
    *,
    ownership: Literal["marker", "filename"],
) -> None:
    """Write one bundled file into dst_dir under the given ownership policy."""
    dst = dst_dir / name
    if dst.is_symlink():
        if ownership == "filename":
            # Filename contract: the name is MissionCache's; replacing the
            # link never destroys its target file.
            dst.unlink()
            dst.write_text(new_content, encoding="utf-8")
            ui.detail(f"Installed {name} (replaced symlink)")
            return
        # Marker policy applies to the LINK TARGET's content: a user who
        # wires this filename as a symlink into their own dotfiles owns it
        # exactly like a regular unmarked file, and must not lose the wiring.
        try:
            existing = dst.read_text(encoding="utf-8")  # follows the link
        except OSError:
            # Dangling link - not functioning config; replace it.
            dst.unlink()
            dst.write_text(new_content, encoding="utf-8")
            ui.detail(f"Installed {name} (replaced broken symlink)")
            return
        if existing == new_content:
            ui.detail(f"{name} already up to date (symlink left in place)")
            return
        first_line = existing.split("\n", 1)[0]
        if MANAGED_MARKER in first_line:
            # A local-mode leftover pointing at MissionCache-managed content;
            # a pypi install deliberately converts it to a real copy.
            dst.unlink()
            dst.write_text(new_content, encoding="utf-8")
            ui.detail(f"Refreshed {name} (replaced symlink)")
        else:
            ui.warn(
                f"{name} is a symlink to content without the MissionCache "
                "managed marker - treating it as user-owned and leaving it "
                "untouched."
            )
            ui.detail(
                "  To let MissionCache manage it again, remove your symlink "
                "and re-run the installer."
            )
        return
    if not dst.exists():
        dst.write_text(new_content, encoding="utf-8")
        ui.detail(f"Installed {name}")
        return
    try:
        existing = dst.read_text(encoding="utf-8")
    except OSError as e:
        ui.warn(f"Could not read existing {name} ({e}) - leaving it untouched")
        return
    if existing == new_content:
        ui.detail(f"{name} already up to date")
        return

    if ownership == "marker":
        first_line = existing.split("\n", 1)[0]
        if MANAGED_MARKER in first_line:
            dst.write_text(new_content, encoding="utf-8")
            ui.detail(f"Refreshed {name}")
        else:
            ui.warn(
                f"{name} exists without the MissionCache managed marker - "
                "treating it as user-owned and leaving it untouched."
            )
            ui.detail(
                "  To let MissionCache manage it again, move your file aside "
                "and re-run the installer."
            )
        return

    # ownership == "filename": we own this name, but the first backup is the
    # user's original and must survive every later run.
    bak = dst.with_suffix(dst.suffix + ".bak")
    if not bak.exists():
        dst.rename(bak)
        ui.detail(f"Backed up existing {name} -> {name}.bak")
    dst.write_text(new_content, encoding="utf-8")
    ui.detail(f"Installed {name}")


# ---------------------------------------------------------------------------
# pip / pipx helpers (stubs)
# ---------------------------------------------------------------------------

def _pipx_install(package: str) -> None:
    """Install or upgrade a package via pipx. Falls back to `uv tool install`.

    Uses --force so re-installs are idempotent (same code path for --update).
    Prefers a bare `pipx` on PATH; falls back to `python -m pipx` (for users
    who bootstrap'd pipx this session without a shell restart); finally falls
    back to `uv tool install`.

    The uv path also passes --refresh. --force reinstalls but resolves from
    uv's cached index metadata, so an --update run shortly after a release
    reinstalls the version the user already had and reports success. Measured
    2026-08-19 minutes after publishing: --force alone kept 1.0.17 and 1.0.4,
    --force --refresh took 1.0.18 and 1.0.5 in the same minute. The pipx
    branches are left alone because that behaviour was not tested here.
    """
    if shutil.which("pipx"):
        cmd = ["pipx", "install", package, "--force"]
    elif _has_pipx_module():
        cmd = [sys.executable, "-m", "pipx", "install", package, "--force"]
    elif shutil.which("uv"):
        cmd = ["uv", "tool", "install", package, "--force", "--refresh"]
    else:
        ui.fail(f"Cannot install {package}: neither pipx nor uv is available.")
        return
    ui.detail(f"Running: {' '.join(cmd)}")
    subprocess_utils.run_streaming(cmd)


def _pipx_uninstall(package: str) -> None:
    """Uninstall a pipx/uv-managed package. Silent no-op if not installed."""
    if shutil.which("pipx"):
        cmd = ["pipx", "uninstall", package]
    elif _has_pipx_module():
        cmd = [sys.executable, "-m", "pipx", "uninstall", package]
    elif shutil.which("uv"):
        cmd = ["uv", "tool", "uninstall", package]
    else:
        ui.warn(f"Cannot uninstall {package}: neither pipx nor uv available.")
        return
    try:
        subprocess_utils.run(cmd)
        ui.detail(f"Uninstalled {package}")
    except subprocess_utils.CommandFailed as e:
        combined = (e.stderr + e.stdout).lower()
        if "not installed" in combined or "nothing to uninstall" in combined:
            ui.detail(f"{package} was not installed")
        else:
            ui.warn(f"{package} uninstall failed: {e.stderr.strip()}")


def _has_pipx_module() -> bool:
    """True if `python -m pipx` works (pipx installed but not on PATH yet)."""
    try:
        subprocess_utils.run([sys.executable, "-c", "import pipx"])
        return True
    except subprocess_utils.CommandFailed:
        return False


def _pip_install_editable(path: Path) -> None:
    """`python -m pip install -e <path>` for --local maintainer installs."""
    ui.detail(f"pip install -e {path}")
    subprocess_utils.run_streaming(
        [sys.executable, "-m", "pip", "install", "-e", str(path), "--quiet"]
    )


def _require_repo(ctx: InstallContext) -> Path:
    """Narrow ctx.repo_root to Path - invariant in local mode. Fails loudly otherwise."""
    if ctx.repo_root is None:
        raise RuntimeError(
            "Internal error: local-mode installer called without repo_root set"
        )
    return ctx.repo_root


def _recorded_mode(component: str, ctx: InstallContext) -> Mode:
    """Install-time mode for a component, preferring recorded state over ctx.mode.

    Uninstall and update re-derive ctx.mode from the current directory (see
    __main__._resolve_mode_and_repo), which is wrong when they run from a
    different directory than the install (e.g. uninstalling from a clone a
    package that was pipx-installed). The pipx-vs-editable distinction must
    follow how the component was actually installed, so read the per-component
    mode persisted at install time; fall back to ctx.mode only when nothing
    was recorded (manual install, or state predating this field).
    """
    recorded = state.load().get("components", {}).get(component, {}).get("mode")
    if recorded in ("pypi", "local"):
        return recorded
    return ctx.mode


def _service_kind(skip: bool) -> str:
    if skip:
        return "none"
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform.startswith("linux"):
        return "systemd"
    if sys.platform == "win32":
        return "schtasks"
    return "manual"


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

# Order matters: plugin first (creates ~/.claude/ structure expected by hooks),
# dashboard before statusline (statusline entry point ships with dashboard pkg),
# missioncache-auto is standalone.
ALL_COMPONENTS: tuple[str, ...] = (
    "plugin",
    "dashboard",
    "missioncache_auto",
    "statusline",
    "rules",
    "user_commands",
    "missioncache_db",
    "codex",
    "codex_commands",
    "opencode",
    "opencode_commands",
    "vscode",
    "vscode_commands",
)

_INSTALLERS = {
    "plugin": install_plugin,
    "dashboard": install_dashboard,
    "missioncache_auto": install_missioncache_auto,
    "statusline": install_statusline,
    "rules": install_rules,
    "user_commands": install_user_commands,
    "missioncache_db": install_missioncache_db,
    "codex": mcp_clients.install_codex,
    "codex_commands": command_clients.install_codex_commands,
    "opencode": mcp_clients.install_opencode,
    "opencode_commands": command_clients.install_opencode_commands,
    "vscode": mcp_clients.install_vscode,
    "vscode_commands": command_clients.install_vscode_commands,
}

_UNINSTALLERS = {
    "plugin": uninstall_plugin,
    "dashboard": uninstall_dashboard,
    "missioncache_auto": uninstall_missioncache_auto,
    "statusline": uninstall_statusline,
    "rules": uninstall_rules,
    "user_commands": uninstall_user_commands,
    "missioncache_db": uninstall_missioncache_db,
    "codex": mcp_clients.uninstall_codex,
    "codex_commands": command_clients.uninstall_codex_commands,
    "opencode": mcp_clients.uninstall_opencode,
    "opencode_commands": command_clients.uninstall_opencode_commands,
    "vscode": mcp_clients.uninstall_vscode,
    "vscode_commands": command_clients.uninstall_vscode_commands,
}


def install_components(components: list[str], ctx: InstallContext) -> list[str]:
    """Run install for each component in ALL_COMPONENTS order.

    Isolates per-component failures: a CommandFailed from one installer is
    reported and the remaining (independent) components still run - a failed
    dashboard pipx install must not skip the pure-file-copy rules/commands that
    follow it. Returns the list of components that failed so callers can render
    an honest summary. Programming/invariant errors (anything other than
    CommandFailed) still propagate so real bugs are not silently swallowed.
    """
    ordered = [c for c in ALL_COMPONENTS if c in components]
    failed: list[str] = []
    for c in ordered:
        try:
            _INSTALLERS[c](ctx)
        except subprocess_utils.CommandFailed as e:
            reason = e.stderr.strip() or e.stdout.strip() or str(e)
            ui.warn(f"{c.replace('_', '-')} failed to install: {reason}")
            failed.append(c)
    # Failed components never reach record_component, so without this they
    # would vanish from state and the retry advice below could not work.
    state.update_failures(attempted=ordered, failed=failed)
    _invalidate_update_cache()
    if failed:
        ui.warn(
            "Some components did not install: "
            + ", ".join(c.replace("_", "-") for c in failed)
            + ". Re-run `missioncache-install --update` after fixing the issue(s).",
            stderr=True,
        )
    return failed


def uninstall_components(components: list[str], ctx: InstallContext) -> None:
    """Uninstall in reverse order of ALL_COMPONENTS."""
    ordered = [c for c in reversed(ALL_COMPONENTS) if c in components]
    for c in ordered:
        _UNINSTALLERS[c](ctx)
    # An uninstalled component has nothing left to retry.
    state.update_failures(attempted=ordered, failed=[])
    _invalidate_update_cache()


def update_all(ctx: InstallContext) -> None:
    """Refresh what state tracks and retry previously failed installs.

    Never adds components the user did not choose: anything installed on this
    machine but absent from state (a manual/maintainer install, or a reset
    state file) is detected read-only and reported, not acted on.
    """
    st = state.load()
    tracked = list(st.get("components", {}).keys())
    failed_prev = [c for c in state.failed_components() if c not in tracked]
    targets = tracked + failed_prev
    if not targets:
        ui.warn("Nothing to update - no prior install detected in state file.")
        _report_untracked(targets)
        return
    # Update must reinstall in the SAME mode it was installed in, not the mode
    # re-derived from the current directory. The install-time mode was recorded
    # globally at install; prefer it so an update run from a clone doesn't flip
    # a pypi install to an editable one (or vice versa).
    recorded = st.get("mode")
    if recorded in ("pypi", "local") and recorded != ctx.mode:
        # Adopting "local" needs a clone: every local-mode installer resolves
        # paths through _require_repo, and repo_root is only populated when the
        # run started inside one. Without this check the mismatch surfaces as an
        # internal-error traceback partway through the update, after the first
        # component has already printed its step header. State is not enough to
        # recover the path - it records the mode, never where the clone was.
        if recorded == "local" and ctx.repo_root is None:
            ui.fail(
                "This install is recorded as maintainer (local) mode, which updates "
                "from a missioncache clone, but this is not a clone. Either re-run "
                "the update from your clone, or run `uvx missioncache-install` from a "
                "non-clone directory to switch the install to PyPI mode."
            )
        ctx = replace(ctx, mode=recorded)
    ctx = replace(ctx, updating=True)
    ui.info(f"Updating: {', '.join(c.replace('_', '-') for c in tracked)}")
    if failed_prev:
        ui.info(
            "Retrying previously failed: "
            + ", ".join(c.replace("_", "-") for c in failed_prev)
        )
    install_components(targets, ctx)
    _report_untracked(state.installed_components() + state.failed_components())


def _invalidate_update_cache() -> None:
    """Drop the shared update-check cache after installs change anything.

    The statusline and dashboard serve ~/.missioncache/update-check.json for
    up to its 6h TTL; without this, a successful update keeps showing the
    pre-update "update available" answer for hours. Deleting forces the next
    render to recompute against what this run just installed. Computed at call
    time (not a module constant) so Path.home() redirection works in tests.
    """
    cache = Path.home() / ".missioncache" / "update-check.json"
    try:
        cache.unlink(missing_ok=True)
    except OSError:
        pass  # advisory cache; the next TTL expiry recomputes anyway


def _probe_settings() -> dict:
    """settings.json content for read-only probes; {} on any read problem."""
    try:
        return settings.load()
    except Exception:
        return {}


# Read-only evidence that a component is present on this machine even though
# state does not track it. Existence checks only - probes must never mutate.
_UNTRACKED_PROBES = {
    "plugin": lambda: any(
        k.startswith("missioncache@")
        for k in _probe_settings().get("enabledPlugins", {})
    ),
    "dashboard": lambda: shutil.which("missioncache-dashboard") is not None,
    "missioncache_auto": lambda: shutil.which("missioncache-auto") is not None,
    "missioncache_db": lambda: shutil.which("missioncache-db") is not None,
    "statusline": lambda: (
        isinstance(_probe_settings().get("statusLine"), dict)
        and _is_our_statusline(_probe_settings()["statusLine"].get("command"))
    ),
}


def _report_untracked(tracked: list[str]) -> None:
    """Report MissionCache components found installed but absent from state.

    Report-only by design: acting on them could pipx-install over a
    maintainer's editable setup or re-acquire config the user manages
    elsewhere. The user re-adopts explicitly by re-running the installer.
    """
    found = []
    for comp, probe in _UNTRACKED_PROBES.items():
        if comp in tracked:
            continue
        try:
            if probe():
                found.append(comp)
        except Exception:
            continue
    if not found:
        return
    ui.warn(
        "Installed but not tracked by the installer, so not updated: "
        + ", ".join(c.replace("_", "-") for c in found)
    )
    ui.detail(
        "  These may come from a manual/maintainer install or a reset state file."
    )
    ui.detail(
        "  To bring them under --update management, re-run: uvx missioncache-install"
    )

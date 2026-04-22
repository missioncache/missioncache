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
import shutil
import sys
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Literal

from . import settings, state, subprocess_utils, ui


MARKETPLACE_DIR = Path.home() / ".claude" / "plugins" / "local-marketplace"
PLUGIN_GITHUB_SOURCE = "tomerbr1/claude-orbit"
PLUGIN_ID_PYPI = "orbit@claude-orbit"
PLUGIN_ID_LOCAL = "orbit@local"
USER_COMMAND_FILES = ("whats-new.md", "optimize-prompt.md")


Mode = Literal["pypi", "local"]


@dataclass
class InstallContext:
    """Shared options passed to every installer."""

    mode: Mode
    repo_root: Path | None   # populated only in local mode
    skip_service: bool       # --no-service; dashboard installs without launchd/systemd
    port: int                # dashboard port (default 8787)
    assume_yes: bool         # --yes; skip per-file confirmations (still honors component selection)


# ---------------------------------------------------------------------------
# Plugin core (MCP server + commands + hooks + rules-via-plugin)
# ---------------------------------------------------------------------------

def install_plugin(ctx: InstallContext) -> None:
    """Register the orbit plugin with Claude Code.

    PyPI mode: adds the upstream marketplace and installs orbit@claude-orbit.
    Local mode: creates ~/.claude/plugins/local-marketplace pointing at the
    clone, then installs orbit@local. Mirrors setup.sh:152-217.
    """
    ui.step("1", "Core plugin")
    if ctx.mode == "local":
        _install_plugin_local(ctx)
    else:
        _install_plugin_pypi()
    state.record_component(
        "plugin",
        {"mode": "marketplace" if ctx.mode == "pypi" else "local"},
    )
    ui.success("Core plugin installed")


def _install_plugin_pypi() -> None:
    """Add the upstream marketplace and install orbit@claude-orbit."""
    if not shutil.which("claude"):
        ui.warn("Claude CLI not found - skipping plugin registration.")
        ui.detail("After installing Claude Code, run: orbit-install --update")
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

    settings.enable_plugin(PLUGIN_ID_PYPI)
    ui.detail(f"Installing {PLUGIN_ID_PYPI}")
    subprocess_utils.run(["claude", "plugins", "install", PLUGIN_ID_PYPI])


def _install_plugin_local(ctx: InstallContext) -> None:
    """Create a local marketplace symlinking the clone, install orbit@local.

    Ports setup.sh:152-217 to Python.
    """
    repo = _require_repo(ctx)
    plugins_dir = MARKETPLACE_DIR / "plugins"
    marketplace_json = MARKETPLACE_DIR / ".claude-plugin" / "marketplace.json"
    plugin_link = plugins_dir / "orbit"

    plugins_dir.mkdir(parents=True, exist_ok=True)
    marketplace_json.parent.mkdir(parents=True, exist_ok=True)

    _write_local_marketplace_json(marketplace_json)

    if plugin_link.is_symlink():
        if plugin_link.readlink() == repo:
            ui.detail("Plugin symlink already correct")
        else:
            plugin_link.unlink()
            plugin_link.symlink_to(repo)
            ui.detail(f"Updated symlink -> {repo}")
    elif plugin_link.is_dir():
        ui.warn("Removing existing plugins/orbit directory (not a symlink)")
        shutil.rmtree(plugin_link)
        plugin_link.symlink_to(repo)
        ui.detail(f"Created symlink -> {repo}")
    elif plugin_link.exists():
        ui.warn(f"Unexpected file at {plugin_link}; removing")
        plugin_link.unlink()
        plugin_link.symlink_to(repo)
    else:
        plugin_link.symlink_to(repo)
        ui.detail(f"Created symlink -> {repo}")

    settings.enable_plugin(PLUGIN_ID_LOCAL)

    if shutil.which("claude"):
        try:
            subprocess_utils.run(["claude", "plugins", "install", PLUGIN_ID_LOCAL])
            ui.detail(f"Installed {PLUGIN_ID_LOCAL} via Claude CLI")
        except subprocess_utils.CommandFailed as e:
            ui.warn(f"Claude CLI install failed: {e.stderr.strip() or 'unknown error'}")
            ui.detail(f"You can retry with: claude plugins install {PLUGIN_ID_LOCAL}")
    else:
        ui.warn(f"Claude CLI not found. Run: claude plugins install {PLUGIN_ID_LOCAL}")


def _write_local_marketplace_json(path: Path) -> None:
    """Create or update marketplace.json to include orbit. Idempotent."""
    entry = {
        "name": "orbit",
        "source": "./plugins/orbit",
        "description": "Project management with time tracking and autonomous execution",
        "category": "productivity",
    }
    if path.exists():
        data = json.loads(path.read_text())
        plugins = data.setdefault("plugins", [])
        if any(p.get("name") == "orbit" for p in plugins):
            ui.detail("orbit already in marketplace.json")
            return
        plugins.append(entry)
        path.write_text(json.dumps(data, indent=2))
        ui.detail("Added orbit to existing marketplace.json")
        return
    path.write_text(json.dumps({
        "name": "local",
        "owner": {"name": "Tomer Brami"},
        "plugins": [entry],
    }, indent=2))
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

def install_dashboard(ctx: InstallContext) -> None:
    """Install orbit-dashboard and register it as a background service.

    Also wires the PostToolUse edit-count HTTP hook that the statusline needs.
    """
    ui.step("2", "Dashboard")
    if ctx.mode == "local":
        _pip_install_editable(_require_repo(ctx) / "orbit-dashboard")
    else:
        _pipx_install("orbit-dashboard")
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
    """Delegate to `orbit-dashboard install-service` (ships with the dashboard pkg)."""
    binary = shutil.which("orbit-dashboard")
    if not binary:
        ui.warn(
            "orbit-dashboard not on PATH - restart your shell and run: "
            "orbit-dashboard install-service"
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
    if shutil.which("orbit-dashboard"):
        try:
            subprocess_utils.run_streaming(["orbit-dashboard", "uninstall-service"])
        except subprocess_utils.CommandFailed:
            ui.warn("orbit-dashboard uninstall-service failed (non-fatal)")
    if ctx.mode != "local":
        _pipx_uninstall("orbit-dashboard")
    settings.remove_edit_count_hook()
    state.remove_component("dashboard")
    ui.detail("Dashboard uninstalled")


# ---------------------------------------------------------------------------
# orbit-auto CLI
# ---------------------------------------------------------------------------

def install_orbit_auto(ctx: InstallContext) -> None:
    """Install the orbit-auto CLI via pipx (or editable in local mode)."""
    ui.step("3", "orbit-auto CLI")
    if ctx.mode == "local":
        _pip_install_editable(_require_repo(ctx) / "orbit-auto")
    else:
        _pipx_install("orbit-auto")
    if shutil.which("orbit-auto"):
        ui.detail(f"orbit-auto available at {shutil.which('orbit-auto')}")
    else:
        ui.warn("orbit-auto not on PATH - restart your shell")
    state.record_component("orbit_auto", {"mode": ctx.mode})
    ui.success("orbit-auto installed")


def uninstall_orbit_auto(ctx: InstallContext) -> None:
    if ctx.mode != "local":
        _pipx_uninstall("orbit-auto")
    state.remove_component("orbit_auto")
    ui.detail("orbit-auto uninstalled")


# ---------------------------------------------------------------------------
# Statusline - touches settings.json, so extra-careful about user consent
# ---------------------------------------------------------------------------

def install_statusline(ctx: InstallContext) -> bool:
    """Wire settings.json statusLine -> `orbit-statusline`.

    The entry point itself is installed by install_dashboard (orbit-statusline
    ships in the orbit-dashboard PyPI package).

    Respects user consent: if an existing statusLine points at something
    non-orbit, shows the current command and asks before overwriting. Returns
    True if the statusline was wired, False if the user declined.
    """
    ui.step("4", "Statusline")

    # Legacy: old setup.sh installed a symlink at ~/.claude/scripts/statusline.py.
    # The pip entry point orbit-statusline supersedes it. Back up or remove cleanly.
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

    if current_cmd and current_cmd != "orbit-statusline":
        ui.warn(f"An existing statusLine is wired in ~/.claude/settings.json:")
        ui.detail(f"  command: {current_cmd}")
        ui.detail("Overwriting will back up the current value to settings.json.bak")
        if not (ctx.assume_yes or ui.ask_yn("Replace it with orbit-statusline?", default=False)):
            ui.info("Keeping your existing statusline. Skipping.")
            return False

    bak = settings.set_statusline("orbit-statusline")
    if bak:
        ui.detail(f"Backed up previous statusLine to {bak}")
    state.record_component(
        "statusline",
        {"command": "orbit-statusline", "backup": str(bak) if bak else None},
    )
    ui.success("Statusline wired (orbit-statusline)")
    return True


def uninstall_statusline(ctx: InstallContext) -> None:
    """Remove the statusLine block. Leaves any .bak file alone for manual restore."""
    info = state.load().get("components", {}).get("statusline", {})
    bak_path = info.get("backup")
    if bak_path:
        ui.detail(f"Your previous statusline is preserved at {bak_path}")
        ui.detail("Restore it manually or re-run orbit-install to wire a new one.")
    settings.unset_statusline()
    state.remove_component("statusline")


# ---------------------------------------------------------------------------
# Rules (~/.claude/rules/)
# ---------------------------------------------------------------------------

def install_rules(ctx: InstallContext) -> None:
    """Install rule files to ~/.claude/rules/.

    PyPI: copy bundled files out of orbit_install.bundled.rules.
    Local: symlink from <repo>/rules/ so maintainer edits are live.

    Existing files with different content are backed up to .bak; existing
    symlinks are replaced; existing orbit-managed files (marker: `<!-- orbit-plugin:managed -->`
    on line 1) are refreshed in place.
    """
    ui.step("5", "Rules")
    dst = Path.home() / ".claude" / "rules"
    dst.mkdir(parents=True, exist_ok=True)
    if ctx.mode == "local":
        _symlink_md_dir(_require_repo(ctx) / "rules", dst)
    else:
        _copy_bundled_dir("orbit_install.bundled.rules", dst)
    state.record_component(
        "rules",
        {"mode": "symlink" if ctx.mode == "local" else "copy"},
    )
    ui.success("Rules installed")


def uninstall_rules(ctx: InstallContext) -> None:
    """Remove orbit-managed rule files from ~/.claude/rules/.

    Symlinks pointing into the repo or the bundled package are removed.
    Regular files are removed only if they carry the `<!-- orbit-plugin:managed -->`
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
            if "orbit_install/bundled" in str(target) or target.parent.name == "rules":
                f.unlink()
                removed += 1
            continue
        try:
            first = f.read_text(errors="replace").split("\n", 1)[0]
        except OSError:
            continue
        if "orbit-plugin:managed" in first:
            f.unlink()
            removed += 1
    ui.detail(f"Removed {removed} orbit-managed rule file(s)")
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
        _copy_bundled_dir("orbit_install.bundled.user_commands", dst)
    state.record_component(
        "user_commands",
        {"mode": "symlink" if ctx.mode == "local" else "copy"},
    )
    ui.success("User commands installed")


def uninstall_user_commands(ctx: InstallContext) -> None:
    """Remove /whats-new and /optimize-prompt from ~/.claude/commands/.

    Only removes the specific filenames orbit-install installs. Any other
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
# orbit-db CLI
# ---------------------------------------------------------------------------

def install_orbit_db(ctx: InstallContext) -> None:
    """Install the orbit-db CLI as a standalone tool for terminal task management."""
    ui.step("7", "orbit-db CLI")
    if ctx.mode == "local":
        _pip_install_editable(_require_repo(ctx) / "orbit-db")
    else:
        _pipx_install("orbit-db")
    if shutil.which("orbit-db"):
        ui.detail(f"orbit-db available at {shutil.which('orbit-db')}")
    else:
        ui.warn("orbit-db not on PATH - restart your shell")
    state.record_component("orbit_db", {"mode": ctx.mode})
    ui.success("orbit-db installed")


def uninstall_orbit_db(ctx: InstallContext) -> None:
    if ctx.mode != "local":
        _pipx_uninstall("orbit-db")
    state.remove_component("orbit_db")
    ui.detail("orbit-db uninstalled")


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------

def _symlink_md_dir(src_dir: Path, dst_dir: Path) -> None:
    """For each *.md in src_dir, symlink into dst_dir. Backs up regular files."""
    if not src_dir.is_dir():
        ui.warn(f"Source not found: {src_dir}")
        return
    for src in sorted(src_dir.glob("*.md")):
        link = dst_dir / src.name
        if link.is_symlink():
            if link.readlink() == src:
                ui.detail(f"Already linked: {src.name}")
                continue
            link.unlink()
        elif link.exists():
            bak = link.with_suffix(link.suffix + ".bak")
            link.rename(bak)
            ui.detail(f"Backed up existing {src.name} -> {src.name}.bak")
        link.symlink_to(src)
        ui.detail(f"Linked {src.name}")


def _copy_bundled_dir(package_path: str, dst_dir: Path) -> None:
    """Copy every *.md file out of the bundled package into dst_dir.

    package_path: dotted path to the bundled resource package, e.g.
    "orbit_install.bundled.rules". Existing files are backed up to .bak.
    """
    try:
        src_files = resources.files(package_path)
    except (ModuleNotFoundError, FileNotFoundError):
        ui.warn(f"Bundled package {package_path} not found - skipping")
        return
    for item in src_files.iterdir():
        if not item.name.endswith(".md"):
            continue
        dst = dst_dir / item.name
        if dst.exists() or dst.is_symlink():
            bak = dst.with_suffix(dst.suffix + ".bak")
            if dst.is_symlink():
                dst.unlink()
            else:
                dst.rename(bak)
                ui.detail(f"Backed up existing {item.name} -> {item.name}.bak")
        dst.write_text(item.read_text())
        ui.detail(f"Installed {item.name}")


# ---------------------------------------------------------------------------
# pip / pipx helpers (stubs)
# ---------------------------------------------------------------------------

def _pipx_install(package: str) -> None:
    """Install or upgrade a package via pipx. Falls back to `uv tool install`.

    Uses --force so re-installs are idempotent (same code path for --update).
    Prefers a bare `pipx` on PATH; falls back to `python -m pipx` (for users
    who bootstrap'd pipx this session without a shell restart); finally falls
    back to `uv tool install`.
    """
    if shutil.which("pipx"):
        cmd = ["pipx", "install", package, "--force"]
    elif _has_pipx_module():
        cmd = [sys.executable, "-m", "pipx", "install", package, "--force"]
    elif shutil.which("uv"):
        cmd = ["uv", "tool", "install", package, "--force"]
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


def _service_kind(skip: bool) -> str:
    if skip:
        return "none"
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform.startswith("linux"):
        return "systemd"
    return "manual"


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

# Order matters: plugin first (creates ~/.claude/ structure expected by hooks),
# dashboard before statusline (statusline entry point ships with dashboard pkg),
# orbit-auto is standalone.
ALL_COMPONENTS: tuple[str, ...] = (
    "plugin",
    "dashboard",
    "orbit_auto",
    "statusline",
    "rules",
    "user_commands",
    "orbit_db",
)

_INSTALLERS = {
    "plugin": install_plugin,
    "dashboard": install_dashboard,
    "orbit_auto": install_orbit_auto,
    "statusline": install_statusline,
    "rules": install_rules,
    "user_commands": install_user_commands,
    "orbit_db": install_orbit_db,
}

_UNINSTALLERS = {
    "plugin": uninstall_plugin,
    "dashboard": uninstall_dashboard,
    "orbit_auto": uninstall_orbit_auto,
    "statusline": uninstall_statusline,
    "rules": uninstall_rules,
    "user_commands": uninstall_user_commands,
    "orbit_db": uninstall_orbit_db,
}


def install_components(components: list[str], ctx: InstallContext) -> None:
    """Run install for each component in ALL_COMPONENTS order."""
    ordered = [c for c in ALL_COMPONENTS if c in components]
    for c in ordered:
        _INSTALLERS[c](ctx)


def uninstall_components(components: list[str], ctx: InstallContext) -> None:
    """Uninstall in reverse order of ALL_COMPONENTS."""
    ordered = [c for c in reversed(ALL_COMPONENTS) if c in components]
    for c in ordered:
        _UNINSTALLERS[c](ctx)


def update_all(ctx: InstallContext) -> None:
    """Refresh only what's already in state. No new components are added."""
    installed = state.installed_components()
    if not installed:
        ui.warn("Nothing to update - no prior install detected in state file.")
        return
    ui.info(f"Updating: {', '.join(installed)}")
    install_components(installed, ctx)

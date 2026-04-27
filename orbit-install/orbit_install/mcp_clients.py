"""Per-tool MCP server installers for non-Claude AI coding tools.

Three tools are supported in Phase 11.1:

- Codex (CLI: codex 0.125.0+) - registered via `codex mcp add orbit -- mcp-orbit`.
  Codex owns the TOML round-trip, so no third-party TOML library is needed.
- OpenCode (CLI: opencode 1.4.x+) - direct JSON merge into
  ~/.config/opencode/opencode.json. The `opencode mcp add` subcommand is
  interactive-only with no flags, so subprocess-driving is not an option.
- VSCode (Copilot Chat, macOS only for 11.1) - direct JSON merge into
  ~/Library/Application Support/Code/User/mcp.json. Linux/Windows paths
  are deferred to a later phase.

Claude Code uses the existing plugin manifest flow (see install_plugin in
installers.py); this module is only for tools that consume the orbit MCP
server via the `mcp-orbit` binary on PATH. Each writer is idempotent and
warn-and-skips when the tool itself is not installed on the system.

Schemas verified against codex 0.125.0 and opencode 1.4.3 on 2026-04-26;
see ~/.orbit/active/orbit-public-release/multi-tool-research.md for the
full discovery notes.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import state, subprocess_utils, ui

if TYPE_CHECKING:
    from .installers import InstallContext


OPENCODE_CONFIG_PATH = Path.home() / ".config" / "opencode" / "opencode.json"

# VSCode MCP config lives under the user's default profile on macOS. Workspace
# (`<repo>/.vscode/mcp.json`) and per-profile paths are intentionally not touched
# here - workspace config is the user's per-project call, and multi-profile
# support is documented as a known limitation.
VSCODE_USER_MCP_PATH = (
    Path.home() / "Library" / "Application Support" / "Code" / "User" / "mcp.json"
)

# VSCode + Insiders + VSCodium app bundles. `code` is rarely on PATH on macOS,
# so detection is filesystem-based.
VSCODE_APP_PATHS: tuple[Path, ...] = (
    Path("/Applications/Visual Studio Code.app"),
    Path("/Applications/Visual Studio Code - Insiders.app"),
    Path.home() / "Applications" / "Visual Studio Code.app",
    Path("/Applications/VSCodium.app"),
)


# ---------------------------------------------------------------------------
# Shared: ensure mcp-orbit is on PATH (prereq for every non-Claude tool)
# ---------------------------------------------------------------------------

def _ensure_mcp_orbit_on_path() -> bool:
    """Install mcp-orbit via pipx/uv if not already on PATH. Idempotent.

    Returns True when registration should proceed, False when the prereq
    install failed outright. A successful pipx install whose binary has not
    yet propagated into the current process's PATH still returns True - the
    config entry is stored as a literal command string, and the tool resolves
    it on its own next session, after the user's shell rehashes.
    """
    if shutil.which("mcp-orbit"):
        ui.detail(f"mcp-orbit already on PATH at {shutil.which('mcp-orbit')}")
        return True
    ui.detail("Installing mcp-orbit (required for non-Claude MCP clients)")
    # Late import: installers imports this module at the top, so dodge the
    # circular at module-load time. _pipx_install handles pipx -> uv fallback.
    from . import installers
    try:
        installers._pipx_install("mcp-orbit")
    except subprocess_utils.CommandFailed as e:
        ui.warn(f"Failed to install mcp-orbit: {e.stderr.strip() or 'unknown error'}")
        return False
    if shutil.which("mcp-orbit"):
        ui.detail(f"mcp-orbit installed at {shutil.which('mcp-orbit')}")
    else:
        ui.warn("mcp-orbit installed but not on PATH yet - restart your shell to use it")
    return True


# ---------------------------------------------------------------------------
# Codex: codex mcp add orbit -- mcp-orbit
# ---------------------------------------------------------------------------

def install_codex(ctx: "InstallContext") -> None:
    """Register orbit's MCP server with the Codex CLI."""
    ui.step("8", "Codex MCP integration")
    if not shutil.which("codex"):
        ui.warn(
            "Codex CLI not found - skipping. "
            "Install Codex, then run: orbit-install --update"
        )
        ctx.mcp_success["codex"] = False
        return
    if not _ensure_mcp_orbit_on_path():
        ui.warn("Skipping Codex registration - mcp-orbit prereq failed")
        ctx.mcp_success["codex"] = False
        return
    if _codex_orbit_registered():
        ui.detail("orbit already registered with Codex")
        state.record_component("codex", {"command": "mcp-orbit"})
        ui.success("Codex MCP integration confirmed")
        ctx.mcp_success["codex"] = True
        return
    ui.detail("Running: codex mcp add orbit -- mcp-orbit")
    try:
        subprocess_utils.run(["codex", "mcp", "add", "orbit", "--", "mcp-orbit"])
    except subprocess_utils.CommandFailed as e:
        ui.warn(
            f"codex mcp add failed: {e.stderr.strip() or e.stdout.strip() or 'unknown error'}"
        )
        ctx.mcp_success["codex"] = False
        return
    state.record_component("codex", {"command": "mcp-orbit"})
    ui.success("Codex MCP integration installed")
    ctx.mcp_success["codex"] = True


def uninstall_codex(ctx: "InstallContext") -> None:
    """Remove orbit from Codex via `codex mcp remove orbit`. Silent no-op if absent."""
    if not shutil.which("codex"):
        ui.detail("Codex CLI not found - nothing to uninstall")
        state.remove_component("codex")
        return
    if not _codex_orbit_registered():
        ui.detail("orbit not registered with Codex - nothing to remove")
        state.remove_component("codex")
        return
    try:
        subprocess_utils.run(["codex", "mcp", "remove", "orbit"])
        ui.detail("Removed orbit from Codex MCP config")
    except subprocess_utils.CommandFailed as e:
        ui.warn(f"codex mcp remove failed: {e.stderr.strip()}")
    state.remove_component("codex")


def _codex_orbit_registered() -> bool:
    """True if `codex mcp list` includes a line whose first whitespace-stripped token is `orbit`."""
    try:
        result = subprocess_utils.run(["codex", "mcp", "list"])
    except subprocess_utils.CommandFailed:
        return False
    for line in result.stdout.splitlines():
        stripped = line.strip()
        # Codex hasn't documented the exact list output format, so be conservative:
        # match a line whose first token is exactly `orbit`. That covers both
        # "orbit  command  status" tabular layouts and bullet-prefixed forms.
        if stripped == "orbit" or stripped.split(None, 1)[:1] == ["orbit"]:
            return True
    return False


# ---------------------------------------------------------------------------
# OpenCode: idempotent JSON merge into ~/.config/opencode/opencode.json
# ---------------------------------------------------------------------------

def install_opencode(ctx: "InstallContext") -> None:
    """Register orbit in OpenCode's global config via direct JSON merge.

    Preserves all top-level keys we don't own - in particular `$schema`, which
    OpenCode auto-injects on first write - and any other servers under `mcp`.
    """
    ui.step("9", "OpenCode MCP integration")
    if not _opencode_detected():
        ui.warn(
            "OpenCode CLI not found - skipping. "
            "Install OpenCode, then run: orbit-install --update"
        )
        ctx.mcp_success["opencode"] = False
        return
    if not _ensure_mcp_orbit_on_path():
        ui.warn("Skipping OpenCode registration - mcp-orbit prereq failed")
        ctx.mcp_success["opencode"] = False
        return

    desired = {"type": "local", "command": ["mcp-orbit"]}
    OPENCODE_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        data, indent, used_jsonc = _load_json_object(OPENCODE_CONFIG_PATH)
    except json.JSONDecodeError as e:
        ui.warn(f"Cannot parse {OPENCODE_CONFIG_PATH}: {e}. Fix the file and re-run.")
        ctx.mcp_success["opencode"] = False
        return

    mcp = data.get("mcp") if isinstance(data.get("mcp"), dict) else None
    if mcp is not None and mcp.get("orbit") == desired:
        ui.detail(f"orbit already configured in {OPENCODE_CONFIG_PATH}")
    elif used_jsonc:
        # File has comments or trailing commas. json.dumps would silently
        # strip them; refuse to write and tell the user exactly what to add.
        ui.warn(
            f"{OPENCODE_CONFIG_PATH} contains comments or trailing commas. "
            "Auto-merge would strip them. Add this entry manually under "
            f'"mcp" and re-run with --update:\n  "orbit": {json.dumps(desired)}'
        )
        ctx.mcp_success["opencode"] = False
        return
    else:
        if mcp is None:
            mcp = {}
            data["mcp"] = mcp
        mcp["orbit"] = desired
        OPENCODE_CONFIG_PATH.write_text(json.dumps(data, indent=indent))

    state.record_component("opencode", {"path": str(OPENCODE_CONFIG_PATH)})
    ui.success(f"OpenCode MCP integration installed ({OPENCODE_CONFIG_PATH})")
    ctx.mcp_success["opencode"] = True


def uninstall_opencode(ctx: "InstallContext") -> None:
    """Remove `mcp.orbit` from OpenCode config. Preserves every other key."""
    if not OPENCODE_CONFIG_PATH.exists():
        ui.detail("OpenCode config not found - nothing to remove")
        state.remove_component("opencode")
        return
    try:
        data, indent, used_jsonc = _load_json_object(OPENCODE_CONFIG_PATH)
    except json.JSONDecodeError as e:
        ui.warn(f"Cannot parse {OPENCODE_CONFIG_PATH}: {e}. Skipping uninstall.")
        state.remove_component("opencode")
        return
    mcp = data.get("mcp")
    if not isinstance(mcp, dict) or "orbit" not in mcp:
        ui.detail("orbit not present in OpenCode config")
        state.remove_component("opencode")
        return
    if used_jsonc:
        # Same data-loss concern as install: writing back via json.dumps
        # would strip the user's comments. Tell them what to remove instead.
        ui.warn(
            f"{OPENCODE_CONFIG_PATH} contains comments or trailing commas. "
            'Auto-edit would strip them. Remove the "orbit" entry under '
            '"mcp" manually.'
        )
        state.remove_component("opencode")
        return
    mcp.pop("orbit", None)
    OPENCODE_CONFIG_PATH.write_text(json.dumps(data, indent=indent))
    ui.detail(f"Removed orbit from {OPENCODE_CONFIG_PATH}")
    state.remove_component("opencode")


def _opencode_detected() -> bool:
    """True when the opencode binary is on PATH or under ~/.opencode/bin."""
    if shutil.which("opencode"):
        return True
    return (Path.home() / ".opencode" / "bin" / "opencode").exists()


# ---------------------------------------------------------------------------
# VSCode: idempotent JSON merge into ~/Library/.../Code/User/mcp.json (macOS)
# ---------------------------------------------------------------------------

def install_vscode(ctx: "InstallContext") -> None:
    """Register orbit in VSCode's user-level mcp.json (macOS Phase 11.1 only).

    Preserves any existing servers in the file. Copilot Chat picks up changes
    automatically on save - no extension restart needed.
    """
    ui.step("10", "VSCode MCP integration")
    if sys.platform != "darwin":
        ui.warn("VSCode MCP integration is macOS-only in this release - skipping")
        ctx.mcp_success["vscode"] = False
        return
    if not _vscode_detected():
        ui.warn(
            "VSCode not found in /Applications - skipping. "
            "Install VSCode, then run: orbit-install --update"
        )
        ctx.mcp_success["vscode"] = False
        return
    if not _ensure_mcp_orbit_on_path():
        ui.warn("Skipping VSCode registration - mcp-orbit prereq failed")
        ctx.mcp_success["vscode"] = False
        return

    desired = {"type": "stdio", "command": "mcp-orbit"}
    VSCODE_USER_MCP_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        data, indent, used_jsonc = _load_json_object(VSCODE_USER_MCP_PATH)
    except json.JSONDecodeError as e:
        ui.warn(f"Cannot parse {VSCODE_USER_MCP_PATH}: {e}. Fix the file and re-run.")
        ctx.mcp_success["vscode"] = False
        return

    servers = data.get("servers") if isinstance(data.get("servers"), dict) else None
    if servers is not None and servers.get("orbit") == desired:
        ui.detail(f"orbit already configured in {VSCODE_USER_MCP_PATH}")
    elif used_jsonc:
        # File has comments or trailing commas; refuse auto-merge to avoid
        # silently stripping them. mcp.json is JSONC by VSCode convention.
        ui.warn(
            f"{VSCODE_USER_MCP_PATH} contains comments or trailing commas. "
            "Auto-merge would strip them. Add this entry manually under "
            f'"servers" and re-run with --update:\n  "orbit": {json.dumps(desired)}'
        )
        ctx.mcp_success["vscode"] = False
        return
    else:
        if servers is None:
            servers = {}
            data["servers"] = servers
        servers["orbit"] = desired
        VSCODE_USER_MCP_PATH.write_text(json.dumps(data, indent=indent))

    state.record_component("vscode", {"path": str(VSCODE_USER_MCP_PATH)})
    ui.success(f"VSCode MCP integration installed ({VSCODE_USER_MCP_PATH})")
    ctx.mcp_success["vscode"] = True


def uninstall_vscode(ctx: "InstallContext") -> None:
    """Remove `servers.orbit` from VSCode mcp.json. Preserves every other key."""
    if not VSCODE_USER_MCP_PATH.exists():
        ui.detail("VSCode mcp.json not found - nothing to remove")
        state.remove_component("vscode")
        return
    try:
        data, indent, used_jsonc = _load_json_object(VSCODE_USER_MCP_PATH)
    except json.JSONDecodeError as e:
        ui.warn(f"Cannot parse {VSCODE_USER_MCP_PATH}: {e}. Skipping uninstall.")
        state.remove_component("vscode")
        return
    servers = data.get("servers")
    if not isinstance(servers, dict) or "orbit" not in servers:
        ui.detail("orbit not present in VSCode mcp.json")
        state.remove_component("vscode")
        return
    if used_jsonc:
        ui.warn(
            f"{VSCODE_USER_MCP_PATH} contains comments or trailing commas. "
            'Auto-edit would strip them. Remove the "orbit" entry under '
            '"servers" manually.'
        )
        state.remove_component("vscode")
        return
    servers.pop("orbit", None)
    VSCODE_USER_MCP_PATH.write_text(json.dumps(data, indent=indent))
    ui.detail(f"Removed orbit from {VSCODE_USER_MCP_PATH}")
    state.remove_component("vscode")


def _vscode_detected() -> bool:
    """True when any known macOS VSCode/VSCodium app bundle exists."""
    return any(p.exists() for p in VSCODE_APP_PATHS)


# ---------------------------------------------------------------------------
# JSON helper
# ---------------------------------------------------------------------------

def _load_json_object(path: Path) -> tuple[dict[str, Any], str, bool]:
    """Read a JSON or JSONC file. Return (data, indent style, used_jsonc_fallback).

    Empty / nonexistent / compact files yield ({}, '  ', False) so fresh
    writes use the standard 2-space indent. When indented content is present,
    the indent string mirrors what's already in the file (tab or N spaces) so
    an idempotent merge (read -> mutate -> write) doesn't rewrite the file
    in a different style.

    JSONC support: OpenCode documents JSONC (comments + trailing commas) as
    a supported config format, and VSCode user settings (settings.json,
    mcp.json) accept JSONC by convention. Strict json.loads is tried first
    so the happy path stays on stdlib; on JSONDecodeError we fall back to
    json5 which parses both JSON and JSONC. The fallback is lazy-imported
    to keep cold-start cost off the strict-parse path.

    The third return value is True iff the json5 fallback was needed to
    parse the file. Callers MUST NOT auto-mutate-and-write a file when this
    flag is True: json.dumps() emits plain JSON and would silently strip
    the user's comments and trailing commas. Refuse to write and print a
    manual snippet instead.

    Raises json.JSONDecodeError when the file exists but isn't a JSON
    object, or when it can't be parsed even as JSONC - we won't silently
    overwrite an array, scalar, or genuinely malformed config.
    """
    if not path.exists():
        return {}, "  ", False
    raw = path.read_text()
    if not raw.strip():
        return {}, "  ", False
    used_jsonc = False
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as strict_err:
        try:
            import json5  # lazy: only paid when strict parse fails
        except ImportError:
            raise strict_err
        try:
            data = json5.loads(raw)
            used_jsonc = True
        except Exception:  # noqa: BLE001 - json5 raises a variety of errors
            # Re-raise the original strict-JSON error so the caller's
            # warning quotes a position users can act on. json5's own error
            # is less actionable for the common "I added a comment" case.
            raise strict_err
    if not isinstance(data, dict):
        raise json.JSONDecodeError(
            f"Expected a JSON object at root, got {type(data).__name__}",
            raw,
            0,
        )
    # Detect the file's existing indent: first content line with leading
    # whitespace tells us one level. Compact / single-line files default to 2.
    indent = "  "
    for line in raw.splitlines():
        if not line.strip():
            continue
        if line[0] == "\t":
            indent = "\t"
            break
        if line[0] == " ":
            n = len(line) - len(line.lstrip(" "))
            indent = " " * (n if n > 0 else 2)
            break
    return data, indent, used_jsonc

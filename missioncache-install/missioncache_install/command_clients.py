"""Per-tool slash command installers for non-Claude AI coding tools.

Phase 11.1 ships MissionCache's six canonical slash commands (load, save, new, done,
prompts, mode) into Codex, OpenCode, and VSCode Copilot Chat alongside the
existing Claude plugin. The MCP server is registered by mcp_clients.py; this
module handles only the slash command surface.

Four transformations apply to every non-Claude variant. Two are applied by
`_render_for_non_claude`; the filename prefix is applied at the call site:

1. Filename gets a `missioncache-` prefix (missioncache-load.md etc.). All three tools have
   flat slash command namespaces, so the prefix avoids clashes with other
   plugins' /go, /save, etc.
2. The Claude-specific `argument-hint:` frontmatter line is stripped.
3. The MCP tool prefix `mcp__plugin_missioncache_pm__` is rewritten to `mcp__missioncache__`.
   Claude registers MissionCache as a plugin (tools surface as plugin_missioncache_pm__*);
   the other three tools register MissionCache as a top-level MCP server (tools
   surface as missioncache__*).
4. Cross-references between commands are rewritten from `/missioncache:<name>` to
   `/missioncache-<name>` so that "Run /missioncache:prompts my-project" prose in command
   bodies points at a slash command that actually exists in the target tool.

Per-tool destination summary:

| Tool     | Files                                               | Registration                        |
|----------|-----------------------------------------------------|-------------------------------------|
| OpenCode | ~/.config/opencode/commands/missioncache-<name>.md         | filesystem only (filename = cmd)    |
| VSCode   | ~/.missioncache/vscode/prompts/missioncache-<name>.prompt.md      | chat.promptFilesLocations in user   |
|          |                                                     | settings.json                       |
| Codex    | ~/.missioncache/codex-marketplace/plugins/missioncache/commands/  | codex plugin marketplace add +      |
|          | missioncache-<name>.md (plus marketplace.json + plugin.json) | [plugins."missioncache@missioncache"] stanza in |
|          |                                                     | ~/.codex/config.toml                |

Source of truth: MissionCache's repo `commands/*.md`. PyPI mode reads from the
bundled package (built into the wheel via force-include of ../commands).
Local mode reads from the clone so maintainer edits are picked up.
"""

from __future__ import annotations

import errno
import json
import re
import shutil
import sys
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

from . import fs_utils, mcp_clients, state, subprocess_utils, ui
from .mcp_clients import _load_json_object

if TYPE_CHECKING:
    from .installers import InstallContext


# Canonical MissionCache commands. Order matches the existing Claude plugin layout.
CANONICAL_COMMANDS: tuple[str, ...] = (
    "load", "save", "new", "done", "prompts", "mode",
)

# Per-tool destination paths. Module-level so tests can monkeypatch.
OPENCODE_COMMANDS_DIR = Path.home() / ".config" / "opencode" / "commands"
VSCODE_PROMPTS_DIR = Path.home() / ".missioncache" / "vscode" / "prompts"
VSCODE_USER_SETTINGS_PATH = (
    Path.home() / "Library" / "Application Support" / "Code" / "User" / "settings.json"
)
CODEX_MARKETPLACE_DIR = Path.home() / ".missioncache" / "codex-marketplace"
CODEX_CONFIG_TOML = Path.home() / ".codex" / "config.toml"

# Codex plugin manifest version. Independent of missioncache-install's version - the
# plugin is a stable artifact whose bumps signal command-shape changes, not
# installer-tooling changes.
CODEX_PLUGIN_VERSION = "1.0.0"

# Substitution regex for the MCP tool prefix. Anchored at a word boundary so
# the rewrite only matches the full `mcp__plugin_missioncache_pm__` literal and not
# any partial substring inside a longer identifier.
_MCP_PREFIX_RE = re.compile(r"\bmcp__plugin_missioncache_pm__")

# Cross-reference rewrite. Source command files reference each other by
# Claude-namespaced slug ("/missioncache:prompts my-project"); in flat-namespace
# tools (Codex / OpenCode / VSCode) the corresponding slash command is
# `/missioncache-prompts`. The capture restricts to lowercase a-z so we don't
# rewrite unrelated `:` separators (timestamps, ratios, etc.).
_MISSIONCACHE_SLASH_REF_RE = re.compile(r"/missioncache:([a-z]+)")

# Codex marketplace add: the CLI lacks an "already registered" exit code, so
# we sniff stderr for an idempotency phrase. Restricted to the unambiguous
# "already <verb>" wordings; anything ambiguous (e.g. "marketplace exists at
# this path with different content" or "already in use by another marketplace")
# is treated as a real failure rather than silently swallowed as success.
_CODEX_ALREADY_REGISTERED_RE = re.compile(
    r"\balready (added|registered|installed|exists)\b",
    re.IGNORECASE,
)
# Symmetric pattern for the uninstall path: a remove against a non-registered
# marketplace produces one of these wordings; anything else is a real failure.
_CODEX_ABSENT_RE = re.compile(
    r"\b(not found|no such|does not exist|unknown marketplace)\b",
    re.IGNORECASE,
)
# Codex plugin activation stanza detection. Anchored to start-of-line and
# allows leading whitespace but rejects a leading `#` (commented-out stanzas
# do not activate the plugin and must not be treated as already-enabled).
_CODEX_PLUGIN_STANZA_RE = re.compile(
    r'^\s*\[plugins\."missioncache@missioncache"\]\s*$',
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _mcp_ready_for(tool: str, ctx: "InstallContext") -> bool:
    """Gate slash command install on this-run MCP success for the same tool.

    Slash commands invoke mcp__missioncache__* tools at runtime, so installing them
    without the matching MCP server registration produces a successful-looking
    install with commands that fail when the user runs them. We track per-run
    outcomes in `ctx.mcp_success` (in-memory only - state.json can hold a
    stale prior-run success and we will not trust it for fresh decisions).

    Three cases:

    - ``ctx.mcp_success[tool] is True``: parent ran in this session and
      succeeded (including the "already registered, just confirming" path).
      Proceed.
    - ``ctx.mcp_success[tool] is False``: parent ran and failed. Skip with
      a clear pointer to the failure the user already saw upstream.
    - ``tool not in ctx.mcp_success``: parent did not run in this session
      (e.g. ``--<tool>-commands --no-<tool>``). Skip with a pointer to
      ``missioncache-install --<tool>``, which is idempotent and detects pre-existing
      manual registrations.

    Returns True when the caller should proceed, False when it should return.
    """
    outcome = ctx.mcp_success.get(tool)
    if outcome is True:
        return True
    if outcome is False:
        ui.warn(
            f"Skipping {tool} slash commands - {tool} MCP registration "
            "failed earlier in this run. Fix the underlying issue and "
            f"re-run with --update."
        )
        return False
    ui.warn(
        f"Skipping {tool} slash commands - {tool} MCP server was not "
        f"registered in this run. Run `missioncache-install --{tool}` first "
        "(idempotent; detects pre-existing manual registrations) so "
        "the commands have an MCP server to call."
    )
    return False


def _read_canonical_command(name: str, ctx: "InstallContext") -> str:
    """Read the source content of a MissionCache command by name (without `.md`).

    PyPI mode: read from bundled package data (missioncache_install.bundled.commands).
    Local mode: read from <repo>/commands/<name>.md so maintainer edits
    immediately flow through to non-Claude tools.
    """
    if ctx.mode == "local":
        from .installers import _require_repo
        path = _require_repo(ctx) / "commands" / f"{name}.md"
        if not path.exists():
            raise FileNotFoundError(f"Local command source missing: {path}")
        return path.read_text()
    files = resources.files("missioncache_install.bundled.commands")
    return (files / f"{name}.md").read_text()


def _write_command_files(
    ctx: "InstallContext",
    dest_dir: Path,
    suffix: str,
) -> list[str]:
    """Render every canonical command into `dest_dir` with the given filename suffix.

    Returns the list of paths actually written (omits any source whose
    `_read_canonical_command` raised FileNotFoundError - we warn but continue
    so the caller can compare len(written) vs len(CANONICAL_COMMANDS) and
    emit honest "partial install" messaging instead of a misleading success.
    """
    written: list[str] = []
    for name in CANONICAL_COMMANDS:
        try:
            content = _read_canonical_command(name, ctx)
        except FileNotFoundError as e:
            ui.warn(str(e))
            continue
        out_path = dest_dir / f"missioncache-{name}{suffix}"
        out_path.write_text(_render_for_non_claude(content))
        written.append(str(out_path))
    return written


def _emit_command_install_outcome(
    *,
    tool: str,
    invocation_prefix: str,
    written: list[str],
    extra_success_clause: str | None = None,
) -> None:
    """Print a green success or yellow degraded message based on real outcome.

    Full success: every CANONICAL_COMMANDS entry was written.
    Degraded: fewer than expected wrote OR the caller passed extra_success_clause=None
    when registration was skipped (handled by caller before calling us).

    The success line lists invocations derived from `written` (not hardcoded),
    so a partial install does NOT enumerate commands the user does not have.
    """
    expected = len(CANONICAL_COMMANDS)
    invocations = ", ".join(
        f"{invocation_prefix}{Path(p).stem.removeprefix('missioncache-').removesuffix('.prompt')}"
        for p in written
    )
    if len(written) < expected:
        ui.warn(
            f"Installed {len(written)}/{expected} {tool} slash commands. "
            "Re-run with --update after fixing missing sources."
        )
        return
    msg = f"Installed {len(written)} {tool} slash commands ({invocations})"
    if extra_success_clause:
        msg = f"{msg}; {extra_success_clause}"
    ui.success(msg)


def _render_for_non_claude(content: str) -> str:
    """Apply the three non-Claude transformations to a command's source content.

    1. Strip the `argument-hint: ...` frontmatter line if present.
    2. Rewrite `mcp__plugin_missioncache_pm__` -> `mcp__missioncache__` everywhere.
    3. Rewrite `/missioncache:<name>` -> `/missioncache-<name>` so cross-references between
       commands ("Run /missioncache:prompts ...") point at the slash command name
       that actually exists in the target tool.

    Frontmatter is the leading `---\\n...---\\n` block. If absent, the
    substitutions still apply to the body. The body is otherwise untouched.
    """
    if content.startswith("---\n"):
        end = content.find("\n---\n", 4)
        if end != -1:
            head_block = content[: end + 5]
            body = content[end + 5:]
            head_lines = [
                line for line in head_block.splitlines(keepends=True)
                if not line.lstrip().startswith("argument-hint:")
            ]
            content = "".join(head_lines) + body
    content = _MCP_PREFIX_RE.sub("mcp__missioncache__", content)
    content = _MISSIONCACHE_SLASH_REF_RE.sub(r"/missioncache-\1", content)
    return content


# ---------------------------------------------------------------------------
# OpenCode: filesystem-only install at ~/.config/opencode/commands/
# ---------------------------------------------------------------------------

def install_opencode_commands(ctx: "InstallContext") -> None:
    """Install MissionCache's six slash commands as /missioncache-<name> in OpenCode."""
    ui.step("11", "OpenCode slash commands")
    if not mcp_clients._opencode_detected():
        ui.warn(
            "OpenCode CLI not found - skipping. "
            "Install OpenCode, then run: missioncache-install --update"
        )
        return
    if not _mcp_ready_for("opencode", ctx):
        return
    OPENCODE_COMMANDS_DIR.mkdir(parents=True, exist_ok=True)
    written = _write_command_files(ctx, OPENCODE_COMMANDS_DIR, suffix=".md")
    state.record_component("opencode_commands", {"files": written})
    _emit_command_install_outcome(
        tool="OpenCode",
        invocation_prefix="/missioncache-",
        written=written,
    )


def uninstall_opencode_commands(ctx: "InstallContext") -> None:
    """Remove only the files this installer wrote. Other commands are left alone."""
    info = state.load().get("components", {}).get("opencode_commands", {})
    files = info.get("files", [])
    removed = 0
    for path in files:
        p = Path(path)
        if p.exists():
            p.unlink()
            removed += 1
    ui.detail(f"Removed {removed} OpenCode command file(s)")
    state.remove_component("opencode_commands")


# ---------------------------------------------------------------------------
# VSCode: ~/.missioncache/vscode/prompts/ + chat.promptFilesLocations registration
# ---------------------------------------------------------------------------

def install_vscode_commands(ctx: "InstallContext") -> None:
    """Install MissionCache slash commands as /missioncache-<name> in VSCode Copilot Chat.

    Files go to a MissionCache-owned directory under ~/.missioncache/. The directory is
    registered in VSCode user settings via `chat.promptFilesLocations`, which
    makes the prompts available across all workspaces without any per-repo
    opt-in. Mirrors the user's existing `chat.instructionsFilesLocations`
    pattern.
    """
    ui.step("12", "VSCode slash commands")
    if sys.platform != "darwin":
        ui.warn("VSCode commands install is macOS-only in this release - skipping")
        return
    if not mcp_clients._vscode_detected():
        ui.warn(
            "VSCode not found in /Applications - skipping. "
            "Install VSCode, then run: missioncache-install --update"
        )
        return
    if not _mcp_ready_for("vscode", ctx):
        return

    VSCODE_PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    written = _write_command_files(ctx, VSCODE_PROMPTS_DIR, suffix=".prompt.md")

    settings_status = _register_vscode_prompts_location()
    state.record_component(
        "vscode_commands",
        {
            "files": written,
            "settings_status": settings_status,
            "prompts_dir": str(VSCODE_PROMPTS_DIR),
        },
    )

    expected = len(CANONICAL_COMMANDS)
    if len(written) < expected or settings_status == "failed":
        # Partial install or settings registration failed - warn rather than mislead.
        problems: list[str] = []
        if len(written) < expected:
            problems.append(f"{len(written)}/{expected} prompt files written")
        if settings_status == "failed":
            problems.append(
                "chat.promptFilesLocations registration was skipped (settings.json "
                "could not be parsed); add the entry manually or fix settings.json "
                "and re-run --update"
            )
        ui.warn("VSCode slash commands install incomplete: " + "; ".join(problems))
        return
    extra = (
        f"registered {VSCODE_PROMPTS_DIR} in chat.promptFilesLocations"
        if settings_status == "registered"
        else "chat.promptFilesLocations was already registered"
    )
    _emit_command_install_outcome(
        tool="VSCode",
        invocation_prefix="/missioncache-",
        written=written,
        extra_success_clause=extra,
    )


def _register_vscode_prompts_location() -> str:
    """Idempotently add the MissionCache prompts dir to chat.promptFilesLocations.

    Preserves all other top-level keys (chat.instructionsFilesLocations,
    user theme, etc.) and the file's existing indent style.

    Return values distinguish three outcomes the caller must surface
    differently to the user:

    - ``"registered"``: the file was modified to add the MissionCache entry.
    - ``"already-present"``: the entry was already correct; no write needed.
    - ``"failed"``: the file could not be parsed (JSONC comments, syntax
      error). The MissionCache entry was NOT added; the user must intervene.
    """
    location_key = str(VSCODE_PROMPTS_DIR)

    if not VSCODE_USER_SETTINGS_PATH.exists():
        VSCODE_USER_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        fs_utils.write_config_text(VSCODE_USER_SETTINGS_PATH, "{}\n")

    try:
        data, indent, used_jsonc = _load_json_object(VSCODE_USER_SETTINGS_PATH)
    except json.JSONDecodeError as e:
        # VSCode settings.json is JSONC by convention but truly malformed
        # input still raises. Fail loud rather than overwrite.
        ui.warn(
            f"Cannot parse {VSCODE_USER_SETTINGS_PATH}: {e}. "
            "chat.promptFilesLocations registration skipped - add the entry "
            f"manually: {{\"chat.promptFilesLocations\": {{\"{location_key}\": true}}}}"
        )
        return "failed"

    locations = data.get("chat.promptFilesLocations")
    if isinstance(locations, dict) and locations.get(location_key) is True:
        ui.detail(
            f"chat.promptFilesLocations already registered for {VSCODE_PROMPTS_DIR}"
        )
        return "already-present"

    if used_jsonc:
        # settings.json with comments is the common case for VSCode users.
        # Refuse to write rather than strip their formatting.
        ui.warn(
            f"{VSCODE_USER_SETTINGS_PATH} contains comments or trailing commas. "
            "Auto-merge would strip them. Add this entry manually: "
            f'"chat.promptFilesLocations": {{"{location_key}": true}}'
        )
        return "failed"

    if not isinstance(locations, dict):
        locations = {}
        data["chat.promptFilesLocations"] = locations
    locations[location_key] = True
    fs_utils.write_config_text(VSCODE_USER_SETTINGS_PATH, json.dumps(data, indent=indent))
    ui.detail(f"Registered {VSCODE_PROMPTS_DIR} in chat.promptFilesLocations")
    return "registered"


def uninstall_vscode_commands(ctx: "InstallContext") -> None:
    """Reverse install: delete prompt files, remove settings entry, prune dir."""
    info = state.load().get("components", {}).get("vscode_commands", {})
    files = info.get("files", [])
    removed = 0
    for path in files:
        p = Path(path)
        if p.exists():
            p.unlink()
            removed += 1

    location_key = info.get("prompts_dir", str(VSCODE_PROMPTS_DIR))
    if VSCODE_USER_SETTINGS_PATH.exists():
        try:
            data, indent, used_jsonc = _load_json_object(VSCODE_USER_SETTINGS_PATH)
            locations = data.get("chat.promptFilesLocations")
            if isinstance(locations, dict) and location_key in locations:
                if used_jsonc:
                    ui.warn(
                        f"{VSCODE_USER_SETTINGS_PATH} contains comments or "
                        "trailing commas. Auto-edit would strip them. Remove "
                        f'"{location_key}" from "chat.promptFilesLocations" '
                        "manually."
                    )
                else:
                    locations.pop(location_key, None)
                    fs_utils.write_config_text(
                        VSCODE_USER_SETTINGS_PATH, json.dumps(data, indent=indent)
                    )
                    ui.detail("Removed MissionCache entry from chat.promptFilesLocations")
        except json.JSONDecodeError as e:
            ui.warn(
                f"Cannot parse {VSCODE_USER_SETTINGS_PATH}: {e}. "
                "chat.promptFilesLocations entry not removed - delete manually."
            )

    if VSCODE_PROMPTS_DIR.exists():
        try:
            VSCODE_PROMPTS_DIR.rmdir()
        except OSError as e:
            # Only "directory not empty" is acceptable here - user added their
            # own prompts and we leave the dir alone. Other errnos (EACCES,
            # EBUSY, EPERM) are real failures the user needs to see.
            if e.errno == errno.ENOTEMPTY:
                ui.detail(
                    f"{VSCODE_PROMPTS_DIR} not removed (still contains user files)"
                )
            else:
                ui.warn(f"Could not remove {VSCODE_PROMPTS_DIR}: {e}")

    ui.detail(f"Removed {removed} VSCode prompt file(s)")
    state.remove_component("vscode_commands")


# ---------------------------------------------------------------------------
# Codex: full plugin marketplace under ~/.missioncache/codex-marketplace/
# ---------------------------------------------------------------------------

def install_codex_commands(ctx: "InstallContext") -> None:
    """Install MissionCache slash commands as a Codex plugin via local marketplace.

    Codex doesn't accept loose markdown commands - they have to be packaged as
    a plugin. We build a real plugin under ~/.missioncache/codex-marketplace/, register
    it via `codex plugin marketplace add`, and activate it by writing the
    `[plugins."missioncache@missioncache"]` stanza into ~/.codex/config.toml.
    """
    ui.step("13", "Codex slash commands")
    if not shutil.which("codex"):
        ui.warn(
            "Codex CLI not found - skipping. "
            "Install Codex, then run: missioncache-install --update"
        )
        return

    if not _mcp_ready_for("codex", ctx):
        return

    expected = len(CANONICAL_COMMANDS)
    command_count = _build_codex_marketplace(ctx)
    if command_count == 0:
        ui.warn("No Codex commands written - skipping marketplace registration")
        return

    if not _register_codex_marketplace():
        # Marketplace registration failed for an unknown reason. Do NOT
        # activate the stanza or record state - the plugin would point at a
        # marketplace Codex doesn't know about and `--update` / `--uninstall`
        # would operate on a fiction.
        ui.warn(
            "Skipping plugin activation in ~/.codex/config.toml because "
            "marketplace registration failed. Re-run with --update once the "
            "underlying issue is resolved."
        )
        return

    _enable_codex_plugin()
    state.record_component(
        "codex_commands",
        {
            "marketplace_dir": str(CODEX_MARKETPLACE_DIR),
            "command_count": command_count,
        },
    )

    if command_count < expected:
        ui.warn(
            f"Installed Codex MissionCache plugin ({command_count}/{expected} commands). "
            "Restart Codex; some sources were missing - re-run --update after "
            "fixing them."
        )
        return
    ui.success(
        f"Installed Codex MissionCache plugin ({command_count} commands). "
        "Restart Codex to load /missioncache-load, /missioncache-save, /missioncache-new, /missioncache-done, "
        "/missioncache-prompts, /missioncache-mode."
    )


def _build_codex_marketplace(ctx: "InstallContext") -> int:
    """Generate the on-disk Codex local marketplace + MissionCache plugin tree.

    Layout:
      <root>/.agents/plugins/marketplace.json   - registry pointing at MissionCache
      <root>/plugins/missioncache/.codex-plugin/plugin.json   - plugin manifest
      <root>/plugins/missioncache/commands/missioncache-<name>.md    - the six commands

    Returns the count of commands written.
    """
    plugin_dir = CODEX_MARKETPLACE_DIR / "plugins" / "missioncache"
    commands_dir = plugin_dir / "commands"
    codex_plugin_dir = plugin_dir / ".codex-plugin"
    registry_path = CODEX_MARKETPLACE_DIR / ".agents" / "plugins" / "marketplace.json"

    commands_dir.mkdir(parents=True, exist_ok=True)
    codex_plugin_dir.mkdir(parents=True, exist_ok=True)
    registry_path.parent.mkdir(parents=True, exist_ok=True)

    plugin_manifest = {
        "name": "missioncache",
        "version": CODEX_PLUGIN_VERSION,
        "description": "MissionCache project management slash commands for Codex",
        "author": {"name": "Tomer Brami"},
        "homepage": "https://github.com/missioncache/missioncache",
        "license": "MIT",
        "keywords": ["missioncache", "project-management", "task-tracking", "productivity"],
        "interface": {
            "displayName": "MissionCache",
            "shortDescription": "Project management with time tracking",
            "longDescription": (
                "MissionCache's slash commands inside Codex. Provides /missioncache-load, /missioncache-save, "
                "/missioncache-new, /missioncache-done, /missioncache-prompts, and /missioncache-mode for managing "
                "MissionCache projects. Requires the MissionCache MCP server to be registered "
                "separately via `codex mcp add missioncache -- mcp-missioncache`."
            ),
            "developerName": "Tomer Brami",
            "category": "Productivity",
            "capabilities": ["Read", "Write"],
        },
    }
    (codex_plugin_dir / "plugin.json").write_text(
        json.dumps(plugin_manifest, indent=2) + "\n"
    )

    marketplace = {
        "name": "missioncache",
        "interface": {"displayName": "MissionCache"},
        "plugins": [
            {
                "name": "missioncache",
                "source": {"source": "local", "path": "./plugins/missioncache"},
                "policy": {"installation": "AVAILABLE", "authentication": "OFF"},
                "category": "Productivity",
            }
        ],
    }
    registry_path.write_text(json.dumps(marketplace, indent=2) + "\n")

    written = 0
    for name in CANONICAL_COMMANDS:
        try:
            content = _read_canonical_command(name, ctx)
        except FileNotFoundError as e:
            ui.warn(str(e))
            continue
        (commands_dir / f"missioncache-{name}.md").write_text(_render_for_non_claude(content))
        written += 1
    ui.detail(f"Built Codex marketplace at {CODEX_MARKETPLACE_DIR} ({written} commands)")
    return written


def _register_codex_marketplace() -> bool:
    """Run `codex plugin marketplace add <path>` for the MissionCache local marketplace.

    Returns True when the marketplace is registered (or was already
    registered), False on any other failure. The caller MUST gate downstream
    config writes and state recording on this return value: a marketplace
    that is not known to Codex cannot serve commands, so emitting an
    `[plugins."missioncache@missioncache"]` stanza pointing at it produces a broken setup.

    Idempotency uses an anchored phrase regex (see `_CODEX_ALREADY_REGISTERED_RE`)
    rather than the bare substrings "already" / "exists" - those swallow real
    failures like "marketplace exists at this path with different content"
    or "already in use by another marketplace".
    """
    try:
        subprocess_utils.run(
            ["codex", "plugin", "marketplace", "add", str(CODEX_MARKETPLACE_DIR)]
        )
        ui.detail("Registered marketplace via codex plugin marketplace add")
        return True
    except subprocess_utils.CommandFailed as e:
        combined = (e.stderr or "") + (e.stdout or "")
        if _CODEX_ALREADY_REGISTERED_RE.search(combined):
            ui.detail("Codex marketplace already registered")
            return True
        ui.warn(
            f"codex plugin marketplace add failed: "
            f"{e.stderr.strip() or e.stdout.strip() or 'unknown error'}"
        )
        return False


def _enable_codex_plugin() -> None:
    """Append `[plugins."missioncache@missioncache"]` to ~/.codex/config.toml.

    Codex activates plugins via an empty TOML stanza in config.toml; the bare
    header is enough to enable the plugin without per-plugin overrides.

    Existence check uses an anchored regex (`_CODEX_PLUGIN_STANZA_RE`) so a
    commented-out stanza (`# [plugins."missioncache@missioncache"]`) is correctly ignored
    and the active stanza gets appended. A bare substring check would have
    treated the comment as already-enabled and silently skipped activation.

    We keep missioncache-install dep-free by handling the file as plain text rather
    than parsing/serializing TOML.
    """
    if not CODEX_CONFIG_TOML.exists():
        CODEX_CONFIG_TOML.parent.mkdir(parents=True, exist_ok=True)
        fs_utils.write_config_text(CODEX_CONFIG_TOML, "")

    text = CODEX_CONFIG_TOML.read_text()
    stanza_header = '[plugins."missioncache@missioncache"]'
    if _CODEX_PLUGIN_STANZA_RE.search(text):
        ui.detail("Codex plugin stanza already present in config.toml")
        return
    if text and not text.endswith("\n"):
        text += "\n"
    if text and not text.endswith("\n\n"):
        text += "\n"
    text += stanza_header + "\n"
    fs_utils.write_config_text(CODEX_CONFIG_TOML, text)
    ui.detail('Added [plugins."missioncache@missioncache"] stanza to ~/.codex/config.toml')


def uninstall_codex_commands(ctx: "InstallContext") -> None:
    """Reverse install: marketplace remove, config stanza strip, tree delete."""
    if shutil.which("codex"):
        try:
            subprocess_utils.run(
                ["codex", "plugin", "marketplace", "remove", "missioncache"]
            )
            ui.detail("Removed missioncache marketplace from Codex")
        except subprocess_utils.CommandFailed as e:
            combined = (e.stderr or "") + (e.stdout or "")
            if _CODEX_ABSENT_RE.search(combined):
                ui.detail("Codex marketplace already absent")
            else:
                ui.warn(
                    f"codex plugin marketplace remove failed: "
                    f"{e.stderr.strip() or e.stdout.strip() or 'unknown error'}"
                )

    if CODEX_CONFIG_TOML.exists():
        text = CODEX_CONFIG_TOML.read_text()
        new_text = _strip_codex_plugin_stanza(text)
        if new_text != text:
            fs_utils.write_config_text(CODEX_CONFIG_TOML, new_text)
            ui.detail('Removed [plugins."missioncache@missioncache"] from config.toml')

    if CODEX_MARKETPLACE_DIR.exists():
        shutil.rmtree(CODEX_MARKETPLACE_DIR)
        ui.detail(f"Deleted {CODEX_MARKETPLACE_DIR}")

    state.remove_component("codex_commands")


def _strip_codex_plugin_stanza(text: str) -> str:
    """Remove the `[plugins."missioncache@missioncache"]` section (and its subsections) from TOML.

    A TOML section runs from its `[header]` line up to the next `[section]`
    line or end-of-file. The naive "any header ends the skip" rule is wrong
    because subsections of MissionCache (e.g. `[plugins."missioncache@missioncache".overrides]`)
    look like fresh sections to a substring-only check and would leak past
    the strip. We treat subsections of `plugins."missioncache@missioncache"` as part of
    MissionCache's own stanza tree and only end skip mode on a header that does
    NOT belong to MissionCache.

    Limitations (acceptable since MissionCache only writes the bare header today):
    - The stripper is line-based, not TOML-aware. A `[header]` literal that
      appears inside a multi-line string value would prematurely end skip.
    - Array-of-tables form `[[plugins."missioncache@missioncache"]]` is not recognized.
      MissionCache never emits that form; if a user hand-edits to it, the stanza
      will not be stripped on uninstall and they'll need to delete it
      manually. Documented rather than handled because adding tomllib here
      to cover a hypothetical hand-edit is over-engineering today.

    Only MissionCache's own stanza lines are dropped; the rest of the file is
    left byte-for-byte intact. Since install adds exactly one blank line before
    the stanza, removing it leaves no runaway blank-line growth across
    install/uninstall cycles - so no global blank-line collapse is applied
    (that would corrupt blank lines inside unrelated multi-line string values).
    """
    target = '[plugins."missioncache@missioncache"]'
    subsection_prefix = '[plugins."missioncache@missioncache".'
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    skip = False
    for line in lines:
        stripped = line.strip()
        if not skip and stripped == target:
            skip = True
            continue
        if skip:
            if stripped.startswith("[") and stripped.endswith("]"):
                if stripped.startswith(subsection_prefix):
                    # Subsection of MissionCache. Stay in skip mode and drop it
                    # along with the rest of MissionCache's stanza tree.
                    continue
                # Sibling section - end skip and keep this line.
                skip = False
                out.append(line)
                continue
            continue
        out.append(line)
    return "".join(out)

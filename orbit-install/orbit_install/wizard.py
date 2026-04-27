"""Interactive component-selection wizard.

Core principle: confirm every component explicitly. Default is yes (one keypress
to accept the common case), but any component can be declined without affecting
the others. The statusline installer does its own second-level confirmation
before overwriting an existing settings.json entry.

The non-Claude MCP integrations (Codex, OpenCode, VSCode) are gated by tool
detection - if the tool's CLI / app bundle is not present, the wizard skips
the prompt silently. Users can still force-install via explicit `--codex`
etc. flags after installing the tool.
"""

from __future__ import annotations

import shutil
import sys

from . import installers, mcp_clients, prereqs, ui


# Human-facing descriptions shown next to each y/N prompt.
_COMPONENT_DESCRIPTIONS: dict[str, tuple[str, str]] = {
    "plugin": (
        "Orbit plugin",
        "Slash commands (/orbit:new, /orbit:go, ...), MCP server, hooks.",
    ),
    "dashboard": (
        "Orbit Dashboard",
        "Local web UI at http://localhost:8787 with task tracking and analytics. "
        "Runs as a background service.",
    ),
    "orbit_auto": (
        "orbit-auto CLI",
        "Autonomous task execution across multiple workers.",
    ),
    "statusline": (
        "Statusline",
        "Enhanced Claude Code statusline with project, git, and token info. "
        "Overwrites ~/.claude/settings.json statusLine (asks first if one exists).",
    ),
    "rules": (
        "Rule files",
        "Behavioral rules installed into ~/.claude/rules/ to teach Claude how "
        "to use orbit (context preservation, session resolution, ...).",
    ),
    "user_commands": (
        "User-level slash commands",
        "/whats-new and /optimize-prompt installed into ~/.claude/commands/.",
    ),
    "orbit_db": (
        "orbit-db CLI",
        "Terminal CLI for task management (list-active, create-task, task-time, ...). "
        "Complements the dashboard web UI for shell or script use.",
    ),
    "codex": (
        "Codex (MCP server + slash commands)",
        "Register orbit's MCP server via `codex mcp add` and install /orbit-go, "
        "/orbit-save, ... as a Codex plugin (~/.orbit/codex-marketplace/).",
    ),
    "opencode": (
        "OpenCode (MCP server + slash commands)",
        "Register orbit's MCP server in OpenCode's global config and install "
        "/orbit-go, /orbit-save, ... into ~/.config/opencode/commands/.",
    ),
    "vscode": (
        "VSCode (MCP server + slash commands)",
        "Register orbit's MCP server in VSCode for Copilot Chat agent mode and "
        "install /orbit-go, /orbit-save, ... as user-level prompt files "
        "(macOS only).",
    ),
}


# When a "parent" component is selected, automatically pull in the matching
# slash commands component. Users can opt out granularly via --no-codex-commands
# etc. on the command line. The wizard never asks about the child directly -
# pairing them keeps the prompt count low and the parity message coherent.
COMMAND_IMPLIES: dict[str, str] = {
    "codex": "codex_commands",
    "opencode": "opencode_commands",
    "vscode": "vscode_commands",
}
_IMPLIED_CHILDREN: frozenset[str] = frozenset(COMMAND_IMPLIES.values())


# Components whose y/N prompt should only fire when the corresponding tool is
# installed locally. Each detector returns True iff orbit can register MCP for
# that tool right now on this system.
_TOOL_DETECTORS = {
    "codex": lambda: shutil.which("codex") is not None,
    "opencode": mcp_clients._opencode_detected,
    "vscode": lambda: sys.platform == "darwin" and mcp_clients._vscode_detected(),
}


def run(ctx: installers.InstallContext) -> None:
    """Run the interactive wizard and execute the chosen installers."""
    ui.banner()

    p = prereqs.detect()
    prereqs.report(p)
    prereqs.ensure_python_or_fail(p)
    prereqs.ensure_claude_cli_or_warn(p)

    if ctx.mode == "pypi" and not prereqs.ensure_pip_runner_or_prompt(p):
        ui.info("Aborting - pipx/uv is required for PyPI-mode installs.")
        return

    selected = _select_components()
    if not selected:
        ui.warn("No components selected. Nothing to install.")
        return

    print()
    ui.info(f"Installing: {', '.join(c.replace('_', '-') for c in selected)}")
    installers.install_components(selected, ctx)

    ui.success_banner(selected, dashboard_port=ctx.port)
    _print_next_steps(selected)


def _select_components() -> list[str]:
    """Ask y/N for each component. Returns the selected names in ALL_COMPONENTS order.

    Components in `_TOOL_DETECTORS` are skipped silently when the matching tool
    is not installed - those integrations only make sense if the user has the
    target tool. Force-install is still possible via explicit `--codex` etc.

    Slash command companion components (codex_commands, opencode_commands,
    vscode_commands) are NOT prompted independently; saying yes to the parent
    integration (codex / opencode / vscode) installs both MCP and commands.
    The CLI offers `--no-codex-commands` etc. for granular opt-out.
    """
    print()
    ui.step("?", "Choose components to install")
    ui.detail("Press Enter to accept the default in [brackets].")
    print()

    selected: list[str] = []
    for name in installers.ALL_COMPONENTS:
        if name in _IMPLIED_CHILDREN:
            continue  # auto-paired with parent integration component
        detector = _TOOL_DETECTORS.get(name)
        if detector is not None and not detector():
            continue
        label, desc = _COMPONENT_DESCRIPTIONS[name]
        print(f"  {label}")
        print(f"    {desc}")
        if ui.ask_yn(f"  Install {label}?", default=True):
            selected.append(name)
            child = COMMAND_IMPLIES.get(name)
            if child is not None:
                selected.append(child)
        print()
    return selected


def _print_next_steps(selected: list[str]) -> None:
    """Show a short how-to-get-started block tailored to what was installed."""
    ui.info("Next steps:")
    ui.detail("Create a project:  /orbit:new my-project")
    ui.detail("Resume work:       /orbit:go")
    if "dashboard" in selected:
        ui.detail("Dashboard:         http://localhost:8787")
    ui.detail("Docs:              https://github.com/tomerbr1/claude-orbit")
    print()
    ui.detail("Update later:  uvx orbit-install --update")
    ui.detail("Uninstall:     uvx orbit-install --uninstall")

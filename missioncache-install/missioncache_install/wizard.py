"""Interactive component-selection wizard.

Core principle: confirm every component explicitly. Default is yes (one keypress
to accept the common case), but any component can be declined without affecting
the others. The statusline installer does its own second-level confirmation
before overwriting an existing settings.json entry.

The non-Claude MCP integrations (Codex, OpenCode, VSCode) are gated by tool
detection - if the tool's CLI / app bundle is not present, the wizard skips
the prompt silently. Users can still force-install via explicit `--codex`
etc. flags after installing the tool.

Uninstall-side: `run_uninstall_wizard()` is the parallel flow for `--uninstall`
without `--all`. It reads tracked components from `state.json` and asks for
a comma-separated index list (or `all` to wipe everything tracked).
"""

from __future__ import annotations

import shutil
import sys

from . import installers, mcp_clients, prereqs, state, ui


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


def run_uninstall_wizard() -> list[str] | None:
    """Show installed components and ask user to pick which to uninstall.

    Reads tracked components from `state.json`, sorted by `ALL_COMPONENTS`
    order so two users with the same components see the same numbered menu
    regardless of install order.

    Returns:
    - list[str] - components the user picked (in `ALL_COMPONENTS` order).
    - None - user cancelled (blank input or EOFError from `input()`).

    Exits via `ui.fail` (not return) when the wizard cannot proceed:
    - Empty state (no prior orbit-install tracked).
    - Non-TTY shell (interactive wizard requires a terminal).
    - Invalid selection (non-numeric, out-of-range, or junk input).

    The interactive selector accepts:
    - Comma-separated 1-based indices (e.g. `1,3,5`) - uninstalls those.
    - `all` - uninstalls everything tracked.
    - blank - cancels.

    Project data and DBs (`~/.orbit/`) are never touched by the underlying
    uninstallers, regardless of selection.
    """
    tracked = state.installed_components()
    # Drop unknown keys (schema-evolution defense) and warn about them.
    valid = [c for c in tracked if c in installers.ALL_COMPONENTS]
    unknown = [c for c in tracked if c not in installers.ALL_COMPONENTS]
    if unknown:
        ui.warn(
            f"State file references unknown components: {', '.join(unknown)}.\n"
            "  Skipping them (not in this orbit-install version's catalog)."
        )
    # Render in ALL_COMPONENTS order so selection numbers are reproducible.
    installed = [c for c in installers.ALL_COMPONENTS if c in valid]

    if not installed:
        ui.fail(
            f"No prior orbit-install tracked in {state.STATE_FILE}.\n"
            "  If you installed orbit manually outside the installer, remove\n"
            "  components by hand. Otherwise nothing to uninstall."
        )
        raise AssertionError("unreachable")  # ui.fail exits

    if not sys.stdin.isatty():
        ui.fail(
            "Bare `--uninstall` requires an interactive terminal.\n"
            "  Use `--uninstall --all` (remove everything tracked) or\n"
            "  `--uninstall <comp1>,<comp2>` (positive list) instead.",
        )
        raise AssertionError("unreachable")  # ui.fail exits

    ui.banner()
    print()
    ui.step("?", "Uninstall components")
    ui.detail(f"Tracked in {state.STATE_FILE}")
    print()
    for i, comp in enumerate(installed, 1):
        print(f"  {i}. {comp.replace('_', '-')}")
    print()
    print("  Pick components to uninstall:")
    print("    Comma-separated numbers (e.g. 1,3,5): uninstall those")
    print('    "all": uninstall everything tracked')
    print("    blank: cancel")
    try:
        answer = input("  > ").strip().lower()
    except EOFError:
        # Could be intentional Ctrl-D OR unexpected stdin loss (parent
        # process death, redirected stdin drained mid-prompt). We can't
        # distinguish, so warn instead of info to make it visible in logs.
        ui.warn("Input ended unexpectedly. Cancelled.")
        return None

    if not answer:
        ui.info("Cancelled.")
        return None
    if answer == "all":
        return installed

    try:
        indices = [int(x.strip()) - 1 for x in answer.split(",")]
    except ValueError:
        ui.fail(
            f"Invalid selection {answer!r}. "
            "Expected comma-separated numbers or 'all'.",
        )
        raise AssertionError("unreachable")  # ui.fail exits
    if any(i < 0 or i >= len(installed) for i in indices):
        ui.fail(
            f"Selection out of range: {answer!r}. "
            f"Valid indices are 1..{len(installed)}.",
        )
        raise AssertionError("unreachable")  # ui.fail exits
    # Dedup while preserving first-occurrence order so `1,1,2` doesn't
    # call uninstall_components([plugin, plugin, dashboard]).
    return list(dict.fromkeys(installed[i] for i in indices))


def _print_next_steps(selected: list[str]) -> None:
    """Show a short how-to-get-started block tailored to what was installed."""
    ui.info("Next steps:")
    ui.detail("Create a project:  /orbit:new my-project")
    ui.detail("Resume work:       /orbit:go")
    if "dashboard" in selected:
        ui.detail("Dashboard:         http://localhost:8787")
    ui.detail("Docs:              https://github.com/tomerbr1/orbit-pm")
    print()
    ui.detail("Update later:  uvx orbit-install --update")
    ui.detail("Uninstall:     uvx orbit-install --uninstall")

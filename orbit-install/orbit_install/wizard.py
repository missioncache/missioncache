"""Interactive component-selection wizard.

Core principle: confirm every component explicitly. Default is yes (one keypress
to accept the common case), but any component can be declined without affecting
the others. The statusline installer does its own second-level confirmation
before overwriting an existing settings.json entry.
"""

from __future__ import annotations

from . import installers, prereqs, ui


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

    ui.success_banner()
    _print_next_steps(selected)


def _select_components() -> list[str]:
    """Ask y/N for each component. Returns the selected names in ALL_COMPONENTS order."""
    print()
    ui.step("?", "Choose components to install")
    ui.detail("Press Enter to accept the default in [brackets].")
    print()

    selected: list[str] = []
    for name in installers.ALL_COMPONENTS:
        label, desc = _COMPONENT_DESCRIPTIONS[name]
        print(f"  {label}")
        print(f"    {desc}")
        if ui.ask_yn(f"  Install {label}?", default=True):
            selected.append(name)
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

"""Entry point for `uvx orbit-install` / `pipx run orbit-install`.

Invocation patterns:
    uvx orbit-install                       # interactive install wizard
    uvx orbit-install --all                 # install all components non-interactively
    uvx orbit-install --dashboard           # install only the dashboard
    uvx orbit-install --all --no-statusline # install everything except the statusline
    uvx orbit-install --update              # refresh whatever is in state.json
    uvx orbit-install --uninstall           # interactive uninstall wizard (TTY only)
    uvx orbit-install --uninstall --all     # uninstall every tracked component
    uvx orbit-install --uninstall codex,vscode  # uninstall a specific list
    uvx orbit-install --local               # maintainer mode: editable installs from clone

Project data and DBs at `~/.orbit/` are never touched by any uninstall flow.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, installers, state, ui, wizard
from .wizard import COMMAND_IMPLIES


DEFAULT_PORT = 8787

# Sentinel for bare `--uninstall` (no value supplied). A unique object so
# `is` comparison distinguishes it from `--uninstall ""` (empty list, e.g.
# unset shell var) and from `--uninstall foo,bar` (positive list). Cannot
# collide with any string a user could pass on the CLI.
INTERACTIVE_WIZARD = object()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="orbit-install",
        description="Bootstrap installer for Orbit (project manager for Claude Code).",
    )
    p.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )

    # `--all` and `--update` are install-time verbs and remain mutually
    # exclusive. `--uninstall` is a different verb and lives outside the
    # group so it can compose with `--all` (uninstall everything tracked,
    # bypass wizard) and accept an optional positive list.
    action = p.add_mutually_exclusive_group()
    action.add_argument(
        "--all", action="store_true",
        help="With no other verb: install all components non-interactively. "
             "With --uninstall: remove every tracked component, bypassing the "
             "interactive wizard.",
    )
    action.add_argument(
        "--update", action="store_true",
        help="Update installed components in place (reads state).",
    )
    p.add_argument(
        "--uninstall",
        nargs="?",
        const=INTERACTIVE_WIZARD,
        default=None,
        metavar="COMP1,COMP2",
        help="Uninstall components. Bare flag opens the interactive wizard "
             "(requires TTY). Pass a comma-separated component list (e.g. "
             "`--uninstall codex,vscode`) for non-interactive removal of "
             "specific components. Combine with `--all` to remove everything "
             "tracked. Project data at ~/.orbit/ is never touched.",
    )

    # Per-component opt-in flags. Any of these triggers non-interactive mode
    # for exactly the components listed (in combination with --no-* opt-outs).
    opt_in = p.add_argument_group(
        "component opt-in (non-interactive)",
        "Install only the components listed. Can be combined with --no-* to "
        "exclude specific ones from --all.",
    )
    for flag, dest in (
        ("--plugin", "plugin"),
        ("--dashboard", "dashboard"),
        ("--orbit-auto", "orbit_auto"),
        ("--statusline", "statusline"),
        ("--rules", "rules"),
        ("--user-commands", "user_commands"),
        ("--orbit-db", "orbit_db"),
        ("--codex", "codex"),
        ("--codex-commands", "codex_commands"),
        ("--opencode", "opencode"),
        ("--opencode-commands", "opencode_commands"),
        ("--vscode", "vscode"),
        ("--vscode-commands", "vscode_commands"),
    ):
        opt_in.add_argument(flag, dest=dest, action="store_true")

    opt_out = p.add_argument_group(
        "component opt-out",
        "Exclude specific components from --all (e.g. `--all --no-statusline`). "
        "`--no-codex-commands` keeps the Codex MCP server but skips the slash "
        "command plugin (same for opencode / vscode).",
    )
    for flag, dest in (
        ("--no-plugin", "no_plugin"),
        ("--no-dashboard", "no_dashboard"),
        ("--no-orbit-auto", "no_orbit_auto"),
        ("--no-statusline", "no_statusline"),
        ("--no-rules", "no_rules"),
        ("--no-user-commands", "no_user_commands"),
        ("--no-orbit-db", "no_orbit_db"),
        ("--no-codex", "no_codex"),
        ("--no-codex-commands", "no_codex_commands"),
        ("--no-opencode", "no_opencode"),
        ("--no-opencode-commands", "no_opencode_commands"),
        ("--no-vscode", "no_vscode"),
        ("--no-vscode-commands", "no_vscode_commands"),
    ):
        opt_out.add_argument(flag, dest=dest, action="store_true")

    p.add_argument(
        "--local", action="store_true",
        help="Maintainer mode: editable installs + local marketplace from the "
             "current clone. Auto-detected when run from a repo root.",
    )
    p.add_argument(
        "--no-service", action="store_true",
        help="Skip launchd/systemd service registration (dashboard will not auto-start).",
    )
    p.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help=f"Dashboard port (default: {DEFAULT_PORT}).",
    )
    p.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip per-file confirmations (still honors --no-* component opt-outs).",
    )
    return p


def _explicit_components(args: argparse.Namespace) -> list[str]:
    """Components explicitly opted in via --plugin / --dashboard / etc."""
    return [
        c for c in installers.ALL_COMPONENTS
        if getattr(args, c, False)
    ]


def _excluded_components(args: argparse.Namespace) -> set[str]:
    """Components explicitly opted out via --no-*.

    Also auto-excludes a slash command companion when its parent MCP component
    is excluded - slash commands need the MCP server to function, so installing
    `codex_commands` without `codex` is a foot-gun. Users who really want that
    asymmetry can override by passing `--codex-commands` explicitly.
    """
    excluded = {
        c for c in installers.ALL_COMPONENTS
        if getattr(args, f"no_{c}", False)
    }
    for parent, child in COMMAND_IMPLIES.items():
        if parent in excluded and not getattr(args, child, False):
            excluded.add(child)
    return excluded


def _expand_implies(selected: list[str], excluded: set[str]) -> list[str]:
    """Auto-add slash command companions for selected MCP integration parents.

    `--codex` (or any `--all` that includes codex) implicitly turns on
    `codex_commands` so that opting in to the Codex integration delivers the
    full parity experience by default. Use `--no-codex-commands` to install
    MCP without slash commands. Same pattern for opencode and vscode.

    No-op if the child is already in `selected` (e.g. user passed both
    `--codex` and `--codex-commands`) or explicitly excluded.
    """
    out = list(selected)
    for parent, child in COMMAND_IMPLIES.items():
        if parent in out and child not in out and child not in excluded:
            out.append(child)
    return out


def _resolve_mode_and_repo(args: argparse.Namespace) -> tuple[str, Path | None]:
    """Decide pypi vs local mode and locate the repo root if local."""
    cwd = Path.cwd()
    marker = cwd / ".claude-plugin" / "plugin.json"
    if args.local:
        if not marker.exists():
            ui.fail(
                f"--local requires running from a orbit-pm clone "
                f"(expected {marker} to exist)."
            )
        return "local", cwd
    if marker.exists():
        # Silent auto-detect: if they're in a clone, assume maintainer workflow.
        return "local", cwd
    return "pypi", None


def _expand_command_pairs(requested: list[str], installed: list[str]) -> list[str]:
    """Auto-add `<tool>_commands` when uninstalling its parent `<tool>`.

    Symmetric counterpart to the wizard's COMMAND_IMPLIES install-side pairing:
    install pairs codex+codex_commands, so uninstall should too. Only adds the
    child if it's still in the tracked-installed list (user may have already
    removed it independently).

    Asymmetric on purpose: removing `codex_commands` does NOT also remove
    `codex` - users may want MCP without slash commands.
    """
    out = list(requested)
    for parent, child in COMMAND_IMPLIES.items():
        if parent in out and child in installed and child not in out:
            out.append(child)
    return out


def _filter_known_state(tracked: list[str]) -> list[str]:
    """Drop state.json keys that are no longer in `ALL_COMPONENTS`.

    Schema-evolution defense: if a future release deletes a component, an
    older state.json may still name it. Filter and warn so the user sees
    the orphan instead of a silent no-op or KeyError downstream.
    """
    valid = [c for c in tracked if c in installers.ALL_COMPONENTS]
    unknown = [c for c in tracked if c not in installers.ALL_COMPONENTS]
    if unknown:
        ui.warn(
            f"State file references unknown components: {', '.join(unknown)}.\n"
            "  These are not in this orbit-install version's ALL_COMPONENTS list. "
            "Skipping them."
        )
    return valid


def _run_uninstall(args: argparse.Namespace, ctx: installers.InstallContext) -> int:
    """Dispatch the three uninstall patterns.

    Patterns:
    - `--uninstall --all` -> remove every tracked component. Refuses (warn +
      no-op) if no state is tracked, matching `update_all`'s safer pattern.
    - `--uninstall comp1,comp2` -> remove the listed components. Errors if
      state is empty, list is empty after parsing, components are unknown,
      or components aren't currently installed. Auto-expands `<tool>` to
      include `<tool>_commands` if the latter is still tracked.
    - `--uninstall` (bare, sentinel `INTERACTIVE_WIZARD`) -> interactive
      wizard. Errors on non-TTY shells or empty state.

    Combining `--all` with a positive list (e.g. `--uninstall foo --all`) is
    an ambiguous error. Empty-string input (e.g. unset shell var) is rejected.
    """
    uninstall_arg = args.uninstall
    bypass_wizard = args.all

    if isinstance(uninstall_arg, str) and bypass_wizard:
        # `--uninstall foo --all` is ambiguous: positive list AND --all both
        # specified. (`--uninstall --all` alone has uninstall_arg=sentinel.)
        ui.fail(
            "Pass either `--uninstall --all` (everything tracked) OR "
            "`--uninstall <list>` (specific components), not both."
        )
        raise AssertionError("unreachable")  # ui.fail exits

    if bypass_wizard:
        tracked = _filter_known_state(state.installed_components())
        if not tracked:
            ui.warn(
                "No tracked components to uninstall. State file is empty or "
                "missing.\n"
                "  If you installed orbit manually outside the installer, "
                "remove components by hand or restore the state file at "
                f"{state.STATE_FILE}."
            )
            return 0
        installers.uninstall_components(tracked, ctx)
        return 0

    if isinstance(uninstall_arg, str):
        # Empty-string from unset shell var (`--uninstall "$EMPTY"`) lands
        # here as `""` and is rejected loudly. Bare flag would have been
        # the sentinel, never `""`.
        if not uninstall_arg.strip():
            ui.fail(
                f"Empty `--uninstall` argument: {uninstall_arg!r}.\n"
                "  Pass a comma-separated component list, `--all`, or invoke "
                "without a value to open the interactive wizard."
            )
            raise AssertionError("unreachable")  # ui.fail exits

        requested = [
            c.strip().lower().replace("-", "_")
            for c in uninstall_arg.split(",")
            if c.strip()
        ]
        # Dedup while preserving first-occurrence order.
        requested = list(dict.fromkeys(requested))

        # Separator-only input (`,`, ` , , `, etc.) bypasses the whitespace
        # guard above (",".strip() == ",") but yields an empty list after
        # the if-c.strip() filter. Without this check we'd silently no-op
        # via uninstall_components([]) - same failure mode the empty-string
        # guard prevents.
        if not requested:
            ui.fail(
                f"No component names found in `--uninstall {uninstall_arg!r}`.\n"
                "  Input contained only commas/whitespace. Pass a real "
                "component list, `--all`, or invoke without a value for the "
                "interactive wizard."
            )
            raise AssertionError("unreachable")  # ui.fail exits

        unknown = [c for c in requested if c not in installers.ALL_COMPONENTS]
        if unknown:
            ui.fail(
                f"Unknown components: {', '.join(unknown)}.\n"
                f"  Valid components: {', '.join(installers.ALL_COMPONENTS)}"
            )
            raise AssertionError("unreachable")  # ui.fail exits

        installed = _filter_known_state(state.installed_components())
        if not installed:
            ui.fail(
                "No prior orbit-install was tracked.\n"
                "  Use `--uninstall --all` to attempt a best-effort uninstall."
            )
            raise AssertionError("unreachable")  # ui.fail exits

        # Auto-expand <tool> to include <tool>_commands. Inform the user
        # so they don't think we're going off-script. Re-dedupe in case
        # they explicitly passed both parent and child.
        expanded = list(dict.fromkeys(_expand_command_pairs(requested, installed)))
        added = [c for c in expanded if c not in requested]
        if added:
            ui.detail(
                f"Auto-adding paired components: {', '.join(added)} "
                "(parent install pairs them; uninstall keeps the pairing)."
            )

        not_installed = [c for c in expanded if c not in installed]
        if not_installed:
            ui.fail(
                f"Not currently installed: {', '.join(not_installed)}.\n"
                f"  Currently installed: {', '.join(installed)}"
            )
            raise AssertionError("unreachable")  # ui.fail exits

        installers.uninstall_components(expanded, ctx)
        return 0

    # Bare `--uninstall` -> interactive wizard.
    components = wizard.run_uninstall_wizard()
    if components is None:
        # Wizard either reported an error itself (already exited) or the
        # user cancelled. Either way, no work to do.
        return 0

    # Mirror the positive-list path: auto-expand <tool> -> <tool>_commands
    # so picking `codex` from the wizard menu also removes its paired
    # slash-command plugin. Without this, the wizard path would leave
    # orphaned `/orbit-*` commands pointing at a removed MCP integration.
    installed = _filter_known_state(state.installed_components())
    expanded = list(dict.fromkeys(_expand_command_pairs(components, installed)))
    added = [c for c in expanded if c not in components]
    if added:
        ui.detail(
            f"Auto-adding paired components: {', '.join(added)} "
            "(parent install pairs them; uninstall keeps the pairing)."
        )
    installers.uninstall_components(expanded, ctx)
    return 0


def main() -> int:
    args = build_parser().parse_args()
    if args.uninstall is not None and args.update:
        ui.fail("--uninstall and --update cannot be combined (different verbs).")
        raise AssertionError("unreachable")  # ui.fail exits
    mode, repo_root = _resolve_mode_and_repo(args)
    state.set_mode(mode)
    ctx = installers.InstallContext(
        mode=mode,
        repo_root=repo_root,
        skip_service=args.no_service,
        port=args.port,
        assume_yes=args.yes,
    )

    if args.uninstall is not None:
        return _run_uninstall(args, ctx)

    if args.update:
        installers.update_all(ctx)
        return 0

    explicit = _explicit_components(args)
    excluded = _excluded_components(args)

    if args.all or explicit:
        base = list(installers.ALL_COMPONENTS) if args.all else explicit
        selected = [c for c in base if c not in excluded]
        selected = _expand_implies(selected, excluded)
        # statusline needs the orbit-statusline entry point, which ships in the
        # orbit-dashboard package. Installing statusline without dashboard wires
        # settings.json to a command that won't resolve. Auto-add dashboard.
        if "statusline" in selected and "dashboard" not in selected and "dashboard" not in excluded:
            ui.warn("statusline depends on orbit-dashboard (provides the orbit-statusline entry point). Adding dashboard to the install.")
            selected.insert(selected.index("statusline"), "dashboard")
        if not selected:
            ui.warn("Component selection is empty after applying --no-* flags.")
            return 0
        ui.banner()
        ui.info(f"Installing: {', '.join(c.replace('_', '-') for c in selected)}")
        installers.install_components(selected, ctx)
        ui.success_banner(selected, dashboard_port=ctx.port)
        return 0

    # Default: interactive wizard.
    wizard.run(ctx)
    return 0


if __name__ == "__main__":
    sys.exit(main())

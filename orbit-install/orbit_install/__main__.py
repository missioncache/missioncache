"""Entry point for `uvx orbit-install` / `pipx run orbit-install`.

Invocation patterns:
    uvx orbit-install                 # interactive wizard (default)
    uvx orbit-install --all           # install all components non-interactively
    uvx orbit-install --dashboard     # install only the dashboard
    uvx orbit-install --all --no-statusline
                                      # install everything except the statusline
    uvx orbit-install --update        # refresh whatever is in state.json
    uvx orbit-install --uninstall     # remove everything (preserves ~/.orbit/)
    uvx orbit-install --local         # maintainer mode: editable installs from clone
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, installers, state, ui, wizard
from .wizard import COMMAND_IMPLIES


DEFAULT_PORT = 8787


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="orbit-install",
        description="Bootstrap installer for Orbit (project manager for Claude Code).",
    )
    p.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )

    action = p.add_mutually_exclusive_group()
    action.add_argument(
        "--all", action="store_true",
        help="Install all components non-interactively.",
    )
    action.add_argument(
        "--update", action="store_true",
        help="Update installed components in place (reads state).",
    )
    action.add_argument(
        "--uninstall", action="store_true",
        help="Remove every component the installer installed.",
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


def main() -> int:
    args = build_parser().parse_args()
    mode, repo_root = _resolve_mode_and_repo(args)
    state.set_mode(mode)
    ctx = installers.InstallContext(
        mode=mode,
        repo_root=repo_root,
        skip_service=args.no_service,
        port=args.port,
        assume_yes=args.yes,
    )

    if args.uninstall:
        installed = state.installed_components() or list(installers.ALL_COMPONENTS)
        installers.uninstall_components(installed, ctx)
        return 0

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

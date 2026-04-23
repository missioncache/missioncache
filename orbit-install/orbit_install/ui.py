"""Rich-backed terminal output helpers for the install wizard.

Uses `rich` for panels, tables, and styled prompts. Rich auto-detects TTY and
NO_COLOR, so callers do not need to check either. All public functions keep
the same names and signatures as the pre-rich version, except `success_banner`
which now takes an optional list of installed component keys to render as a
summary table.
"""

from __future__ import annotations

import sys

import pyfiglet
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


# Single Console so rich's layout state (live regions, cursor) stays consistent
# across calls. soft_wrap=False keeps long lines from breaking mid-word inside
# panels; rich still wraps on panel boundaries.
_console = Console(soft_wrap=False)
_error_console = Console(stderr=True, style="bold red")


# Human-readable labels for the end-of-install summary table. Keys match
# installers.ALL_COMPONENTS. Kept here so ui is self-contained.
_COMPONENT_LABELS: dict[str, str] = {
    "plugin": "Core plugin",
    "dashboard": "Dashboard",
    "orbit_auto": "orbit-auto CLI",
    "statusline": "Statusline",
    "rules": "Rules",
    "user_commands": "User commands",
    "orbit_db": "orbit-db CLI",
}

# Brand gradient, approximately matching the logo's purple→blue:
# left lobe ≈ violet-500 (#A855F7), right lobe ≈ blue-500 (#3B82F6).
_LOGO_GRADIENT_START = (168, 85, 247)
_LOGO_GRADIENT_END = (59, 130, 246)


def _gradient_text(raw: str, start: tuple[int, int, int], end: tuple[int, int, int]) -> Text:
    """Render multi-line text with a per-column left-to-right RGB gradient.

    Space characters are emitted uncolored so the gradient only applies to
    glyphs - this keeps terminals that render selected whitespace cleanly.
    """
    lines = raw.rstrip().split("\n")
    max_width = max((len(line) for line in lines), default=1)
    out = Text()
    for i, line in enumerate(lines):
        for col, ch in enumerate(line):
            if ch == " ":
                out.append(ch)
                continue
            t = col / max(max_width - 1, 1)
            r = int(start[0] + (end[0] - start[0]) * t)
            g = int(start[1] + (end[1] - start[1]) * t)
            b = int(start[2] + (end[2] - start[2]) * t)
            out.append(ch, style=f"rgb({r},{g},{b})")
        if i < len(lines) - 1:
            out.append("\n")
    return out


def banner() -> None:
    """Print the install-start banner: gradient ORBIT wordmark + tagline."""
    logo = _gradient_text(
        pyfiglet.figlet_format("ORBIT", font="ansi_shadow"),
        _LOGO_GRADIENT_START,
        _LOGO_GRADIENT_END,
    )
    _console.print()
    _console.print(logo)
    _console.print("  [dim]Project Manager for Claude Code[/dim]")
    _console.print()


def success_banner(
    components: list[str] | None = None,
    dashboard_port: int = 8787,
) -> None:
    """Print the end-of-install summary.

    If `components` is provided, renders a table of installed components with a
    check next to each. If None, falls back to a simple success panel (kept for
    back-compat with any caller that does not pass the list).

    When "dashboard" is in `components`, also prints `http://localhost:<port>`
    using the port actually registered by the installer (authoritative - matches
    what ends up in launchd/systemd and state.json).
    """
    _console.print()
    if not components:
        _console.print(
            Panel(
                Text("Install complete!", style="bold green"),
                border_style="green",
                padding=(0, 4),
                expand=False,
            )
        )
        _console.print()
        return

    table = Table(
        title="[bold green]Install complete[/bold green]",
        title_justify="left",
        border_style="green",
        show_header=False,
        box=None,
        padding=(0, 2),
    )
    table.add_column("status", justify="center", no_wrap=True)
    table.add_column("component", no_wrap=True)
    for key in components:
        label = _COMPONENT_LABELS.get(key, key)
        table.add_row("[green]✓[/green]", label)
    _console.print(
        Panel(table, border_style="green", padding=(1, 2), expand=False)
    )
    # Surface the dashboard URL only when the user actually installed it -
    # printing it after a dashboard-less install would teach them the link
    # does not work. Port comes from the installer context so `--port 9999`
    # shows the correct URL rather than the default.
    if "dashboard" in components:
        _console.print(
            f"  [dim]Dashboard:[/dim] [cyan]http://localhost:{dashboard_port}[/cyan]"
        )
    _console.print()


def step(n: int | str, title: str) -> None:
    """Print a numbered step header."""
    _console.print()
    _console.print(
        f"[bold cyan]Step {n}[/bold cyan] [dim]·[/dim] [bold]{title}[/bold]"
    )


def info(msg: str) -> None:
    """Print an informational action line."""
    _console.print(f"  [cyan]→[/cyan] {msg}")


def success(msg: str) -> None:
    """Print a success line."""
    _console.print(f"  [green]✓[/green] {msg}")


def warn(msg: str) -> None:
    """Print a warning line (non-fatal)."""
    _console.print(f"  [yellow]⚠[/yellow]  {msg}")


def detail(msg: str) -> None:
    """Print a dimmed detail line (secondary info)."""
    _console.print(f"  [dim]▸[/dim] [dim]{msg}[/dim]")


def fail(msg: str, exit_code: int = 1) -> None:
    """Print an error message and exit. Surfaces actual errors - does not paraphrase."""
    _error_console.print(f"  ✗ {msg}")
    sys.exit(exit_code)


def ask_yn(prompt: str, default: bool = True) -> bool:
    """Yes/no prompt. Returns default when stdin is not a TTY (CI/pipe safe).

    Intentionally uses stdlib `input()` instead of rich.prompt.Confirm so test
    fixtures that monkeypatch `builtins.input` keep working.
    """
    if not sys.stdin.isatty():
        return default
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"  {prompt} {suffix} ").strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer.startswith("y")

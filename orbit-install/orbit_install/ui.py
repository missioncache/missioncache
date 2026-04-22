"""Colored terminal output helpers, ported from setup.sh.

No third-party deps (no rich, no colorama). Colors are stripped automatically
when stdout is not a TTY so piped output stays clean.
"""

from __future__ import annotations

import sys


_IS_TTY = sys.stdout.isatty()


class _Color:
    RED = "\033[0;31m" if _IS_TTY else ""
    GREEN = "\033[0;32m" if _IS_TTY else ""
    YELLOW = "\033[1;33m" if _IS_TTY else ""
    BLUE = "\033[0;34m" if _IS_TTY else ""
    CYAN = "\033[0;36m" if _IS_TTY else ""
    BOLD = "\033[1m" if _IS_TTY else ""
    DIM = "\033[2m" if _IS_TTY else ""
    NC = "\033[0m" if _IS_TTY else ""


def banner() -> None:
    """Print the orbit install banner."""
    print()
    print(f"{_Color.BOLD}{_Color.CYAN}  +-----------------------------------------+{_Color.NC}")
    print(f"{_Color.BOLD}{_Color.CYAN}  |       Orbit - Project Manager for       |{_Color.NC}")
    print(f"{_Color.BOLD}{_Color.CYAN}  |            Claude Code                  |{_Color.NC}")
    print(f"{_Color.BOLD}{_Color.CYAN}  +-----------------------------------------+{_Color.NC}")
    print()


def success_banner() -> None:
    """Print the end-of-install success banner."""
    print()
    print(f"{_Color.BOLD}{_Color.GREEN}  +-----------------------------------------+{_Color.NC}")
    print(f"{_Color.BOLD}{_Color.GREEN}  |         Install complete!               |{_Color.NC}")
    print(f"{_Color.BOLD}{_Color.GREEN}  +-----------------------------------------+{_Color.NC}")
    print()


def step(n: int | str, title: str) -> None:
    """Print a numbered step header."""
    print(f"\n{_Color.BOLD}{_Color.BLUE}Step {n}: {title}{_Color.NC}")


def info(msg: str) -> None:
    """Print an informational action line."""
    print(f"  {_Color.CYAN}->{_Color.NC} {msg}")


def success(msg: str) -> None:
    """Print a success line."""
    print(f"  {_Color.GREEN}OK{_Color.NC} {msg}")


def warn(msg: str) -> None:
    """Print a warning line (non-fatal)."""
    print(f"  {_Color.YELLOW}!{_Color.NC} {msg}")


def detail(msg: str) -> None:
    """Print a dimmed detail line (secondary info)."""
    print(f"  {_Color.DIM}.{_Color.NC} {msg}")


def fail(msg: str, exit_code: int = 1) -> None:
    """Print an error message and exit. Surfaces actual errors - does not paraphrase."""
    print(f"  {_Color.RED}FAIL{_Color.NC} {msg}", file=sys.stderr)
    sys.exit(exit_code)


def ask_yn(prompt: str, default: bool = True) -> bool:
    """Yes/no prompt. Returns default when stdin is not a TTY (CI/pipe safe)."""
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

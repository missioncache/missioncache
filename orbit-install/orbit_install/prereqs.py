"""Prerequisite checks: Python version, claude CLI, pipx/uvx/uv."""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass

from . import subprocess_utils, ui


MIN_PYTHON = (3, 11)


@dataclass
class Prereqs:
    """Snapshot of detected tools on the user's system."""

    python_ok: bool
    python_version: tuple[int, int]
    claude_cli: str | None   # absolute path to `claude` or None
    pipx: str | None         # absolute path to `pipx` or None
    uvx: str | None          # absolute path to `uvx` or None
    uv: str | None           # absolute path to `uv` or None (used by `uv tool install`)
    platform: str            # sys.platform value

    @property
    def has_pip_runner(self) -> bool:
        """True if pipx or uv tool is available (needed for PyPI-mode installs)."""
        return bool(self.pipx or self.uv)


def detect() -> Prereqs:
    """Detect prerequisites. Does not fail - caller decides severity."""
    return Prereqs(
        python_ok=sys.version_info >= MIN_PYTHON,
        python_version=(sys.version_info.major, sys.version_info.minor),
        claude_cli=shutil.which("claude"),
        pipx=shutil.which("pipx"),
        uvx=shutil.which("uvx"),
        uv=shutil.which("uv"),
        platform=sys.platform,
    )


def report(p: Prereqs) -> None:
    """Print a human-readable prereq status block."""
    ui.step(0, "Checking prerequisites")
    if p.python_ok:
        ui.success(f"Python {p.python_version[0]}.{p.python_version[1]}")
    else:
        ui.warn(
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ required "
            f"(have {p.python_version[0]}.{p.python_version[1]})"
        )
    _report_tool("claude CLI", p.claude_cli)
    _report_tool("uvx", p.uvx)
    _report_tool("uv", p.uv)
    # pipx is an optional fallback. uv's `uv tool install` covers the same job,
    # so "pipx not found" is only a real problem when uv is also missing. Showing
    # it as a yellow warning otherwise made uv-only users (the common case for
    # anyone running `uvx orbit-install`) think something was broken.
    if p.pipx:
        ui.success(f"pipx ({p.pipx})")
    elif p.uv:
        ui.detail("pipx not installed - optional (uv handles PyPI installs)")
    else:
        ui.warn("pipx not found")


def _report_tool(name: str, path: str | None) -> None:
    if path:
        ui.success(f"{name} ({path})")
    else:
        ui.warn(f"{name} not found")


def ensure_python_or_fail(p: Prereqs) -> None:
    """Hard exit if Python version is below MIN_PYTHON."""
    if not p.python_ok:
        ui.fail(
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required "
            f"(you have {p.python_version[0]}.{p.python_version[1]}). "
            f"Install a newer Python and re-run."
        )


def ensure_claude_cli_or_warn(p: Prereqs) -> None:
    """Warn if claude CLI is missing. Plugin install will be skipped."""
    if not p.claude_cli:
        ui.warn(
            "Claude Code CLI not found - plugin registration will be skipped. "
            "Install from https://claude.ai/code then run: orbit-install --update"
        )


def ensure_pip_runner_or_prompt(p: Prereqs) -> bool:
    """Return True if pipx/uv available, else prompt to bootstrap pipx.

    Returns False if the user declines or the bootstrap fails.
    """
    if p.has_pip_runner:
        return True
    ui.warn("Neither pipx nor uv is installed. These are needed for PyPI-based installs.")
    if not ui.ask_yn("Bootstrap pipx via `python -m pip install --user pipx`?", default=True):
        ui.info("Install pipx manually, then re-run orbit-install:")
        ui.detail("  python3 -m pip install --user pipx")
        ui.detail("  python3 -m pipx ensurepath")
        return False
    return bootstrap_pipx()


def bootstrap_pipx() -> bool:
    """Install pipx via `python -m pip install --user pipx` and run ensurepath.

    Returns True on success. If PEP 668 blocks the install (externally-managed
    environment), prints OS-specific install instructions and returns False.
    """
    ui.detail("Running: python -m pip install --user pipx")
    try:
        subprocess_utils.run(
            [sys.executable, "-m", "pip", "install", "--user", "pipx"]
        )
    except subprocess_utils.CommandFailed as e:
        combined = (e.stderr + e.stdout).lower()
        if "externally-managed-environment" in combined or "pep 668" in combined:
            ui.warn("PEP 668 blocks pip install in this Python environment.")
            ui.info("Install pipx via your system package manager:")
            if sys.platform == "darwin":
                ui.detail("  brew install pipx")
            elif sys.platform.startswith("linux"):
                ui.detail("  sudo apt install pipx     (Debian/Ubuntu)")
                ui.detail("  sudo dnf install pipx     (Fedora/RHEL)")
            else:
                ui.detail("  See https://pipx.pypa.io/stable/installation/")
            ui.detail("Then re-run: orbit-install")
            return False
        ui.warn(f"pip install failed:\n{e.stderr}")
        return False

    try:
        subprocess_utils.run([sys.executable, "-m", "pipx", "ensurepath"])
    except subprocess_utils.CommandFailed as e:
        ui.warn(f"pipx ensurepath failed (non-fatal): {e.stderr}")

    ui.success("pipx installed (using `python -m pipx` for this session)")
    return True

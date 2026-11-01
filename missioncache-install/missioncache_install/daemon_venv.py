"""The venv a terminal panels daemon runs from, under ~/.missioncache/<terminal>.

It is its own venv rather than the dashboard's tool venv: `--update`
reinstalls the dashboard with `--force` and no extras, which would strip the
daemon's dependencies from under it while it runs.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from . import subprocess_utils, ui


def install_packages(python: Path, extra: str, repo_root: Path | None) -> None:
    """Create the venv that ``python`` belongs to if missing, then install or
    upgrade ``missioncache-dashboard[extra]`` into it: editable from the clone
    at ``repo_root``, or from PyPI when it is None."""
    venv = python.parent.parent
    uv = shutil.which("uv")
    # On Windows `which` looks in the current directory first, and what it
    # finds here installs into a venv that runs at every logon. A real uv
    # never lives in the directory the installer runs from.
    if uv and Path(uv).resolve().parent == Path.cwd().resolve():
        uv = None
    if not python.exists():
        if uv:
            cmd = [uv, "venv", "--python", ">=3.11", str(venv)]
        else:
            cmd = [sys.executable, "-m", "venv", str(venv)]
        ui.detail(f"Running: {' '.join(cmd)}")
        subprocess_utils.run_streaming(cmd)
    if repo_root is not None:
        packages = [
            "-e", str(repo_root / "missioncache-db"),
            "-e", f"{repo_root / 'missioncache-dashboard'}[{extra}]",
        ]
    else:
        packages = ["--upgrade", f"missioncache-dashboard[{extra}]"]
    if uv:
        cmd = [uv, "pip", "install", "--python", str(python), *packages]
        if repo_root is None:
            cmd.append("--refresh")
    else:
        cmd = [str(python), "-m", "pip", "install", "--quiet", *packages]
    ui.detail(f"Running: {' '.join(cmd)}")
    subprocess_utils.run_streaming(cmd)

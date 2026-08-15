"""Windows liveness + ancestry probes for the smoke-windows CI job.

These exercise the missioncache_db.proc backend and the claude-process walk on
real Windows - the code paths that no macOS/Linux run can reach and that two
independent reviews flagged as unverifiable without Windows hardware (the
SYNCHRONIZE access-right bug, the off-by-one-level walk that returned a live
plausible ancestor instead of claude).

Exits non-zero with a message on the first failure so the CI step fails loudly.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from missioncache_db import proc


def probe_process_alive_self() -> None:
    """The one-liner that catches the SYNCHRONIZE class: without the
    SYNCHRONIZE access right, WaitForSingleObject returns WAIT_FAILED for every
    live process and process_alive reads this very process as dead."""
    result = proc.process_alive(os.getpid())
    assert result is True, f"process_alive(self) returned {result!r}, expected True"
    print("OK: process_alive(self) is True")


def probe_parent_and_name_self() -> None:
    """Proves the Toolhelp32Snapshot name path resolves - name must not be None
    for a process that plainly exists."""
    ppid, name = proc.parent_and_name(os.getpid())
    assert name is not None, f"parent_and_name(self) name is None (ppid={ppid!r})"
    assert not name.lower().endswith(".exe"), f"name kept .exe suffix: {name!r}"
    print(f"OK: parent_and_name(self) = ({ppid!r}, {name!r})")


def probe_renamed_interpreter_walk(tmp: Path) -> None:
    """The end-to-end walk probe. resolve_claude_process_pid climbs from the
    CALLER upward looking for a process named claude, so the caller must be a
    GRANDCHILD whose parent is a claude-named process:

        this probe -> claude.exe (renamed python, the "spawner")
                          -> plain python grandchild (the "resolver")

    The resolver calls resolve_claude_process_pid(); its parent is claude.exe,
    so the walk must return claude.exe's pid EXACTLY.

    The exact-pid assertion is the point: the weaker is-not-None form passes
    against the off-by-one-level walk bug the review caught, which returns a
    live plausible ancestor (this probe process) instead of claude. Only the
    exact pid discriminates.
    """
    fake = tmp / "claude.exe"
    shutil.copy2(Path(sys.executable), fake)

    db_path = str(Path(proc.__file__).resolve().parents[1])
    out_file = tmp / "resolved.txt"

    # Runs in the plain python grandchild: resolve and record the answer.
    resolver_code = (
        "import os, sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, os.environ['MC_DB_PATH'])\n"
        "from missioncache_db import resolve_claude_process_pid\n"
        "Path(os.environ['MC_OUT']).write_text(str(resolve_claude_process_pid()), encoding='utf-8')\n"
    )
    # Runs in claude.exe (the spawner): launch the real-python grandchild and
    # wait, so claude.exe stays alive above the grandchild during the walk.
    spawner_code = (
        "import os, subprocess\n"
        "subprocess.run([os.environ['MC_REAL_PY'], '-c', os.environ['MC_RESOLVER']])\n"
    )
    env = dict(os.environ)
    env["MC_DB_PATH"] = db_path
    env["MC_OUT"] = str(out_file)
    env["MC_REAL_PY"] = sys.executable  # the real interpreter, not claude.exe
    env["MC_RESOLVER"] = resolver_code

    spawner = subprocess.Popen([str(fake), "-c", spawner_code], env=env)
    try:
        spawner.wait(timeout=45)
    except subprocess.TimeoutExpired:
        spawner.kill()
        raise AssertionError("spawner (claude.exe) never finished")

    assert out_file.exists() and out_file.read_text(encoding="utf-8").strip(), \
        "grandchild resolver never wrote its answer"
    resolved = int(out_file.read_text(encoding="utf-8").strip())

    # The grandchild's parent IS claude.exe (spawner.pid); the walk must return
    # exactly that pid, not this probe process and not None.
    assert resolved == spawner.pid, (
        f"resolve_claude_process_pid() returned {resolved}, expected the "
        f"claude.exe pid {spawner.pid} - the walk stopped at the wrong ancestor"
    )
    print(f"OK: walk resolved claude.exe pid exactly ({resolved})")


def main() -> int:
    import tempfile

    probe_process_alive_self()
    probe_parent_and_name_self()
    if sys.platform == "win32":
        # The walk probe renames the interpreter to claude.exe. Only Windows
        # honors that: Toolhelp32's szExeFile is the filename, so the copy reads
        # as "claude". macOS/Linux `ps comm` reads the real Mach-O/ELF identity
        # and ignores the rename, so the probe cannot work there (and a real
        # claude ancestor would be found instead). This is the platform the
        # probe exists for anyway.
        with tempfile.TemporaryDirectory() as td:
            probe_renamed_interpreter_walk(Path(td))
    else:
        print("SKIP: renamed-interpreter walk is win32-only (ps comm ignores the rename)")
    print("all liveness probes passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

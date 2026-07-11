"""Atomic config writes with a one-time per-run backup.

Config writes (settings.json, VSCode/OpenCode JSON, Codex config.toml) must
never leave a user's file truncated if the process dies mid-write. Every write
goes to a sibling temp file, is fsync'd, then os.replace()'d onto the target -
an atomic swap on POSIX. Before the FIRST mutation of a pre-existing file in a
run we copy it to `<name>.bak`, so a bad merge or a partial write is always
recoverable from the last-known-good copy.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path


# Paths whose pre-existing content has already been preserved (or that did not
# exist) at their first touch this process. One installer invocation == one
# process, so this is exactly "once per run". Tests reset it via the
# isolated_home fixture.
_backed_up: set[Path] = set()


def backup_once(path: Path) -> Path | None:
    """Copy `path` to `<path>.bak` the first time it is touched this run.

    No-op (returns None) when the path was already touched this run or does not
    exist. Marking a not-yet-existing path as touched is intentional: once the
    installer creates a file, later writes in the same run are its own output,
    not the user's pre-existing content, so they need no further backup.
    """
    if path in _backed_up:
        return None
    _backed_up.add(path)
    if not path.exists():
        return None
    bak = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, bak)
    return bak


def atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` atomically (temp file in same dir + fsync + replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if path.exists():
            shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def write_config_text(path: Path, text: str) -> None:
    """Back up a pre-existing user config once, then write it atomically."""
    backup_once(path)
    atomic_write_text(path, text)

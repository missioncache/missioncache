"""Portable cross-process file locking for the context-file sidecar contract.

One implementation for the exclusive sidecar lock that serializes every
context-file writer (the MCP server's ``project_files``, the PM layer's
``pm_items``, the PreCompact hook, migration scripts). The consumers used to
carry four byte-identical ``fcntl.flock`` copies; ``fcntl`` does not exist on
Windows and its module-level import crashed each of them at import time -
including the whole MCP server. This module is the single portable owner.

Mechanics per platform:

* POSIX: ``fcntl.flock(LOCK_EX)`` on the sidecar, blocking indefinitely -
  identical to the historical behavior, so mixed-version processes on one
  POSIX machine still exclude each other correctly.
* Windows: ``msvcrt.locking(LK_LOCK, 1)`` on the first byte. ``LK_LOCK`` is
  NOT flock: it retries ~10 times at 1-second intervals and then raises
  ``OSError`` instead of blocking indefinitely, so acquisition wraps in a
  retry loop to preserve block-until-acquired semantics. Unlock must relock
  the same byte range from the same file position, so the fd seeks back to 0
  first. Native-Windows processes all pick msvcrt and native-POSIX all pick
  flock. The one known gap: an MSYS2/Cygwin Python on a Windows machine has
  ``fcntl`` and would take flock against a file native processes lock via
  msvcrt - no mutual exclusion between those two worlds. MissionCache does
  not support running under MSYS Python; the ``proc`` module refuses MSYS
  tooling for the same reason.

The hook copy in ``hooks/pre_compact.py`` stays deliberately inlined (the
hooks test harness imports hooks as modules and mocks ``missioncache_db``
wholesale, so a shared import would return a MagicMock there) - it mirrors
this file's logic and carries a sync comment. If locking semantics change,
change both.
"""

from __future__ import annotations

import contextlib
import errno
import os
import time
from pathlib import Path
from typing import Iterator

try:  # POSIX
    import fcntl

    _HAVE_FCNTL = True
except ImportError:  # Windows
    import msvcrt

    _HAVE_FCNTL = False

# Seconds between acquisition retries on Windows once msvcrt's own internal
# ~10x1s retry burst is exhausted. Kept short: contended sidecar holds are
# sub-second writes, so a stuck acquisition here means a genuinely long hold.
_WIN_RETRY_INTERVAL = 0.25


@contextlib.contextmanager
def exclusive_lock(lock_path: Path, shared: bool = False) -> Iterator[None]:
    """Hold a cross-process lock on ``lock_path``, blocking until held.

    ``shared=True`` requests a reader lock: POSIX maps it to ``LOCK_SH`` so
    readers run concurrently. Windows has no shared byte-range lock in
    msvcrt, so every acquisition there is exclusive - readers serialize
    behind writers. Correctness is preserved (mutual exclusion is a superset
    of shared locking); the cost is reader concurrency on files whose holds
    are sub-second.

    The file is created if missing and never deleted (deleting a lock file
    races: a process can lock an unlinked inode while a new file takes its
    place, silently splitting the mutex).
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # "a", never "w": "w" truncates BEFORE the lock is taken - on Windows that
    # writes into a byte range another process may hold, and on any platform a
    # pre-planted symlink at the lock path would have its target truncated.
    # Append mode creates-if-missing without either behavior.
    with open(lock_path, "a", encoding="utf-8") as lockfd:
        if _HAVE_FCNTL:
            fcntl.flock(
                lockfd.fileno(), fcntl.LOCK_SH if shared else fcntl.LOCK_EX
            )
            try:
                yield
            finally:
                fcntl.flock(lockfd.fileno(), fcntl.LOCK_UN)
        else:
            fileno = lockfd.fileno()
            while True:
                try:
                    lockfd.seek(0)
                    msvcrt.locking(fileno, msvcrt.LK_LOCK, 1)
                    break
                except OSError as e:
                    # Contention only: LK_LOCK reports an exhausted internal
                    # ~10x1s retry burst as EDEADLK/EACCES. Anything else
                    # (EBADF, EINVAL - a broken fd) re-raises, matching
                    # flock's contract of block-on-contention, raise-on-error.
                    if e.errno not in (errno.EDEADLK, errno.EACCES):
                        raise
                    time.sleep(_WIN_RETRY_INTERVAL)
            try:
                yield
            finally:
                # Closing the fd releases the lock regardless; an unlock error
                # must not replace the body's real exception.
                with contextlib.suppress(OSError):
                    lockfd.seek(0)
                    msvcrt.locking(fileno, msvcrt.LK_UNLCK, 1)


@contextlib.contextmanager
def sidecar_lock(path: Path) -> Iterator[None]:
    """Exclusive lock on the ``<path>.lock`` sidecar - the context-file contract.

    Every writer of ``<name>-context.md`` locks ``<name>-context.md.lock``,
    whichever package the writer lives in. This wrapper owns the sidecar
    naming so the copies cannot drift on it.
    """
    with exclusive_lock(path.with_name(path.name + ".lock")):
        yield


def replace_with_retry(src: "str | Path", dst: "str | Path", attempts: int = 8) -> None:
    """``os.replace`` with a bounded retry on Windows sharing violations.

    Lives here because it exists for the same Windows filesystem-semantics
    class the lock does. On POSIX ``os.replace`` over an open destination
    always succeeds (the old inode lives until its readers close). On Windows
    it raises ``PermissionError`` while any process holds the destination
    open - and the realistic holders here are sub-second (a statusline render
    reading the config the dashboard is writing), so a short exponential
    backoff clears them. Re-raises after ``attempts`` tries: a still-held
    file after ~1.3s is a real conflict the caller should hear about - with
    one named exception: ``atomic_write_json`` deliberately swallows the
    re-raise (best-effort cache writes must not crash hooks), so its callers
    get the retry's persistence but not its final error.

    MIRRORS (deliberately inlined copies that cannot import this module -
    keep the attempts default and backoff constants in sync with both):
    ``hooks/pre_compact.py`` ``_replace_with_retry`` (hooks test harness
    mocks missioncache_db wholesale) and
    ``missioncache_install/fs_utils.py`` ``_replace_with_retry`` (the
    installer runs before missioncache-db is installed).
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    delay = 0.01
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            # Retry is a Windows sharing-violation workaround. On POSIX a
            # PermissionError here (read-only mount, immutable dir) never
            # clears, so raise immediately instead of sleeping through the
            # backoff inside a hook's 5-10s budget.
            if os.name != "nt" or attempt == attempts - 1:
                raise
            time.sleep(delay)
            delay *= 2

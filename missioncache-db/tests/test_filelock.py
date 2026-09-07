"""Tests for missioncache_db.filelock - the portable sidecar lock.

Spec source: the shared-sidecar contract (pm_items/project_files/pre_compact
comments): every context-file writer locks the SAME ``<name>-context.md.lock``
sidecar, acquisition blocks until held, and the sidecar is never deleted.
Runs against the real mechanism of whichever platform executes the suite.

The cross-process test holds the lock in THIS process and proves a real child
blocks until release. This shape is mutation-verified (stubbing the lock to a
nullcontext fails it deterministically) and carries no ordering race: the
parent's negative check can only get weaker on a slow runner, never wrong,
and the only file reads are existence checks, immune to torn writes. An
earlier hold-in-child shape failed 5/12 runs on an idle machine - measured,
not estimated - because its release marker was written outside the lock.
"""

import subprocess
import sys
import time
from pathlib import Path

import pytest

from missioncache_db import filelock


def test_sidecar_naming_contract(tmp_path):
    """The lock file is exactly ``<name>.lock`` next to the target."""
    target = tmp_path / "demo-context.md"
    with filelock.sidecar_lock(target):
        assert (tmp_path / "demo-context.md.lock").exists()


def test_sidecar_survives_release(tmp_path):
    """Never deleted - removal under contention would split the mutex."""
    target = tmp_path / "demo-context.md"
    with filelock.sidecar_lock(target):
        pass
    assert (tmp_path / "demo-context.md.lock").exists()


def test_reentry_after_release(tmp_path):
    """A released lock is immediately acquirable again in-process."""
    target = tmp_path / "demo-context.md"
    with filelock.sidecar_lock(target):
        pass
    with filelock.sidecar_lock(target):
        pass


_WAITER = """
import sys
sys.path.insert(0, {pkg_dir!r})
from pathlib import Path
from missioncache_db import filelock

Path({started!r}).write_text("x", encoding="utf-8")
with filelock.exclusive_lock(Path({lock!r})):
    Path({acquired!r}).write_text("x", encoding="utf-8")
"""


def _wait_for(path, what, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(what)


def test_cross_process_exclusion(tmp_path):
    """A real child blocks on the held lock and acquires only after release."""
    lock_path = tmp_path / "contract.lock"
    started = tmp_path / "started"
    acquired = tmp_path / "acquired"
    pkg_dir = str(Path(filelock.__file__).resolve().parents[1])
    child = None
    try:
        with filelock.exclusive_lock(lock_path):
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    _WAITER.format(
                        pkg_dir=pkg_dir,
                        lock=str(lock_path),
                        started=str(started),
                        acquired=str(acquired),
                    ),
                ]
            )
            _wait_for(started, "child never started")
            # A slow runner only weakens this negative check - it can never
            # produce a false failure.
            time.sleep(1.0)
            assert not acquired.exists(), (
                "child acquired while this process held the lock"
            )
        _wait_for(acquired, "the blocked waiter never acquired after release")
        assert child.wait(timeout=30) == 0
    finally:
        if child and child.poll() is None:
            child.kill()
            child.wait(timeout=30)


class TestSidecarLocks:
    """Contract: dedup, sort, acquire in that order.

    Ordering is the whole point. Two callers taking the same pair in
    opposite orders deadlock, and a sidecar lock blocks forever with no
    timeout and no error, so the failure is a hung process rather than
    anything a user can read.
    """

    def test_paths_are_locked_in_sorted_order(self, tmp_path, monkeypatch):
        taken = []
        real = filelock.sidecar_lock
        monkeypatch.setattr(
            filelock,
            "sidecar_lock",
            lambda p: (taken.append(Path(p).name), real(p))[1],
        )
        paths = [tmp_path / n for n in ("c.md", "a.md", "b.md")]
        with filelock.sidecar_locks(paths):
            pass
        assert taken == ["a.md", "b.md", "c.md"]

    def test_the_same_pair_locks_identically_from_either_direction(
        self, tmp_path, monkeypatch
    ):
        # The deadlock this prevents: caller A locks x then y while caller B
        # locks y then x. Both must produce the same order.
        orders = []
        real = filelock.sidecar_lock
        monkeypatch.setattr(
            filelock,
            "sidecar_lock",
            lambda p: (orders.append(Path(p).name), real(p))[1],
        )
        x, y = tmp_path / "x.md", tmp_path / "y.md"
        with filelock.sidecar_locks([x, y]):
            pass
        first = list(orders)
        orders.clear()
        with filelock.sidecar_locks([y, x]):
            pass
        assert first == orders

    def test_a_repeated_path_is_locked_once(self, tmp_path, monkeypatch):
        # Without the dedup this self-deadlocks: flock blocks a second
        # acquisition of the same lockfile from the same process.
        taken = []
        real = filelock.sidecar_lock
        monkeypatch.setattr(
            filelock,
            "sidecar_lock",
            lambda p: (taken.append(Path(p).name), real(p))[1],
        )
        same = tmp_path / "one.md"
        with filelock.sidecar_locks([same, same, same]):
            pass
        assert taken == ["one.md"]

    def test_it_really_excludes_another_process(self, tmp_path):
        # The recording tests above patch the primitive, so one test must
        # exercise the real lock end to end.
        import subprocess
        import sys

        target = tmp_path / "held.md"
        with filelock.sidecar_locks([target]):
            probe = subprocess.run(
                [
                    sys.executable, "-c",
                    "import sys,pathlib;sys.path.insert(0, sys.argv[1]);"
                    "from missioncache_db import filelock;"
                    "import fcntl;"
                    "f=open(sys.argv[2],'a');"
                    "fcntl.flock(f.fileno(), fcntl.LOCK_EX|fcntl.LOCK_NB)",
                    str(Path(filelock.__file__).parent.parent),
                    str(target) + ".lock",
                ],
                capture_output=True,
                timeout=20,
            )
        assert probe.returncode != 0, "a second process acquired the held lock"

    def test_an_empty_path_list_is_a_no_op(self, tmp_path):
        with filelock.sidecar_locks([]):
            pass

    def test_locks_release_when_the_body_raises(self, tmp_path):
        target = tmp_path / "boom.md"
        with pytest.raises(RuntimeError):
            with filelock.sidecar_locks([target]):
                raise RuntimeError("boom")
        # Re-acquirable, so the ExitStack unwound.
        with filelock.sidecar_locks([target]):
            pass

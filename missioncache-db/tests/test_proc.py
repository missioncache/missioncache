"""Tests for missioncache_db.proc - the portable process backend.

Two layers deliberately mixed here:

* POSIX-runnable behavior tests (they run on whichever platform executes the
  suite; the windows-latest CI job added in this arc runs them there too).
* Contract assertions that hold on EVERY platform for the Windows constants.
  These exist because the Windows branches were written with no Windows
  machine available, and two independent adversarial reviews each found a
  liveness-inverting bug (a missing SYNCHRONIZE access right) that no macOS
  run could surface. The constants are module-level precisely so these
  assertions do not need Windows.
"""

import os
import subprocess
import sys

import pytest

from missioncache_db import proc


class TestWindowsContractConstants:
    def test_alive_mask_carries_synchronize(self):
        """WaitForSingleObject requires SYNCHRONIZE; without it the wait
        returns WAIT_FAILED for every process and process_alive reads every
        live session as dead. The mask is the whole fix - pin it."""
        assert proc.ALIVE_ACCESS_MASK & proc.SYNCHRONIZE
        assert proc.ALIVE_ACCESS_MASK == 0x00101000

    def test_wait_constants_are_distinct(self):
        """WAIT_FAILED (0xFFFFFFFF) must never satisfy either comparison."""
        assert proc.WAIT_TIMEOUT != proc.WAIT_OBJECT_0
        assert 0xFFFFFFFF not in (proc.WAIT_TIMEOUT, proc.WAIT_OBJECT_0)

    def test_undeclared_restype_would_kill_the_handle_guard(self):
        """Documents WHY proc declares restype on every kernel32 function: a
        default c_int return can never equal c_void_p(-1).value on 64-bit,
        so the INVALID_HANDLE comparison would be dead code without it."""
        import ctypes

        assert ctypes.c_int(-1).value != ctypes.c_void_p(-1).value


class TestLiveness:
    def test_self_is_alive(self):
        assert proc.process_alive(os.getpid()) is True

    def test_reaped_child_is_dead(self):
        child = subprocess.Popen([sys.executable, "-c", ""])
        child.wait()
        # A reaped pid may be recycled on a busy machine; accept False (dead)
        # or None (unknown), never True-with-our-token: the recycle guard in
        # session_is_alive is the layer that owns that case.
        assert proc.process_alive(child.pid) in (False, None)

    def test_pid_guards(self):
        assert proc.process_alive(0) is None
        assert proc.process_alive(-5) is None
        assert proc.process_start_token(1) is None
        assert proc.parent_pid(0) is None
        assert proc.process_name(-1) is None
        assert proc.parent_and_name(0) == (None, None)


class TestIdentity:
    def test_start_token_is_stable_for_a_live_process(self):
        first = proc.process_start_token(os.getpid())
        second = proc.process_start_token(os.getpid())
        assert first is not None
        assert first == second

    def test_parent_and_name_matches_the_split_calls(self):
        """The fused lookup must agree with the individual ones - it exists
        for cost, not for different answers."""
        pid = os.getpid()
        ppid, name = proc.parent_and_name(pid)
        assert ppid == proc.parent_pid(pid)
        assert name == proc.process_name(pid)

    def test_process_name_strips_exe(self):
        """Normalization contract: ancestry walks compare against 'claude'
        on every platform, so .exe must never survive."""
        pid = os.getpid()
        name = proc.process_name(pid)
        assert name is not None
        assert not name.lower().endswith(".exe")


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="parses the POSIX `ps` output; on Windows parent_and_name uses "
    "Toolhelp32 and never consults _ps_field, so the monkeypatch is inert",
)
class TestParentAndNameParse:
    """Direct coverage of the fused ps read - the one POSIX behavior change in
    the single-lookup optimization, previously covered only through the walk
    fixture (which is exactly how its first regression hid)."""

    def _with_ps(self, monkeypatch, value):
        monkeypatch.setattr(proc, "_ps_field", lambda pid, fmt: value)

    def test_parses_ppid_and_basename(self, monkeypatch):
        self._with_ps(monkeypatch, "  123 /usr/local/bin/claude")
        assert proc.parent_and_name(999) == (123, "claude")

    def test_strips_exe_suffix(self, monkeypatch):
        self._with_ps(monkeypatch, "123 Claude.exe")
        assert proc.parent_and_name(999) == (123, "Claude")

    def test_none_on_garbage_ppid(self, monkeypatch):
        self._with_ps(monkeypatch, "notapid claude")
        assert proc.parent_and_name(999) == (None, None)

    def test_none_on_empty(self, monkeypatch):
        self._with_ps(monkeypatch, None)
        assert proc.parent_and_name(999) == (None, None)

    def test_ppid_without_name(self, monkeypatch):
        self._with_ps(monkeypatch, "123")
        assert proc.parent_and_name(999) == (123, None)

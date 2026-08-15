"""Portable process introspection: liveness, identity, ancestry.

The session-identity layer needs four questions answered about a pid: is it
alive, when did it start (an opaque token for pid-reuse detection), who is its
parent, and what executable is it. On POSIX those come from ``ps``; on Windows
they must not - two hard reasons:

* ``os.kill(pid, 0)`` is not a probe on Windows: signal 0 is literally
  ``CTRL_C_EVENT``, so the call sends Ctrl+C to the console process GROUP of
  ``pid``. A "liveness check" built on it interrupts the probed session.
* ``ps`` must not be used even when it resolves: Git-for-Windows' MSYS
  ``ps.exe`` answers ``-o lstart=`` with garbage, which would silently record
  bogus start tokens and mark live sessions dead via the pid-reuse branch.
  The platform gate is ``sys.platform``, never a PATH probe.

The Windows backend is ctypes-only (no new dependencies): a Toolhelp32
snapshot for parent pid + executable name, ``OpenProcess`` +
``WaitForSingleObject(0)`` for liveness, and ``GetProcessTimes`` for the
creation-time token.

Start tokens are OPAQUE: a ``ps lstart`` string on POSIX, a FILETIME integer
rendered as a string on Windows. They are only ever compared for equality
against a token recorded on the same machine, so the formats never mix.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from typing import Optional

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

# Module-level (not gated) so POSIX tests can assert the contract without a
# Windows machine: the alive-probe mask MUST carry SYNCHRONIZE, or
# WaitForSingleObject returns WAIT_FAILED for every process and the liveness
# layer inverts. Found by two independent adversarial reviews pre-merge.
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
ALIVE_ACCESS_MASK = PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE
WAIT_OBJECT_0 = 0x0
WAIT_TIMEOUT = 0x102


# ─── POSIX backend ────────────────────────────────────────────────────────


def _ps_field(pid: int, fmt: str) -> Optional[str]:
    """Return a single ``ps -o <fmt>=`` field for ``pid``, or None.

    Best-effort and portable across macOS and Linux (no ``/proc`` dependency,
    which macOS lacks). Returns None when the process is gone, ps is missing,
    or the call errors out.
    """
    try:
        out = subprocess.run(
            ["ps", "-o", f"{fmt}=", "-p", str(pid)],
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


# ─── Windows backend (ctypes; never imported on POSIX) ────────────────────

if _IS_WINDOWS:
    import ctypes
    import ctypes.wintypes as wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _TH32CS_SNAPPROCESS = 0x2
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _ERROR_ACCESS_DENIED = 5
    _ERROR_INVALID_PARAMETER = 87
    _ERROR_BAD_LENGTH = 24

    # Explicit signatures: without restype, ctypes defaults every return to
    # c_int, truncating 64-bit HANDLEs and making the INVALID_HANDLE_VALUE
    # comparison below dead code (c_int -1 != c_void_p(-1).value). Declared
    # once so each behavior is by contract, not accident.
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.c_void_p] * 4
    _kernel32.GetProcessTimes.restype = wintypes.BOOL

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),  # ULONG_PTR
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    _kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _kernel32.Process32FirstW.restype = wintypes.BOOL
    _kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _kernel32.Process32NextW.restype = wintypes.BOOL

    def _snapshot_entry(pid: int) -> Optional[_PROCESSENTRY32W]:
        """The Toolhelp32 process entry for ``pid``, or None.

        ERROR_BAD_LENGTH is a documented transient for this snapshot API with
        an explicit retry recommendation, so it gets a bounded retry before
        the failure degrades to "not found".
        """
        snap = _INVALID_HANDLE_VALUE
        for _ in range(3):
            snap = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
            if snap != _INVALID_HANDLE_VALUE:
                break
            if ctypes.get_last_error() != _ERROR_BAD_LENGTH:
                break
        if snap == _INVALID_HANDLE_VALUE:
            return None
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
            if not _kernel32.Process32FirstW(snap, ctypes.byref(entry)):
                return None
            while True:
                if entry.th32ProcessID == pid:
                    return entry
                if not _kernel32.Process32NextW(snap, ctypes.byref(entry)):
                    return None
        finally:
            _kernel32.CloseHandle(snap)

    def _open_limited(pid: int, access: int = PROCESS_QUERY_LIMITED_INFORMATION):
        return _kernel32.OpenProcess(access, False, pid)


# ─── Public surface ───────────────────────────────────────────────────────


def process_alive(pid: int) -> Optional[bool]:
    """Whether ``pid`` is a running process. Tri-state.

    ``True`` running, ``False`` proven gone, ``None`` unknown (permissions or
    backend failure - callers treat as possibly-alive, matching the
    ``session_is_alive`` contract).
    """
    if not isinstance(pid, int) or pid <= 1:
        return None
    if not _IS_WINDOWS:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except OSError:
            return None
    try:
        handle = _open_limited(
            pid, ALIVE_ACCESS_MASK
        )
        if not handle:
            err = ctypes.get_last_error()
            # Access denied proves a process exists behind the pid, and
            # invalid parameter proves nothing is there. Every OTHER failure
            # (out of memory, a future error mode) is unknown - it must never
            # become the tri-state's proof value. Same inversion class as the
            # SYNCHRONIZE bug, one layer down.
            if err == _ERROR_ACCESS_DENIED:
                return True
            if err == _ERROR_INVALID_PARAMETER:
                return False
            logger.debug("OpenProcess failed for pid %s: winerror %s", pid, err)
            return None
        try:
            # Zero-timeout wait: WAIT_TIMEOUT = not signaled = still running;
            # WAIT_OBJECT_0 = signaled = exited; anything else (WAIT_FAILED)
            # is unknown, never "dead". (GetProcessTimes-style
            # GetExitCodeProcess is the classic alternative but reads exit
            # code 259 as alive.)
            result = _kernel32.WaitForSingleObject(handle, 0)
            if result == WAIT_TIMEOUT:
                return True
            if result == WAIT_OBJECT_0:
                return False
            return None
        finally:
            _kernel32.CloseHandle(handle)
    except Exception:
        # Debug, not warning: hooks run this on every prompt and a broken
        # backend must degrade quietly - but silently-forever is how the
        # SYNCHRONIZE bug class hides, so leave a greppable trace.
        logger.debug("proc backend failed for pid %s", pid, exc_info=True)
        return None


def process_start_token(pid: int) -> Optional[str]:
    """An opaque per-boot-stable token for when ``pid`` started, or None.

    Equal tokens mean the same process; a differing token for a live pid
    means the pid was recycled by an unrelated process. Compare only against
    tokens recorded on the same machine.
    """
    if not isinstance(pid, int) or pid <= 1:
        return None
    if not _IS_WINDOWS:
        return _ps_field(pid, "lstart")
    try:
        handle = _open_limited(pid)
        if not handle:
            return None
        try:
            creation = ctypes.c_ulonglong()
            exit_t = ctypes.c_ulonglong()
            kernel_t = ctypes.c_ulonglong()
            user_t = ctypes.c_ulonglong()
            ok = _kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_t),
                ctypes.byref(kernel_t),
                ctypes.byref(user_t),
            )
            return str(creation.value) if ok else None
        finally:
            _kernel32.CloseHandle(handle)
    except Exception:
        # Debug, not warning: hooks run this on every prompt and a broken
        # backend must degrade quietly - but silently-forever is how the
        # SYNCHRONIZE bug class hides, so leave a greppable trace.
        logger.debug("proc backend failed for pid %s", pid, exc_info=True)
        return None


def parent_pid(pid: int) -> Optional[int]:
    """The parent pid of ``pid``, or None."""
    if not isinstance(pid, int) or pid <= 1:
        return None
    if not _IS_WINDOWS:
        raw = _ps_field(pid, "ppid")
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            return None
    try:
        entry = _snapshot_entry(pid)
        return int(entry.th32ParentProcessID) if entry else None
    except Exception:
        # Debug, not warning: hooks run this on every prompt and a broken
        # backend must degrade quietly - but silently-forever is how the
        # SYNCHRONIZE bug class hides, so leave a greppable trace.
        logger.debug("proc backend failed for pid %s", pid, exc_info=True)
        return None


def process_name(pid: int) -> Optional[str]:
    """The executable basename of ``pid`` with any ``.exe`` stripped, or None.

    Normalized so ancestry walks can compare against ``claude`` on every
    platform (``claude.exe`` on Windows, ``claude`` elsewhere).
    """
    if not isinstance(pid, int) or pid <= 1:
        return None
    if not _IS_WINDOWS:
        comm = _ps_field(pid, "comm")
        if not comm:
            return None
        name = os.path.basename(comm)
    else:
        try:
            entry = _snapshot_entry(pid)
        except Exception:
            return None
        if not entry:
            return None
        name = entry.szExeFile
    if name.lower().endswith(".exe"):
        name = name[: -len(".exe")]
    return name or None


def parent_and_name(pid: int) -> "tuple[Optional[int], Optional[str]]":
    """``(parent_pid, normalized_name)`` of ``pid`` in ONE backend lookup.

    The ancestry walk reads both fields per level; fetching them separately
    doubled the cost - two full Toolhelp32 snapshots per level on Windows,
    two ``ps`` spawns on POSIX - inside hooks with single-digit-second
    budgets. One ``ps -o ppid=,comm=`` / one snapshot serves both.
    """
    if not isinstance(pid, int) or pid <= 1:
        return (None, None)
    if not _IS_WINDOWS:
        raw = _ps_field(pid, "ppid=,comm")
        # ps -o ppid=,comm= emits "  123 /path/to/comm" on one line.
        if not raw:
            return (None, None)
        parts = raw.split(None, 1)
        if not parts:
            return (None, None)
        try:
            ppid = int(parts[0])
        except ValueError:
            return (None, None)
        name = os.path.basename(parts[1].strip()) if len(parts) > 1 else None
        if name and name.lower().endswith(".exe"):
            name = name[: -len(".exe")]
        return (ppid, name or None)
    try:
        entry = _snapshot_entry(pid)
    except Exception:
        logger.debug("proc backend failed for pid %s", pid, exc_info=True)
        return (None, None)
    if not entry:
        return (None, None)
    name = entry.szExeFile
    if name.lower().endswith(".exe"):
        name = name[: -len(".exe")]
    return (int(entry.th32ParentProcessID), name or None)

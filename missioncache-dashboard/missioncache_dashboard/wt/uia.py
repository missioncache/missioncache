"""Windows Terminal over UI Automation, and the panels' keys. Windows only.

Windows Terminal has no API, so tabs are read over UI Automation: every
window's TabItems, their titles and which one is selected. A TabItem has no
id, but its UIA runtime id is stable while the tab lives, so a row id is the
window handle plus that runtime id.

UI Automation runs on one thread of its own. The daemon's event loop hands
it work and awaits the answer, so no TabItem ever crosses threads. A call
into a hung window can block that thread for good, so a thread stuck in one
job is left behind and a new one takes over.
"""

import asyncio
import ctypes
import queue
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from ctypes import wintypes
from typing import Any

import uiautomation as auto

from ..panels.server import NotFound

WT_WINDOW_CLASS = "CASCADIA_HOSTING_WINDOW_CLASS"
CALL_TIMEOUT = 10
# A job running this long means the thread is stuck in a hung window.
STUCK_SECONDS = 30
# The package a Terminal process runs from: stable or Preview.
FAMILY_RE = re.compile(r"\\(Microsoft\.WindowsTerminal\w*)_[^\\]*_(\w+)\\[^\\]+$")

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32.OpenProcess.restype = wintypes.HANDLE
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.SetWindowsHookExW.argtypes = (ctypes.c_int, ctypes.c_void_p, wintypes.HINSTANCE, wintypes.DWORD)
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.CallNextHookEx.argtypes = (wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class Uia:
    def __init__(self) -> None:
        # Row id -> (window handle, TabItem), from the last read. UI thread only.
        self.elements: dict[str, tuple[int, Any]] = {}
        # The Terminal window in front most recently, for the package to open in.
        self.last_window: int | None = None
        self._start()

    def _start(self) -> None:
        self.jobs: queue.Queue = queue.Queue()
        self.failed: BaseException | None = None  # UI Automation did not start
        self.busy_since: float | None = None  # when the running job started
        self.elements = {}  # a new thread must not touch the old one's elements
        threading.Thread(target=self._run, args=(self.jobs,), name="uia", daemon=True).start()

    def _run(self, jobs: queue.Queue) -> None:
        try:
            initializer = auto.UIAutomationInitializerInThread()
        except Exception as e:  # noqa: BLE001 - every call reports it from here on
            self.failed = e
            return
        with initializer:
            while True:
                work, future = jobs.get()
                if not future.set_running_or_notify_cancel():
                    continue  # its caller already gave up
                if jobs is self.jobs:
                    self.busy_since = time.monotonic()
                try:
                    future.set_result(work())
                except BaseException as e:  # noqa: BLE001 - the caller gets the error
                    future.set_exception(e)
                if jobs is not self.jobs:
                    return  # replaced while stuck: the new thread owns the work
                self.busy_since = None

    async def _call(self, work: Callable[[], Any]) -> Any:
        if self.failed:
            raise RuntimeError(f"UI Automation did not start: {self.failed!r}")
        if self.busy_since is not None and time.monotonic() - self.busy_since > STUCK_SECONDS:
            print(f"the UI Automation thread is stuck in one call for over {STUCK_SECONDS} seconds, "
                  "starting a new one", flush=True)
            self._start()
        future: Future = Future()
        self.jobs.put((work, future))
        async with asyncio.timeout(CALL_TIMEOUT):
            return await asyncio.wrap_future(future)

    async def tabs(self) -> tuple[list[tuple[str, str]], set[str]]:
        """``(row id, title)`` of every tab, and the windows that showed no
        tabs (opening or closing), whose rows are kept this round."""
        return await self._call(self._tabs)

    async def front_row(self, hint: str | None) -> str | None:
        """The selected tab of the Terminal window in front, if one is."""
        return await self._call(lambda: self._front_row(hint))

    async def focus(self, row_id: str) -> None:
        await self._call(lambda: self._focus(row_id))

    async def family(self) -> str | None:
        """The package family of the Terminal used last, like
        ``Microsoft.WindowsTerminalPreview_8wekyb3d8bbwe``."""
        return await self._call(self._family)

    def _tabs(self) -> tuple[list[tuple[str, str]], set[str]]:
        tabs, elements, empty = [], {}, set()
        for window in auto.GetRootControl().GetChildren():
            if window.ClassName != WT_WINDOW_CLASS:
                continue
            hwnd = window.NativeWindowHandle
            items = _tab_items(window)
            if not items:
                empty.add(str(hwnd))
            for tab in items:
                rid = f"{hwnd}-{'.'.join(map(str, tab.GetRuntimeId()))}"
                tabs.append((rid, tab.Name))
                elements[rid] = (hwnd, tab)
        self.elements = elements
        return tabs, empty

    def _front_row(self, hint: str | None) -> str | None:
        front = user32.GetForegroundWindow()
        if not front or _class_name(front) != WT_WINDOW_CLASS:
            return None
        self.last_window = front
        # The tab in front is usually still the same one, so it is asked first.
        for rid, (hwnd, tab) in sorted(self.elements.items(), key=lambda e: e[0] != hint):
            if hwnd == front:
                try:
                    if tab.GetSelectionItemPattern().IsSelected:
                        return rid
                except Exception:  # noqa: BLE001 - a tab closing under us; the next read drops it
                    return None
        return None

    def _focus(self, row_id: str) -> None:
        if not (found := self.elements.get(row_id)):
            raise NotFound(f"no live tab {row_id}")
        hwnd, tab = found
        tab.GetSelectionItemPattern().Select()
        if not bring_to_front(hwnd):
            raise NotFound("Windows did not let the Terminal window come to the front")

    def _family(self) -> str | None:
        hwnd = self.last_window or next((h for h, _ in self.elements.values()), None)
        path = _process_path(hwnd) if hwnd else None
        found = FAMILY_RE.search(path or "")
        return f"{found[1]}_{found[2]}" if found else None


def _tab_items(control: Any, depth: int = 0) -> list:
    """The TabItems inside a Terminal window, in the order of its tab row."""
    found = []
    for child in control.GetChildren():
        if child.ControlType == auto.ControlType.TabItemControl:
            found.append(child)
        elif depth < 6:
            found += _tab_items(child, depth + 1)
    return found


def _class_name(hwnd: int) -> str:
    name = ctypes.create_unicode_buffer(64)
    return name.value if user32.GetClassNameW(hwnd, name, 64) else ""


def _process_path(hwnd: int) -> str | None:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(1024)
        path = ctypes.create_unicode_buffer(size.value)
        return path.value if kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)) else None
    finally:
        kernel32.CloseHandle(handle)


def bring_to_front(hwnd: int) -> bool:
    """Windows lets only the process in front move another window to the
    front. Joining the input of the window in front lifts that, for a click in
    the panel (the browser is in front) as well as for a key in a Terminal
    window."""
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    front = user32.GetForegroundWindow()
    if front == hwnd:
        return True
    ours = kernel32.GetCurrentThreadId()
    theirs = user32.GetWindowThreadProcessId(front, None) if front else 0
    attached = bool(theirs) and theirs != ours and user32.AttachThreadInput(ours, theirs, True)
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(ours, theirs, False)
    return user32.GetForegroundWindow() == hwnd


# Ctrl+Shift+<key>: ] is the row below, [ the row above, \ the newest unseen.
# By scan code, the physical key, so they stay on the same keys in every
# keyboard layout, as on macOS.
KEYS = {0x1B: "next", 0x1A: "prev", 0x2B: "unseen"}
WH_KEYBOARD_LL = 13
LLKHF_EXTENDED = 0x01
VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN = 0x10, 0x11, 0x12, 0x5B, 0x5C
WM_KEYDOWN, WM_SYSKEYDOWN, WM_KEYUP, WM_SYSKEYUP = 0x100, 0x104, 0x101, 0x105
HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


def _pressed(vk: int) -> bool:
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def _hotkey_held() -> bool:
    return (_pressed(VK_CONTROL) and _pressed(VK_SHIFT) and not _pressed(VK_MENU)
            and not _pressed(VK_LWIN) and not _pressed(VK_RWIN)
            and _class_name(user32.GetForegroundWindow()) == WT_WINDOW_CLASS)


def watch_keys(on_key: Callable[[str], object]) -> None:
    """Take the three keys while a Terminal window is in front, on a thread
    of its own. A low-level keyboard hook rather than RegisterHotKey, so other
    programs still get them while no Terminal window is in front. Windows
    passes the hook every key press. It looks only for those three keys and
    keeps nothing. Windows drops a hook that answers slowly, so ``on_key``
    must only hand the work on."""

    def run() -> None:
        swallowed = set()

        @HOOKPROC
        def on_event(code: int, wparam: int, lparam: int) -> int:
            if code == 0:
                event = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                key = event.scanCode if not event.flags & LLKHF_EXTENDED else None
                if key in KEYS and (key in swallowed or wparam in (WM_KEYDOWN, WM_SYSKEYDOWN)):
                    if wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                        if _hotkey_held():
                            swallowed.add(key)
                            on_key(KEYS[key])
                            return 1
                    elif wparam in (WM_KEYUP, WM_SYSKEYUP):
                        swallowed.discard(key)
                        return 1
            return user32.CallNextHookEx(None, code, wparam, lparam)

        if not user32.SetWindowsHookExW(WH_KEYBOARD_LL, ctypes.cast(on_event, ctypes.c_void_p), None, 0):
            print(f"keyboard hook failed: error {ctypes.get_last_error()}", flush=True)
            return
        msg = wintypes.MSG()
        while (got := user32.GetMessageW(ctypes.byref(msg), None, 0, 0)) > 0:
            pass
        print(f"keyboard hook stopped: GetMessageW returned {got}, error {ctypes.get_last_error()}", flush=True)

    threading.Thread(target=run, name="keys", daemon=True).start()

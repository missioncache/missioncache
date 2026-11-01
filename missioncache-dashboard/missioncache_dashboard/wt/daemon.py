"""The daemon behind the Windows Terminal panels: it reads the Terminal's tabs,
serves the two panels and a page that shows both on 127.0.0.1, and, when
asked to, takes the three keys while a Terminal window is in front. The HTTP
side and the resume lists are ``panels.server``, shared with every terminal.

Nothing in Windows ties a process to a tab (its console window's owner moves
between Terminal windows), so a tab is joined to its Claude Code session by
title (``logic.join_claude``). A session's status comes from the MissionCache
mod's ``agent_status`` row and Claude Code's own record. A plain shell has
only a title, so a new title is its activity.

The process keeps running while Windows Terminal is closed, and shows its
tabs again once it is back.
"""

import argparse
import asyncio
import os
import time
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from missioncache_db import (
    MISSIONCACHE_ROOT,
    _is_valid_session_id,
    encode_claude_project_dir,
    pid_alive,
    resume_sessions,
)
from missioncache_db.lead_board import agent_status_rows

from ..cli import _hide_own_console_window, _redirect_output_to_log
from ..panels import logic
from ..panels.server import DASHBOARD_URL, NotFound, NotOpened, Panels, read_json
from . import WT_EXE_GLOB

PORT = int(os.environ.get("MISSIONCACHE_WT_PORT", "8791"))
POLL_SECONDS = 2
FOCUS_SECONDS = 0.25
WT_TIMEOUT = 10
OPEN_WAIT_SECONDS = 5
TRANSCRIPT_RESCAN_SECONDS = 60
LISTEN_RETRY_SECONDS = 30
DATA_DIR = MISSIONCACHE_ROOT / "wt"
LOG_FILE = DATA_DIR / "run.log"
PANELS_PAGE = Path(__file__).with_name("panels.html")
# Which wt.exe Open runs. By default the one of the Terminal used last.
WT_EXE_ENV = "MISSIONCACHE_WT_EXE"


class Ui(Protocol):
    """Windows Terminal as ``uia.Uia`` reads it."""

    async def tabs(self) -> tuple[list[tuple[str, str]], set[str]]: ...

    async def front_row(self, hint: str | None) -> str | None: ...

    async def focus(self, row_id: str) -> None: ...

    async def family(self) -> str | None: ...


def wt_arg(value: str) -> str:
    # wt.exe splits its command line into several commands at every ";".
    return value.replace(";", r"\;")


def powershell_exe(system_root: str | None) -> str:
    """Windows PowerShell by its full path, not whatever ``powershell`` the
    new tab finds first in its folder and PATH."""
    return os.path.join(system_root or r"C:\Windows", "System32", "WindowsPowerShell", "v1.0", "powershell.exe")


def open_args(wt_exe: str, row: dict[str, Any], powershell: str) -> list[str]:
    """A new tab in the Terminal window used last, named after the row, whose
    shell stays after the command ends, like a command typed into a tab."""
    return [wt_exe, "-w", "0", "new-tab", "--title", wt_arg(row["name"]), "-d", wt_arg(row["cwd"]),
            powershell, "-NoExit", "-Command", wt_arg(row["command"])]


def child_env(env: dict[str, str]) -> dict[str, str]:
    """A new tab inherits this environment. A Claude Code that finds another
    session's variables runs as its child, which writes no
    ``~/.claude/sessions`` record, so its tab would never be joined."""
    return {k: v for k, v in env.items()
            if not (k in ("CLAUDECODE", "CLAUDE_PID") or k.startswith("CLAUDE_CODE_"))}


def pick_wt_exe(override: str | None, family: str | None, local_appdata: str) -> str:
    """The wt.exe of the Terminal package used last (stable or Preview), so
    the tab opens beside the others. Plain ``wt.exe`` is whichever package
    owns the alias."""
    if override:
        return override
    apps = Path(local_appdata) / "Microsoft" / "WindowsApps"
    if family and (own := apps / family / "wt.exe").is_file():
        return str(own)
    found = sorted(apps.glob(WT_EXE_GLOB),
                   key=lambda p: "Preview" in p.parent.name)
    return str(found[0]) if found else "wt.exe"


def claude_sessions() -> list[dict[str, Any]]:
    """Claude Code's own record of every interactive process still running:
    {"pid", "sessionId", "name", "cwd", "status" (busy or idle),
    "statusUpdatedAt" (ms)}."""
    result = []
    for f in (Path.home() / ".claude" / "sessions").glob("*.json"):
        info = read_json(f, None)
        # Headless `claude -p` runs write a record too, and a record can
        # outlive its process or name a pid now used by another one.
        if not (isinstance(info, dict) and info.get("kind") == "interactive"
                and isinstance(info.get("pid"), int) and _is_valid_session_id(info.get("sessionId"))):
            continue
        start = info.get("procStart")
        if pid_alive(info["pid"], str(start) if start is not None else None) is False:
            continue
        result.append(info)
    return result


class TranscriptTitles:
    """The title Claude Code put in a session's tab: the latest ``/rename``
    title, else the one it generated after the first prompt. Its record in
    ``~/.claude/sessions`` keeps only a derived name. A transcript only grows,
    so each call reads only what was appended."""

    @dataclass
    class Entry:
        path: Path | None
        looked_at: float
        offset: int = 0
        titles: dict[str, str] = field(default_factory=dict)

    def __init__(self) -> None:
        self.cache: dict[str, TranscriptTitles.Entry] = {}

    def title(self, c: dict[str, Any]) -> str | None:
        sid = c["sessionId"]
        entry = self.cache.get(sid)
        if entry is None or (entry.path is None and time.monotonic() - entry.looked_at > TRANSCRIPT_RESCAN_SECONDS):
            # A session waiting for its first prompt has no transcript yet.
            entry = self.cache[sid] = self.Entry(find_transcript(sid, c.get("cwd") or ""), time.monotonic())
        if entry.path is None:
            return None
        entry.offset = resume_sessions.scan_titles(str(entry.path), entry.offset, entry.titles)
        return entry.titles.get("custom-title") or entry.titles.get("ai-title")

    def keep_only(self, running: set[str]) -> None:
        for sid in set(self.cache) - running:
            del self.cache[sid]


def find_transcript(session_id: str, cwd: str) -> Path | None:
    """Its folder is named after the session's cwd, so that is tried first. A
    long cwd gets a shortened folder name, so a scan is the fallback."""
    projects = Path.home() / ".claude" / "projects"
    if cwd and (direct := projects / encode_claude_project_dir(cwd) / f"{session_id}.jsonl").is_file():
        return direct
    return next(projects.glob(f"*/{session_id}.jsonl"), None)


class WindowsTerminal:
    """The panels' Terminal over ``Ui``. Row ids are a window handle and a
    tab's runtime id."""

    def __init__(self, ui: Ui) -> None:
        self.ui = ui
        self.live: set[str] = set()  # the tabs of the last poll
        # Tabs Open made, until a poll lists them: a poll that read the tabs
        # just before the new one showed would otherwise drop it.
        self.opened: dict[str, float] = {}
        self.front: str | None = None  # the selected tab of the Terminal window used last

    def exists(self, row_id: str) -> bool:
        return row_id in self.live or self.opened.get(row_id, 0) > time.monotonic()

    def see_tabs(self, tabs: set[str]) -> None:
        self.live = tabs
        now = time.monotonic()
        self.opened = {rid: until for rid, until in self.opened.items() if rid not in tabs and until > now}

    def current(self) -> str | None:
        return self.front

    async def focus(self, row_id: str) -> None:
        if not self.exists(row_id):
            raise NotFound(f"no live tab {row_id}")
        await self.ui.focus(row_id)
        self.front = row_id

    async def open_tab(self, row: dict[str, Any]) -> str:
        before = {rid for rid, _ in (await self.ui.tabs())[0]}
        wt_exe = pick_wt_exe(os.environ.get(WT_EXE_ENV), await self.ui.family(), os.environ.get("LOCALAPPDATA", ""))
        # The command is derived from the row and quoted, and resume_sessions
        # gives none for a folder name with a control character. No pipes: a
        # Terminal that wt.exe starts could keep them open.
        try:
            proc = await asyncio.create_subprocess_exec(
                *open_args(wt_exe, row, powershell_exe(os.environ.get("SystemRoot"))),
                env=child_env(dict(os.environ)),
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL)
        except OSError as e:
            raise NotOpened(f"cannot run {wt_exe}: {e}") from e
        try:
            code = await asyncio.wait_for(proc.wait(), WT_TIMEOUT)
        except TimeoutError:
            proc.kill()
            raise NotFound(f"{wt_exe} did not finish in {WT_TIMEOUT} seconds") from None
        if code:
            raise NotOpened(f"{wt_exe} exited {code}")
        # wt.exe hands the tab to the running Terminal and exits, so the tab
        # shows up a moment later. Its id is what a second click goes to.
        deadline = time.monotonic() + OPEN_WAIT_SECONDS
        while time.monotonic() < deadline:
            await asyncio.sleep(FOCUS_SECONDS)
            new = [(rid, title) for rid, title in (await self.ui.tabs())[0] if rid not in before]
            if new:
                # A tab you opened meanwhile is new too: the one with the
                # row's name is ours.
                rid = next((r for r, title in new if logic.bare_title(title) == row["name"]), new[0][0])
                self.opened[rid] = time.monotonic() + OPEN_WAIT_SECONDS + POLL_SECONDS * 2
                return rid
        raise NotFound("Windows Terminal did not show the new tab")


def update_entry(p: Panels, rid: str, title: str, own: dict[str, Any] | None,
                 agents: dict[str, dict[str, Any]], now: float) -> None:
    prev = p.state.get(rid)
    if own:
        status, activity = logic.claude_status(agents.get(own["sessionId"]), own)
    else:
        status = None
        activity = now if prev is None or prev.get("title") != title else prev["changed"]
    entry = p.state[rid] = logic.advance(prev, status, activity, activity if own else None, now)
    bound = p.mc_bound.get(own["sessionId"], {}) if own else {}
    entry.update(title=title, path=(own or {}).get("cwd") or entry.get("path") or str(Path.home()),
                 name=logic.row_name(title, own, bound.get("title")),
                 project=bound.get("project_name") or "",
                 claude=own["sessionId"] if own else None)


async def poll_once(p: Panels, t: WindowsTerminal, titles: TranscriptTitles,
                    written: str | None, errors: "ErrorLog") -> tuple[str, list[str]]:
    """One read of the tabs. Returns what end_cycle wrote and the names of
    the sessions no tab was joined to."""
    tabs, empty = await t.ui.tabs()
    claude = await asyncio.to_thread(claude_sessions)
    try:
        agents = await asyncio.to_thread(agent_status_rows, [c["sessionId"] for c in claude])
    except Exception as e:  # noqa: BLE001 - a locked hooks-state.db; Claude Code's own record still gives a status
        errors.report("reading agent_status", e)
        agents = {}
    else:
        errors.clear("reading agent_status")
    mc_titles = {sid: b["title"] for sid, b in p.mc_bound.items() if b.get("title")}
    joined = await asyncio.to_thread(logic.join_claude, tabs, claude, mc_titles, titles.title)
    titles.keep_only({c["sessionId"] for c in claude})
    now = time.time()
    for rid, title in tabs:
        update_entry(p, rid, title, joined.get(rid), agents, now)
    t.see_tabs({rid for rid, _ in tabs})
    # A window opening or closing shows no tabs for a moment: keep its rows.
    kept = {rid for rid in p.state if rid.partition("-")[0] in empty} | set(t.opened)
    in_tabs = {c["sessionId"] for c in joined.values()}
    unjoined = sorted(str(c.get("name")) for c in claude if c["sessionId"] not in in_tabs)
    # The tab in front is marked seen by watch_focus, as it changes.
    return p.end_cycle(t.live | kept, None, written), unjoined


class ErrorLog:
    """Logs a failure that repeats every round once, with the time and its
    traceback, and once more when it stops. run.log is never rotated."""

    def __init__(self) -> None:
        self.failing: dict[str, tuple[str, int]] = {}  # what -> (error, rounds)

    def report(self, what: str, e: BaseException) -> None:
        error, rounds = self.failing.get(what, ("", 0))
        if repr(e) != error:
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {what} failed: {e!r}", flush=True)
            traceback.print_exception(e)
            rounds = 0
        self.failing[what] = (repr(e), rounds + 1)

    def clear(self, what: str) -> None:
        if (failed := self.failing.pop(what, None)) is not None:
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {what} works again "
                  f"after {failed[1]} failed rounds", flush=True)


async def poll(p: Panels, t: WindowsTerminal, errors: ErrorLog) -> None:
    titles, written, logged = TranscriptTitles(), None, None
    while True:
        try:
            written, unjoined = await poll_once(p, t, titles, written, errors)
            errors.clear("reading the tabs")
            if unjoined != logged:
                if unjoined:
                    print(f"no tab for {unjoined}", flush=True)
                logged = unjoined
        except Exception as e:  # noqa: BLE001 - a Terminal window closing mid-read; try again next round
            errors.report("reading the tabs", e)
        await asyncio.sleep(POLL_SECONDS)


async def watch_focus(p: Panels, t: WindowsTerminal, errors: ErrorLog) -> None:
    """Mark the tab in front as seen, also when it finishes while you watch.
    While another window is in front (the panels themselves), the last
    Terminal tab stays current."""
    while True:
        try:
            note_front(p, t, await t.ui.front_row(t.front))
            errors.clear("reading the tab in front")
        except Exception as e:  # noqa: BLE001 - asked again in a moment
            errors.report("reading the tab in front", e)
        await asyncio.sleep(FOCUS_SECONDS)


def note_front(p: Panels, t: WindowsTerminal, front: str | None) -> None:
    if front:
        # Only when it changed: this runs four times a second, and every
        # write would make state.json change on every poll.
        if front != t.front or (front in p.state and logic.is_unseen(p.state[front])):
            p.mark_seen(front)
        t.front = front


async def run_key(action: Callable[[], Awaitable[None]]) -> None:
    try:
        await action()
    except NotFound as e:
        print(f"key: {e}", flush=True)
    except Exception:  # noqa: BLE001 - one key press must not stop the next
        traceback.print_exc()


async def listen(p: Panels) -> Any:
    """The panels' server. Windows can hold the port for a while (a range
    reserved at boot, a daemon still exiting), so it is asked for again."""
    while True:
        try:
            return await p.serve()
        except OSError as e:
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} cannot listen on {p.host}: {e}. "
                  f"Trying again in {LISTEN_RETRY_SECONDS} seconds", flush=True)
            await asyncio.sleep(LISTEN_RETRY_SECONDS)


async def main(keys: bool, dashboard_url: str) -> None:
    from . import uia  # Windows only

    loop = asyncio.get_running_loop()
    t = WindowsTerminal(uia.Uia())
    p = Panels(t, DATA_DIR, PORT, pages={"/panels": PANELS_PAGE}, framed=True, dashboard_url=dashboard_url)
    p.load_state()
    if keys:
        actions = {"next": lambda: p.step(1), "prev": lambda: p.step(-1), "unseen": p.jump_to_unseen}
        running: set[asyncio.Task] = set()  # the loop keeps only weak references

        def start(name: str) -> None:
            task = asyncio.create_task(run_key(actions[name]))
            running.add(task)
            task.add_done_callback(running.discard)

        uia.watch_keys(lambda name: loop.call_soon_threadsafe(start, name))
    errors = ErrorLog()
    async with await listen(p) as server:
        await asyncio.gather(poll(p, t, errors), watch_focus(p, t, errors), p.watch_missioncache(),
                             server.serve_forever())


def run(argv: list[str] | None = None) -> None:
    """Entry point of ``missioncache-wt``, which the logon task runs."""
    parser = argparse.ArgumentParser(prog="missioncache-wt", description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--hidden", action="store_true", help="hide the console window and log to a file")
    parser.add_argument("--keys", action="store_true",
                        help="take Ctrl+Shift+[ ] \\ while a Windows Terminal window is in front")
    parser.add_argument("--dashboard-port", type=int, help="where the logo and Lead buttons open the dashboard")
    args = parser.parse_args(argv)
    dashboard_url = f"http://127.0.0.1:{args.dashboard_port}/" if args.dashboard_port else DASHBOARD_URL
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if args.hidden:
        _hide_own_console_window()
        _redirect_output_to_log(LOG_FILE)
    asyncio.run(main(args.keys, dashboard_url))

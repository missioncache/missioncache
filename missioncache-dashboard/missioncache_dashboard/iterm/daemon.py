"""The daemon behind the iTerm2 panels: it polls iTerm2, serves the two
panels on 127.0.0.1, and registers them and three key-binding functions with
iTerm2. The HTTP side and the resume lists are ``panels.server``, shared
with every terminal.

The process exits when the iTerm2 connection closes, and launchd starts it
again, which reconnects and registers the panels with the new iTerm2.
"""

import asyncio
import os
import subprocess
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import iterm2
import websockets

from missioncache_db import MISSIONCACHE_ROOT, resume_sessions

from ..panels import logic
from ..panels.server import NotFound, Panels, start_holding, stop_holding

PORT = int(os.environ.get("MISSIONCACHE_ITERM_PORT", "8791"))
POLL_SECONDS = 4
CYCLE_TIMEOUT = 30
DATA_DIR = MISSIONCACHE_ROOT / "iterm"
TOOLS = (("Recent Sessions", "com.missioncache.recent-sessions", "/"),
         ("Saved Tabs", "com.missioncache.saved-tabs", "/saved-page"))
HOLDING_PAGE = b"""<!doctype html><meta charset="utf-8">
<body style="margin:0;font:12px -apple-system,sans-serif;background:#1e1e1e;color:#888;padding:10px">
Waiting for iTerm2...
<p>If this stays, turn on iTerm2 Settings &gt; General &gt; Magic &gt; Enable Python API.</p>
<script>
// Reload once the real server answers. Until then a failed fetch is expected.
setInterval(async () => {
  try { if ((await fetch("/state")).ok) location.reload(); } catch (e) {}
}, 1000);
</script>"""

# The holding page's server, while it has the port.
holding: ThreadingHTTPServer | None = None


class ITerm:
    """The panels' Terminal over iTerm2's Python API. Row ids are iTerm2
    session ids."""

    def __init__(self, connection: iterm2.Connection, app: iterm2.App) -> None:
        self.connection = connection
        self.app = app

    def exists(self, row_id: str) -> bool:
        return self.app.get_session_by_id(row_id) is not None

    async def focus(self, row_id: str) -> None:
        if not (session := self.app.get_session_by_id(row_id)):
            raise NotFound(f"no live session {row_id}")
        await session.async_activate()

    def current(self) -> str | None:
        return current_session_id(self.app.current_terminal_window)

    async def open_tab(self, row: dict[str, Any]) -> str:
        window = self.app.current_terminal_window
        if window:
            tab = await window.async_create_tab()
        else:
            window = await iterm2.Window.async_create(self.connection)
            tab = window.current_tab if window else None
        if not tab or not tab.current_session:
            raise NotFound("iTerm2 did not create the tab")
        await tab.async_set_title(row["name"])
        # The command is derived from the row and quoted, and resume_sessions
        # gives none for a folder name with a control character, so typing it
        # runs exactly that line.
        await tab.current_session.async_send_text(row["command"] + "\n")
        await tab.async_activate(order_window_front=True)
        return tab.current_session.session_id


async def iterm_status(connection: iterm2.Connection, session_id: str) -> str | None:
    # The same call `it2 session get-status` makes, over this connection.
    result = await iterm2.async_invoke_function(
        connection, f'iterm2.get_session_status(session_id: "{session_id}")')
    return result.get("status")


async def claude_sessions() -> dict[str, dict[str, Any]]:
    """tty -> Claude Code's own record of the interactive process on it:
    {"sessionId", "name", "cwd", "status" (busy or idle), "statusUpdatedAt" (ms)}."""
    proc = await asyncio.create_subprocess_exec(
        "ps", "-A", "-o", "pid=,tty=", stdout=asyncio.subprocess.PIPE)
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        print(f"ps exited {proc.returncode}", flush=True)
    tty_by_pid = dict(line.split() for line in out.decode().splitlines() if len(line.split()) == 2)
    result = {}
    for f in (Path.home() / ".claude" / "sessions").glob("*.json"):
        tty = tty_by_pid.get(f.stem)
        info = resume_sessions.claude_session_record(int(f.stem)) if tty else None
        # Headless `claude -p` runs write a record too. Only the interactive
        # process is the one in the tab.
        if isinstance(info, dict) and info.get("kind") == "interactive":
            result[f"/dev/{tty}"] = info
    return result


async def update_entry(p: Panels, t: ITerm, s: iterm2.Session,
                       claude: dict[str, dict[str, Any]], now: float) -> None:
    tty = await s.async_get_variable("tty")
    own = claude.get(tty)
    status = logic.effective_status(await iterm_status(t.connection, s.session_id), own)
    own_change = own["statusUpdatedAt"] / 1000 if own and own.get("statusUpdatedAt") else None
    if status is None:
        st = os.stat(tty)
        activity = logic.shell_activity(st.st_atime, st.st_mtime, await s.async_get_variable("jobName"))
    else:
        activity = own_change or now
    title = (await s.tab.async_get_variable("title") if s.tab else None) or s.name
    path = await s.async_get_variable("path") or str(Path.home())
    entry = p.state[s.session_id] = logic.advance(p.state.get(s.session_id), status, activity, own_change, now)
    bound = p.mc_bound.get(own["sessionId"], {}) if own else {}
    entry.update(title=title, path=path, name=logic.row_name(title, own, bound.get("title")),
                 project=bound.get("project_name") or "",
                 claude=own["sessionId"] if own else None)


def current_session_id(window: iterm2.Window | None) -> str | None:
    tab = window.current_tab if window else None
    session = tab.current_session if tab else None
    return session.session_id if session else None


async def poll(p: Panels, t: ITerm) -> None:
    written = None
    while True:
        # A call whose answer never comes would hang here forever. Time the
        # cycle out instead, which ends the process and lets launchd restart it.
        async with asyncio.timeout(CYCLE_TIMEOUT):
            sessions = [s for w in t.app.terminal_windows for tab in w.tabs for s in tab.sessions]
            claude = await claude_sessions()
            now = time.time()
            results = await asyncio.gather(
                *(update_entry(p, t, s, claude, now) for s in sessions), return_exceptions=True)
        for s, result in zip(sessions, results):
            if isinstance(result, websockets.exceptions.ConnectionClosed):
                raise result
            if isinstance(result, Exception):
                # Usually a tab closed during this cycle. The membership check
                # below drops it if the session is gone.
                print(f"update {s.session_id} failed: {result!r}", flush=True)
        front = t.current() if t.app.app_active else None
        written = p.end_cycle({s.session_id for s in sessions}, front, written)
        await asyncio.sleep(POLL_SECONDS)


async def watch_focus(p: Panels, t: ITerm) -> None:
    app = t.app
    async with iterm2.FocusMonitor(t.connection) as monitor:
        while True:
            update = await monitor.async_get_next_update()
            if update.active_session_changed:
                p.mark_seen(update.active_session_changed.session_id)
            elif update.selected_tab_changed:
                tab = app.get_tab_by_id(update.selected_tab_changed.tab_id)
                p.mark_seen(tab.current_session.session_id if tab and tab.current_session else None)
            elif (update.window_changed and update.window_changed.event
                  == iterm2.FocusUpdateWindowChanged.Reason.TERMINAL_WINDOW_BECAME_KEY):
                p.mark_seen(current_session_id(app.get_window_by_id(update.window_changed.window_id)))


async def watch_connection(connection: iterm2.Connection) -> None:
    # The iterm2 library reads the connection in a task nothing waits on, so a
    # closed connection would otherwise leave this process running with frozen
    # data. Raising here ends main, the library exits, and launchd restarts us.
    await connection.websocket.wait_closed()
    raise ConnectionError("iTerm2 connection closed")


async def main(connection: iterm2.Connection) -> None:
    t = ITerm(connection, await iterm2.async_get_app(connection))
    p = Panels(t, DATA_DIR, PORT)
    p.load_state()

    @iterm2.RPC
    async def jump_to_unseen() -> None:
        """Go to the newest session that finished, needs input or failed since
        it was last seen. Bind a key to it with Invoke Script Function:
        jump_to_unseen()"""
        await p.jump_to_unseen()

    @iterm2.RPC
    async def session_next() -> None:
        """The session below the current one in Recent Sessions. Bind a key to
        it with Invoke Script Function: session_next()"""
        await p.step(1)

    @iterm2.RPC
    async def session_prev() -> None:
        """The session above the current one in Recent Sessions. Bind a key to
        it with Invoke Script Function: session_prev()"""
        await p.step(-1)

    for rpc in (jump_to_unseen, session_next, session_prev):
        try:
            await rpc.async_register(connection)
        except iterm2.notifications.SubscriptionException as e:
            # Another script already registered a function by this name (the
            # earlier panels, while both run). The panels work without it.
            print(f"key binding function not registered: {e}", flush=True)
    # Hand the port from the holding page to the real server before
    # registering: iTerm2 reloads an open panel the moment its tool registers,
    # and a load that finds no server leaves the panel blank for good.
    global holding
    if holding:
        stop_holding(holding)
        holding = None
    async with await p.serve() as server:
        for name, identifier, path in TOOLS:
            await iterm2.tool.async_register_web_view_tool(
                connection, name, identifier, False, f"http://{p.host}{path}")
        await asyncio.gather(poll(p, t), watch_focus(p, t), p.watch_missioncache(),
                             watch_connection(connection), server.serve_forever())


def iterm_running() -> bool:
    # ps, not pgrep: pgrep missed a running iTerm2 when started from inside
    # another app's process tree, while ps listed it.
    out = subprocess.run(["ps", "-axo", "ucomm="], capture_output=True, text=True).stdout
    return "iTerm2" in (line.strip() for line in out.splitlines())


def run() -> None:
    """Entry point of ``missioncache-iterm``, which the LaunchAgent runs."""
    global holding
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA_DIR, 0o700)  # state.json holds tab names and paths
    try:
        holding = start_holding(PORT, HOLDING_PAGE)
    except OSError as e:
        # launchd starts us again at once. Waiting keeps the log readable
        # while something else holds the port.
        print(f"cannot listen on 127.0.0.1:{PORT}: {e}. Is another panels server running?", flush=True)
        time.sleep(60)
        raise SystemExit(1) from e
    while True:
        while not iterm_running():
            time.sleep(2)
        try:
            # No retry in the library: its retry polls osascript twice a second.
            # A refused connection (iTerm2 is up but its API is not yet) is
            # retried here, with the holding page still serving. Once connected,
            # a closed connection exits the process and launchd starts it again.
            # The module-level iterm2.run_forever exits the process on a
            # refused connection, so the retry below needs the method.
            iterm2.Connection().run_forever(main, False)
        except Exception as e:  # noqa: BLE001 - keep trying while iTerm2 starts
            print(f"connecting to iTerm2 failed, retrying: {e!r}", flush=True)
            if holding is None:
                holding = start_holding(PORT, HOLDING_PAGE)  # it failed after the real server took the port
            time.sleep(5)

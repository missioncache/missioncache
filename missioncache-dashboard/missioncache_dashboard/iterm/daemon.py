"""The daemon behind the iTerm2 panels: it polls iTerm2, serves the two
panels on 127.0.0.1, and registers them and three key-binding functions with
iTerm2.

The resume lists are the ones the dashboard and ``missioncache-db resume``
show: the daemon reads and writes ``resume_sessions`` through the same
functions. It never records a closed session itself, the hooks do that.

The process exits when the iTerm2 connection closes, and launchd starts it
again, which reconnects and registers the panels with the new iTerm2.
"""

import asyncio
import json
import os
import subprocess
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from collections.abc import Callable
from urllib.parse import urlsplit

import iterm2
import websockets

from missioncache_db import (
    MISSIONCACHE_ROOT,
    TaskDB,
    _is_valid_session_id,
    atomic_write_json,
    live_lead_session,
    live_sessions_all,
    resume_sessions,
)

from ..cli import _env_port
from . import logic

PORT = int(os.environ.get("MISSIONCACHE_ITERM_PORT", "8791"))
HOST = f"127.0.0.1:{PORT}"
DASHBOARD_URL = f"http://127.0.0.1:{_env_port()}/"
POLL_SECONDS = 4
CYCLE_TIMEOUT = 30
REQUEST_TIMEOUT = 5
MAX_BODY = 64 * 1024
# Bindings and the lead change over minutes, and finding live sessions checks
# every pid, so MissionCache is asked less often than iTerm2.
MC_POLL_SECONDS = 15
DATA_DIR = MISSIONCACHE_ROOT / "iterm"
STATE_FILE = DATA_DIR / "state.json"
HERE = Path(__file__).parent
ASSETS = HERE.parent / "assets"
PAGES = {"/": HERE / "panel.html", "/saved-page": HERE / "saved.html",
         "/resume-rows.js": ASSETS / "resume-rows.js",
         "/logo-white.png": ASSETS / "missioncache_logo_white.png",
         "/logo-black.png": ASSETS / "missioncache_logo_black.png",
         "/logo-icon.png": ASSETS / "logo_icon.png"}
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".png": "image/png"}
SECURITY_HEADERS = ("X-Frame-Options: DENY\r\n"
                    "Content-Security-Policy: frame-ancestors 'none'\r\n"
                    "X-Content-Type-Options: nosniff\r\n")
TOOLS = (("Recent Sessions", "com.missioncache.recent-sessions", "/"),
         ("Saved Tabs", "com.missioncache.saved-tabs", "/saved-page"))

# iTerm2 session id -> {"status", "changed", "focused", "title", "path",
#                       "name", "project", "claude"}
state: dict[str, dict[str, Any]] = {}
# The latest MissionCache answer: Claude Code session id -> its binding, and
# the title of the live lead.
mc_bound: dict[str, dict[str, Any]] = {}
lead_title: str | None = None
# Claude Code session ids in Saved, for the star on a running session.
saved: set[str] = set()
app_ref: iterm2.App | None = None
# Claude Code session id -> the iTerm2 session of a tab opened to resume it,
# until the poll sees Claude Code running there. Opens run one at a time, so
# a second click on Open goes to that tab instead of resuming twice.
opening: dict[str, str] = {}
open_lock = asyncio.Lock()


class Rejected(Exception):
    """A request that failed the Host or panel-header check."""


class NotFound(Exception):
    """A request naming a session, tab or route that is not there."""


def with_db(fn: Callable[[Any], Any]) -> Any:
    db = TaskDB()
    try:
        with db.connection() as conn:
            return fn(conn)
    finally:
        db.close()


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def write_state() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_json(STATE_FILE, state)


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


async def watch_missioncache() -> None:
    """Keep mc_bound, lead_title and saved current."""
    global mc_bound, lead_title
    while True:
        try:
            live = await asyncio.to_thread(live_sessions_all)
            lead = await asyncio.to_thread(live_lead_session)
            mc_bound = {s["session_id"]: s for s in live}
            lead_title = (lead or {}).get("title")
        except Exception as e:  # noqa: BLE001 - the panels work without the names
            print(f"reading MissionCache sessions failed: {e!r}", flush=True)
        await asyncio.to_thread(refresh_saved)
        await asyncio.sleep(MC_POLL_SECONDS)


def refresh_saved() -> None:
    global saved
    try:
        saved = with_db(resume_sessions.saved_ids)
    except Exception as e:  # noqa: BLE001 - the live list must show without the database
        print(f"reading saved ids failed: {e!r}", flush=True)


async def update_entry(connection: iterm2.Connection, s: iterm2.Session,
                       claude: dict[str, dict[str, Any]], now: float) -> None:
    tty = await s.async_get_variable("tty")
    own = claude.get(tty)
    status = logic.effective_status(await iterm_status(connection, s.session_id), own)
    own_change = own["statusUpdatedAt"] / 1000 if own and own.get("statusUpdatedAt") else None
    if status is None:
        st = os.stat(tty)
        activity = logic.shell_activity(st.st_atime, st.st_mtime, await s.async_get_variable("jobName"))
    else:
        activity = own_change or now
    title = (await s.tab.async_get_variable("title") if s.tab else None) or s.name
    path = await s.async_get_variable("path") or str(Path.home())
    entry = state[s.session_id] = logic.advance(state.get(s.session_id), status, activity, own_change, now)
    bound = mc_bound.get(own["sessionId"], {}) if own else {}
    entry.update(title=title, path=path, name=logic.row_name(title, own, bound.get("title")),
                 project=bound.get("project_name") or "",
                 claude=own["sessionId"] if own else None)


def mark_seen(session_id: str | None) -> None:
    if session_id in state:
        state[session_id]["focused"] = time.time()


def current_session_id(window: iterm2.Window | None) -> str | None:
    tab = window.current_tab if window else None
    session = tab.current_session if tab else None
    return session.session_id if session else None


def focused_id() -> str | None:
    return current_session_id(app_ref.current_terminal_window) if app_ref else None


async def focus(session: iterm2.Session) -> None:
    await session.async_activate()
    mark_seen(session.session_id)


async def poll(connection: iterm2.Connection, app: iterm2.App) -> None:
    written = None
    while True:
        # A call whose answer never comes would hang here forever. Time the
        # cycle out instead, which ends the process and lets launchd restart it.
        async with asyncio.timeout(CYCLE_TIMEOUT):
            sessions = [s for w in app.terminal_windows for t in w.tabs for s in t.sessions]
            claude = await claude_sessions()
            now = time.time()
            results = await asyncio.gather(
                *(update_entry(connection, s, claude, now) for s in sessions), return_exceptions=True)
        for s, result in zip(sessions, results):
            if isinstance(result, websockets.exceptions.ConnectionClosed):
                raise result
            if isinstance(result, Exception):
                # Usually a tab closed during this cycle. The membership check
                # below drops it if the session is gone.
                print(f"update {s.session_id} failed: {result!r}", flush=True)
        for gone in set(state) - {s.session_id for s in sessions}:
            del state[gone]
        # The tab in front is seen, also when it finishes while you watch.
        if app.app_active:
            mark_seen(current_session_id(app.current_terminal_window))
        if (snapshot := json.dumps(state)) != written:
            write_state()
            written = snapshot
        await asyncio.sleep(POLL_SECONDS)


async def watch_focus(connection: iterm2.Connection, app: iterm2.App) -> None:
    async with iterm2.FocusMonitor(connection) as monitor:
        while True:
            update = await monitor.async_get_next_update()
            if update.active_session_changed:
                mark_seen(update.active_session_changed.session_id)
            elif update.selected_tab_changed:
                tab = app.get_tab_by_id(update.selected_tab_changed.tab_id)
                mark_seen(tab.current_session.session_id if tab and tab.current_session else None)
            elif (update.window_changed and update.window_changed.event
                  == iterm2.FocusUpdateWindowChanged.Reason.TERMINAL_WINDOW_BECAME_KEY):
                mark_seen(current_session_id(app.get_window_by_id(update.window_changed.window_id)))


async def watch_connection(connection: iterm2.Connection) -> None:
    # The iterm2 library reads the connection in a task nothing waits on, so a
    # closed connection would otherwise leave this process running with frozen
    # data. Raising here ends main, the library exits, and launchd restarts us.
    await connection.websocket.wait_closed()
    raise ConnectionError("iTerm2 connection closed")


def rows() -> list:
    out = [{"id": sid, **e, "unseen": logic.is_unseen(e), "saved": e.get("claude") in saved,
            "lead": logic.lead_mark(e.get("name"), lead_title)}
           for sid, e in state.items() if "name" in e]
    return logic.order_rows(out)


def iterm_session_for(claude_id: str) -> str | None:
    """The iTerm2 session running this Claude Code session, if any."""
    return next((sid for sid, e in state.items() if e.get("claude") == claude_id), None)


async def resume_lists() -> dict[str, Any]:
    # The poll changes `state` on the event loop, so it is read here, on the
    # loop, and only the database read goes to a thread.
    live = {e["claude"]: sid for sid, e in state.items() if e.get("claude")}
    sessions = await asyncio.to_thread(with_db, lambda conn: resume_sessions.list_sessions(conn, "all"))
    for row in sessions:
        row["live"] = live.get(row["session_id"])
    return {"sessions": sessions}


async def open_row(connection: iterm2.Connection, app: iterm2.App, claude_id: str) -> None:
    """Go to the tab running this session, or open a tab that resumes it."""
    async with open_lock:
        sid = iterm_session_for(claude_id) or opening.get(claude_id)
        if sid and (session := app.get_session_by_id(sid)):
            await focus(session)
            return
        opening.pop(claude_id, None)
        await resume_in_new_tab(connection, app, claude_id)


async def resume_in_new_tab(connection: iterm2.Connection, app: iterm2.App, claude_id: str) -> None:
    row = await asyncio.to_thread(with_db, lambda conn: resume_sessions.get_session(conn, claude_id))
    if row is None:
        raise NotFound(f"no session {claude_id} in the resume lists")
    if row["open"]:
        raise NotFound(f"{row['name']} is open in another terminal")
    if not row["command"]:
        raise NotFound(f"{row['name']} cannot be resumed: its transcript is gone, "
                       "or its folder name has a control character")
    if not os.path.isdir(row["cwd"]):
        raise NotFound(f"directory {row['cwd']} does not exist")
    window = app.current_terminal_window
    if window:
        tab = await window.async_create_tab()
    else:
        window = await iterm2.Window.async_create(connection)
        tab = window.current_tab if window else None
    if not tab or not tab.current_session:
        raise NotFound("iTerm2 did not create the tab")
    opening[claude_id] = tab.current_session.session_id
    await tab.async_set_title(row["name"])
    # The command is derived from the row and quoted, and resume_sessions
    # gives none for a folder name with a control character, so typing it
    # runs exactly that line.
    await tab.current_session.async_send_text(row["command"] + "\n")
    await tab.async_activate(order_window_front=True)


def act_on_row(claude_id: str, action: str, data: dict[str, Any]) -> None:
    actions = {
        "keep": resume_sessions.keep,
        "unsave": resume_sessions.unsave,
        "forget": resume_sessions.forget,
        "pin": lambda conn, sid: resume_sessions.set_pinned(conn, sid, True),
        "unpin": lambda conn, sid: resume_sessions.set_pinned(conn, sid, False),
        "rename": lambda conn, sid: resume_sessions.rename(conn, sid, str(data.get("name") or "")),
    }
    if action not in actions:
        raise NotFound(f"unknown action {action}")
    if not with_db(lambda conn: actions[action](conn, claude_id)):
        raise NotFound(f"no session {claude_id} in the resume lists")
    refresh_saved()


def open_in_browser(fragment: str) -> None:
    # A fixed address, never one from the request.
    subprocess.run(["open", DASHBOARD_URL + fragment], check=False)


async def handle_post(connection: iterm2.Connection, app: iterm2.App,
                      path: str, raw: bytes) -> tuple:
    parts = path.strip("/").split("/")
    data = json.loads(raw or b"{}")
    if not isinstance(data, dict):
        raise ValueError("body must be a JSON object")
    if parts[:1] == ["focus"] and len(parts) == 2:
        if not (session := app.get_session_by_id(parts[1])):
            raise NotFound(f"no live session {parts[1]}")
        await focus(session)
    elif parts[:1] == ["live"] and len(parts) == 3 and parts[2] in ("save", "unsave"):
        # The star on a running session. It names the iTerm2 session, and the
        # row is the Claude Code session running in it.
        claude_id = (state.get(parts[1]) or {}).get("claude")
        if not claude_id:
            raise NotFound("no Claude Code session in that tab")
        await asyncio.to_thread(act_on_row, claude_id, "keep" if parts[2] == "save" else "unsave", {})
    elif parts[:1] == ["resume"] and len(parts) == 3:
        if not _is_valid_session_id(parts[1]):
            raise NotFound("not a session id")
        if parts[2] == "open":
            await open_row(connection, app, parts[1])
        else:
            await asyncio.to_thread(act_on_row, parts[1], parts[2], data)
    elif parts in (["open", "dashboard"], ["open", "lead"]):
        await asyncio.to_thread(open_in_browser, "#lead" if parts[1] == "lead" else "")
    else:
        raise NotFound(f"no route {path}")
    return b"{}", "application/json"


async def handle_get(path: str) -> tuple:
    if path in PAGES:
        return await asyncio.to_thread(PAGES[path].read_bytes), CONTENT_TYPES[PAGES[path].suffix]
    if path == "/resume":
        return json.dumps(await resume_lists()).encode(), "application/json"
    # These read only what the event loop keeps, so they run on it.
    routes = {
        "/state": rows,
        "/lead": lambda: {"live": bool(lead_title)},
        # Polled several times a second by the panel, so it does no work
        # beyond a lookup in the app object, which iTerm2 keeps current.
        "/current": focused_id,
    }
    if path not in routes:
        raise FileNotFoundError(path)
    return json.dumps(routes[path]()).encode(), "application/json"


async def read_request(reader: asyncio.StreamReader) -> tuple:
    method, target, *_ = (await reader.readline()).decode().split() + ["", ""]
    headers = {}
    while (line := await reader.readline()) not in (b"\r\n", b"\n", b""):
        key, _, value = line.decode().partition(":")
        headers[key.strip().lower()] = value.strip()
    return method, target, headers


async def serve(connection: iterm2.Connection, app: iterm2.App) -> asyncio.Server:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        method = target = ""
        raw = b""
        status, body, ctype = "200 OK", b"", "text/plain"
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                method, target, headers = await read_request(reader)
                # Any web page can reach this port. Requiring this exact Host
                # defeats DNS rebinding, which would otherwise let a page read
                # tab names, paths and resume ids.
                if headers.get("host") != HOST:
                    raise Rejected("bad host")
                if method == "POST":
                    # Open types a command into a terminal, so writes also need
                    # the panel's own header: a cross-site page cannot send a
                    # custom header without a CORS preflight. This server never
                    # answers one, and must never send
                    # Access-Control-Allow-Origin. Both checks run before the
                    # body is read, and the body has a size limit.
                    if headers.get("x-panel") != "1":
                        raise Rejected("not from the panel")
                    length = int(headers.get("content-length") or 0)
                    if not 0 <= length <= MAX_BODY:
                        raise ValueError(f"body of {length} bytes")
                    raw = await reader.readexactly(length)
            url = urlsplit(target)
            if method == "POST":
                body, ctype = await handle_post(connection, app, url.path, raw)
            elif method == "GET":
                body, ctype = await handle_get(url.path)
            else:
                status = "405 Method Not Allowed"
        except Rejected as e:
            status, body = "403 Forbidden", str(e).encode()
        except FileNotFoundError:
            status = "404 Not Found"
        except (NotFound, ValueError, asyncio.IncompleteReadError, TimeoutError) as e:
            print(f"{method} {target} failed: {e!r}", flush=True)
            status, body = "400 Bad Request", str(e).encode()
        except Exception as e:  # noqa: BLE001 - a request must always get an answer
            traceback.print_exc()
            status, body = "500 Internal Server Error", repr(e).encode()
        try:
            writer.write(f"HTTP/1.1 {status}\r\nContent-Type: {ctype}\r\n{SECURITY_HEADERS}"
                         f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
            await writer.drain()
        except ConnectionError:
            pass  # the client went away
        finally:
            writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", PORT)


@iterm2.RPC
async def jump_to_unseen() -> None:
    """Go to the newest session that finished, needs input or failed since
    it was last seen. Bind a key to it with Invoke Script Function:
    jump_to_unseen()"""
    unseen = [r for r in rows() if r["unseen"]]
    newest = max(unseen, key=lambda r: r["changed"], default=None)
    if newest and app_ref and (session := app_ref.get_session_by_id(newest["id"])):
        await focus(session)


async def step(offset: int) -> None:
    target = logic.step_target([r["id"] for r in rows()], focused_id(), offset)
    if target and app_ref and (session := app_ref.get_session_by_id(target)):
        await focus(session)


@iterm2.RPC
async def session_next() -> None:
    """The session below the current one in Recent Sessions. Bind a key to it
    with Invoke Script Function: session_next()"""
    await step(1)


@iterm2.RPC
async def session_prev() -> None:
    """The session above the current one in Recent Sessions. Bind a key to it
    with Invoke Script Function: session_prev()"""
    await step(-1)


async def main(connection: iterm2.Connection) -> None:
    state.update(read_json(STATE_FILE, {}))
    global app_ref
    app = app_ref = await iterm2.async_get_app(connection)
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
    stop_holding()
    async with await serve(connection, app) as server:
        for name, identifier, path in TOOLS:
            await iterm2.tool.async_register_web_view_tool(
                connection, name, identifier, False, f"http://{HOST}{path}")
        await asyncio.gather(poll(connection, app), watch_focus(connection, app), watch_missioncache(),
                             watch_connection(connection), server.serve_forever())


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


class Holding(BaseHTTPRequestHandler):
    """Answers while iTerm2 is not up yet, so a panel that iTerm2 loads at
    startup gets a page that reloads itself, not a blank failed load."""

    def do_GET(self) -> None:
        ok = self.headers.get("host") == HOST and self.path in ("/", "/saved-page")
        self.send_response(200 if ok else 503)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        for line in SECURITY_HEADERS.strip().split("\r\n"):
            key, _, value = line.partition(": ")
            self.send_header(key, value)
        self.end_headers()
        if ok:
            self.wfile.write(HOLDING_PAGE)

    def log_message(self, *args: Any) -> None:
        pass


holding: ThreadingHTTPServer | None = None


def start_holding() -> None:
    """Serve the holding page on the panels' port from the moment the daemon
    starts until the real server takes over."""
    global holding
    holding = ThreadingHTTPServer(("127.0.0.1", PORT), Holding)
    threading.Thread(target=holding.serve_forever, daemon=True).start()


def stop_holding() -> None:
    global holding
    if holding:
        holding.shutdown()
        holding.server_close()
        holding = None


def iterm_running() -> bool:
    # ps, not pgrep: pgrep missed a running iTerm2 when started from inside
    # another app's process tree, while ps listed it.
    out = subprocess.run(["ps", "-axo", "ucomm="], capture_output=True, text=True).stdout
    return "iTerm2" in (line.strip() for line in out.splitlines())


def run() -> None:
    """Entry point of ``missioncache-iterm``, which the LaunchAgent runs."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA_DIR, 0o700)  # state.json holds tab names and paths
    try:
        start_holding()
    except OSError as e:
        # launchd starts us again at once. Waiting keeps the log readable
        # while something else holds the port.
        print(f"cannot listen on {HOST}: {e}. Is another panels server running?", flush=True)
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
                start_holding()  # it failed after the real server took the port
            time.sleep(5)

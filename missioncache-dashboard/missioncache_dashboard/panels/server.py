"""The part of the panels' daemon that is the same for every terminal: the
HTTP server and its guards, the resume lists, the names MissionCache gives
live sessions, the row state and its file, and the holding page.

What depends on the terminal (listing its tabs, going to one, opening one
and knowing which one is in front) sits behind ``Terminal``, which each
terminal's daemon implements.

The resume lists are the ones the dashboard and ``missioncache-db resume``
show: the daemon reads and writes ``resume_sessions`` through the same
functions. It never records a closed session itself, the hooks do that.
"""

import asyncio
import json
import os
import subprocess
import threading
import time
import traceback
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from missioncache_db import (
    TaskDB,
    _is_valid_session_id,
    atomic_write_json,
    live_lead_session,
    live_sessions_all,
    resume_sessions,
)

from ..cli import _env_port
from . import logic

DASHBOARD_URL = f"http://127.0.0.1:{_env_port()}/"
REQUEST_TIMEOUT = 5
MAX_BODY = 64 * 1024
# Bindings and the lead change over minutes, and finding live sessions checks
# every pid, so MissionCache is asked less often than the terminal.
MC_POLL_SECONDS = 15
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


class Rejected(Exception):
    """A request that failed the Host or panel-header check."""


class NotFound(Exception):
    """A request naming a session, tab or route that is not there."""


class Terminal(Protocol):
    """What the panels need from a terminal. Row ids are the terminal's own
    ids for its sessions, the keys of ``Panels.state``. ``exists`` and
    ``current`` run on the event loop for every panel poll, so they answer
    from what the terminal already keeps, with no I/O."""

    def exists(self, row_id: str) -> bool:
        """Whether that session is still open."""
        ...

    async def focus(self, row_id: str) -> None:
        """Bring that session to the front. NotFound when it is gone."""
        ...

    async def open_tab(self, row: dict[str, Any]) -> str:
        """Open a tab named after a resume row that runs its command, and
        return the new tab's row id. NotFound when the terminal could not."""
        ...

    def current(self) -> str | None:
        """The row id of the session in front, if any."""
        ...


def with_db(fn: Callable[[Any], Any]) -> Any:
    db = TaskDB()
    try:
        with db.connection() as conn:
            return fn(conn)
    finally:
        db.close()


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def open_in_browser(fragment: str) -> None:
    # A fixed address, never one from the request.
    subprocess.run(["open", DASHBOARD_URL + fragment], check=False)


class Panels:
    def __init__(self, terminal: Terminal, data_dir: Path, port: int) -> None:
        self.terminal = terminal
        self.data_dir = data_dir
        self.state_file = data_dir / "state.json"
        self.port = port
        # Row id -> {"status", "changed", "focused", "title", "path", "name",
        #            "project", "claude"}
        self.state: dict[str, dict[str, Any]] = {}
        # The latest MissionCache answer: Claude Code session id -> its
        # binding, and the title of the live lead.
        self.mc_bound: dict[str, dict[str, Any]] = {}
        self.lead_title: str | None = None
        # Claude Code session ids in Saved, for the star on a running session.
        self.saved: set[str] = set()
        # Claude Code session id -> the row of a tab opened to resume it, until
        # the poll sees Claude Code running there. Opens run one at a time, so
        # a second click on Open goes to that tab instead of resuming twice.
        self.opening: dict[str, str] = {}
        self.open_lock = asyncio.Lock()

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.port}"

    def load_state(self) -> None:
        self.state.update(read_json(self.state_file, {}))

    def write_state(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.state_file, self.state)

    async def watch_missioncache(self) -> None:
        """Keep mc_bound, lead_title and saved current."""
        while True:
            try:
                live = await asyncio.to_thread(live_sessions_all)
                lead = await asyncio.to_thread(live_lead_session)
                self.mc_bound = {s["session_id"]: s for s in live}
                self.lead_title = (lead or {}).get("title")
            except Exception as e:  # noqa: BLE001 - the panels work without the names
                print(f"reading MissionCache sessions failed: {e!r}", flush=True)
            await asyncio.to_thread(self.refresh_saved)
            await asyncio.sleep(MC_POLL_SECONDS)

    def refresh_saved(self) -> None:
        try:
            self.saved = with_db(resume_sessions.saved_ids)
        except Exception as e:  # noqa: BLE001 - the live list must show without the database
            print(f"reading saved ids failed: {e!r}", flush=True)

    def end_cycle(self, live: set[str], front: str | None, written: str | None) -> str:
        """After a poll: drop the rows whose session is gone, mark the one in
        front as seen (also when it finishes while you watch), and write the
        state file when it changed. Returns what was written, for the next
        call."""
        for gone in set(self.state) - live:
            del self.state[gone]
        self.mark_seen(front)
        if (snapshot := json.dumps(self.state)) != written:
            self.write_state()
        return snapshot

    def mark_seen(self, row_id: str | None) -> None:
        if row_id in self.state:
            self.state[row_id]["focused"] = time.time()

    async def focus(self, row_id: str) -> None:
        await self.terminal.focus(row_id)
        self.mark_seen(row_id)

    def rows(self) -> list:
        out = [{"id": rid, **e, "unseen": logic.is_unseen(e), "saved": e.get("claude") in self.saved,
                "lead": logic.lead_mark(e.get("name"), self.lead_title)}
               for rid, e in self.state.items() if "name" in e]
        return logic.order_rows(out)

    async def jump_to_unseen(self) -> None:
        """Go to the newest session that finished, needs input or failed
        since it was last seen."""
        unseen = [r for r in self.rows() if r["unseen"]]
        if newest := max(unseen, key=lambda r: r["changed"], default=None):
            await self.focus(newest["id"])

    async def step(self, offset: int) -> None:
        """Go to the session `offset` rows away in Recent Sessions."""
        if target := logic.step_target([r["id"] for r in self.rows()], self.terminal.current(), offset):
            await self.focus(target)

    def row_for(self, claude_id: str) -> str | None:
        """The row running this Claude Code session, if any."""
        return next((rid for rid, e in self.state.items() if e.get("claude") == claude_id), None)

    async def resume_lists(self) -> dict[str, Any]:
        # The poll changes `state` on the event loop, so it is read here, on
        # the loop, and only the database read goes to a thread.
        live = {e["claude"]: rid for rid, e in self.state.items() if e.get("claude")}
        sessions = await asyncio.to_thread(with_db, lambda conn: resume_sessions.list_sessions(conn, "all"))
        for row in sessions:
            row["live"] = live.get(row["session_id"])
        return {"sessions": sessions}

    async def open_row(self, claude_id: str) -> None:
        """Go to the tab running this session, or open a tab that resumes it."""
        async with self.open_lock:
            rid = self.row_for(claude_id) or self.opening.get(claude_id)
            if rid and self.terminal.exists(rid):
                await self.focus(rid)
                return
            self.opening.pop(claude_id, None)
            await self.resume_in_new_tab(claude_id)

    async def resume_in_new_tab(self, claude_id: str) -> None:
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
        self.opening[claude_id] = await self.terminal.open_tab(row)

    def act_on_row(self, claude_id: str, action: str, data: dict[str, Any]) -> None:
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
        self.refresh_saved()

    async def handle_post(self, path: str, raw: bytes) -> tuple:
        parts = path.strip("/").split("/")
        data = json.loads(raw or b"{}")
        if not isinstance(data, dict):
            raise ValueError("body must be a JSON object")
        if parts[:1] == ["focus"] and len(parts) == 2:
            await self.focus(parts[1])
        elif parts[:1] == ["live"] and len(parts) == 3 and parts[2] in ("save", "unsave"):
            # The star on a running session. It names the terminal's session,
            # and the row is the Claude Code session running in it.
            claude_id = (self.state.get(parts[1]) or {}).get("claude")
            if not claude_id:
                raise NotFound("no Claude Code session in that tab")
            await asyncio.to_thread(self.act_on_row, claude_id, "keep" if parts[2] == "save" else "unsave", {})
        elif parts[:1] == ["resume"] and len(parts) == 3:
            if not _is_valid_session_id(parts[1]):
                raise NotFound("not a session id")
            if parts[2] == "open":
                await self.open_row(parts[1])
            else:
                await asyncio.to_thread(self.act_on_row, parts[1], parts[2], data)
        elif parts in (["open", "dashboard"], ["open", "lead"]):
            await asyncio.to_thread(open_in_browser, "#lead" if parts[1] == "lead" else "")
        else:
            raise NotFound(f"no route {path}")
        return b"{}", "application/json"

    async def handle_get(self, path: str) -> tuple:
        if path in PAGES:
            return await asyncio.to_thread(PAGES[path].read_bytes), CONTENT_TYPES[PAGES[path].suffix]
        if path == "/resume":
            return json.dumps(await self.resume_lists()).encode(), "application/json"
        # These read only what the event loop keeps, so they run on it.
        routes = {
            "/state": self.rows,
            "/lead": lambda: {"live": bool(self.lead_title)},
            # Polled several times a second by the panel, so it must stay a
            # lookup in what the terminal already keeps.
            "/current": self.terminal.current,
        }
        if path not in routes:
            raise FileNotFoundError(path)
        return json.dumps(routes[path]()).encode(), "application/json"

    async def serve(self) -> asyncio.Server:
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            method = target = ""
            raw = b""
            status, body, ctype = "200 OK", b"", "text/plain"
            try:
                async with asyncio.timeout(REQUEST_TIMEOUT):
                    method, target, headers = await read_request(reader)
                    # Any web page can reach this port. Requiring this exact
                    # Host defeats DNS rebinding, which would otherwise let a
                    # page read tab names, paths and resume ids.
                    if headers.get("host") != self.host:
                        raise Rejected("bad host")
                    if method == "POST":
                        # Open types a command into a terminal, so writes also
                        # need the panel's own header: a cross-site page cannot
                        # send a custom header without a CORS preflight. This
                        # server never answers one, and must never send
                        # Access-Control-Allow-Origin. Both checks run before
                        # the body is read, and the body has a size limit.
                        if headers.get("x-panel") != "1":
                            raise Rejected("not from the panel")
                        length = int(headers.get("content-length") or 0)
                        if not 0 <= length <= MAX_BODY:
                            raise ValueError(f"body of {length} bytes")
                        raw = await reader.readexactly(length)
                url = urlsplit(target)
                if method == "POST":
                    body, ctype = await self.handle_post(url.path, raw)
                elif method == "GET":
                    body, ctype = await self.handle_get(url.path)
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

        return await asyncio.start_server(handle, "127.0.0.1", self.port)


async def read_request(reader: asyncio.StreamReader) -> tuple:
    method, target, *_ = (await reader.readline()).decode().split() + ["", ""]
    headers = {}
    while (line := await reader.readline()) not in (b"\r\n", b"\n", b""):
        key, _, value = line.decode().partition(":")
        headers[key.strip().lower()] = value.strip()
    return method, target, headers


class _HoldingHandler(BaseHTTPRequestHandler):
    server: "_HoldingServer"

    def do_GET(self) -> None:
        ok = self.headers.get("host") == f"127.0.0.1:{self.server.server_port}" and self.path in ("/", "/saved-page")
        self.send_response(200 if ok else 503)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        for line in SECURITY_HEADERS.strip().split("\r\n"):
            key, _, value = line.partition(": ")
            self.send_header(key, value)
        self.end_headers()
        if ok:
            self.wfile.write(self.server.page)

    def log_message(self, *args: Any) -> None:
        pass


class _HoldingServer(ThreadingHTTPServer):
    page = b""


def start_holding(port: int, page: bytes) -> ThreadingHTTPServer:
    """Answer on the panels' port while the terminal is not up yet, so a
    panel loaded at that moment gets a page that reloads itself, not a blank
    failed load."""
    server = _HoldingServer(("127.0.0.1", port), _HoldingHandler)
    server.page = page
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def stop_holding(server: ThreadingHTTPServer) -> None:
    server.shutdown()
    server.server_close()

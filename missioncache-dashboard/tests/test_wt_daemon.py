"""The Windows Terminal panels' daemon, against a fake Windows Terminal.

Spec: the phase 3 plan. A tab is joined to its Claude Code session by title,
its status comes from the mod's agent_status row, and a plain shell's only
activity is a new title. Open runs wt.exe with the row's command, escaped for
wt.exe and without this process's Claude Code variables, and a second Open
goes to the tab the first one made. The panels may be framed by the daemon's
own page, by no other.
"""

import asyncio
import json
import os
import pathlib

import missioncache_db
import pytest
from missioncache_db import resume_sessions
from missioncache_db.lead_board import report_agent_status

from missioncache_dashboard.panels import server
from missioncache_dashboard.wt import daemon

SID = "aaaaaaaa-1111-4111-8111-111111111111"
SHELL = "100-1.2"
CLAUDE_TAB = "100-1.3"


class FakeUi:
    def __init__(self) -> None:
        self.tab_list: list[tuple[str, str]] = []
        self.empty: set[str] = set()
        self.focused: list[str] = []

    async def tabs(self):
        return list(self.tab_list), set(self.empty)

    async def front_row(self, hint):
        return None

    async def focus(self, row_id):
        self.focused.append(row_id)

    async def family(self):
        return None


@pytest.fixture
def sandboxed(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude" / "sessions").mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(missioncache_db, "MISSIONCACHE_ROOT", tmp_path / ".missioncache")
    monkeypatch.setattr(missioncache_db, "DB_PATH", tmp_path / "tasks.db")
    monkeypatch.setattr(missioncache_db, "HOOKS_STATE_DB_PATH", home / ".claude" / "hooks-state.db")
    monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(resume_sessions, "WINDOWS", True)
    ui = FakeUi()
    t = daemon.WindowsTerminal(ui)
    panels = server.Panels(t, tmp_path / "wt", 0, pages={"/panels": daemon.PANELS_PAGE}, framed=True)
    return {"home": home, "repo": repo, "ui": ui, "t": t, "panels": panels}


def _claude(home: pathlib.Path, name: str = "brave-otter", status: str = "idle") -> None:
    # This test's own pid, so the record counts as a running process.
    record = {"pid": os.getpid(), "sessionId": SID, "name": name, "cwd": "C:\\work\\repo",
              "kind": "interactive", "status": status, "statusUpdatedAt": 1_000}
    (home / ".claude" / "sessions" / f"{os.getpid()}.json").write_text(json.dumps(record))


def _poll(s) -> list[str]:
    _, unjoined = asyncio.run(daemon.poll_once(s["panels"], s["t"], daemon.TranscriptTitles(), None, daemon.ErrorLog()))
    return unjoined


def test_a_tab_joins_its_session_and_takes_the_mods_status(sandboxed):
    _claude(sandboxed["home"])
    report_agent_status(SID, "needs_you", needs_reason="question")
    sandboxed["ui"].tab_list = [(SHELL, "PowerShell"), (CLAUDE_TAB, "✳ brave-otter")]
    assert _poll(sandboxed) == []
    entry = sandboxed["panels"].state[CLAUDE_TAB]
    assert (entry["claude"], entry["status"], entry["name"]) == (SID, "waiting", "brave-otter")
    assert sandboxed["panels"].state[SHELL]["claude"] is None
    assert sandboxed["t"].exists(CLAUDE_TAB)


def test_a_session_with_no_tab_is_reported(sandboxed):
    _claude(sandboxed["home"])
    sandboxed["ui"].tab_list = [(SHELL, "PowerShell"), ("100-1.4", "✳ one"), ("100-1.5", "✳ two")]
    assert _poll(sandboxed) == ["brave-otter"]


def test_a_plain_shells_activity_is_a_new_title(sandboxed):
    ui, panels = sandboxed["ui"], sandboxed["panels"]
    ui.tab_list = [(SHELL, "PowerShell")]
    _poll(sandboxed)
    panels.state[SHELL]["changed"] = 5
    _poll(sandboxed)
    assert panels.state[SHELL]["changed"] == 5
    ui.tab_list = [(SHELL, "pytest")]
    _poll(sandboxed)
    assert panels.state[SHELL]["changed"] > 5


def test_a_window_showing_no_tabs_keeps_its_rows_and_a_closed_tab_goes(sandboxed):
    ui, panels = sandboxed["ui"], sandboxed["panels"]
    ui.tab_list = [(SHELL, "PowerShell"), ("200-1.2", "PowerShell")]
    _poll(sandboxed)
    ui.tab_list, ui.empty = [("200-1.2", "PowerShell")], {"100"}
    _poll(sandboxed)
    assert SHELL in panels.state
    ui.empty = set()
    _poll(sandboxed)
    assert SHELL not in panels.state


def test_a_tab_coming_to_the_front_is_marked_seen(sandboxed):
    panels, t = sandboxed["panels"], sandboxed["t"]
    panels.state[SHELL] = {"status": "done", "changed": 10, "focused": 20}  # already seen
    daemon.note_front(panels, t, SHELL)
    assert panels.state[SHELL]["focused"] > 20
    daemon.note_front(panels, t, None)  # another window in front
    assert t.current() == SHELL


def test_the_tab_in_front_is_marked_seen_when_it_finishes_and_only_then(sandboxed):
    panels, t = sandboxed["panels"], sandboxed["t"]
    panels.state[SHELL] = {"status": "done", "changed": 10, "focused": 20}
    t.front = SHELL
    daemon.note_front(panels, t, SHELL)
    assert panels.state[SHELL]["focused"] == 20  # nothing new: no write
    panels.state[SHELL]["changed"] = 30  # it finished while you watched
    daemon.note_front(panels, t, SHELL)
    assert panels.state[SHELL]["focused"] > 30


def test_the_open_command_line():
    row = {"name": "fix; login", "cwd": "C:\\a;b", "command": "Set-Location -LiteralPath 'C:\\a;b'; claude --resume x"}
    assert daemon.open_args("wt.exe", row, "ps.exe") == [
        "wt.exe", "-w", "0", "new-tab", "--title", "fix\\; login", "-d", "C:\\a\\;b",
        "ps.exe", "-NoExit", "-Command", "Set-Location -LiteralPath 'C:\\a\\;b'\\; claude --resume x"]


def test_powershell_runs_by_its_full_path():
    assert daemon.powershell_exe("D:\\Win").replace("\\", "/") == "D:/Win/System32/WindowsPowerShell/v1.0/powershell.exe"


def test_a_new_tab_gets_no_claude_code_variables():
    env = {"PATH": "p", "CLAUDECODE": "1", "CLAUDE_PID": "7", "CLAUDE_CODE_SESSION_ID": "s", "CLAUDE_CONFIG": "c"}
    assert daemon.child_env(env) == {"PATH": "p", "CLAUDE_CONFIG": "c"}


def test_open_uses_the_wt_exe_of_the_terminal_used_last(tmp_path):
    apps = tmp_path / "Microsoft" / "WindowsApps"
    for family in ("Microsoft.WindowsTerminal_8wekyb3d8bbwe", "Microsoft.WindowsTerminalPreview_8wekyb3d8bbwe"):
        (apps / family).mkdir(parents=True)
        (apps / family / "wt.exe").write_text("")
    preview = "Microsoft.WindowsTerminalPreview_8wekyb3d8bbwe"
    assert daemon.pick_wt_exe(None, preview, str(tmp_path)) == str(apps / preview / "wt.exe")
    assert "WindowsTerminal_" in daemon.pick_wt_exe(None, None, str(tmp_path))
    assert daemon.pick_wt_exe("D:\\wt.exe", preview, str(tmp_path)) == "D:\\wt.exe"
    assert daemon.pick_wt_exe(None, None, str(tmp_path / "none")) == "wt.exe"


def _closed_session(repo: pathlib.Path) -> None:
    conn = resume_sessions.hook_connect()
    resume_sessions.record_seen(conn, SID, str(repo), None)
    resume_sessions.close_session(conn, SID, "other")
    conn.close()


def _fake_wt(monkeypatch, ui, *, code=0, new_tab=("100-1.9", "repo"), runs=None):
    """wt.exe that exits with `code` and, when it succeeds, adds `new_tab`."""
    monkeypatch.setattr(daemon, "FOCUS_SECONDS", 0.01)

    class Proc:
        async def wait(self):
            if not code and new_tab:
                ui.tab_list.append(new_tab)
            return code

    async def exec_(*args, env, **kwargs):
        if runs is not None:
            runs.append((args, env))
        return Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_)


def test_two_opens_run_wt_once_and_the_second_goes_to_its_tab(sandboxed, monkeypatch):
    _closed_session(sandboxed["repo"])
    ui, runs = sandboxed["ui"], []
    _fake_wt(monkeypatch, ui, runs=runs)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "outer")

    async def both():
        panels = sandboxed["panels"]
        await asyncio.gather(panels.open_row(SID), panels.open_row(SID))

    asyncio.run(both())
    [(args, env)] = runs
    assert args[-1].endswith(f"\\; claude --resume {SID}")
    assert "CLAUDE_CODE_SESSION_ID" not in env
    assert ui.focused == ["100-1.9"]


def test_a_poll_just_before_the_tab_shows_does_not_drop_it(sandboxed, monkeypatch):
    _closed_session(sandboxed["repo"])
    _fake_wt(monkeypatch, sandboxed["ui"])
    panels, t = sandboxed["panels"], sandboxed["t"]
    asyncio.run(panels.open_row(SID))
    t.see_tabs(set())  # a poll that read the tabs before the new one
    assert t.exists("100-1.9")
    asyncio.run(panels.open_row(SID))
    assert sandboxed["ui"].focused == ["100-1.9"]


def test_open_fails_when_no_tab_shows_up_and_a_second_open_waits(sandboxed, monkeypatch):
    _closed_session(sandboxed["repo"])
    runs = []
    monkeypatch.setattr(daemon, "OPEN_WAIT_SECONDS", 0.05)
    _fake_wt(monkeypatch, sandboxed["ui"], new_tab=None, runs=runs)
    with pytest.raises(server.NotFound, match="did not show"):
        asyncio.run(sandboxed["panels"].open_row(SID))
    # The tab can still show up later, and a second resume would run the
    # transcript twice.
    with pytest.raises(server.NotFound, match="opened a moment ago"):
        asyncio.run(sandboxed["panels"].open_row(SID))
    assert len(runs) == 1


def test_a_wt_exe_that_opened_nothing_lets_open_try_again(sandboxed, monkeypatch):
    _closed_session(sandboxed["repo"])
    runs = []
    _fake_wt(monkeypatch, sandboxed["ui"], code=1, runs=runs)
    for _ in range(2):
        with pytest.raises(server.NotOpened, match="exited 1"):
            asyncio.run(sandboxed["panels"].open_row(SID))
    assert len(runs) == 2


def test_a_missing_wt_exe_is_reported(sandboxed, monkeypatch):
    _closed_session(sandboxed["repo"])

    async def exec_(*args, **kwargs):
        raise FileNotFoundError(2, "No such file", args[0])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_)
    monkeypatch.setenv(daemon.WT_EXE_ENV, "D:\\nowhere\\wt.exe")
    head, body = _request(sandboxed["panels"], "POST", f"/resume/{SID}/open")
    assert head.startswith("HTTP/1.1 400") and b"cannot run D:\\nowhere\\wt.exe" in body


def test_open_through_the_server_outlasts_the_request_timeout(sandboxed, monkeypatch):
    _closed_session(sandboxed["repo"])
    ui = sandboxed["ui"]
    monkeypatch.setattr(server, "REQUEST_TIMEOUT", 0.05)
    monkeypatch.setattr(daemon, "FOCUS_SECONDS", 0.01)

    class Proc:
        async def wait(self):
            await asyncio.sleep(0.2)  # a Terminal starting up
            ui.tab_list.append(("100-1.9", "repo"))
            return 0

    async def exec_(*args, **kwargs):
        return Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_)
    head, _ = _request(sandboxed["panels"], "POST", f"/resume/{SID}/open")
    assert head.startswith("HTTP/1.1 200")


def test_an_unknown_path_is_404_and_a_failure_is_not(sandboxed, monkeypatch):
    head, _ = _get(sandboxed["panels"], "/nowhere")
    assert head.startswith("HTTP/1.1 404")
    monkeypatch.setitem(sandboxed["panels"].pages, "/panels", pathlib.Path("/no/such/panels.html"))
    head, _ = _get(sandboxed["panels"], "/panels")
    assert head.startswith("HTTP/1.1 500")


def test_a_failure_every_round_is_logged_once(capsys):
    errors = daemon.ErrorLog()
    for _ in range(3):
        errors.report("reading the tabs", TimeoutError())
    errors.clear("reading the tabs")
    errors.clear("reading the tabs")
    out = capsys.readouterr()
    assert out.out.count("reading the tabs failed") == 1
    assert out.err.count("Traceback") <= 1
    assert out.out.count("works again after 3 failed rounds") == 1


def _request(panels: server.Panels, method: str, path: str) -> tuple[str, bytes]:
    async def go():
        listener = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        panels.port = listener.sockets[0].getsockname()[1]
        listener.close()
        await listener.wait_closed()
        srv = await panels.serve()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", panels.port)
            extra = "X-Panel: 1\r\nContent-Length: 0\r\n" if method == "POST" else ""
            writer.write(f"{method} {path} HTTP/1.1\r\nHost: {panels.host}\r\n{extra}\r\n".encode())
            await writer.drain()
            response = await reader.read()
            writer.close()
        finally:
            srv.close()
            await srv.wait_closed()
        return response

    head, _, body = asyncio.run(go()).partition(b"\r\n\r\n")
    return head.decode(), body


def _get(panels: server.Panels, path: str) -> tuple[str, bytes]:
    return _request(panels, "GET", path)


def test_the_panels_page_frames_both_panels_from_this_origin_only(sandboxed):
    head, body = _get(sandboxed["panels"], "/panels")
    assert b'src="/saved-page?zoom=' in body
    head, _ = _get(sandboxed["panels"], "/saved-page")  # a framed page
    assert "X-Frame-Options: SAMEORIGIN" in head
    assert "frame-ancestors 'self'" in head

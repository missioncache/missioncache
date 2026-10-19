"""The lead board's data layer: session state and the per-project board.

Spec source: the data section of the lead-board plan
(~/.claude/plans/replicated-splashing-crown.md, "התכנון: שני כלי MCP חדשים",
decided with Tomer 2026-10-17/18). The contract:

* A session reports its state only when it changes, and ``state_since``
  moves only on a change, so the board can say how long one has needed you.
* Free text a session reports is one clean line, clipped server-side.
* The board's unit is the project. It shows projects with a live session or
  a change in the last 12 hours, and lists the rest as quiet (Tomer's call,
  2026-10-18: a risk flag alone brought in 17 of 18 projects);
  ``scope="all"`` shows every active project.
* A project's session carries that session's reported state.
* The lead's own session is never a project's session, and its writes stay
  out of the feed.
* An unchanged watermark answers ``changed: False`` and builds nothing; a
  session-state write moves the watermark, even within the same second.
* Projects that need you sort first, and every session that needs you is
  listed, including a second session on one project.
* The feed cursor never skips an event.
* No string the board returns carries a control or format character.
* Only the lead may jump to a tab (checked in the MCP tool tests).
"""

import json
import sqlite3
from datetime import date

import missioncache_db
import pytest
from missioncache_db import events, lead_board, pm_items

TODAY = date.today()

CONTEXT = """# {name} - Context

**Last Updated:** 2026-10-18 09:15

## Description

x

## Waiting on

| What | Who | Since | Gates |
|------|-----|-------|-------|
{rows}

## Next Steps

1. TBD

## Recent Changes

### 2026-10-18 09:15

- wired the retry path into the client
"""

TASKS = """# {name} - Tasks

- [x] 1. Write the parser
- [ ] 2. Add the retry path
- [ ] 3. Run the e2e suite
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / ".missioncache"
    root.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(missioncache_db, "MISSIONCACHE_ROOT", root)
    monkeypatch.setattr(missioncache_db, "DB_PATH", tmp_path / "tasks.db")
    monkeypatch.setattr(missioncache_db, "HOOKS_STATE_DB_PATH", home / ".claude" / "hooks-state.db")
    monkeypatch.setattr(missioncache_db.Path, "home", staticmethod(lambda: home))
    (home / ".claude").mkdir()
    live: list[dict] = []
    monkeypatch.setattr(missioncache_db, "live_sessions_all", lambda exclude_session_id=None: [
        s for s in live if s["session_id"] != exclude_session_id])
    monkeypatch.setattr(missioncache_db, "live_sessions_for_project", lambda project_name, exclude_session_id=None: [
        s for s in live if s["project_name"] == project_name and s["session_id"] != exclude_session_id])
    db = missioncache_db.TaskDB()
    db.initialize()
    yield {"root": root, "home": home, "db": db, "live": live}
    db.close()


def _project(env, name, waiting_rows=""):
    task = env["db"].create_task(name, task_type="coding", repo_id=None)
    d = env["root"] / "active" / name
    d.mkdir(parents=True)
    (d / f"{name}-context.md").write_text(CONTEXT.format(name=name, rows=waiting_rows))
    (d / f"{name}-tasks.md").write_text(TASKS.format(name=name))
    return task


def _live(env, session_id, project):
    env["live"].append({"session_id": session_id, "title": project, "last_active": "x",
                        "project_name": project})


def _status(session_id):
    return lead_board.agent_status_rows([session_id]).get(session_id)


def _board(env, **kw):
    return lead_board.build_lead_board(env["db"], today=TODAY, **kw)


SID_A = "aaaaaaaa-0000-0000-0000-000000000001"
SID_B = "bbbbbbbb-0000-0000-0000-000000000002"
LEAD = "cccccccc-0000-0000-0000-000000000003"


class TestReportAgentStatus:
    def test_state_since_holds_while_the_state_repeats(self, env, monkeypatch):
        stamps = iter(["2026-10-18 10:00:00", "2026-10-18 10:05:00", "2026-10-18 10:09:00"])

        class _Clock:
            @staticmethod
            def now():
                from datetime import datetime
                return datetime.strptime(next(stamps), "%Y-%m-%d %H:%M:%S")

        monkeypatch.setattr(lead_board, "datetime", _Clock)
        lead_board.report_agent_status(SID_A, "needs_you", needs_reason="permission: Bash")
        lead_board.report_agent_status(SID_A, "needs_you", needs_reason="permission: Bash")
        assert _status(SID_A)["state_since"] == "2026-10-18 10:00:00"
        lead_board.report_agent_status(SID_A, "working")
        assert _status(SID_A)["state_since"] == "2026-10-18 10:09:00"

    def test_reason_lives_only_while_the_session_needs_you(self, env):
        lead_board.report_agent_status(SID_A, "needs_you", needs_reason="question")
        assert _status(SID_A)["needs_reason"] == "question"
        lead_board.report_agent_status(SID_A, "working")
        assert _status(SID_A)["needs_reason"] is None

    def test_idle_clears_the_running_tool_and_keeps_the_rest(self, env):
        lead_board.report_agent_status(SID_A, "working", prompt="add retries", now="Bash pytest -q")
        lead_board.report_agent_status(SID_A, "idle", context_pct=41, last_answer="done")
        row = _status(SID_A)
        assert row["now"] is None
        assert row["prompt"] == "add retries"
        assert row["context_pct"] == 41
        assert row["last_turn_at"] is not None

    def test_free_text_is_one_clean_clipped_line(self, env):
        lead_board.report_agent_status(SID_A, "idle", last_answer="\x1b[31mred\x1b[0m\nnext " + "x" * 100)
        answer = _status(SID_A)["last_answer"]
        assert "\x1b" not in answer and "\n" not in answer
        assert len(answer) == lead_board.ANSWER_MAX_CHARS

    def test_an_unsafe_session_id_is_refused_and_writes_nothing(self, env):
        lead_board.report_agent_status(SID_B, "idle")
        for bad in ("../etc", ""):
            with pytest.raises(ValueError):
                lead_board.report_agent_status(bad, "idle")
        conn = sqlite3.connect(missioncache_db.HOOKS_STATE_DB_PATH)
        assert conn.execute("SELECT session_id FROM agent_status").fetchall() == [(SID_B,)]
        conn.close()

    @pytest.mark.parametrize("cost", [float("nan"), float("inf")])
    def test_a_cost_that_is_not_a_number_is_refused(self, env, cost):
        with pytest.raises(ValueError):
            lead_board.report_agent_status(SID_A, "idle", cost_usd=cost)
        assert _status(SID_A) is None

    def test_an_unknown_state_is_refused(self, env):
        with pytest.raises(ValueError):
            lead_board.report_agent_status(SID_A, "busy")


class TestBuildLeadBoard:
    def test_a_project_with_a_live_session_shows_its_state(self, env):
        _project(env, "api-gateway")
        _live(env, SID_A, "api-gateway")
        lead_board.report_agent_status(SID_A, "working", prompt="add retries")

        [card] = _board(env)["projects"]
        assert card["name"] == "api-gateway"
        assert (card["completed_count"], card["total_count"]) == (1, 3)
        assert card["next_up"] == "Add the retry path"
        assert card["left_off"] == "wired the retry path into the client"
        assert card["last_save"] == "2026-10-18 09:15"
        assert card["session"]["session_id"] == SID_A
        assert card["session"]["state"] == "working"
        assert card["session"]["prompt"] == "add retries"

    def test_a_session_that_never_reported_has_no_state(self, env):
        _project(env, "api-gateway")
        _live(env, SID_A, "api-gateway")
        [card] = _board(env)["projects"]
        assert card["session"]["session_id"] == SID_A
        assert card["session"]["state"] is None

    def test_a_project_changed_today_shows_without_a_session(self, env):
        _project(env, "docs-site")
        events.record_events(env["db"], "docs-site", [("recent_change", "install page rewritten", None)])
        [card] = _board(env)["projects"]
        assert card["session"] is None
        assert card["last_event"]["what"] == "install page rewritten"

    def test_a_quiet_project_shows_only_under_all(self, env):
        _project(env, "quiet")
        board = _board(env)
        assert board["projects"] == []
        assert board["quiet"] == [{"name": "quiet", "last_change": None}]
        assert [p["name"] for p in _board(env, scope="all")["projects"]] == ["quiet"]

    def test_a_change_older_than_the_window_is_quiet(self, env):
        _project(env, "yesterday")
        events.record_events(env["db"], "yesterday", [("recent_change", "old note", None)])
        with env["db"].connection() as conn:
            conn.execute("UPDATE events SET created_at = datetime('now', 'localtime', '-13 hours')")
            conn.commit()
        board = _board(env)
        assert board["projects"] == []
        assert [q["name"] for q in board["quiet"]] == ["yesterday"]

    def test_the_counts_are_the_boards_own(self, env):
        _project(env, "api-gateway")
        _live(env, SID_A, "api-gateway")
        lead_board.report_agent_status(SID_A, "working")
        events.record_events(env["db"], "api-gateway", [("task_done", "task 3 done", None)])
        assert _board(env)["counts"] == {"needs_you": 0, "working": 1, "sessions": 1, "changes": 1}

    def test_the_active_task_comes_from_the_sessions_pointer(self, env):
        _project(env, "api-gateway")
        _live(env, SID_A, "api-gateway")
        pointer = env["home"] / ".claude" / "hooks" / "state" / "active-missioncache-task"
        pointer.mkdir(parents=True)
        (pointer / f"{SID_A}.json").write_text(json.dumps(
            {"project_name": "api-gateway", "task_numbers": ["2"]}))
        [card] = _board(env)["projects"]
        assert card["active_task"] == {"number": "2", "title": "Add the retry path"}

    def test_the_newest_waiting_row_is_named(self, env):
        # The newest row is listed second, so taking the first row fails.
        _project(env, "api-gateway", waiting_rows=(
            "| design reply | Dana | 2026-08-01 | rollout |\n| schema signoff | Robin | 2026-10-15 | merge |"))
        _live(env, SID_A, "api-gateway")
        [card] = _board(env)["projects"]
        assert card["waiting"]["count"] == 2
        assert card["waiting"]["latest"]["what"] == "schema signoff"

    def test_next_step_is_the_first_of_the_next_steps(self, env):
        task = _project(env, "api-gateway")
        ctx = env["root"] / "active" / "api-gateway" / "api-gateway-context.md"
        ctx.write_text(ctx.read_text().replace(
            "1. TBD", "1. **Ship the retry path** to staging\n2. Run the e2e suite"))
        _live(env, SID_A, "api-gateway")
        [card] = _board(env)["projects"]
        # The tasks file still has an older unchecked item first.
        assert card["next_up"] == "Add the retry path"
        assert card["next_step"] == "Ship the retry path to staging"

    def test_a_project_that_needs_you_sorts_first(self, env):
        _project(env, "alpha")
        _project(env, "beta")
        _live(env, SID_A, "alpha")
        _live(env, SID_B, "beta")
        lead_board.report_agent_status(SID_A, "idle")
        lead_board.report_agent_status(SID_B, "needs_you", needs_reason="permission: Bash")
        # alpha is the more recent one, so only the attention rank puts beta first.
        events.record_events(env["db"], "alpha", [("recent_change", "fresh work", None)])
        board = _board(env)
        assert [p["name"] for p in board["projects"]] == ["beta", "alpha"]
        assert board["needs_you"][0]["project_name"] == "beta"
        assert board["needs_you"][0]["what"] == "permission: Bash"

    def test_an_overdue_action_item_of_mine_needs_me(self, env):
        task = _project(env, "billing")
        pm_items.add_action_item(env["db"], task.id, "send the schema note", due_date="2000-01-01")
        board = _board(env)
        assert [p["name"] for p in board["projects"]] == ["billing"]
        assert board["needs_you"][0]["what"] == "send the schema note"
        assert board["needs_you"][0]["kind"] == "overdue"

    def test_the_lead_is_no_projects_session_and_its_writes_stay_out_of_the_feed(self, env):
        _project(env, "api-gateway")
        _live(env, LEAD, "api-gateway")
        events.record_events(env["db"], "api-gateway", [("recent_change", "from the lead", None)],
                             source_session=LEAD)
        events.record_events(env["db"], "api-gateway", [("recent_change", "from a worker", None)],
                             source_session=SID_A)
        board = _board(env, lead_session_id=LEAD, caller_session_id=LEAD)
        [card] = board["projects"]
        assert card["session"] is None
        assert [e["what"] for e in board["events"]] == ["from a worker"]
        assert board["lead"]["is_me"] is True
        assert _board(env, lead_session_id=LEAD, caller_session_id=SID_A)["lead"]["is_me"] is False

    def test_a_second_session_that_needs_you_is_not_hidden(self, env):
        _project(env, "api-gateway")
        _live(env, SID_A, "api-gateway")  # the most recently active one
        _live(env, SID_B, "api-gateway")
        lead_board.report_agent_status(SID_A, "working")
        lead_board.report_agent_status(SID_B, "needs_you", needs_reason="question")
        board = _board(env)
        [card] = board["projects"]
        assert card["session"]["session_id"] == SID_B
        assert card["session_count"] == 2
        assert [(n["session_id"], n["what"]) for n in board["needs_you"]] == [(SID_B, "question")]

    def test_a_malformed_active_task_pointer_is_ignored(self, env):
        _project(env, "api-gateway")
        _live(env, SID_A, "api-gateway")
        pointer = env["home"] / ".claude" / "hooks" / "state" / "active-missioncache-task"
        pointer.mkdir(parents=True)
        (pointer / f"{SID_A}.json").write_text(json.dumps(
            {"project_name": "api-gateway", "task_numbers": 2}))
        [card] = _board(env)["projects"]
        assert card["active_task"] is None

    def test_no_returned_string_carries_a_control_or_format_character(self, env):
        _project(env, "api-gateway")
        _live(env, SID_A, "api-gateway")
        events.record_events(env["db"], "api-gateway",
                             [("recent_change", "fixed \u202eevil\u202c \x1b[2Jthing \u2067x\u2069", None)])
        board = _board(env)
        text = json.dumps(board, ensure_ascii=False)
        for ch in ("\u202e", "\u202c", "\u2067", "\u2069", "\x1b"):
            assert ch not in text
        assert board["events"][0]["what"] == "fixed evil [2Jthing x"

    def test_a_status_read_failure_is_reported_not_hidden(self, env, monkeypatch):
        _project(env, "api-gateway")
        _live(env, SID_A, "api-gateway")

        def _fail(_ids):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(lead_board, "agent_status_rows", _fail)
        board = _board(env)
        assert board["status_error"] == "database is locked"
        assert board["projects"][0]["session"]["state"] is None

    def test_the_feed_reads_after_the_cursor(self, env):
        _project(env, "api-gateway")
        events.record_events(env["db"], "api-gateway", [("recent_change", "first", None)])
        cursor = _board(env)["last_event_id"]
        events.record_events(env["db"], "api-gateway", [("task_done", "second", None)])
        assert [e["what"] for e in _board(env, after_id=cursor)["events"]] == ["second"]

    def test_a_backlog_longer_than_a_page_is_read_without_a_gap(self, env):
        _project(env, "api-gateway")
        events.record_events(env["db"], "api-gateway", [("recent_change", "start", None)])
        cursor = _board(env)["last_event_id"]
        total = lead_board.EVENTS_PAGE + 50
        events.record_events(env["db"], "api-gateway",
                             [("recent_change", f"e{i}", None) for i in range(total)])
        seen = []
        while True:
            board = _board(env, after_id=cursor)
            seen += [e["what"] for e in board["events"]]
            cursor = board["last_event_id"]
            if not board["has_more"]:
                break
        assert sorted(seen) == sorted(f"e{i}" for i in range(total))


class TestWatermark:
    def test_an_unchanged_watermark_builds_nothing(self, env):
        _project(env, "api-gateway")
        _live(env, SID_A, "api-gateway")
        first = _board(env)
        assert _board(env, watermark=first["watermark"]) == {
            "changed": False, "watermark": first["watermark"]}

    def test_a_session_state_write_moves_the_watermark(self, env):
        _project(env, "api-gateway")
        # The first write creates hooks-state.db, which alone moves the token.
        # Start from an existing DB so only the status write can move it.
        lead_board.report_agent_status(SID_B, "idle")
        before = lead_board.lead_board_watermark(env["db"])
        lead_board.report_agent_status(SID_A, "working")
        assert lead_board.lead_board_watermark(env["db"]) != before

    def test_a_state_change_in_the_same_second_moves_the_watermark(self, env, monkeypatch):
        from datetime import datetime

        class _Frozen:
            @staticmethod
            def now():
                return datetime(2026, 10, 18, 10, 0, 0)

        monkeypatch.setattr(lead_board, "datetime", _Frozen)
        lead_board.report_agent_status(SID_A, "working")
        before = lead_board.lead_board_watermark(env["db"])
        lead_board.report_agent_status(SID_A, "needs_you", needs_reason="question")
        assert lead_board.lead_board_watermark(env["db"]) != before

    def test_a_new_lead_moves_the_watermark(self, env):
        _project(env, "api-gateway")
        assert (lead_board.lead_board_watermark(env["db"], LEAD)
                != lead_board.lead_board_watermark(env["db"], SID_A))


    def test_an_active_task_change_moves_the_watermark(self, env):
        """The pointer is a file the MCP server rewrites with no DB trace, and
        the board shows it, so a stale token would hide the new active task."""
        import os

        _project(env, "api-gateway")
        pointer = env["home"] / ".claude" / "hooks" / "state" / "active-missioncache-task"
        pointer.mkdir(parents=True)
        path = pointer / f"{SID_A}.json"
        path.write_text(json.dumps({"project_name": "api-gateway", "task_numbers": ["2"]}))
        os.utime(path, (1_000_000, 1_000_000))
        before = lead_board.lead_board_watermark(env["db"])
        path.write_text(json.dumps({"project_name": "api-gateway", "task_numbers": ["3"]}))
        os.utime(path, (2_000_000, 2_000_000))
        assert lead_board.lead_board_watermark(env["db"]) != before

class TestFocusTerminal:
    def _term(self, env, rows):
        conn = sqlite3.connect(missioncache_db.HOOKS_STATE_DB_PATH)
        missioncache_db.init_hooks_state_db_schema(conn)
        conn.executemany(
            "INSERT INTO term_sessions (term_session_id, session_id, updated_at) VALUES (?, ?, ?)", rows)
        conn.commit()
        conn.close()

    def test_the_newest_terminal_of_a_session_wins(self, env):
        # Newest inserted first, so insertion order alone cannot pass this.
        self._term(env, [
            ("w0t2p0:B0AB9405-DA13-4B25-85A7-585A56ADAFC0", SID_A, "2026-10-18 11:00:00"),
            ("w0t0p0:0E0496A1-6355-4E36-8D7B-4F8E71CB71F1", SID_A, "2026-10-17 09:00:00"),
        ])
        assert lead_board.terminal_for_session(SID_A) == "B0AB9405-DA13-4B25-85A7-585A56ADAFC0"

    def test_no_terminal_means_no_jump(self, env, monkeypatch):
        monkeypatch.setattr(lead_board, "focus_available", lambda: True)
        assert lead_board.focus_terminal(SID_A) == {
            "success": False, "reason": "no terminal recorded for this session"}

    def test_off_macos_osascript_never_runs(self, env, monkeypatch):
        self._term(env, [("w0t2p0:B0AB9405-DA13-4B25-85A7-585A56ADAFC0", SID_A, "2026-10-18 11:00:00")])
        monkeypatch.setattr(lead_board, "focus_available", lambda: False)
        monkeypatch.setattr(lead_board.subprocess, "run", lambda *a, **k: pytest.fail("osascript ran"))
        result = lead_board.focus_terminal(SID_A)
        assert result["success"] is False
        assert "macOS" in result["reason"]

    def test_an_osascript_refusal_is_passed_on(self, env, monkeypatch):
        self._term(env, [("w0t2p0:B0AB9405-DA13-4B25-85A7-585A56ADAFC0", SID_A, "2026-10-18 11:00:00")])
        monkeypatch.setattr(lead_board, "focus_available", lambda: True)
        monkeypatch.setattr(lead_board.subprocess, "run", lambda *a, **k: type(
            "R", (), {"returncode": 1, "stdout": "", "stderr": "Not authorized to send Apple events"})())
        assert lead_board.focus_terminal(SID_A) == {
            "success": False, "reason": "Not authorized to send Apple events"}

    def test_the_uuid_reaches_osascript_as_an_argument(self, env, monkeypatch):
        self._term(env, [("w0t2p0:B0AB9405-DA13-4B25-85A7-585A56ADAFC0", SID_A, "2026-10-18 11:00:00")])
        monkeypatch.setattr(lead_board, "focus_available", lambda: True)
        seen = {}

        def _run(args, **_kw):
            seen["args"] = args
            return type("R", (), {"returncode": 0, "stdout": "focused\n", "stderr": ""})()

        monkeypatch.setattr(lead_board.subprocess, "run", _run)
        assert lead_board.focus_terminal(SID_A) == {"success": True}
        assert seen["args"][-1] == "B0AB9405-DA13-4B25-85A7-585A56ADAFC0"
        assert not any("B0AB9405" in a for a in seen["args"][:-1])


class TestPrune:
    def test_an_old_dead_sessions_state_is_swept_and_a_live_one_kept(self, env, monkeypatch):
        lead_board.report_agent_status(SID_A, "idle")
        lead_board.report_agent_status(SID_B, "idle")
        conn = sqlite3.connect(missioncache_db.HOOKS_STATE_DB_PATH)
        conn.execute("UPDATE agent_status SET updated_at = '2000-01-01 00:00:00'")
        conn.commit()
        conn.close()
        monkeypatch.setattr(missioncache_db, "session_is_alive", lambda sid: sid == SID_B)

        counts = missioncache_db.prune_session_state(max_age_days=7)

        assert counts["agent_status_rows"] == 1
        assert _status(SID_A) is None
        assert _status(SID_B) is not None


# --- today's picks -----------------------------------------------------------


def test_picks_round_trip_clean_and_reach_the_board(env):
    missioncache_db.set_lead_session(SID_A)
    written = lead_board.set_lead_picks(SID_A, [
        {"what": "Close the\x1b[31m schema note", "project": "api-gateway", "why": "overdue 2d"},
        {"what": "Publish the docs site"},
    ])
    assert written == [
        {"what": "Close the [31m schema note", "project": "api-gateway", "why": "overdue 2d"},
        {"what": "Publish the docs site", "project": None, "why": None},
    ]
    board = lead_board.build_lead_board(env["db"])
    assert board["picks"]["items"] == written
    assert board["picks"]["is_today"] is True


def test_new_picks_replace_the_old_and_move_the_watermark(env):
    missioncache_db.set_lead_session(SID_A)
    lead_board.set_lead_picks(SID_A, [{"what": "first"}])
    mark = _board(env)["watermark"]
    lead_board.set_lead_picks(SID_A, [{"what": "second"}])
    board = _board(env, watermark=mark)
    assert board["changed"] is True
    assert [p["what"] for p in board["picks"]["items"]] == ["second"]


def test_no_picks_yet_reads_none(env):
    assert _board(env)["picks"] is None


@pytest.mark.parametrize("items", [
    [{"what": "a"}, {"what": "b"}, {"what": "c"}, {"what": "d"}],
    [{"project": "api-gateway"}],
    ["not an object"],
    {"what": "not a list"},
])
def test_malformed_picks_are_refused_and_write_nothing(env, items):
    missioncache_db.set_lead_session(SID_A)
    with pytest.raises(ValueError):
        lead_board.set_lead_picks(SID_A, items)
    assert lead_board.lead_picks() is None


def test_lead_only_picks_need_the_caller_to_be_the_lead(env):
    missioncache_db.set_lead_session(SID_A)
    assert lead_board.set_lead_picks(SID_B, [{"what": "not mine"}]) is None
    assert lead_board.lead_picks() is None
    assert lead_board.set_lead_picks(SID_A, [{"what": "mine"}]) == [
        {"what": "mine", "project": None, "why": None}]
    # A new lead takes over: the old lead's late write no longer lands.
    missioncache_db.set_lead_session(SID_B)
    assert lead_board.set_lead_picks(SID_A, [{"what": "late"}]) is None
    assert [p["what"] for p in lead_board.lead_picks()["items"]] == ["mine"]


# --- relay -----------------------------------------------------------------
# Spec: B3 of the lead-board plan, decided with Tomer 2026-10-19. The lead
# builds one envelope line for a project's live session; the receiving side
# logs whether the person there ran it or dropped it.


def _envelope(result):
    prefix, _, body = result["envelope"].partition(" ")
    assert prefix == lead_board.RELAY_PREFIX
    return json.loads(body)


def test_relay_goes_to_the_live_session_the_lookup_ranks_first_as_one_clean_line(env):
    _live(env, SID_A, "api-gateway")
    _live(env, SID_B, "api-gateway")
    result = lead_board.prepare_relay(LEAD, "api-gateway", "run the e2e\nsuite \x1b[31mnow")
    assert (result["success"], result["session_id"], result["title"]) == (True, SID_A, "api-gateway")
    body = _envelope(result)
    assert (body["to"], body["from"], body["project"], body["text"]) == (
        SID_A, "missioncache-lead", "api-gateway", "run the e2e suite [31mnow")
    assert body["id"] and body["ts"].endswith("+00:00")
    assert "\n" not in result["envelope"]


def test_relay_never_goes_to_the_sender(env):
    _live(env, LEAD, "api-gateway")
    assert lead_board.prepare_relay(LEAD, "api-gateway", "hi") == {
        "success": False, "reason": "api-gateway has no live session"}


def test_relay_text_runs_as_typed_or_is_refused(env):
    # Cutting a relay could drop the part that changes its meaning, and a
    # leading "!" would run a shell command outside the permission prompts.
    _live(env, SID_A, "api-gateway")
    longest = "x" * lead_board.RELAY_TEXT_MAX_CHARS
    assert _envelope(lead_board.prepare_relay(LEAD, "api-gateway", longest))["text"] == longest
    for refused in (longest + "x", " \n ", "!rm -rf build"):
        with pytest.raises(ValueError):
            lead_board.prepare_relay(LEAD, "api-gateway", refused)


def test_record_relay_logs_on_the_receivers_project(env, monkeypatch):
    _project(env, "api-gateway")
    monkeypatch.setattr(missioncache_db, "bound_project_for_session",
                        lambda sid: "api-gateway" if sid == SID_A else None)
    assert lead_board.record_relay(env["db"], SID_A, "ran", "run the e2e suite") is True
    assert lead_board.record_relay(env["db"], SID_A, "failed", "print the date") is True
    assert lead_board.record_relay(env["db"], SID_B, "ran", "x") is False
    rows = events.list_events(env["db"], project="api-gateway", kinds=["relayed"])
    assert sorted((r["what"], r["source_session"]) for r in rows) == [
        ("relay failed: print the date", SID_A), ("relay ran: run the e2e suite", SID_A)]
    with pytest.raises(ValueError):
        lead_board.record_relay(env["db"], SID_A, "maybe", "x")

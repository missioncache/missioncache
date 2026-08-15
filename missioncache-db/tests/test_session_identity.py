"""Tests for the session identity layer: liveness, applied titles, and the
project -> live sessions lookup.

Spec source: the cross-session notifications contract in
``rules/missioncache.md`` and ``docs/hooks.md``:

* A caller that has just written a project's context must be able to find the
  live Claude Code sessions bound to that project, and address each by the name
  ``SendMessage`` resolves - which is the session title.
* Bindings come from ``project_state`` (explicit /missioncache:load), never from
  cwd auto-resolution, so both the titling hook and the lookup describe one set
  of sessions.
* ``project_state`` rows are never deleted on session exit, so a dead session
  must not be reported as a notification target. Only *proven* dead is dead.
* One project can carry several live sessions, so titles must stay distinct.

Assertions trace to those rules, not to the implementation.
"""

import json
import os
import sqlite3
import subprocess
import sys

import pytest

import missioncache_db as m


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Redirect Path.home() and the hooks-state DB into a temp home.

    Mirrors ``_redirect_state`` in hooks/tests/test_hooks.py: the state-file
    paths resolve ``Path.home()`` per call, while HOOKS_STATE_DB_PATH is a
    module constant and has to be patched directly.
    """
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    db_path = tmp_path / ".claude" / "hooks-state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(m, "HOOKS_STATE_DB_PATH", db_path)
    return tmp_path


def _bind(home, rows):
    """Seed project_state with (session_id, project_name) rows."""
    conn = sqlite3.connect(home / ".claude" / "hooks-state.db")
    try:
        m.init_hooks_state_db_schema(conn)
        conn.executemany(
            "INSERT INTO project_state (session_id, project_name) VALUES (?, ?)", rows
        )
        conn.commit()
    finally:
        conn.close()


def _bind_live(home, rows):
    """Bind sessions AND give each a live pid record.

    The default for anything expected to be listed: a session is only a
    notification target when its pid proves it is running, so a bare _bind is
    a *stale* row, not a live one.
    """
    _bind(home, rows)
    for session_id, _project in rows:
        _seed_pid(home, session_id, os.getpid())


def _seed_pid(home, session_id, pid, start_time=None):
    path = m.session_pid_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"sessionId": session_id, "pid": pid, "startTime": start_time})
    )


_REAPED = []  # keeps the Windows process handle open so the pid cannot recycle


def _dead_pid():
    """A pid verified dead, resilient to Windows pid recycling.

    Windows reuses freed pids aggressively; holding the Popen object keeps
    the kernel process object (and so the pid) reserved, and the verification
    loop makes the precondition observable instead of assumed.
    """
    from missioncache_db import proc as _proc

    for _ in range(10):
        p = subprocess.Popen([sys.executable, "-c", ""])
        p.wait()
        _REAPED.append(p)
        if _proc.process_alive(p.pid) is False:
            return p.pid
    raise AssertionError("could not obtain a reliably dead pid")


class TestSessionIsAlive:
    def test_unknown_without_a_pid_record(self, home):
        """No record is 'unknown', not 'dead' - the caller must not drop it."""
        assert m.session_is_alive("never-recorded") is None

    def test_alive_for_a_running_pid(self, home):
        _seed_pid(home, "live-sid", os.getpid())
        assert m.session_is_alive("live-sid") is True

    def test_dead_for_an_exited_pid(self, home):
        _seed_pid(home, "dead-sid", _dead_pid())
        assert m.session_is_alive("dead-sid") is False

    def test_dead_when_the_pid_was_recycled(self, home):
        """Live pid but a start time that no longer matches: a different
        process now owns the number, so the session is gone."""
        _seed_pid(home, "reused-sid", os.getpid(), start_time="Thu Jan  1 00:00:00 1970")
        assert m.session_is_alive("reused-sid") is False

    def test_unknown_for_a_path_traversing_id(self, home):
        """An id that would escape the state dir must not read the file it
        points at.

        The record is PLANTED at the resolved traversal target first, so the
        guard is observable: without it the plant is read back and reports True.
        Asserting None against a nonexistent path would pass either way, since
        an unreadable file also yields None.
        """
        escaped = m.session_pid_path("../../plantedfile")
        escaped.parent.mkdir(parents=True, exist_ok=True)
        escaped.write_text(json.dumps({"pid": os.getpid(), "startTime": None}))
        assert m.session_is_alive("../../plantedfile") is None


class TestSessionTitleRecord:
    def test_round_trips_title_and_project(self, home):
        m.write_session_title("sid-a", "demo-2", "demo")
        record = m.read_session_title("sid-a")
        assert record["title"] == "demo-2"
        assert record["projectName"] == "demo"

    def test_missing_record_reads_as_none(self, home):
        assert m.read_session_title("sid-a") is None

    def test_refuses_to_write_a_path_traversing_id(self, home):
        """Assert on the path the write would ACTUALLY reach.

        `session_title_path("../escape")` resolves to `state/escape.json`, one
        level up from the session-title dir. An earlier version of this test
        asserted on `home.parent/escape.json`, which no write could ever reach,
        so it passed with the guard deleted.
        """
        escaped = (m.session_title_path("../escape")).resolve()
        m.write_session_title("../escape", "x", "x")
        assert not escaped.exists()
        assert not m.session_title_path("ok").parent.exists(), (
            "a rejected id must not even create the session-title dir"
        )

    def test_writes_a_record_for_a_valid_id(self, home):
        """Positive control: the guard rejects the bad id specifically, rather
        than the write being broken for every id."""
        m.write_session_title("good-sid", "demo", "demo")
        assert m.session_title_path("good-sid").exists()


class TestLiveSessionsForProject:
    def test_lists_sessions_bound_to_the_project(self, home):
        _bind_live(home, [("sid-a", "proj-x"), ("sid-b", "proj-y")])
        found = m.live_sessions_for_project("proj-x")
        assert [s["session_id"] for s in found] == ["sid-a"]

    def test_reports_the_title_the_session_actually_carries(self, home):
        """The lookup must return the applied title, not the project name -
        they diverge as soon as a second session takes a suffix."""
        _bind_live(home, [("sid-a", "proj-x")])
        m.write_session_title("sid-a", "proj-x-2", "proj-x")
        assert m.live_sessions_for_project("proj-x")[0]["title"] == "proj-x-2"

    def test_title_is_none_when_the_hook_never_ran(self, home):
        """A null title is the signal to ask the user, so it must not be
        silently replaced with the project name."""
        _bind_live(home, [("sid-a", "proj-x")])
        assert m.live_sessions_for_project("proj-x")[0]["title"] is None

    def test_excludes_the_caller(self, home):
        _bind_live(home, [("sid-a", "proj-x"), ("sid-b", "proj-x")])
        found = m.live_sessions_for_project("proj-x", exclude_session_id="sid-a")
        assert [s["session_id"] for s in found] == ["sid-b"]

    def test_drops_sessions_proven_dead(self, home):
        """project_state rows outlive the session, so a stale row must not
        become a notification target."""
        _bind_live(home, [("sid-a", "proj-x")])
        _bind(home, [("sid-dead", "proj-x")])
        _seed_pid(home, "sid-dead", _dead_pid())
        found = m.live_sessions_for_project("proj-x")
        assert [s["session_id"] for s in found] == ["sid-a"]

    def test_drops_sessions_whose_liveness_is_unknown(self, home):
        """A session must be PROVEN alive, not merely unproven-dead.

        project_state is a full history - every session that ever loaded the
        project, never cleaned on exit. Rows that old have no pid record and so
        read as unknown; admitting unknowns would announce every context write
        to months of dead sessions and fire the ask-the-user fallback once per
        corpse. Found by the live test: one real project carried 34 such rows
        going back two months, against 0 live ones.
        """
        _bind_live(home, [("sid-a", "proj-x")])
        _bind(home, [("sid-ancient", "proj-x")])  # no pid record, like a June row
        found = m.live_sessions_for_project("proj-x")
        assert [s["session_id"] for s in found] == ["sid-a"]

    def test_empty_when_no_state_db_exists(self, home, monkeypatch):
        monkeypatch.setattr(m, "HOOKS_STATE_DB_PATH", home / "nope.db")
        assert m.live_sessions_for_project("proj-x") == []


class TestChooseSessionTitle:
    def test_plain_project_name_when_alone(self, home):
        _bind_live(home, [("sid-a", "proj-x")])
        assert m.choose_session_title("proj-x", "sid-a") == "proj-x"

    def test_suffixes_when_another_live_session_holds_the_name(self, home):
        _bind_live(home, [("sid-a", "proj-x"), ("sid-b", "proj-x")])
        m.write_session_title("sid-a", "proj-x", "proj-x")
        assert m.choose_session_title("proj-x", "sid-b") == "proj-x-2"

    def test_takes_the_lowest_free_suffix(self, home):
        _bind_live(home, [(f"sid-{n}", "proj-x") for n in "abc"])
        m.write_session_title("sid-a", "proj-x", "proj-x")
        m.write_session_title("sid-c", "proj-x-3", "proj-x")
        assert m.choose_session_title("proj-x", "sid-b") == "proj-x-2"

    def test_a_closed_session_frees_its_name(self, home):
        _bind_live(home, [("sid-a", "proj-x"), ("sid-b", "proj-x")])
        m.write_session_title("sid-a", "proj-x", "proj-x")
        _seed_pid(home, "sid-a", _dead_pid())
        assert m.choose_session_title("proj-x", "sid-b") == "proj-x"


class TestBoundProjectForSession:
    def test_returns_the_explicitly_bound_project(self, home):
        _bind(home, [("sid-a", "proj-x")])
        assert m.bound_project_for_session("sid-a") == "proj-x"

    def test_none_when_unbound(self, home):
        assert m.bound_project_for_session("sid-a") is None


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="drives the walk through the POSIX ps seam; Windows uses the "
    "ctypes snapshot backend, covered by test_proc's contract tests",
)
class TestResolveClaudeProcessPid:
    """The walk every liveness answer derives from, previously untested.

    If it picked the login shell instead of `claude`, every closed session would
    report alive forever - the exact stale-row storm the feature exists to stop.
    """

    def _walk_over(self, monkeypatch, tree):
        """Drive the walk over a fake process tree: {pid: (ppid, comm)}.

        Patches the proc backend's ps reader - the seam both parent_pid and
        process_name flow through on POSIX - so the walk logic above it runs
        for real (basename + .exe normalization included).
        """
        monkeypatch.setattr(m.os, "getpid", lambda: 100)

        def _fake_ps(pid, fmt):
            entry = tree.get(pid)
            if entry is None:
                return None
            ppid, comm = entry
            if fmt.startswith("ppid=,"):
                # parent_and_name's fused read: "  <ppid> <comm>" on one line.
                if ppid is None:
                    return None
                return f"{ppid} {comm or ''}".strip()
            return ppid if fmt == "ppid" else comm

        monkeypatch.setattr(m.proc, "_ps_field", _fake_ps)

    def test_returns_the_claude_ancestor_not_the_parent(self, monkeypatch):
        """The documented shape is claude -> uv -> python, so the match is the
        grandparent. Returning the immediate parent would be wrong."""
        self._walk_over(monkeypatch, {
            100: ("200", "python"), 200: ("300", "uv"), 300: ("1", "/usr/local/bin/claude"),
        })
        assert m.resolve_claude_process_pid() == 300

    def test_none_when_no_claude_ancestor(self, monkeypatch):
        """Claude Desktop's process shape has no claude ancestor to find."""
        self._walk_over(monkeypatch, {
            100: ("200", "python"), 200: ("1", "login"),
        })
        assert m.resolve_claude_process_pid() is None

    def test_none_when_ps_gives_nothing(self, monkeypatch):
        self._walk_over(monkeypatch, {})
        assert m.resolve_claude_process_pid() is None

    def test_none_on_a_non_integer_ppid(self, monkeypatch):
        self._walk_over(monkeypatch, {100: ("not-a-pid", "python")})
        assert m.resolve_claude_process_pid() is None

    def test_terminates_on_a_cyclic_tree(self, monkeypatch):
        """Bounded by _SESSION_PID_WALK_MAX_DEPTH: a pathological tree must
        return rather than spin."""
        self._walk_over(monkeypatch, {
            100: ("200", "python"), 200: ("100", "python"),
        })
        assert m.resolve_claude_process_pid() is None


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="ps is POSIX-only; on Windows MSYS ps.exe is deliberately unused",
)
class TestPsField:
    def test_returns_a_field_for_a_live_pid(self):
        assert m.proc._ps_field(os.getpid(), "comm")

    def test_none_for_a_dead_pid(self):
        assert m.proc._ps_field(_dead_pid(), "comm") is None

    def test_none_rather_than_raising_when_ps_is_unusable(self, monkeypatch):
        """session_is_alive treats a raise as fatal, so this must swallow."""
        def _boom(*_a, **_k):
            raise OSError("no ps")
        monkeypatch.setattr(m.proc.subprocess, "run", _boom)
        assert m.proc._ps_field(os.getpid(), "comm") is None


class TestLiveSessionOrdering:
    def test_newest_binding_first(self, home):
        """The docstring promises newest-first; an ASC typo must fail. Explicit
        timestamps because project_state.updated_at is second-resolution and two
        rows seeded in the same second would tie."""
        conn = sqlite3.connect(home / ".claude" / "hooks-state.db")
        try:
            m.init_hooks_state_db_schema(conn)
            conn.executemany(
                "INSERT INTO project_state (session_id, project_name, updated_at) "
                "VALUES (?, ?, ?)",
                [("sid-old", "proj-x", "2026-01-01 00:00:00"),
                 ("sid-new", "proj-x", "2026-06-01 00:00:00")],
            )
            conn.commit()
        finally:
            conn.close()
        for sid in ("sid-old", "sid-new"):
            _seed_pid(home, sid, os.getpid())
        assert [s["session_id"] for s in m.live_sessions_for_project("proj-x")] == [
            "sid-new", "sid-old",
        ]

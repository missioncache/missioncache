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
* Nothing deletes a ``project_state`` row on session exit (only an explicit
  ``prune-sessions`` run does), so a dead session must not be reported as a
  notification target. Only *proven* dead is dead.
* One project can carry several live sessions, so titles must stay distinct.

Assertions trace to those rules, not to the implementation.
"""

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta

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


def _age_file(path, days):
    """Backdate a state file so the prune age floor sees it as old."""
    old = time.time() - days * 86400
    os.utime(path, (old, old))


def _seed_pointer(home, session_id, project_name, age_days=0):
    m.write_session_binding(session_id, project_name)
    path = m.session_binding_path(session_id)
    if age_days:
        _age_file(path, age_days)
    return path


def _bind_aged(home, session_id, project_name, age_days):
    """A project_state row whose updated_at is ``age_days`` in the past."""
    stamp = (datetime.now() - timedelta(days=age_days)).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(home / ".claude" / "hooks-state.db")
    try:
        m.init_hooks_state_db_schema(conn)
        conn.execute(
            "INSERT INTO project_state (session_id, project_name, updated_at) "
            "VALUES (?, ?, ?)",
            (session_id, project_name, stamp),
        )
        conn.commit()
    finally:
        conn.close()


def _state_rows(home):
    conn = sqlite3.connect(home / ".claude" / "hooks-state.db")
    try:
        return {row[0] for row in conn.execute("SELECT session_id FROM project_state")}
    finally:
        conn.close()


def _seed_transcript(home, session_id, age_seconds=0):
    """A Claude Code transcript for this session, optionally backdated.

    Transcript mtime is the clock parallel-session detection filters on, and
    therefore the clock the pid-record gate has to respect.
    """
    directory = home / ".claude" / "projects" / "-some-repo"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"
    path.write_text("{}\n")
    if age_seconds:
        old = time.time() - age_seconds
        os.utime(path, (old, old))
    return path


def _deleted(counts):
    """Just the deletion counts - the returned dict also echoes the floor."""
    return {k: v for k, v in counts.items() if k != "max_age_days"}


class TestPruneSessionState:
    """Spec source: the prune contract in ``docs/hooks.md``.

    Two guards, and each test names which one it exercises. Liveness decides
    whether a session may be swept at all; age decides whether enough time has
    passed for the sweep to be invisible to ``_detect_parallel_sessions``,
    whose 10-minute window is the only place a dead session's pid record is
    still read.
    """

    def test_a_live_session_survives_however_old_its_records_are(self, home):
        """Liveness guard. Age must never override a resolvable pid: a session
        running since before the floor is the normal case for long work, and
        pruning it would break its heartbeat resolution mid-session."""
        _bind_aged(home, "live-sid", "proj-x", age_days=400)
        _seed_pid(home, "live-sid", os.getpid())
        _age_file(m.session_pid_path("live-sid"), 400)
        _seed_pointer(home, "live-sid", "proj-x", age_days=400)

        counts = m.prune_session_state(max_age_days=7)

        assert _deleted(counts) == {
            "session_pids": 0,
            "project_pointers": 0,
            "project_state_rows": 0,
        }
        assert m.session_pid_path("live-sid").exists()
        assert m.session_binding_path("live-sid").exists()
        assert _state_rows(home) == {"live-sid"}

    def test_an_old_dead_session_is_swept_from_all_three_stores(self, home):
        """The case the command exists for: nothing deletes these on exit, so
        a machine accumulates one set per session that ever ran."""
        _bind_aged(home, "dead-sid", "proj-x", age_days=30)
        _seed_pid(home, "dead-sid", _dead_pid())
        _age_file(m.session_pid_path("dead-sid"), 30)
        _seed_pointer(home, "dead-sid", "proj-x", age_days=30)

        counts = m.prune_session_state(max_age_days=7)

        assert _deleted(counts) == {
            "session_pids": 1,
            "project_pointers": 1,
            "project_state_rows": 1,
        }
        assert not m.session_pid_path("dead-sid").exists()
        assert not m.session_binding_path("dead-sid").exists()
        assert _state_rows(home) == set()

    def test_a_recently_dead_session_is_kept(self, home):
        """Age guard, and the reason it exists. Deleting a fresh corpse's pid
        record turns ``session_is_alive`` from False (proven dead) into None
        (unknown), and ``_detect_parallel_sessions`` keeps unknowns - so the
        session would come back as a phantom parallel session inside its
        10-minute transcript window."""
        _bind_aged(home, "just-died", "proj-x", age_days=0)
        _seed_pid(home, "just-died", _dead_pid())
        _seed_pointer(home, "just-died", "proj-x")

        counts = m.prune_session_state(max_age_days=7)

        assert counts["session_pids"] == 0
        assert m.session_is_alive("just-died") is False
        assert _state_rows(home) == {"just-died"}

    def test_an_old_session_with_no_pid_record_is_swept(self, home):
        """Unknown liveness plus age is enough. Rows predating the pid feature
        can never become 'proven dead', so requiring proof would strand them
        forever - and they are the bulk of an old install's table."""
        _bind_aged(home, "ancient-sid", "proj-x", age_days=90)
        _seed_pointer(home, "ancient-sid", "proj-x", age_days=90)

        counts = m.prune_session_state(max_age_days=7)

        assert m.session_is_alive("ancient-sid") is None
        assert counts["project_pointers"] == 1
        assert counts["project_state_rows"] == 1
        assert _state_rows(home) == set()

    def test_each_store_is_judged_on_its_own_age(self, home):
        """Age is per record, not per session: a pointer can outlive its row.
        Sweeping the old pointer while leaving the fresh row is correct - the
        row is what a live peer lookup reads, the pointer is not."""
        _bind_aged(home, "split-sid", "proj-x", age_days=0)
        _seed_pointer(home, "split-sid", "proj-x", age_days=90)

        counts = m.prune_session_state(max_age_days=7)

        assert counts["project_pointers"] == 1
        assert counts["project_state_rows"] == 0
        assert _state_rows(home) == {"split-sid"}

    def test_dry_run_reports_without_deleting(self, home):
        """The counts must be the real ones, or a dry run cannot be used to
        decide whether to do it for real."""
        _bind_aged(home, "dead-sid", "proj-x", age_days=30)
        _seed_pid(home, "dead-sid", _dead_pid())
        _age_file(m.session_pid_path("dead-sid"), 30)
        _seed_pointer(home, "dead-sid", "proj-x", age_days=30)

        counts = m.prune_session_state(max_age_days=7, dry_run=True)

        assert _deleted(counts) == {
            "session_pids": 1,
            "project_pointers": 1,
            "project_state_rows": 1,
        }
        assert m.session_pid_path("dead-sid").exists()
        assert m.session_binding_path("dead-sid").exists()
        assert _state_rows(home) == {"dead-sid"}

    def test_a_dead_session_with_a_fresh_transcript_keeps_its_pid_record(self, home):
        """Transcript guard, and the reason age alone cannot do this job.

        ``write_session_pid`` runs only on a SessionStart, so a record's mtime
        is time since the last start, not time since death - a session that ran
        longer than the floor and then exited leaves a record that is old AND a
        fresh corpse. Parallel-session detection filters on transcript mtime and
        keeps unknowns, so deleting this record would bring the session back as
        a phantom. The pointer and the row have no such consumer and still go.
        """
        _bind_aged(home, "long-runner", "proj-x", age_days=30)
        _seed_pid(home, "long-runner", _dead_pid())
        _age_file(m.session_pid_path("long-runner"), 30)
        _seed_pointer(home, "long-runner", "proj-x", age_days=30)
        _seed_transcript(home, "long-runner")  # touched just now

        counts = m.prune_session_state(max_age_days=7)

        assert counts["session_pids"] == 0
        assert m.session_pid_path("long-runner").exists()
        assert m.session_is_alive("long-runner") is False, (
            "the record must survive still able to prove the session dead"
        )
        assert counts["project_pointers"] == 1
        assert counts["project_state_rows"] == 1

    def test_a_stale_transcript_does_not_hold_the_pid_record(self, home):
        """Positive control for the guard above: a transcript outside the
        window is not a reason to keep anything, or the gate would freeze the
        whole store on any install that keeps its transcripts."""
        _seed_pid(home, "old-runner", _dead_pid())
        _age_file(m.session_pid_path("old-runner"), 30)
        _seed_transcript(
            home, "old-runner", age_seconds=m.PRUNE_TRANSCRIPT_WINDOW_SECONDS + 60
        )

        counts = m.prune_session_state(max_age_days=7)

        assert counts["session_pids"] == 1
        assert not m.session_pid_path("old-runner").exists()

    def test_an_unreadable_transcript_dir_stops_the_pid_sweep(self, home):
        """Fail-safe direction. If the walk cannot answer which sessions are
        recent, the answer is not 'none of them' - it deletes nothing from the
        store the gate protects, and leaves the ungated stores alone to it."""
        _seed_pid(home, "dead-sid", _dead_pid())
        _age_file(m.session_pid_path("dead-sid"), 30)
        _seed_pointer(home, "dead-sid", "proj-x", age_days=30)
        monkey = lambda *_a, **_k: None
        original = m._sessions_with_recent_transcripts
        m._sessions_with_recent_transcripts = monkey
        try:
            counts = m.prune_session_state(max_age_days=7)
        finally:
            m._sessions_with_recent_transcripts = original

        assert counts["session_pids"] == 0
        assert m.session_pid_path("dead-sid").exists()
        assert counts["project_pointers"] == 1

    def test_a_planted_file_with_an_invalid_name_is_left_alone(self, home):
        """The docstring promises records whose id fails validation are not
        ours to delete. Nothing in this repo writes a traversal-shaped name, so
        a file carrying one came from somewhere else.

        ``..evil`` is chosen because it actually fails ``_is_valid_session_id``.
        A plain ``..json`` does not: the shared validator allows a dot and only
        rejects a doubled one, so its stem of ``.`` passes and the file is
        swept. That is harmless (the resolved path stays inside the pid dir)
        but it is not what this test is about.
        """
        pid_dir = m.session_pid_path("x").parent
        pid_dir.mkdir(parents=True, exist_ok=True)
        planted = pid_dir / "..evil.json"
        planted.write_text("{}")
        _age_file(planted, 30)
        assert not m._is_valid_session_id(planted.stem), "precondition"

        counts = m.prune_session_state(max_age_days=7)

        assert counts["session_pids"] == 0
        assert planted.exists()

    def test_a_row_with_an_invalid_session_id_is_left_alone(self, home):
        """Same promise, DB side."""
        _bind_aged(home, "../../evil", "proj-x", age_days=90)

        counts = m.prune_session_state(max_age_days=7)

        assert counts["project_state_rows"] == 0
        assert _state_rows(home) == {"../../evil"}

    def test_the_floor_is_at_least_one_day(self, home):
        """``--days 0`` must not become 'prune everything dead right now' -
        that is exactly the phantom-parallel-session window the floor closes."""
        _bind_aged(home, "just-died", "proj-x", age_days=0)
        _seed_pid(home, "just-died", _dead_pid())
        _seed_pointer(home, "just-died", "proj-x")

        counts = m.prune_session_state(max_age_days=0)

        assert counts["max_age_days"] == 1
        assert _deleted(counts) == {
            "session_pids": 0,
            "project_pointers": 0,
            "project_state_rows": 0,
        }
        assert _state_rows(home) == {"just-died"}

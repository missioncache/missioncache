"""Integration tests for session_start, pre_compact, and stop hooks.

Tests mock orbit_db and use tmp_path for file I/O.
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ── shared helpers ────────────────────────────────────────────────────────


def _patch_stdin_payload(monkeypatch, payload: dict) -> None:
    """Replace ``sys.stdin`` with a real OS pipe carrying ``payload`` as JSON.

    ``session_start.get_session_context`` polls ``sys.stdin`` via
    ``select.select`` to peek without blocking. ``select.select`` requires
    a real file descriptor (``fileno()``), which ``io.StringIO`` does not
    expose - hence the OS pipe instead of a StringIO. The write end is
    closed immediately so ``json.load`` sees EOF after the payload.
    """
    import os as _os
    import sys as _sys

    r, w = _os.pipe()
    _os.write(w, json.dumps(payload).encode())
    _os.close(w)
    monkeypatch.setattr(_sys, "stdin", _os.fdopen(r, "r"))


# ── session_start ─────────────────────────────────────────────────────────


class TestSessionStart:
    def test_find_task_for_cwd_integration(self, tmp_path, monkeypatch, capsys):
        """session_start calls find_task_for_cwd and outputs context for a match."""
        # Redirect Path.home() so state-file writes land in tmp_path, not
        # the real ~/.claude/hooks/state/ (prevents test pollution).
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        # Build a mock task
        mock_task = SimpleNamespace(
            id=1,
            name="my-task",
            status="active",
            jira_key=None,
            repo_id=10,
            full_path="active/my-task",
        )
        mock_repo = SimpleNamespace(short_name="my-repo", path="/fake/repo")

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = mock_task
        mock_db.get_repo.return_value = mock_repo
        mock_db.get_task_time.return_value = 0
        mock_db.format_duration.return_value = "0m"

        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-42")
        monkeypatch.setattr("os.getcwd", lambda: "/fake/repo")

        with patch.dict("sys.modules", {"orbit_db": MagicMock(TaskDB=lambda: mock_db)}):
            # Re-import to pick up mocked module
            import importlib
            import hooks.session_start as mod

            importlib.reload(mod)
            mod.main()

        output = capsys.readouterr().out
        assert "my-task" in output
        assert "Active Task Detected" in output

    def test_writes_cwd_session_pointer(self, tmp_path, monkeypatch):
        """write_cwd_session_pointer records {sessionId, cwd, updatedAt} keyed by cwd."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        fake_cwd = tmp_path / "some" / "repo"
        fake_cwd.mkdir(parents=True)
        monkeypatch.chdir(fake_cwd)

        import importlib
        import hooks.session_start as mod

        importlib.reload(mod)
        mod.write_cwd_session_pointer("abc-123")

        cwd_key = str(fake_cwd).replace("/", "-")
        pointer_file = tmp_path / ".claude" / "hooks" / "state" / "cwd-session" / f"{cwd_key}.json"
        assert pointer_file.exists()

        data = json.loads(pointer_file.read_text())
        assert data["sessionId"] == "abc-123"
        assert data["cwd"] == str(fake_cwd)
        assert "updatedAt" in data

    def test_cwd_session_pointer_skipped_when_no_session_id(self, tmp_path, monkeypatch):
        """Empty session_id is a no-op - no file created."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        monkeypatch.chdir(tmp_path)

        import importlib
        import hooks.session_start as mod

        importlib.reload(mod)
        mod.write_cwd_session_pointer("")

        pointer_dir = tmp_path / ".claude" / "hooks" / "state" / "cwd-session"
        # Directory should not be created when session_id is empty.
        assert not pointer_dir.exists()

    def test_outputs_context_message(self, tmp_path, monkeypatch, capsys):
        """session_start prints context including task name and status."""
        # Redirect Path.home() so state-file writes land in tmp_path, not
        # the real ~/.claude/hooks/state/ (prevents test pollution).
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        mock_task = SimpleNamespace(
            id=5,
            name="context-task",
            status="active",
            jira_key="PROJ-999",
            repo_id=1,
            full_path="active/context-task",
        )
        mock_repo = SimpleNamespace(short_name="repo", path="/repo")

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = mock_task
        mock_db.get_repo.return_value = mock_repo
        mock_db.get_task_time.return_value = 3600
        mock_db.format_duration.return_value = "1h 0m"

        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-99")
        monkeypatch.setattr("os.getcwd", lambda: "/repo")

        with patch.dict("sys.modules", {"orbit_db": MagicMock(TaskDB=lambda: mock_db)}):
            import importlib
            import hooks.session_start as mod

            importlib.reload(mod)
            mod.main()

        output = capsys.readouterr().out
        assert "context-task" in output
        assert "PROJ-999" in output
        assert "1h 0m" in output


class TestSessionStartResumePickup:
    """Tests for ``_pickup_previous_session_binding`` and the resume-aware main flow.

    Resume changes Claude Code's session_id; without these helpers the previous
    session's project_state binding is orphaned and the statusline drops the
    project field until /orbit:go is re-run. The pickup logic copies the
    binding to the new sid before write_cwd_session_pointer overwrites the
    breadcrumb that points back to the old sid.

    Test fixtures use orbit_db's real ``init_hooks_state_db_schema`` rather
    than hand-rolled DDL so a future column add in production is caught here
    instead of silently passing because the test seeded its own minimal shape.
    """

    @staticmethod
    def _redirect_state(monkeypatch, home: Path) -> Path:
        """Redirect Path.home() and orbit_db.HOOKS_STATE_DB_PATH onto ``home``.

        ``HOOKS_STATE_DB_PATH`` is captured at orbit_db import time using the
        real ``Path.home()``, so monkeypatching ``pathlib.Path.home`` alone
        leaves orbit_db reading the user's real DB. Patch both.

        Returns the redirected hooks-state.db path for assertion convenience.
        """
        import orbit_db  # type: ignore[import-not-found]

        monkeypatch.setattr("pathlib.Path.home", lambda: home)
        db_path = home / ".claude" / "hooks-state.db"
        monkeypatch.setattr(orbit_db, "HOOKS_STATE_DB_PATH", db_path)
        return db_path

    @classmethod
    def _seed_project_state(cls, home: Path, rows: list[tuple[str, str]]) -> Path:
        """Create the hooks-state.db schema (via the production init function)
        and insert (sid, project) rows.

        Importing the real schema function instead of hand-rolling the DDL
        means tests catch column drift the moment production schema changes.
        """
        import sqlite3 as _sqlite3

        from orbit_db import init_hooks_state_db_schema  # type: ignore[import-not-found]

        db_path = home / ".claude" / "hooks-state.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = _sqlite3.connect(str(db_path))
        try:
            init_hooks_state_db_schema(conn)
            conn.executemany(
                "INSERT INTO project_state (session_id, project_name) VALUES (?, ?)",
                rows,
            )
            conn.commit()
        finally:
            conn.close()
        return db_path

    @staticmethod
    def _seed_pointer(home: Path, cwd: Path, session_id: str) -> Path:
        """Write a cwd-session pointer file as if a previous session owned this cwd."""
        cwd_key = str(cwd).replace("/", "-")
        pointer_dir = home / ".claude" / "hooks" / "state" / "cwd-session"
        pointer_dir.mkdir(parents=True, exist_ok=True)
        pointer_file = pointer_dir / f"{cwd_key}.json"
        pointer_file.write_text(
            json.dumps({"sessionId": session_id, "cwd": str(cwd), "updatedAt": "ignored"})
        )
        return pointer_file

    def _reload_module(self):
        import importlib
        import hooks.session_start as mod

        importlib.reload(mod)
        return mod

    def test_pickup_returns_project_when_pointer_and_state_match(self, tmp_path, monkeypatch):
        """Happy path: prev sid in pointer + project_state row -> returns project name."""
        self._redirect_state(monkeypatch, tmp_path)
        cwd = tmp_path / "repo"
        cwd.mkdir()
        self._seed_pointer(tmp_path, cwd, "prev-sid")
        self._seed_project_state(tmp_path, [("prev-sid", "carried-over-project")])

        mod = self._reload_module()
        assert mod._pickup_previous_session_binding(cwd, "new-sid") == "carried-over-project"

    def test_pickup_returns_none_when_pointer_missing(self, tmp_path, monkeypatch):
        """Fresh start at a cwd that never had a session - no-op."""
        self._redirect_state(monkeypatch, tmp_path)
        cwd = tmp_path / "fresh"
        cwd.mkdir()

        mod = self._reload_module()
        assert mod._pickup_previous_session_binding(cwd, "new-sid") is None

    def test_pickup_returns_none_when_pointer_too_old(self, tmp_path, monkeypatch):
        """Pointer mtime older than 24h is treated as fresh start."""
        self._redirect_state(monkeypatch, tmp_path)
        cwd = tmp_path / "stale"
        cwd.mkdir()
        pointer_file = self._seed_pointer(tmp_path, cwd, "stale-sid")
        self._seed_project_state(tmp_path, [("stale-sid", "abandoned-project")])

        # Backdate mtime to 25h ago.
        old_time = time.time() - (25 * 3600)
        os.utime(pointer_file, (old_time, old_time))

        mod = self._reload_module()
        assert mod._pickup_previous_session_binding(cwd, "new-sid") is None

    def test_pickup_returns_none_when_pointer_sid_matches_new_sid(self, tmp_path, monkeypatch):
        """Defensive: same sid in pointer and incoming - never resurrect ourselves."""
        self._redirect_state(monkeypatch, tmp_path)
        cwd = tmp_path / "self"
        cwd.mkdir()
        self._seed_pointer(tmp_path, cwd, "same-sid")
        self._seed_project_state(tmp_path, [("same-sid", "my-project")])

        mod = self._reload_module()
        assert mod._pickup_previous_session_binding(cwd, "same-sid") is None

    def test_pickup_returns_none_when_no_project_bound_to_prev_sid(self, tmp_path, monkeypatch):
        """Pointer present but project_state has no row - prev session never ran /orbit:go."""
        self._redirect_state(monkeypatch, tmp_path)
        cwd = tmp_path / "unbound"
        cwd.mkdir()
        self._seed_pointer(tmp_path, cwd, "unbound-sid")
        self._seed_project_state(tmp_path, [])

        mod = self._reload_module()
        assert mod._pickup_previous_session_binding(cwd, "new-sid") is None

    def test_pickup_returns_none_when_pointer_missing_session_id_key(self, tmp_path, monkeypatch):
        """Pointer JSON valid but lacks 'sessionId' key - the not-prev_session_id branch.

        A future schema change or a manually edited pointer can produce this
        shape. Without explicit coverage, dropping the ``not isinstance(...)``
        guard would silently query the DB with None and the bug would slip.
        """
        self._redirect_state(monkeypatch, tmp_path)
        cwd = tmp_path / "no-sid-key"
        cwd.mkdir()
        cwd_key = str(cwd).replace("/", "-")
        pointer_dir = tmp_path / ".claude" / "hooks" / "state" / "cwd-session"
        pointer_dir.mkdir(parents=True, exist_ok=True)
        (pointer_dir / f"{cwd_key}.json").write_text(
            json.dumps({"cwd": str(cwd), "updatedAt": "x"})
        )
        self._seed_project_state(tmp_path, [])

        mod = self._reload_module()
        assert mod._pickup_previous_session_binding(cwd, "new-sid") is None

    def test_pickup_returns_none_when_pointer_session_id_too_long(self, tmp_path, monkeypatch):
        """A corrupt pointer with a multi-MB sessionId is rejected before the SQL bind.

        Defends against the trickle of garbage data into the DB and bounds the
        memory footprint of the pickup path.
        """
        self._redirect_state(monkeypatch, tmp_path)
        cwd = tmp_path / "huge-sid"
        cwd.mkdir()
        cwd_key = str(cwd).replace("/", "-")
        pointer_dir = tmp_path / ".claude" / "hooks" / "state" / "cwd-session"
        pointer_dir.mkdir(parents=True, exist_ok=True)
        (pointer_dir / f"{cwd_key}.json").write_text(
            json.dumps({"sessionId": "x" * 10000, "cwd": str(cwd)})
        )
        self._seed_project_state(tmp_path, [])

        mod = self._reload_module()
        assert mod._pickup_previous_session_binding(cwd, "new-sid") is None

    def test_pickup_corrupt_pointer_is_unlinked(self, tmp_path, monkeypatch, capsys):
        """Malformed JSON returns None AND deletes the corrupt file so the next
        resume gets a clean slate. Also surfaces a stderr breadcrumb so the
        user knows their pointer was reset."""
        self._redirect_state(monkeypatch, tmp_path)
        cwd = tmp_path / "corrupt"
        cwd.mkdir()
        cwd_key = str(cwd).replace("/", "-")
        pointer_dir = tmp_path / ".claude" / "hooks" / "state" / "cwd-session"
        pointer_dir.mkdir(parents=True, exist_ok=True)
        pointer_file = pointer_dir / f"{cwd_key}.json"
        pointer_file.write_text("not-valid-json{{{")

        mod = self._reload_module()
        assert mod._pickup_previous_session_binding(cwd, "new-sid") is None
        assert not pointer_file.exists(), "corrupt pointer should be unlinked"
        assert "corrupt cwd-session pointer" in capsys.readouterr().err

    def test_pickup_returns_none_on_sqlite_error(self, tmp_path, monkeypatch):
        """A sqlite3.Error during the project_state lookup must not propagate.

        The docstring promises silent handling; without this test, a refactor
        that drops the except clause would be undetectable.
        """
        self._redirect_state(monkeypatch, tmp_path)
        cwd = tmp_path / "db-broken"
        cwd.mkdir()
        self._seed_pointer(tmp_path, cwd, "prev-sid")
        # No DB created at all - sqlite3.connect will succeed but the SELECT
        # raises OperationalError ('no such table'). That hits the
        # OperationalError branch which is silent (no stderr) and returns None.

        mod = self._reload_module()
        assert mod._pickup_previous_session_binding(cwd, "new-sid") is None

    def test_bind_works_on_fresh_install_without_table(self, tmp_path, monkeypatch):
        """Fresh install (dashboard never ran) - bind must auto-create the schema.

        Without ``init_hooks_state_db_schema``, the INSERT raises
        ``OperationalError: no such table`` which the bare ``except sqlite3.Error``
        swallows, and the resume binding silently no-ops. This is exactly the
        Critical bug the review flagged.
        """
        import sqlite3 as _sqlite3

        db_path = self._redirect_state(monkeypatch, tmp_path)
        # No _seed_project_state call - DB and table do not exist yet.

        mod = self._reload_module()
        mod._bind_session_to_project("new-sid", "my-project")

        conn = _sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT project_name FROM project_state WHERE session_id = ?",
                ("new-sid",),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None and row[0] == "my-project"

    def test_bind_writes_project_state_and_per_session_pointer(self, tmp_path, monkeypatch):
        """_bind_session_to_project upserts the DB row and writes projects/<sid>.json."""
        import sqlite3 as _sqlite3

        db_path = self._redirect_state(monkeypatch, tmp_path)
        self._seed_project_state(tmp_path, [])

        mod = self._reload_module()
        mod._bind_session_to_project("new-sid", "my-project")

        conn = _sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT project_name FROM project_state WHERE session_id = ?",
                ("new-sid",),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None and row[0] == "my-project"

        pointer_file = tmp_path / ".claude" / "hooks" / "state" / "projects" / "new-sid.json"
        assert pointer_file.exists()
        data = json.loads(pointer_file.read_text())
        assert data["projectName"] == "my-project"
        assert data["sessionId"] == "new-sid"

    def test_bind_upserts_when_session_id_already_bound(self, tmp_path, monkeypatch):
        """Calling bind twice replaces the project_name (ON CONFLICT DO UPDATE)."""
        import sqlite3 as _sqlite3

        db_path = self._redirect_state(monkeypatch, tmp_path)
        self._seed_project_state(tmp_path, [("dup-sid", "stale-project")])

        mod = self._reload_module()
        mod._bind_session_to_project("dup-sid", "fresh-project")

        conn = _sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT project_name FROM project_state WHERE session_id = ?",
                ("dup-sid",),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None and row[0] == "fresh-project"

    def test_bind_logs_to_stderr_on_db_failure_and_skips_pointer(
        self, tmp_path, monkeypatch, capsys
    ):
        """When the DB write fails, log a breadcrumb AND skip the pointer write.

        Silent failure here was the load-bearing review finding: without a
        stderr trail, the user's statusline goes blank with no diagnostic.
        Per-session pointer must NOT be written when DB fails - that's the
        documented invariant (DB row is the source of truth).
        """
        import sqlite3 as _sqlite3

        self._redirect_state(monkeypatch, tmp_path)

        def _broken_connect(*args, **kwargs):
            raise _sqlite3.OperationalError("simulated DB failure")

        monkeypatch.setattr(_sqlite3, "connect", _broken_connect)

        mod = self._reload_module()
        mod._bind_session_to_project("new-sid", "my-project")

        # Stderr breadcrumb surfaced.
        err = capsys.readouterr().err
        assert "bind_session failed" in err
        assert "new-sid" in err

        # Pointer file NOT written when DB failed.
        pointer_file = tmp_path / ".claude" / "hooks" / "state" / "projects" / "new-sid.json"
        assert not pointer_file.exists(), "pointer must not be written when DB write fails"

    def test_main_carries_project_across_resume(self, tmp_path, monkeypatch):
        """Full main() flow: new sid inherits the project bound to the previous sid.

        The hook input carries ``source="resume"`` here, which is the only
        case (alongside ``"compact"``) that triggers inheritance under the
        post-fix contract. Tests for the gated-out cases live in
        :class:`TestSessionStartSourceGating` below.
        """
        import sqlite3 as _sqlite3

        db_path = self._redirect_state(monkeypatch, tmp_path)
        cwd = tmp_path / "resume" / "repo"
        cwd.mkdir(parents=True)
        monkeypatch.chdir(cwd)
        monkeypatch.setattr("os.getcwd", lambda: str(cwd))
        # Pipe the SessionStart payload through real stdin so
        # ``get_session_context`` sees both ``session_id`` and ``source``.
        # ``CLAUDE_SESSION_ID`` is intentionally NOT set so the stdin path
        # is exercised end-to-end.
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        _patch_stdin_payload(monkeypatch, {"session_id": "new-sid", "source": "resume"})

        self._seed_pointer(tmp_path, cwd, "prev-sid")
        self._seed_project_state(tmp_path, [("prev-sid", "carried-over")])

        mod = self._reload_module()
        # Replace TaskDB on the real orbit_db module with a no-task mock so
        # find_task_for_cwd returns None (we're testing the pickup path, not
        # the existing task-detection path).
        import orbit_db  # type: ignore[import-not-found]

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = None
        # Mock get_task_by_name to None so _is_cwd_compatible_with_inherited_project
        # takes the conservative-inherit branch ("task not found in DB"). The
        # test is about the resume-pickup path, not the cwd-validation gate -
        # the gate has its own coverage in TestPickupCwdCompatibilityGate.
        mock_db.get_task_by_name.return_value = None
        monkeypatch.setattr(orbit_db, "TaskDB", lambda: mock_db)
        mod.main()

        # New session inherited the project binding.
        conn = _sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT project_name FROM project_state WHERE session_id = ?",
                ("new-sid",),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None and row[0] == "carried-over"

        # Per-session pointer file written for the new sid.
        pointer_file = tmp_path / ".claude" / "hooks" / "state" / "projects" / "new-sid.json"
        assert pointer_file.exists()

        # Existing behavior preserved: cwd-session pointer is overwritten with new sid.
        cwd_key = str(cwd).replace("/", "-")
        cwd_pointer = tmp_path / ".claude" / "hooks" / "state" / "cwd-session" / f"{cwd_key}.json"
        assert json.loads(cwd_pointer.read_text())["sessionId"] == "new-sid"


class TestPickupCwdCompatibilityGate:
    """``_pickup_previous_session_binding`` must validate the inherited project
    against the current cwd before binding.

    Bug scenario: previous session at ``/Users/tbrami/work`` (umbrella dir
    holding many repos) was bound to ``project-X`` whose actual repo path is
    ``/Users/tbrami/work/some-repo``. When a new session resumes at the same
    umbrella cwd, the prior logic blindly inherited ``project-X`` even though
    the new session is sitting in a parent directory and might be intending a
    completely different project. The inherited binding then routed the new
    session's heartbeats to the wrong task and made the statusline lie.

    The gate: only inherit when the inherited project's repo path is the
    current cwd OR an ancestor of it (i.e. the cwd is inside the project's
    repo). If the repo lives *under* the cwd (umbrella case) or in an
    unrelated location, skip the inherit and let the new session start clean.

    Non-coding tasks (no repo_id) and lookup failures (orbit_db unavailable,
    task renamed/deleted, repo deleted) are treated conservatively: inherit
    proceeds, preserving the prior behavior. The gate only fires when we have
    affirmative evidence the inherit is wrong.
    """

    @staticmethod
    def _redirect_state(monkeypatch, home: Path) -> Path:
        import orbit_db  # type: ignore[import-not-found]

        monkeypatch.setattr("pathlib.Path.home", lambda: home)
        db_path = home / ".claude" / "hooks-state.db"
        monkeypatch.setattr(orbit_db, "HOOKS_STATE_DB_PATH", db_path)
        return db_path

    @staticmethod
    def _seed_pointer(home: Path, cwd: Path, session_id: str) -> Path:
        cwd_key = str(cwd).replace("/", "-")
        pointer_dir = home / ".claude" / "hooks" / "state" / "cwd-session"
        pointer_dir.mkdir(parents=True, exist_ok=True)
        pointer_file = pointer_dir / f"{cwd_key}.json"
        pointer_file.write_text(
            json.dumps({"sessionId": session_id, "cwd": str(cwd), "updatedAt": "x"})
        )
        return pointer_file

    @staticmethod
    def _seed_project_state(home: Path, rows: list[tuple[str, str]]) -> Path:
        import sqlite3 as _sqlite3

        from orbit_db import init_hooks_state_db_schema  # type: ignore[import-not-found]

        db_path = home / ".claude" / "hooks-state.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = _sqlite3.connect(str(db_path))
        try:
            init_hooks_state_db_schema(conn)
            if rows:
                conn.executemany(
                    "INSERT INTO project_state (session_id, project_name) VALUES (?, ?)",
                    rows,
                )
            conn.commit()
        finally:
            conn.close()
        return db_path

    @staticmethod
    def _mock_taskdb(monkeypatch, *, task=None, repo=None) -> None:
        """Replace ``orbit_db.TaskDB`` with a mock whose ``get_task_by_name``
        returns ``task`` and ``get_repo`` returns ``repo``.

        Pass ``task=None`` to simulate "task not found" (renamed/deleted).
        Pass ``repo=None`` with a task to simulate "task exists but repo
        orphaned" (deleted repo row).
        """
        import orbit_db  # type: ignore[import-not-found]

        mock_db = MagicMock()
        mock_db.get_task_by_name.return_value = task
        mock_db.get_repo.return_value = repo
        mock_db.find_task_for_cwd.return_value = None
        monkeypatch.setattr(orbit_db, "TaskDB", lambda: mock_db)

    def _reload_module(self):
        import importlib
        import hooks.session_start as mod

        importlib.reload(mod)
        return mod

    def test_pickup_skips_when_repo_is_subdirectory_of_cwd_umbrella(
        self, tmp_path, monkeypatch, capsys
    ):
        """REGRESSION: umbrella-cwd false positive.

        Previous session at ``/work`` was on a project whose repo is
        ``/work/repo-x``. New session at the SAME umbrella cwd ``/work``
        must NOT inherit - the spatial signal says the user is in the
        umbrella, not the project repo. Fails on master, passes on fix.
        """
        self._redirect_state(monkeypatch, tmp_path)
        umbrella = tmp_path / "work"
        umbrella.mkdir()
        repo_x = umbrella / "repo-x"
        repo_x.mkdir()

        self._seed_pointer(tmp_path, umbrella, "prev-sid")
        self._seed_project_state(tmp_path, [("prev-sid", "project-x")])

        task = SimpleNamespace(
            id=1, name="project-x", repo_id=42, full_path="active/project-x"
        )
        repo = SimpleNamespace(id=42, path=str(repo_x))
        self._mock_taskdb(monkeypatch, task=task, repo=repo)

        mod = self._reload_module()
        result = mod._pickup_previous_session_binding(umbrella, "new-sid")
        assert result is None, (
            "Umbrella cwd /work must not inherit project-x whose repo is at "
            f"/work/repo-x. Got {result!r}."
        )
        # Surface a stderr breadcrumb so the user knows why their statusline
        # went blank instead of inheriting.
        assert "skipping inherit" in capsys.readouterr().err.lower()

    def test_pickup_inherits_when_cwd_equals_repo_path(
        self, tmp_path, monkeypatch
    ):
        """Happy path: previous session was at the repo, new session is also
        at the repo. Inherit proceeds - this is the canonical "resume the
        project I was working on" case.
        """
        self._redirect_state(monkeypatch, tmp_path)
        repo_dir = tmp_path / "repo-x"
        repo_dir.mkdir()

        self._seed_pointer(tmp_path, repo_dir, "prev-sid")
        self._seed_project_state(tmp_path, [("prev-sid", "project-x")])

        task = SimpleNamespace(
            id=1, name="project-x", repo_id=42, full_path="active/project-x"
        )
        repo = SimpleNamespace(id=42, path=str(repo_dir))
        self._mock_taskdb(monkeypatch, task=task, repo=repo)

        mod = self._reload_module()
        assert mod._pickup_previous_session_binding(repo_dir, "new-sid") == "project-x"

    def test_pickup_inherits_when_cwd_is_descendant_of_repo(
        self, tmp_path, monkeypatch
    ):
        """Working inside a subdir of the repo (e.g. ``repo-x/src``) - the
        spatial signal still ties to the repo. Inherit proceeds.
        """
        self._redirect_state(monkeypatch, tmp_path)
        repo_dir = tmp_path / "repo-x"
        subdir = repo_dir / "src" / "deep"
        subdir.mkdir(parents=True)

        self._seed_pointer(tmp_path, subdir, "prev-sid")
        self._seed_project_state(tmp_path, [("prev-sid", "project-x")])

        task = SimpleNamespace(
            id=1, name="project-x", repo_id=42, full_path="active/project-x"
        )
        repo = SimpleNamespace(id=42, path=str(repo_dir))
        self._mock_taskdb(monkeypatch, task=task, repo=repo)

        mod = self._reload_module()
        assert mod._pickup_previous_session_binding(subdir, "new-sid") == "project-x"

    def test_pickup_inherits_when_task_has_no_repo(self, tmp_path, monkeypatch):
        """Non-coding task (repo_id is None) - no repo to validate against.
        Inherit proceeds; the cwd-pointer match is the only signal we have.
        """
        self._redirect_state(monkeypatch, tmp_path)
        cwd = tmp_path / "anywhere"
        cwd.mkdir()

        self._seed_pointer(tmp_path, cwd, "prev-sid")
        self._seed_project_state(tmp_path, [("prev-sid", "meeting-notes")])

        task = SimpleNamespace(
            id=1, name="meeting-notes", repo_id=None, full_path="global/meeting-notes"
        )
        self._mock_taskdb(monkeypatch, task=task, repo=None)

        mod = self._reload_module()
        assert mod._pickup_previous_session_binding(cwd, "new-sid") == "meeting-notes"

    def test_pickup_inherits_when_task_lookup_returns_none(
        self, tmp_path, monkeypatch
    ):
        """Task was renamed or deleted - get_task_by_name returns None.
        Conservative fallback: inherit proceeds with whatever project_state
        says. (The user can re-run /orbit:go to correct if wrong.)
        """
        self._redirect_state(monkeypatch, tmp_path)
        cwd = tmp_path / "anywhere"
        cwd.mkdir()

        self._seed_pointer(tmp_path, cwd, "prev-sid")
        self._seed_project_state(tmp_path, [("prev-sid", "deleted-project")])

        self._mock_taskdb(monkeypatch, task=None, repo=None)

        mod = self._reload_module()
        assert mod._pickup_previous_session_binding(cwd, "new-sid") == "deleted-project"

    def test_pickup_inherits_when_taskdb_raises(self, tmp_path, monkeypatch):
        """TaskDB connection error - conservative fallback: inherit.

        We don't want a transient DB issue to silently strip the inherit
        and confuse the user with a blank statusline. The cwd-pointer match
        is still a strong-enough signal on its own.
        """
        self._redirect_state(monkeypatch, tmp_path)
        cwd = tmp_path / "anywhere"
        cwd.mkdir()

        self._seed_pointer(tmp_path, cwd, "prev-sid")
        self._seed_project_state(tmp_path, [("prev-sid", "some-project")])

        import orbit_db  # type: ignore[import-not-found]

        def _raising_taskdb():
            raise RuntimeError("simulated DB connection failure")

        monkeypatch.setattr(orbit_db, "TaskDB", _raising_taskdb)

        mod = self._reload_module()
        assert mod._pickup_previous_session_binding(cwd, "new-sid") == "some-project"

    def test_pickup_skips_when_repo_is_unrelated_to_cwd(
        self, tmp_path, monkeypatch
    ):
        """Previous project's repo is in a completely unrelated path (no
        spatial relationship to cwd in either direction). Skip the inherit.
        """
        self._redirect_state(monkeypatch, tmp_path)
        cwd = tmp_path / "work"
        cwd.mkdir()
        unrelated_repo = tmp_path / "elsewhere" / "repo"
        unrelated_repo.mkdir(parents=True)

        self._seed_pointer(tmp_path, cwd, "prev-sid")
        self._seed_project_state(tmp_path, [("prev-sid", "off-topic")])

        task = SimpleNamespace(
            id=1, name="off-topic", repo_id=42, full_path="active/off-topic"
        )
        repo = SimpleNamespace(id=42, path=str(unrelated_repo))
        self._mock_taskdb(monkeypatch, task=task, repo=repo)

        mod = self._reload_module()
        assert mod._pickup_previous_session_binding(cwd, "new-sid") is None


class TestSessionStartSourceGating:
    """Inheritance must fire ONLY on resume/compact, never on startup/clear.

    The umbrella-cwd false positive: a fresh ``startup`` session in a cwd
    like ``~/work`` (which has many active orbit projects under it) used to
    inherit whatever project the previous session in that cwd was working
    on. Result: the new conversation got mis-tagged, statusline showed the
    wrong project, and heartbeats were attributed to the wrong task.

    The fix gates the inheritance on Claude Code's ``source`` field
    (``startup`` / ``resume`` / ``clear`` / ``compact``). Only ``resume``
    and ``compact`` count as a continuation of prior work; the other two
    are new conversations that should start blank.
    """

    @staticmethod
    def _redirect_state(monkeypatch, home: Path) -> Path:
        """Mirror :class:`TestSessionStartResumePickup._redirect_state`."""
        import orbit_db  # type: ignore[import-not-found]

        monkeypatch.setattr("pathlib.Path.home", lambda: home)
        db_path = home / ".claude" / "hooks-state.db"
        monkeypatch.setattr(orbit_db, "HOOKS_STATE_DB_PATH", db_path)
        return db_path

    @staticmethod
    def _seed_for_inheritance(home: Path, cwd: Path) -> None:
        """Seed the cwd-pointer + project_state row that inheritance reads."""
        import sqlite3 as _sqlite3
        from orbit_db import init_hooks_state_db_schema  # type: ignore[import-not-found]

        cwd_key = str(cwd).replace("/", "-")
        pointer_dir = home / ".claude" / "hooks" / "state" / "cwd-session"
        pointer_dir.mkdir(parents=True, exist_ok=True)
        (pointer_dir / f"{cwd_key}.json").write_text(
            json.dumps({"sessionId": "prev-sid", "cwd": str(cwd), "updatedAt": "x"})
        )

        db_path = home / ".claude" / "hooks-state.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = _sqlite3.connect(str(db_path))
        try:
            init_hooks_state_db_schema(conn)
            conn.execute(
                "INSERT INTO project_state (session_id, project_name) VALUES (?, ?)",
                ("prev-sid", "previous-project"),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _wire_no_task(monkeypatch) -> None:
        """Stub TaskDB so the existing find_task_for_cwd path is a no-op.

        We are testing the gating of the pickup branch only; the task-detection
        branch has its own coverage in :class:`TestSessionStart`. ``get_task_by_name``
        also returns None so the cwd-compatibility gate (introduced for the
        umbrella-cwd fix) takes the conservative-inherit branch and does not
        spuriously skip valid pickups under test.
        """
        import orbit_db  # type: ignore[import-not-found]

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = None
        mock_db.get_task_by_name.return_value = None
        monkeypatch.setattr(orbit_db, "TaskDB", lambda: mock_db)

    def _reload_module(self):
        import importlib
        import hooks.session_start as mod

        importlib.reload(mod)
        return mod

    @pytest.mark.parametrize(
        "source,should_inherit",
        [
            ("startup", False),
            ("clear", False),
            ("resume", True),
            ("compact", True),
        ],
    )
    def test_main_inherits_only_on_resume_or_compact(
        self, source, should_inherit, tmp_path, monkeypatch
    ):
        """Resume/compact -> inheritance fires. Startup/clear -> no binding for new sid.

        This is the load-bearing assertion: a fresh ``startup`` in an umbrella
        cwd must not steal the previous session's project name. A regression
        here re-introduces the original bug.
        """
        import sqlite3 as _sqlite3

        db_path = self._redirect_state(monkeypatch, tmp_path)
        cwd = tmp_path / "umbrella" / "repo"
        cwd.mkdir(parents=True)
        monkeypatch.chdir(cwd)
        monkeypatch.setattr("os.getcwd", lambda: str(cwd))
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        _patch_stdin_payload(monkeypatch, {"session_id": "new-sid", "source": source})

        self._seed_for_inheritance(tmp_path, cwd)
        self._wire_no_task(monkeypatch)

        mod = self._reload_module()
        mod.main()

        conn = _sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT project_name FROM project_state WHERE session_id = ?",
                ("new-sid",),
            ).fetchone()
        finally:
            conn.close()

        if should_inherit:
            assert row is not None and row[0] == "previous-project", (
                f"source={source!r} should have inherited 'previous-project'"
            )
        else:
            assert row is None, (
                f"source={source!r} must NOT bind new-sid - umbrella-cwd false "
                f"positive. Got binding {row[0]!r}."
            )

        # The cwd-session pointer is overwritten regardless of source. That
        # is the live "who owns this cwd right now" signal; gating it on
        # source would break /orbit:save in a fresh session.
        cwd_key = str(cwd).replace("/", "-")
        cwd_pointer = tmp_path / ".claude" / "hooks" / "state" / "cwd-session" / f"{cwd_key}.json"
        assert json.loads(cwd_pointer.read_text())["sessionId"] == "new-sid"

    def test_main_does_not_inherit_when_source_field_missing(self, tmp_path, monkeypatch):
        """Older Claude Code versions or direct invocations omit ``source``.

        Default to no-inherit so we fail to "no project" instead of "wrong
        project". A blank statusline is recoverable; mis-attributed task
        history is not.
        """
        import sqlite3 as _sqlite3

        db_path = self._redirect_state(monkeypatch, tmp_path)
        cwd = tmp_path / "no-source" / "repo"
        cwd.mkdir(parents=True)
        monkeypatch.chdir(cwd)
        monkeypatch.setattr("os.getcwd", lambda: str(cwd))
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        # Payload omits ``source`` entirely.
        _patch_stdin_payload(monkeypatch, {"session_id": "new-sid"})

        self._seed_for_inheritance(tmp_path, cwd)
        self._wire_no_task(monkeypatch)

        mod = self._reload_module()
        mod.main()

        conn = _sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT project_name FROM project_state WHERE session_id = ?",
                ("new-sid",),
            ).fetchone()
        finally:
            conn.close()
        assert row is None, "missing source must not trigger inheritance"

    def test_main_emits_breadcrumb_on_inherit(self, tmp_path, monkeypatch, capsys):
        """A successful inherit logs a stderr breadcrumb naming project + source.

        The breadcrumb is the user's only signal that auto-binding fired. If
        the statusline ever shows an unexpected project, this line in the
        session transcript JSONL under ``~/.claude/projects/`` is the first
        place to look. Format-locking via
        substring asserts (not a full-string match) so the wording can
        evolve without test churn; the three invariants - the "inherited"
        verb, the project name, and the source value - stay.
        """
        self._redirect_state(monkeypatch, tmp_path)
        cwd = tmp_path / "breadcrumb" / "repo"
        cwd.mkdir(parents=True)
        monkeypatch.chdir(cwd)
        monkeypatch.setattr("os.getcwd", lambda: str(cwd))
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        _patch_stdin_payload(monkeypatch, {"session_id": "new-sid", "source": "resume"})

        self._seed_for_inheritance(tmp_path, cwd)
        self._wire_no_task(monkeypatch)

        mod = self._reload_module()
        mod.main()

        err = capsys.readouterr().err
        # Lock canonical phrase order so a refactor that splits the
        # breadcrumb (e.g. printing project on a separate line from
        # source) is caught. Substring asserts on individual tokens
        # would false-pass on a poorly-ordered re-emit.
        assert "orbit: inherited project=previous-project" in err
        assert "source=resume" in err

    def test_main_emits_no_breadcrumb_when_gated_out(self, tmp_path, monkeypatch, capsys):
        """``startup`` short-circuits before the breadcrumb. Stderr stays quiet."""
        self._redirect_state(monkeypatch, tmp_path)
        cwd = tmp_path / "gated" / "repo"
        cwd.mkdir(parents=True)
        monkeypatch.chdir(cwd)
        monkeypatch.setattr("os.getcwd", lambda: str(cwd))
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        _patch_stdin_payload(monkeypatch, {"session_id": "new-sid", "source": "startup"})

        self._seed_for_inheritance(tmp_path, cwd)
        self._wire_no_task(monkeypatch)

        mod = self._reload_module()
        mod.main()

        # The "inherited" verb is the load-bearing token to keep out of
        # stderr on a gated-out session. The new diagnostic breadcrumbs
        # ("no previous binding", "unknown source") use different verbs,
        # so this assert specifically protects the success-path phrase
        # from leaking into the gated-out path.
        assert "inherited" not in capsys.readouterr().err

    def test_main_logs_breadcrumb_when_resume_has_no_previous_binding(
        self, tmp_path, monkeypatch, capsys
    ):
        """Resume + nothing to inherit emits the "no previous binding" diagnostic.

        This is the user-visible failure mode the original bug fix did
        not address: source=resume but the cwd has no pointer or the
        previous session never bound a project. Without this breadcrumb,
        a user whose statusline goes blank on resume has zero log signal
        to debug from. Failing closed is correct behavior; failing
        invisibly is not.
        """
        self._redirect_state(monkeypatch, tmp_path)
        cwd = tmp_path / "no-prev" / "repo"
        cwd.mkdir(parents=True)
        monkeypatch.chdir(cwd)
        monkeypatch.setattr("os.getcwd", lambda: str(cwd))
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        _patch_stdin_payload(monkeypatch, {"session_id": "new-sid", "source": "resume"})

        # No _seed_for_inheritance: the cwd has no pointer file and the
        # DB has no project_state row, so _pickup_previous_session_binding
        # returns None.
        self._wire_no_task(monkeypatch)

        mod = self._reload_module()
        mod.main()

        err = capsys.readouterr().err
        assert "no previous binding to inherit" in err
        assert "source=resume" in err
        # The success-path "inherited project=" phrase MUST NOT appear
        # since nothing was inherited; this guards against a refactor
        # that prints the success line unconditionally.
        assert "inherited project=" not in err

    def test_main_logs_breadcrumb_for_unknown_source(self, tmp_path, monkeypatch, capsys):
        """An unrecognized source value (future Claude Code addition) is logged.

        If Anthropic ships a new SessionStart source variant beyond the
        current four (startup/resume/clear/compact), the gate fails
        closed correctly but inheritance silently stops working. This
        breadcrumb makes contract drift visible without users having to
        grep the hook source. Replace ``"agent"`` with whatever the
        actual new value turns out to be when the day comes; the test's
        intent is "any non-allowlisted, non-known source is observable",
        not the specific string.
        """
        self._redirect_state(monkeypatch, tmp_path)
        cwd = tmp_path / "unknown-src" / "repo"
        cwd.mkdir(parents=True)
        monkeypatch.chdir(cwd)
        monkeypatch.setattr("os.getcwd", lambda: str(cwd))
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        _patch_stdin_payload(monkeypatch, {"session_id": "new-sid", "source": "agent"})

        # Seed a pointer so any accidental inherit would have data to
        # find; if the gating is broken this test will see "inherited"
        # in stderr instead of the unknown-source breadcrumb.
        self._seed_for_inheritance(tmp_path, cwd)
        self._wire_no_task(monkeypatch)

        mod = self._reload_module()
        mod.main()

        err = capsys.readouterr().err
        assert "unknown source" in err
        assert "'agent'" in err  # the unknown value is in the breadcrumb (repr form)
        assert "no inherit" in err
        # Critical: must NOT have inherited despite the seeded pointer.
        assert "inherited project=" not in err


class TestGetSessionContextValidation:
    """Direct unit tests for ``get_session_context`` validation + precedence.

    The ``TestSessionStartSourceGating`` class tests ``main()`` end-to-end;
    these tests exercise ``get_session_context`` in isolation to lock
    invariants that drive the security and precedence properties of the
    surrounding flow.
    """

    def _reload_module(self):
        import importlib
        import hooks.session_start as mod

        importlib.reload(mod)
        return mod

    def test_rejects_path_traversal_session_id(self, monkeypatch):
        """A stdin session_id with path separators is dropped to None.

        Defense against CWE-22: ``session_id`` is interpolated into
        ``projects/<sid>.json`` in ``write_session_project``. A payload
        like ``"../../../tmp/pwn"`` would otherwise produce a write
        outside ``~/.claude/hooks/state/projects/``. The validator
        rejects on charset mismatch (``/`` and ``.`` are outside
        ``[A-Za-z0-9_-]``).
        """
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        _patch_stdin_payload(
            monkeypatch,
            {"session_id": "../../../tmp/pwn", "source": "resume"},
        )

        mod = self._reload_module()
        sid, source = mod.get_session_context()
        assert sid is None
        # source still propagates - the source field is documented as a
        # plain enum string and isn't a security boundary.
        assert source == "resume"

    def test_rejects_oversized_session_id(self, monkeypatch):
        """A multi-megabyte session_id is dropped to None.

        Bounds the value flowing into DB rows (project_state),
        filesystem writes (projects/<sid>.json), and breadcrumb stderr
        output. 257 chars (one over the limit) is sufficient to verify
        the boundary.
        """
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        _patch_stdin_payload(
            monkeypatch,
            {"session_id": "a" * 257, "source": "resume"},
        )

        mod = self._reload_module()
        sid, _source = mod.get_session_context()
        assert sid is None

    def test_rejects_non_string_session_id(self, monkeypatch):
        """A JSON integer session_id is dropped to None.

        Claude Code emits string UUIDs, but the JSON contract doesn't
        enforce type at the wire layer. A buggy or hostile producer
        could send ``"session_id": 12345``. ``isinstance(value, str)``
        guards downstream code that assumes string operations on the id.
        """
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        _patch_stdin_payload(
            monkeypatch,
            {"session_id": 12345, "source": "resume"},
        )

        mod = self._reload_module()
        sid, _source = mod.get_session_context()
        assert sid is None

    def test_accepts_uuid_shaped_session_id(self, monkeypatch):
        """A standard UUID-shaped session_id passes validation.

        Sanity check the validator isn't over-tight; the actual
        Claude-Code-issued shape (8-4-4-4-12 lowercase hex with hyphens)
        must still get through. Synthetic all-zeros UUID so it never
        collides with a real developer session_id even if the test ever
        loses its isolation.
        """
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        _patch_stdin_payload(
            monkeypatch,
            {
                "session_id": "00000000-0000-0000-0000-000000000000",
                "source": "resume",
            },
        )

        mod = self._reload_module()
        sid, source = mod.get_session_context()
        assert sid == "00000000-0000-0000-0000-000000000000"
        assert source == "resume"

    def test_env_var_wins_for_session_id_when_both_set(self, monkeypatch):
        """env CLAUDE_SESSION_ID wins for sid; source still comes from stdin.

        This is the documented split-precedence rule: env carries only
        session_id (no source field), so when both are set, the env
        value is the session_id but source still has to come from
        stdin. A regression that returns early on the env var path
        (skipping the stdin read) would silently break inheritance for
        users whose terminal sets ``CLAUDE_SESSION_ID``.
        """
        monkeypatch.setenv("CLAUDE_SESSION_ID", "env-sid")
        _patch_stdin_payload(
            monkeypatch,
            {"session_id": "stdin-sid", "source": "resume"},
        )

        mod = self._reload_module()
        sid, source = mod.get_session_context()
        assert sid == "env-sid"
        assert source == "resume"

    def test_invalid_env_var_session_id_is_dropped(self, monkeypatch):
        """A path-traversal env var is also rejected (env isn't a trust shortcut).

        Validation runs at the function exit boundary, so the env var
        path goes through the same gate as the stdin path. A user who
        manually exports ``CLAUDE_SESSION_ID="../etc/passwd"`` doesn't
        get to bypass the filename-safety check.
        """
        monkeypatch.setenv("CLAUDE_SESSION_ID", "../../../tmp/pwn")
        # No stdin payload - exercise the env-var-only path.

        mod = self._reload_module()
        sid, _source = mod.get_session_context()
        assert sid is None


# ── pre_compact ───────────────────────────────────────────────────────────


class TestPreCompact:
    """Tests for the redesigned PreCompact hook (MAJOR-13).

    The hook now:
    1. Reads JSONL transcript, captures last N user/assistant turns into
       a Pre-Compact Snapshot subsection.
    2. Wraps DB calls in retry-with-backoff for sqlite lock contention.
    3. Writes a sticky error file on terminal failure for /orbit:go to
       surface on next resume.
    """

    def _setup_task(self, tmp_path, ctx_seed=None):
        """Build a task dir + mock task/repo. Returns (task_dir, ctx_file, mocks)."""
        task_dir = tmp_path / "orbit" / "active" / "compact-task"
        task_dir.mkdir(parents=True)
        ctx_file = task_dir / "compact-task-context.md"
        ctx_file.write_text(
            ctx_seed
            or "# Context\n\n**Last Updated:** 2025-01-01 00:00\n\n## Recent Changes\n\n### Old\n\n- prior change\n"
        )

        mock_task = SimpleNamespace(
            id=1,
            name="compact-task",
            repo_id=1,
            full_path="active/compact-task",
        )
        mock_repo = SimpleNamespace(path=str(tmp_path / "orbit"))
        return task_dir, ctx_file, mock_task, mock_repo

    def _run(self, monkeypatch, mock_db, transcript_path=None):
        """Reload pre_compact with stdin payload and mock orbit_db."""
        payload = {"transcript_path": str(transcript_path) if transcript_path else "", "cwd": "/fake/cwd"}
        monkeypatch.setattr("sys.stdin", StringIO(json.dumps(payload)))
        with patch.dict(
            "sys.modules", {"orbit_db": MagicMock(TaskDB=lambda: mock_db)}
        ):
            import importlib
            import hooks.pre_compact as mod

            importlib.reload(mod)
            mod.main()
            return mod

    def test_updates_context_timestamp_and_writes_snapshot(
        self, tmp_path, monkeypatch
    ):
        """Hook stamps timestamp and prepends a Pre-Compact Snapshot subsection."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        _task_dir, ctx_file, mock_task, mock_repo = self._setup_task(tmp_path)

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = mock_task
        mock_db.get_repo.return_value = mock_repo
        mock_db.process_heartbeats.return_value = 0

        self._run(monkeypatch, mock_db)

        content = ctx_file.read_text()
        assert "2025-01-01 00:00" not in content
        assert "**Last Updated:**" in content
        # New snapshot marker (replaces the legacy "Auto-saved before compaction")
        assert "Pre-Compact Snapshot" in content

    def test_snapshot_includes_recent_user_and_assistant_turns(
        self, tmp_path, monkeypatch
    ):
        """Snapshot body contains the recent user prompts and assistant text."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        _task_dir, ctx_file, mock_task, mock_repo = self._setup_task(tmp_path)

        # Build a fixture JSONL transcript with 2 user prompts + 2 assistant
        # responses, plus one isMeta system-injected user (should be skipped)
        # and one assistant tool_use block (should be skipped).
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            "\n".join(
                [
                    json.dumps({
                        "type": "user",
                        "isMeta": True,
                        "message": {"role": "user", "content": "<system-injected>"},
                    }),
                    json.dumps({
                        "type": "user",
                        "message": {"role": "user", "content": "fix the bug in foo.py"},
                    }),
                    json.dumps({
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "thinking",
                                    "thinking": "THINKING-BLOCK-XYZZY",
                                },
                                {"type": "text", "text": "I will fix it now."},
                            ],
                        },
                    }),
                    json.dumps({
                        "type": "user",
                        "message": {"role": "user", "content": "also add tests"},
                    }),
                    json.dumps({
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {"type": "tool_use", "name": "Edit"},
                                {"type": "text", "text": "Tests added in test_foo.py"},
                            ],
                        },
                    }),
                ]
            )
        )

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = mock_task
        mock_db.get_repo.return_value = mock_repo

        self._run(monkeypatch, mock_db, transcript_path=transcript)

        content = ctx_file.read_text()
        assert "fix the bug in foo.py" in content
        assert "also add tests" in content
        assert "I will fix it now." in content
        assert "Tests added in test_foo.py" in content
        # Filtered noise must NOT appear
        assert "system-injected" not in content
        assert "THINKING-BLOCK-XYZZY" not in content  # thinking block dropped

    def test_db_lock_writes_sticky_error(self, tmp_path, monkeypatch):
        """OperationalError('database is locked') after retry → sticky error file,
        no context.md write."""
        import sqlite3

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        # Speed up the test by zeroing out the retry delay
        monkeypatch.setattr("time.sleep", lambda *_: None)

        _task_dir, ctx_file, _, _ = self._setup_task(tmp_path)
        original_content = ctx_file.read_text()

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.side_effect = sqlite3.OperationalError(
            "database is locked"
        )

        mod = self._run(monkeypatch, mock_db)

        assert mod.ERROR_FILE.exists(), "sticky error file should be written"
        sticky = json.loads(mod.ERROR_FILE.read_text())
        assert "database is locked" in sticky["reason"]
        assert "find_task_for_cwd" in sticky["reason"]
        # context.md should be untouched - DB lookup never succeeded
        assert ctx_file.read_text() == original_content

    def test_successful_run_clears_prior_sticky_error(
        self, tmp_path, monkeypatch
    ):
        """A successful run removes any leftover sticky error file from a
        previous failed compaction so /orbit:go does not surface stale warnings."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        _task_dir, _ctx_file, mock_task, mock_repo = self._setup_task(tmp_path)

        # Pre-seed a sticky error from a previous failed run. Build the path
        # the same way the module will (so the assertion can use mod.ERROR_FILE
        # for the same-target check as the other sticky-error tests).
        error_dir = tmp_path / ".claude" / "hooks" / "state"
        error_dir.mkdir(parents=True)
        (error_dir / "last-precompact-error.json").write_text(
            json.dumps({"timestamp": "old", "task_name": "compact-task", "reason": "old failure"})
        )

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = mock_task
        mock_db.get_repo.return_value = mock_repo

        mod = self._run(monkeypatch, mock_db)

        assert not mod.ERROR_FILE.exists(), (
            "successful run must clear prior sticky error file"
        )

    def test_db_lock_recovers_on_retry(self, tmp_path, monkeypatch):
        """Lock once, succeed on second attempt → no sticky error, snapshot lands."""
        import sqlite3

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        monkeypatch.setattr("time.sleep", lambda *_: None)

        _task_dir, ctx_file, mock_task, mock_repo = self._setup_task(tmp_path)
        original = ctx_file.read_text()

        mock_db = MagicMock()
        # First call raises locked, second call returns the task
        mock_db.find_task_for_cwd.side_effect = [
            sqlite3.OperationalError("database is locked"),
            mock_task,
        ]
        mock_db.get_repo.return_value = mock_repo

        mod = self._run(monkeypatch, mock_db)

        assert ctx_file.read_text() != original, "snapshot should have landed"
        assert "Pre-Compact Snapshot" in ctx_file.read_text()
        assert not mod.ERROR_FILE.exists(), (
            "retry success must not leave a sticky error"
        )


# ── stop ──────────────────────────────────────────────────────────────────


class TestStop:
    def _run_stop(self, monkeypatch, stdin_data, mock_db):
        """Helper to run stop.main() with given stdin and mock DB."""
        monkeypatch.setattr("sys.stdin", StringIO(json.dumps(stdin_data)))

        with patch.dict("sys.modules", {"orbit_db": MagicMock(TaskDB=lambda: mock_db)}):
            import importlib
            import hooks.stop as mod

            importlib.reload(mod)
            mod.main()

    def test_detects_edits_shows_reminder(self, tmp_path, monkeypatch, capsys):
        """stop shows orbit reminder when transcript contains Write/Edit tool uses."""
        # Create a fake transcript with edit tool uses
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            '{"type": "tool_use", "name": "Edit"}\n'
            '{"type": "tool_use", "name": "Write"}\n'
        )

        orbit_dir = tmp_path / ".claude" / "orbit" / "active" / "stop-task"
        orbit_dir.mkdir(parents=True)
        (orbit_dir / "stop-task-context.md").write_text("# Context")

        mock_task = SimpleNamespace(
            id=1, name="stop-task", full_path="active/stop-task"
        )
        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = mock_task

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        self._run_stop(
            monkeypatch,
            {"transcript_path": str(transcript), "cwd": str(tmp_path)},
            mock_db,
        )

        err = capsys.readouterr().err
        assert "stop-task" in err
        assert "orbit:save" in err.lower() or "Orbit Reminder" in err

    def test_no_reminder_when_no_edits(self, tmp_path, monkeypatch, capsys):
        """stop does not show reminder when transcript has no Write/Edit tool uses."""
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text('{"type": "tool_use", "name": "Read"}\n')

        mock_db = MagicMock()

        self._run_stop(
            monkeypatch,
            {"transcript_path": str(transcript), "cwd": str(tmp_path)},
            mock_db,
        )

        err = capsys.readouterr().err
        assert "Orbit Reminder" not in err


# ── task_tracker ──────────────────────────────────────────────────────────


class TestTaskTracker:
    """Tests for the UserPromptSubmit divergence detection hook."""

    def _setup_project(
        self,
        tmp_path: Path,
        monkeypatch,
        tasks_content: str,
        context_content: str,
        *,
        context_newer: bool = True,
    ) -> SimpleNamespace:
        """Create fake orbit project files under tmp_path's fake HOME.

        Points Path.home() at tmp_path so the hook's orbit_root resolution
        (~/.orbit) lands in our sandbox. Returns a fake task object
        ready to be plugged into `mock_db.find_task_for_cwd.return_value`.
        """
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        orbit_dir = tmp_path / ".orbit" / "active" / "fake-task"
        orbit_dir.mkdir(parents=True)

        tasks_file = orbit_dir / "fake-task-tasks.md"
        context_file = orbit_dir / "fake-task-context.md"
        tasks_file.write_text(tasks_content)
        context_file.write_text(context_content)

        # Force mtime ordering: context is always newer by default.
        if context_newer:
            os.utime(tasks_file, (1000, 1000))
            os.utime(context_file, (2000, 2000))
        else:
            os.utime(tasks_file, (2000, 2000))
            os.utime(context_file, (1000, 1000))

        return SimpleNamespace(
            id=1,
            name="fake-task",
            repo_id=1,
            full_path="active/fake-task",
        )

    def _run_tracker(self, monkeypatch, stdin_data, mock_db=None):
        """Helper to run task_tracker.main() with given stdin and mock DB."""
        monkeypatch.setattr("sys.stdin", StringIO(json.dumps(stdin_data)))

        module_patch = {"orbit_db": MagicMock(TaskDB=lambda: mock_db)}
        with patch.dict("sys.modules", module_patch):
            import importlib
            import hooks.task_tracker as mod

            importlib.reload(mod)
            mod.main()

    def test_no_active_project_silent(self, monkeypatch, capsys):
        """Returns silently when there's no orbit project for the cwd."""
        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = None

        self._run_tracker(
            monkeypatch,
            {"session_id": "s1", "cwd": "/tmp", "prompt": "hello"},
            mock_db,
        )

        out = capsys.readouterr().out
        assert out == ""

    def test_missing_tasks_file_silent(self, tmp_path, monkeypatch, capsys):
        """Returns silently when the tasks file doesn't exist."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        orbit_dir = tmp_path / ".claude" / "orbit" / "active" / "fake-task"
        orbit_dir.mkdir(parents=True)
        # Only create context file, no tasks file
        (orbit_dir / "fake-task-context.md").write_text("### Task 1: something")

        task = SimpleNamespace(
            id=1, name="fake-task", repo_id=1, full_path="active/fake-task"
        )

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = task

        self._run_tracker(
            monkeypatch,
            {"session_id": "s1", "cwd": str(tmp_path), "prompt": "hello"},
            mock_db,
        )

        assert capsys.readouterr().out == ""

    def test_missing_context_file_silent(self, tmp_path, monkeypatch, capsys):
        """Returns silently when the context file doesn't exist."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        orbit_dir = tmp_path / ".claude" / "orbit" / "active" / "fake-task"
        orbit_dir.mkdir(parents=True)
        (orbit_dir / "fake-task-tasks.md").write_text("- [ ] 1. Task one")

        task = SimpleNamespace(
            id=1, name="fake-task", repo_id=1, full_path="active/fake-task"
        )

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = task

        self._run_tracker(
            monkeypatch,
            {"session_id": "s1", "cwd": str(tmp_path), "prompt": "hello"},
            mock_db,
        )

        assert capsys.readouterr().out == ""

    def test_divergence_fires_regardless_of_mtime_order(
        self, tmp_path, monkeypatch, capsys
    ):
        """Warn on divergence even if tasks file was touched more recently.

        Motivation: a Claude session can mark one task complete (touching
        the tasks file) while leaving other tasks with context-file findings
        still unchecked. In this state, tasks_mtime > context_mtime but the
        divergence is still real.
        """
        task = self._setup_project(
            tmp_path,
            monkeypatch,
            tasks_content=(
                "- [x] 1. Done task\n"
                "- [ ] 2. Divergent task\n"
            ),
            context_content=(
                "### Task 1: Done task\nfindings\n"
                "### Task 2: Divergent task\nfindings\n"
            ),
            context_newer=False,  # tasks file is newer
        )

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = task

        self._run_tracker(
            monkeypatch,
            {"session_id": "s1", "cwd": str(tmp_path), "prompt": "hello"},
            mock_db,
        )

        out = capsys.readouterr().out
        assert "Task 2: Divergent task" in out
        assert "Task 1:" not in out

    def test_no_divergence_all_marked(self, tmp_path, monkeypatch, capsys):
        """No warning when every heading has a matching [x] in tasks file."""
        task = self._setup_project(
            tmp_path,
            monkeypatch,
            tasks_content="- [x] 1. First task\n",
            context_content="### Task 1: First task\nfindings\n",
        )

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = task

        self._run_tracker(
            monkeypatch,
            {"session_id": "s1", "cwd": str(tmp_path), "prompt": "hello"},
            mock_db,
        )

        assert capsys.readouterr().out == ""

    def test_single_divergence(self, tmp_path, monkeypatch, capsys):
        """Warns when context has a heading for an unchecked task."""
        task = self._setup_project(
            tmp_path,
            monkeypatch,
            tasks_content="- [ ] 2. Framework wiring review\n",
            context_content="### Task 2: Framework wiring review\ndetailed findings\n",
        )

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = task

        self._run_tracker(
            monkeypatch,
            {"session_id": "s1", "cwd": str(tmp_path), "prompt": "hello"},
            mock_db,
        )

        out = capsys.readouterr().out
        assert "Orbit task tracking divergence" in out
        assert "Task 2: Framework wiring review" in out
        assert "update_tasks_file" in out

    def test_multiple_divergence(self, tmp_path, monkeypatch, capsys):
        """Warns about all divergent tasks, not just one."""
        task = self._setup_project(
            tmp_path,
            monkeypatch,
            tasks_content=(
                "- [ ] 2. Framework review\n"
                "- [ ] 3. Helper review\n"
                "- [ ] 4. Templates review\n"
            ),
            context_content=(
                "### Task 2: Framework review\nfindings\n"
                "### Task 3: Helper review\nfindings\n"
                "### Task 4: Templates review\nfindings\n"
            ),
        )

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = task

        self._run_tracker(
            monkeypatch,
            {"session_id": "s1", "cwd": str(tmp_path), "prompt": "hello"},
            mock_db,
        )

        out = capsys.readouterr().out
        assert "Task 2: Framework review" in out
        assert "Task 3: Helper review" in out
        assert "Task 4: Templates review" in out

    def test_partial_divergence(self, tmp_path, monkeypatch, capsys):
        """Only warns about tasks that have headings AND are still unchecked."""
        task = self._setup_project(
            tmp_path,
            monkeypatch,
            tasks_content=(
                "- [x] 1. Done task\n"
                "- [ ] 2. Pending with heading\n"
                "- [ ] 3. Pending without heading\n"
            ),
            context_content=(
                "### Task 1: Done task\nfindings\n"
                "### Task 2: Pending with heading\nfindings\n"
            ),
        )

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = task

        self._run_tracker(
            monkeypatch,
            {"session_id": "s1", "cwd": str(tmp_path), "prompt": "hello"},
            mock_db,
        )

        out = capsys.readouterr().out
        assert "Task 2: Pending with heading" in out
        # Task 1 is done - not flagged
        assert "Task 1:" not in out
        # Task 3 has no heading - not flagged
        assert "Task 3:" not in out

    def test_skip_slash_command(self, monkeypatch, capsys):
        """Skips divergence check for slash commands."""
        mock_db = MagicMock()

        self._run_tracker(
            monkeypatch,
            {"session_id": "s1", "cwd": "/tmp", "prompt": "/orbit:save"},
            mock_db,
        )

        assert capsys.readouterr().out == ""
        # Should never have called the DB
        mock_db.find_task_for_cwd.assert_not_called()

    def test_skip_subagent(self, monkeypatch, capsys):
        """Skips divergence check when running in a subagent context."""
        mock_db = MagicMock()

        self._run_tracker(
            monkeypatch,
            {
                "session_id": "s1",
                "cwd": "/tmp",
                "prompt": "hello",
                "agent_id": "sub-42",
            },
            mock_db,
        )

        assert capsys.readouterr().out == ""
        mock_db.find_task_for_cwd.assert_not_called()

    def test_skip_empty_prompt(self, monkeypatch, capsys):
        """Skips divergence check for empty prompts."""
        mock_db = MagicMock()

        self._run_tracker(
            monkeypatch,
            {"session_id": "s1", "cwd": "/tmp", "prompt": "   "},
            mock_db,
        )

        assert capsys.readouterr().out == ""
        mock_db.find_task_for_cwd.assert_not_called()

    def test_heading_without_description_counts(
        self, tmp_path, monkeypatch, capsys
    ):
        """A bare `### Task N` heading (no colon) still triggers a warning."""
        task = self._setup_project(
            tmp_path,
            monkeypatch,
            tasks_content="- [ ] 5. Review thing\n",
            context_content="### Task 5\nsome findings without colon\n",
        )

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = task

        self._run_tracker(
            monkeypatch,
            {"session_id": "s1", "cwd": str(tmp_path), "prompt": "hello"},
            mock_db,
        )

        out = capsys.readouterr().out
        assert "Task 5: Review thing" in out

    def test_subtask_layout_divergence(self, tmp_path, monkeypatch, capsys):
        """Subtask directories use plain tasks.md/context.md (no prefix).

        Mirrors the layout that orbit_db's scan_repo treats as a subtask
        marker. Verifies the hook falls back to the non-prefixed filenames
        when the prefixed form is absent.
        """
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        # Subtask dir: active/parent-task/sub-task with plain tasks.md/context.md
        subtask_dir = (
            tmp_path / ".orbit" / "active" / "parent-task" / "sub-task"
        )
        subtask_dir.mkdir(parents=True)
        (subtask_dir / "tasks.md").write_text(
            "- [x] 1. Done subtask item\n"
            "- [ ] 2. Divergent subtask item\n"
        )
        (subtask_dir / "context.md").write_text(
            "### Task 1: Done subtask item\nfindings\n"
            "### Task 2: Divergent subtask item\nfindings\n"
        )

        task = SimpleNamespace(
            id=2,
            name="sub-task",
            repo_id=1,
            full_path="active/parent-task/sub-task",
        )

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = task

        self._run_tracker(
            monkeypatch,
            {"session_id": "s1", "cwd": str(tmp_path), "prompt": "hello"},
            mock_db,
        )

        out = capsys.readouterr().out
        assert "Task 2: Divergent subtask item" in out
        # Task 1 is done - not flagged
        assert "Task 1:" not in out


# ── session_start task discipline reminder ────────────────────────────────


class TestSessionStartTaskDiscipline:
    """Verify the session_start hook includes the task tracking discipline reminder."""

    def test_output_includes_discipline_reminder(
        self, tmp_path, monkeypatch, capsys
    ):
        """session_start output mentions update_tasks_file and the TaskCreate anti-pattern."""
        # Redirect Path.home() to tmp_path so the hook's state-file writes
        # (pending-task.json, projects/<session>.json) land in our sandbox
        # instead of polluting the real ~/.claude/hooks/state/.
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        # Real on-disk task_dir so the `task_dir.exists()` check passes.
        repo_path = tmp_path / "repo"
        task_dir = repo_path / "active" / "my-task"
        task_dir.mkdir(parents=True)

        mock_task = SimpleNamespace(
            id=1,
            name="my-task",
            status="active",
            jira_key=None,
            repo_id=10,
            full_path="active/my-task",
        )
        mock_repo = SimpleNamespace(short_name="my-repo", path=str(repo_path))

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = mock_task
        mock_db.get_repo.return_value = mock_repo
        mock_db.get_task_time.return_value = 0
        mock_db.format_duration.return_value = "0m"

        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-discipline-test")
        monkeypatch.setattr("os.getcwd", lambda: str(task_dir))

        with patch.dict(
            "sys.modules", {"orbit_db": MagicMock(TaskDB=lambda: mock_db)}
        ):
            import importlib
            import hooks.session_start as mod

            importlib.reload(mod)
            mod.main()

        output = capsys.readouterr().out
        assert "Task tracking discipline" in output
        assert "update_tasks_file" in output
        assert "TaskCreate" in output


class TestParallelSessionDetection:
    """Tests for ``_read_cwd_pointer_sid``, ``_detect_parallel_sessions``,
    ``_projects_for_sessions``, ``_format_collision_warning``, and the
    main() integration that uses them to (a) skip ambiguous resume-pickup
    and (b) surface a warning to Claude's context.

    Failure mode being guarded against: when two Claude sessions are alive
    in the same cwd, the cwd-session pointer is last-writer-wins. Resuming
    either session inherits via that pointer and can bind the wrong project
    silently. The fix detects parallel sessions via transcript jsonl mtime
    in ``~/.claude/projects/<cwd-key>/`` and refuses to auto-pickup when
    *another* session beyond the resumed-from one is recently active. The
    resumed-from session itself is excluded from parallel detection - its
    transcript is often still fresh from end-of-session writes but it is
    the conversation being continued, not a parallel session.
    """

    @staticmethod
    def _redirect_state(monkeypatch, home: Path) -> Path:
        """Mirror :class:`TestSessionStartResumePickup._redirect_state`."""
        import orbit_db  # type: ignore[import-not-found]

        monkeypatch.setattr("pathlib.Path.home", lambda: home)
        db_path = home / ".claude" / "hooks-state.db"
        monkeypatch.setattr(orbit_db, "HOOKS_STATE_DB_PATH", db_path)
        return db_path

    @staticmethod
    def _seed_transcript(
        home: Path,
        cwd: Path,
        session_id: str,
        mtime_offset_seconds: float = 0.0,
    ) -> Path:
        """Create a ``~/.claude/projects/<cwd-key>/<sid>.jsonl`` transcript
        with mtime set to ``now + mtime_offset_seconds``.

        Negative offsets backdate the transcript to simulate stale sessions.
        """
        cwd_key = str(cwd).replace("/", "-")
        proj_dir = home / ".claude" / "projects" / cwd_key
        proj_dir.mkdir(parents=True, exist_ok=True)
        jsonl = proj_dir / f"{session_id}.jsonl"
        jsonl.write_text("{}\n")
        if mtime_offset_seconds:
            t = time.time() + mtime_offset_seconds
            os.utime(jsonl, (t, t))
        return jsonl

    @classmethod
    def _seed_project_state(cls, home: Path, rows: list[tuple[str, str]]) -> Path:
        """Seed project_state with (sid, project) rows using the real schema."""
        import sqlite3 as _sqlite3
        from orbit_db import init_hooks_state_db_schema  # type: ignore[import-not-found]

        db_path = home / ".claude" / "hooks-state.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = _sqlite3.connect(str(db_path))
        try:
            init_hooks_state_db_schema(conn)
            conn.executemany(
                "INSERT INTO project_state (session_id, project_name) VALUES (?, ?)",
                rows,
            )
            conn.commit()
        finally:
            conn.close()
        return db_path

    def _reload_module(self):
        import importlib
        import hooks.session_start as mod

        importlib.reload(mod)
        return mod

    # ── _detect_parallel_sessions ─────────────────────────────────────────

    def test_detect_returns_empty_when_proj_dir_missing(self, tmp_path, monkeypatch):
        """No transcripts dir at all (fresh cwd Claude has never written in)."""
        self._redirect_state(monkeypatch, tmp_path)
        cwd = tmp_path / "fresh-cwd"
        cwd.mkdir()
        mod = self._reload_module()
        assert mod._detect_parallel_sessions(cwd, "my-sid") == []

    def test_detect_excludes_own_session(self, tmp_path, monkeypatch):
        """My own transcript must NOT appear in the parallel list."""
        self._redirect_state(monkeypatch, tmp_path)
        cwd = tmp_path / "repo"
        cwd.mkdir()
        self._seed_transcript(tmp_path, cwd, "my-sid")
        mod = self._reload_module()
        assert mod._detect_parallel_sessions(cwd, "my-sid") == []

    def test_detect_returns_other_fresh_sessions(self, tmp_path, monkeypatch):
        """A fresh transcript for another sid is detected."""
        self._redirect_state(monkeypatch, tmp_path)
        cwd = tmp_path / "repo"
        cwd.mkdir()
        self._seed_transcript(tmp_path, cwd, "my-sid")
        self._seed_transcript(tmp_path, cwd, "other-sid")
        mod = self._reload_module()
        result = mod._detect_parallel_sessions(cwd, "my-sid")
        assert result == ["other-sid"]

    def test_detect_excludes_stale_transcripts(self, tmp_path, monkeypatch):
        """Transcripts older than the threshold are not 'parallel'."""
        self._redirect_state(monkeypatch, tmp_path)
        cwd = tmp_path / "repo"
        cwd.mkdir()
        self._seed_transcript(tmp_path, cwd, "my-sid")
        # 30 minutes ago > 10 minute threshold.
        self._seed_transcript(
            tmp_path, cwd, "stale-sid", mtime_offset_seconds=-30 * 60
        )
        mod = self._reload_module()
        assert mod._detect_parallel_sessions(cwd, "my-sid") == []

    # ── PID liveness: _session_is_alive + _detect filtering ───────────────

    @staticmethod
    def _seed_pid_record(
        home: Path, session_id: str, pid: int, start_time: str | None = None
    ) -> Path:
        """Write a ``~/.claude/hooks/state/session-pids/<sid>.json`` record
        mirroring what ``write_session_pid`` produces."""
        rec_dir = home / ".claude" / "hooks" / "state" / "session-pids"
        rec_dir.mkdir(parents=True, exist_ok=True)
        rec = rec_dir / f"{session_id}.json"
        rec.write_text(
            json.dumps(
                {
                    "sessionId": session_id,
                    "pid": pid,
                    "startTime": start_time,
                    "updatedAt": datetime.now().astimezone().isoformat(),
                }
            )
        )
        return rec

    @staticmethod
    def _dead_pid() -> int:
        """Return a pid that is guaranteed dead: spawn a trivial process and
        reap it, so the kernel has released it before we hand it back."""
        proc = subprocess.Popen([sys.executable, "-c", ""])
        proc.wait()
        return proc.pid

    def test_session_is_alive_none_when_no_record(self, tmp_path, monkeypatch):
        """No pid record -> None (unknown), so the caller falls back to mtime."""
        self._redirect_state(monkeypatch, tmp_path)
        mod = self._reload_module()
        assert mod._session_is_alive("never-recorded") is None

    def test_session_is_alive_true_for_live_pid(self, tmp_path, monkeypatch):
        """A recorded pid that is still running -> True."""
        self._redirect_state(monkeypatch, tmp_path)
        mod = self._reload_module()
        # This test process is unquestionably alive; no start time, so the
        # reuse guard is skipped and liveness rests on os.kill alone.
        self._seed_pid_record(tmp_path, "live-sid", os.getpid(), start_time=None)
        assert mod._session_is_alive("live-sid") is True

    def test_session_is_alive_true_when_start_time_matches(self, tmp_path, monkeypatch):
        """Live pid AND a matching recorded start time -> True (not a reuse)."""
        self._redirect_state(monkeypatch, tmp_path)
        mod = self._reload_module()
        start = mod._ps_field(os.getpid(), "lstart")
        self._seed_pid_record(tmp_path, "live-sid", os.getpid(), start_time=start)
        assert mod._session_is_alive("live-sid") is True

    def test_session_is_alive_false_for_dead_pid(self, tmp_path, monkeypatch):
        """A recorded pid whose process has exited -> False (the fix's core)."""
        self._redirect_state(monkeypatch, tmp_path)
        mod = self._reload_module()
        self._seed_pid_record(tmp_path, "dead-sid", self._dead_pid(), start_time=None)
        assert mod._session_is_alive("dead-sid") is False

    def test_session_is_alive_false_on_pid_reuse(self, tmp_path, monkeypatch):
        """Live pid but a start time that no longer matches -> False: the pid
        was recycled by an unrelated process after the session exited."""
        self._redirect_state(monkeypatch, tmp_path)
        mod = self._reload_module()
        self._seed_pid_record(
            tmp_path, "reused-sid", os.getpid(), start_time="Thu Jan  1 00:00:00 1970"
        )
        assert mod._session_is_alive("reused-sid") is False

    def test_detect_drops_dead_candidate(self, tmp_path, monkeypatch):
        """A fresh transcript whose session is proven dead is NOT parallel.

        This is the regression the fix targets: /clear-then-relaunch (or
        quit-and-reopen) leaves the just-closed session's transcript fresh,
        but its pid is gone, so it must not trip the warning.
        """
        self._redirect_state(monkeypatch, tmp_path)
        cwd = tmp_path / "repo"
        cwd.mkdir()
        self._seed_transcript(tmp_path, cwd, "my-sid")
        self._seed_transcript(tmp_path, cwd, "dead-sid")  # fresh mtime
        mod = self._reload_module()
        self._seed_pid_record(tmp_path, "dead-sid", self._dead_pid(), start_time=None)
        assert mod._detect_parallel_sessions(cwd, "my-sid") == []

    def test_detect_keeps_live_candidate(self, tmp_path, monkeypatch):
        """A fresh transcript whose session is provably alive stays parallel."""
        self._redirect_state(monkeypatch, tmp_path)
        cwd = tmp_path / "repo"
        cwd.mkdir()
        self._seed_transcript(tmp_path, cwd, "my-sid")
        self._seed_transcript(tmp_path, cwd, "live-sid")
        mod = self._reload_module()
        self._seed_pid_record(tmp_path, "live-sid", os.getpid(), start_time=None)
        assert mod._detect_parallel_sessions(cwd, "my-sid") == ["live-sid"]

    def test_detect_keeps_candidate_without_pid_record(self, tmp_path, monkeypatch):
        """Migration safety: a fresh transcript with no pid record (session
        predating the feature) is kept, preserving the mtime-only behavior."""
        self._redirect_state(monkeypatch, tmp_path)
        cwd = tmp_path / "repo"
        cwd.mkdir()
        self._seed_transcript(tmp_path, cwd, "my-sid")
        self._seed_transcript(tmp_path, cwd, "legacy-sid")  # no pid record
        mod = self._reload_module()
        assert mod._detect_parallel_sessions(cwd, "my-sid") == ["legacy-sid"]

    # ── PID record path-traversal guard (CWE-22) ──────────────────────────

    def test_write_session_pid_rejects_path_like_id(self, tmp_path, monkeypatch):
        """A path-like session id is rejected before a filename is built, so
        no pid record is written - the session id never escapes the state dir.
        Pid resolution is forced to succeed so id validation is the only gate.
        """
        self._redirect_state(monkeypatch, tmp_path)
        mod = self._reload_module()
        monkeypatch.setattr(mod, "_resolve_session_pid", lambda: os.getpid())
        mod.write_session_pid("../escape")
        state_dir = tmp_path / ".claude" / "hooks" / "state"
        pid_dir = state_dir / "session-pids"
        assert not (state_dir / "escape.json").exists()
        assert not pid_dir.exists() or list(pid_dir.glob("*.json")) == []

    def test_write_session_pid_writes_for_valid_id(self, tmp_path, monkeypatch):
        """Positive control so the rejection test isn't vacuous: a valid id
        does produce a record inside the state dir."""
        self._redirect_state(monkeypatch, tmp_path)
        mod = self._reload_module()
        monkeypatch.setattr(mod, "_resolve_session_pid", lambda: os.getpid())
        mod.write_session_pid("valid-sid-123")
        rec = (
            tmp_path / ".claude" / "hooks" / "state" / "session-pids"
            / "valid-sid-123.json"
        )
        assert rec.exists()

    def test_session_is_alive_rejects_path_like_id(self, tmp_path, monkeypatch):
        """A path-like candidate id returns None (unknown -> mtime fallback)
        rather than reading a file outside the state dir."""
        self._redirect_state(monkeypatch, tmp_path)
        mod = self._reload_module()
        assert mod._session_is_alive("../escape") is None
        assert mod._session_is_alive("a/b") is None

    # ── _projects_for_sessions ────────────────────────────────────────────

    def test_projects_for_sessions_maps_bound_sids(self, tmp_path, monkeypatch):
        self._redirect_state(monkeypatch, tmp_path)
        self._seed_project_state(
            tmp_path, [("sid-a", "alpha"), ("sid-b", "beta"), ("sid-c", "")]
        )
        mod = self._reload_module()
        result = mod._projects_for_sessions(["sid-a", "sid-b", "sid-c", "sid-d"])
        # sid-c has empty name (filtered), sid-d has no row.
        assert result == {"sid-a": "alpha", "sid-b": "beta"}

    def test_projects_for_sessions_empty_input(self, tmp_path, monkeypatch):
        """Empty input must not issue a SQL query - guards against an
        IN () syntax error that some SQLite versions reject."""
        self._redirect_state(monkeypatch, tmp_path)
        self._seed_project_state(tmp_path, [])
        mod = self._reload_module()
        assert mod._projects_for_sessions([]) == {}

    # ── _format_collision_warning ─────────────────────────────────────────

    def test_format_warning_mentions_my_project_when_bound(self, tmp_path, monkeypatch):
        self._redirect_state(monkeypatch, tmp_path)
        mod = self._reload_module()
        warning = mod._format_collision_warning(
            "alpha", {"other-sid-abc": "beta"}
        )
        assert "alpha" in warning
        assert "beta" in warning
        assert "other-si" in warning  # truncated sid prefix
        assert "/orbit:go" in warning

    def test_format_warning_handles_unbound_self(self, tmp_path, monkeypatch):
        self._redirect_state(monkeypatch, tmp_path)
        mod = self._reload_module()
        warning = mod._format_collision_warning(None, {"sid-x-12345": "beta"})
        assert "beta" in warning
        assert "/orbit:go" in warning

    # ── main() integration: skip pickup under ambiguity ───────────────────

    def test_main_skips_resume_pickup_when_parallel_session_exists(
        self, tmp_path, monkeypatch
    ):
        """The bug fix: when another session is alive in the same cwd, do NOT
        auto-pickup the project from the cwd-pointer (it could name the
        wrong previous session). project_state for new-sid must stay empty."""
        import sqlite3 as _sqlite3

        db_path = self._redirect_state(monkeypatch, tmp_path)
        cwd = tmp_path / "resume" / "repo"
        cwd.mkdir(parents=True)
        monkeypatch.chdir(cwd)
        monkeypatch.setattr("os.getcwd", lambda: str(cwd))
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        _patch_stdin_payload(monkeypatch, {"session_id": "new-sid", "source": "resume"})

        # Seed pointer + project_state as if a previous session (prev-sid) was
        # the last writer to this cwd. WITHOUT the parallel-session detection,
        # main() would inherit "carried-over" onto new-sid.
        cwd_key = str(cwd).replace("/", "-")
        pointer_dir = tmp_path / ".claude" / "hooks" / "state" / "cwd-session"
        pointer_dir.mkdir(parents=True, exist_ok=True)
        (pointer_dir / f"{cwd_key}.json").write_text(
            json.dumps({"sessionId": "prev-sid", "cwd": str(cwd), "updatedAt": "x"})
        )
        self._seed_project_state(
            tmp_path, [("prev-sid", "carried-over"), ("other-sid", "concurrent-project")]
        )
        # Concurrent session "other-sid" has a fresh transcript - this triggers
        # the parallel detection that gates the pickup.
        self._seed_transcript(tmp_path, cwd, "other-sid")

        mod = self._reload_module()
        import orbit_db  # type: ignore[import-not-found]

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = None
        mock_db.get_task_by_name.return_value = None
        monkeypatch.setattr(orbit_db, "TaskDB", lambda: mock_db)
        mod.main()

        # new-sid did NOT inherit "carried-over" because parallel sessions
        # made the inheritance ambiguous.
        conn = _sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT project_name FROM project_state WHERE session_id = ?",
                ("new-sid",),
            ).fetchone()
        finally:
            conn.close()
        assert row is None, "new-sid must NOT inherit when parallel sessions exist"

    def test_main_inherits_when_no_parallel_sessions(self, tmp_path, monkeypatch):
        """Negative case for the gate: with a stale lone other transcript
        OLDER than the threshold, pickup still works (preserves existing
        single-session resume behavior).
        """
        import sqlite3 as _sqlite3

        db_path = self._redirect_state(monkeypatch, tmp_path)
        cwd = tmp_path / "lone-resume"
        cwd.mkdir(parents=True)
        monkeypatch.chdir(cwd)
        monkeypatch.setattr("os.getcwd", lambda: str(cwd))
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        _patch_stdin_payload(monkeypatch, {"session_id": "new-sid", "source": "resume"})

        cwd_key = str(cwd).replace("/", "-")
        pointer_dir = tmp_path / ".claude" / "hooks" / "state" / "cwd-session"
        pointer_dir.mkdir(parents=True, exist_ok=True)
        (pointer_dir / f"{cwd_key}.json").write_text(
            json.dumps({"sessionId": "prev-sid", "cwd": str(cwd), "updatedAt": "x"})
        )
        self._seed_project_state(tmp_path, [("prev-sid", "carried-over")])
        # An old transcript exists but is well past the threshold - must not
        # trigger the parallel-session gate.
        self._seed_transcript(
            tmp_path, cwd, "stale-sid", mtime_offset_seconds=-30 * 60
        )

        mod = self._reload_module()
        import orbit_db  # type: ignore[import-not-found]

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = None
        mock_db.get_task_by_name.return_value = None
        monkeypatch.setattr(orbit_db, "TaskDB", lambda: mock_db)
        mod.main()

        conn = _sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT project_name FROM project_state WHERE session_id = ?",
                ("new-sid",),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None and row[0] == "carried-over"

    # ── main() integration: warning emission ──────────────────────────────

    def test_main_emits_warning_when_parallel_session_has_different_project(
        self, tmp_path, monkeypatch, capsys
    ):
        """The warning fires on stdout (Claude's context) when another active
        session is bound to a different project than this one."""
        self._redirect_state(monkeypatch, tmp_path)
        cwd = tmp_path / "collision-repo"
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        monkeypatch.setattr("os.getcwd", lambda: str(cwd))
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        _patch_stdin_payload(
            monkeypatch, {"session_id": "my-sid", "source": "startup"}
        )

        # My session is bound to "alpha"; another active session has "beta".
        self._seed_project_state(
            tmp_path, [("my-sid", "alpha"), ("other-sid", "beta")]
        )
        self._seed_transcript(tmp_path, cwd, "other-sid")

        mod = self._reload_module()
        import orbit_db  # type: ignore[import-not-found]

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = None
        monkeypatch.setattr(orbit_db, "TaskDB", lambda: mock_db)
        mod.main()

        out = capsys.readouterr().out
        assert "Parallel Orbit Session Warning" in out
        # Structural assertions: "alpha" must appear as the bound-self
        # project (in the intro line), "beta" as the bullet for the other
        # session. Bare token checks would also pass if alpha/beta swapped
        # roles, which is the exact contract we want this test to enforce.
        assert "bound to orbit project `alpha`" in out
        assert "- `beta`" in out

    def test_main_no_warning_when_parallel_session_has_same_project(
        self, tmp_path, monkeypatch, capsys
    ):
        """Two sessions on the same project is still a parallel-work risk
        but not a *name collision* - the user explicitly asked for the
        warning to fire only on different project names. Keeping it scoped
        avoids noise on the harmless same-project case.
        """
        self._redirect_state(monkeypatch, tmp_path)
        cwd = tmp_path / "same-project-repo"
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        monkeypatch.setattr("os.getcwd", lambda: str(cwd))
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        _patch_stdin_payload(
            monkeypatch, {"session_id": "my-sid", "source": "startup"}
        )

        self._seed_project_state(
            tmp_path, [("my-sid", "alpha"), ("other-sid", "alpha")]
        )
        self._seed_transcript(tmp_path, cwd, "other-sid")

        mod = self._reload_module()
        import orbit_db  # type: ignore[import-not-found]

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = None
        monkeypatch.setattr(orbit_db, "TaskDB", lambda: mock_db)
        mod.main()

        out = capsys.readouterr().out
        assert "Parallel Orbit Session Warning" not in out

    def test_main_no_warning_when_no_parallel_sessions(
        self, tmp_path, monkeypatch, capsys
    ):
        """Sanity check: solo session, no warning."""
        self._redirect_state(monkeypatch, tmp_path)
        cwd = tmp_path / "solo"
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        monkeypatch.setattr("os.getcwd", lambda: str(cwd))
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        _patch_stdin_payload(
            monkeypatch, {"session_id": "my-sid", "source": "startup"}
        )

        self._seed_project_state(tmp_path, [("my-sid", "alpha")])

        mod = self._reload_module()
        import orbit_db  # type: ignore[import-not-found]

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = None
        monkeypatch.setattr(orbit_db, "TaskDB", lambda: mock_db)
        mod.main()

        out = capsys.readouterr().out
        assert "Parallel Orbit Session Warning" not in out

    # ── PID liveness: no warning on a serial handoff (the reported bug) ────

    @pytest.mark.parametrize("source", ["clear", "startup"])
    def test_main_no_warning_when_prior_session_is_dead(
        self, tmp_path, monkeypatch, capsys, source
    ):
        """The reported recurrence: save -> /clear -> new session, and
        quit -> relaunch. The prior session's transcript is seconds-fresh but
        its process is gone. Same setup as the different-project warning test,
        except the other session is proven dead, so NO warning must fire.

        Parametrized over the two `source` values that exercise this path
        (`clear` and a fresh `startup`); neither gets the resume-only
        exclusion, so liveness is the only thing that suppresses the warning.
        """
        self._redirect_state(monkeypatch, tmp_path)
        cwd = tmp_path / "handoff-repo"
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        monkeypatch.setattr("os.getcwd", lambda: str(cwd))
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        _patch_stdin_payload(monkeypatch, {"session_id": "my-sid", "source": source})

        # Different project on the other session: would warn if it were alive.
        self._seed_project_state(
            tmp_path, [("my-sid", "alpha"), ("prev-sid", "beta")]
        )
        self._seed_transcript(tmp_path, cwd, "prev-sid")  # fresh mtime

        mod = self._reload_module()
        # Don't depend on the live process tree for our own pid write.
        monkeypatch.setattr(mod, "_resolve_session_pid", lambda: None)
        # The prior session's recorded pid is dead -> proven not parallel.
        self._seed_pid_record(tmp_path, "prev-sid", self._dead_pid(), start_time=None)

        import orbit_db  # type: ignore[import-not-found]

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = None
        monkeypatch.setattr(orbit_db, "TaskDB", lambda: mock_db)
        mod.main()

        out = capsys.readouterr().out
        assert "Parallel Orbit Session Warning" not in out

    def test_main_still_warns_when_prior_session_alive(
        self, tmp_path, monkeypatch, capsys
    ):
        """Guard against over-suppression: a genuinely concurrent session
        (live pid, different project) must STILL warn."""
        self._redirect_state(monkeypatch, tmp_path)
        cwd = tmp_path / "concurrent-repo"
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        monkeypatch.setattr("os.getcwd", lambda: str(cwd))
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        _patch_stdin_payload(
            monkeypatch, {"session_id": "my-sid", "source": "startup"}
        )

        self._seed_project_state(
            tmp_path, [("my-sid", "alpha"), ("other-sid", "beta")]
        )
        self._seed_transcript(tmp_path, cwd, "other-sid")

        mod = self._reload_module()
        monkeypatch.setattr(mod, "_resolve_session_pid", lambda: None)
        # Other session's recorded pid is this (alive) test process.
        self._seed_pid_record(tmp_path, "other-sid", os.getpid(), start_time=None)

        import orbit_db  # type: ignore[import-not-found]

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = None
        monkeypatch.setattr(orbit_db, "TaskDB", lambda: mock_db)
        mod.main()

        out = capsys.readouterr().out
        assert "Parallel Orbit Session Warning" in out
        assert "- `beta`" in out

    # ── Codex P1: exclude resumed-from session from parallel detection ────

    def test_main_inherits_on_resume_when_only_prev_sids_transcript_is_fresh(
        self, tmp_path, monkeypatch
    ):
        """Codex P1 fix. On a normal solo-session resume, the previous
        session's transcript is often still fresh (closed seconds ago,
        end-of-session writes touched it). Without the cwd-pointer-based
        exclusion in main(), ``_detect_parallel_sessions`` returns the
        prev-sid, main() treats that as a parallel session, skips pickup,
        and the statusline goes blank on every normal resume. This test
        proves the fix: pickup MUST run when the only "parallel" detected
        is the resumed-from session itself.
        """
        import sqlite3 as _sqlite3

        db_path = self._redirect_state(monkeypatch, tmp_path)
        cwd = tmp_path / "normal-resume"
        cwd.mkdir(parents=True)
        monkeypatch.chdir(cwd)
        monkeypatch.setattr("os.getcwd", lambda: str(cwd))
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        _patch_stdin_payload(monkeypatch, {"session_id": "new-sid", "source": "resume"})

        cwd_key = str(cwd).replace("/", "-")
        pointer_dir = tmp_path / ".claude" / "hooks" / "state" / "cwd-session"
        pointer_dir.mkdir(parents=True, exist_ok=True)
        (pointer_dir / f"{cwd_key}.json").write_text(
            json.dumps({"sessionId": "prev-sid", "cwd": str(cwd), "updatedAt": "x"})
        )
        self._seed_project_state(tmp_path, [("prev-sid", "carried-over")])
        # Prev session's transcript is FRESH (just touched). Without the
        # fix this would make main() see prev-sid as a parallel session
        # and skip pickup.
        self._seed_transcript(tmp_path, cwd, "prev-sid")

        mod = self._reload_module()
        import orbit_db  # type: ignore[import-not-found]

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = None
        mock_db.get_task_by_name.return_value = None
        monkeypatch.setattr(orbit_db, "TaskDB", lambda: mock_db)
        mod.main()

        conn = _sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT project_name FROM project_state WHERE session_id = ?",
                ("new-sid",),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None and row[0] == "carried-over"

    def test_main_skips_pickup_when_third_session_parallel_to_resumed_pair(
        self, tmp_path, monkeypatch
    ):
        """Codex P1 fix: only the resumed-from session is excluded, not
        all fresh sessions. When a THIRD session is also alive in the same
        cwd, the ambiguous-resume path still fires and pickup is skipped.
        Without this guard the fix would over-relax the gate and
        reintroduce the wrong-project-on-resume bug.
        """
        import sqlite3 as _sqlite3

        db_path = self._redirect_state(monkeypatch, tmp_path)
        cwd = tmp_path / "three-way"
        cwd.mkdir(parents=True)
        monkeypatch.chdir(cwd)
        monkeypatch.setattr("os.getcwd", lambda: str(cwd))
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        _patch_stdin_payload(monkeypatch, {"session_id": "new-sid", "source": "resume"})

        cwd_key = str(cwd).replace("/", "-")
        pointer_dir = tmp_path / ".claude" / "hooks" / "state" / "cwd-session"
        pointer_dir.mkdir(parents=True, exist_ok=True)
        (pointer_dir / f"{cwd_key}.json").write_text(
            json.dumps({"sessionId": "prev-sid", "cwd": str(cwd), "updatedAt": "x"})
        )
        self._seed_project_state(
            tmp_path,
            [("prev-sid", "carried-over"), ("third-sid", "concurrent-project")],
        )
        # prev-sid is the resumed-from session (excluded by the fix).
        self._seed_transcript(tmp_path, cwd, "prev-sid")
        # third-sid is a different live session - the gate MUST still fire.
        self._seed_transcript(tmp_path, cwd, "third-sid")

        mod = self._reload_module()
        import orbit_db  # type: ignore[import-not-found]

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = None
        mock_db.get_task_by_name.return_value = None
        monkeypatch.setattr(orbit_db, "TaskDB", lambda: mock_db)
        mod.main()

        conn = _sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT project_name FROM project_state WHERE session_id = ?",
                ("new-sid",),
            ).fetchone()
        finally:
            conn.close()
        assert row is None, "third-sid is genuinely parallel; pickup must be skipped"

    # ── Compact source coverage ───────────────────────────────────────────

    def test_main_skips_compact_pickup_when_parallel_session_exists(
        self, tmp_path, monkeypatch
    ):
        """``source="compact"`` shares the gated-pickup path with
        ``"resume"``. The original review flagged that compact was
        documented as gated but never tested. This is the compact-path
        analog of test_main_skips_resume_pickup_when_parallel_session_exists.
        """
        import sqlite3 as _sqlite3

        db_path = self._redirect_state(monkeypatch, tmp_path)
        cwd = tmp_path / "compact-collide"
        cwd.mkdir(parents=True)
        monkeypatch.chdir(cwd)
        monkeypatch.setattr("os.getcwd", lambda: str(cwd))
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        _patch_stdin_payload(monkeypatch, {"session_id": "new-sid", "source": "compact"})

        cwd_key = str(cwd).replace("/", "-")
        pointer_dir = tmp_path / ".claude" / "hooks" / "state" / "cwd-session"
        pointer_dir.mkdir(parents=True, exist_ok=True)
        (pointer_dir / f"{cwd_key}.json").write_text(
            json.dumps({"sessionId": "prev-sid", "cwd": str(cwd), "updatedAt": "x"})
        )
        self._seed_project_state(
            tmp_path,
            [("prev-sid", "carried-over"), ("other-sid", "concurrent-project")],
        )
        self._seed_transcript(tmp_path, cwd, "other-sid")

        mod = self._reload_module()
        import orbit_db  # type: ignore[import-not-found]

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = None
        mock_db.get_task_by_name.return_value = None
        monkeypatch.setattr(orbit_db, "TaskDB", lambda: mock_db)
        mod.main()

        conn = _sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT project_name FROM project_state WHERE session_id = ?",
                ("new-sid",),
            ).fetchone()
        finally:
            conn.close()
        assert row is None, "compact path must gate pickup the same as resume"

    # ── _detect_parallel_sessions boundary at the freshness threshold ─────

    @pytest.mark.parametrize(
        "offset_seconds,expected_in_parallel",
        [
            (-599, True),   # 1s inside the window
            (-600, True),   # exactly at the boundary (>= comparison)
            (-601, False),  # 1s outside the window
        ],
        ids=["inside_by_1s", "exactly_at_threshold", "outside_by_1s"],
    )
    def test_detect_threshold_boundary(
        self, tmp_path, monkeypatch, offset_seconds, expected_in_parallel
    ):
        """Lock in the ``>=`` semantics of the freshness threshold so a
        future edit changing it to ``>`` (or shifting the constant) is
        caught. Pin ``time.time`` to a fixed value so the seed's mtime and
        the production code's threshold agree (without this, wall-clock
        advance between the two calls makes the exactly-at-threshold case
        flaky).
        """
        import time as _time

        frozen_now = 1_700_000_000.0
        monkeypatch.setattr(_time, "time", lambda: frozen_now)

        self._redirect_state(monkeypatch, tmp_path)
        cwd = tmp_path / "boundary"
        cwd.mkdir()
        self._seed_transcript(
            tmp_path, cwd, "other-sid", mtime_offset_seconds=offset_seconds
        )

        mod = self._reload_module()
        result = mod._detect_parallel_sessions(cwd, "my-sid")
        if expected_in_parallel:
            assert result == ["other-sid"]
        else:
            assert result == []

    # ── _projects_for_sessions DB error paths ─────────────────────────────

    def test_projects_for_sessions_returns_empty_on_connect_error(
        self, tmp_path, monkeypatch, capsys
    ):
        """``sqlite3.Error`` (non-OperationalError) raised by connect()
        must NOT propagate. A breadcrumb fires so silent degradation is
        visible in the session transcript JSONL under ``~/.claude/projects/``.
        """
        import sqlite3 as _sqlite3

        self._redirect_state(monkeypatch, tmp_path)

        def _broken_connect(*args, **kwargs):
            raise _sqlite3.DatabaseError("simulated DB corruption")

        monkeypatch.setattr(_sqlite3, "connect", _broken_connect)

        mod = self._reload_module()
        assert mod._projects_for_sessions(["sid-a"]) == {}
        err = capsys.readouterr().err
        assert "project_state connect failed" in err

    def test_projects_for_sessions_returns_empty_on_query_error(
        self, tmp_path, monkeypatch, capsys
    ):
        """If the DB connects but the query raises a non-OperationalError
        (schema drift, programming error), return ``{}`` with a stderr
        breadcrumb. sqlite3.Connection is immutable in 3.11+, so use a
        fake connection returned by a patched connect().
        """
        import sqlite3 as _sqlite3

        self._redirect_state(monkeypatch, tmp_path)

        class _FakeConn:
            def execute(self, *args, **kwargs):
                raise _sqlite3.DatabaseError("simulated schema drift")

            def close(self):
                pass

        monkeypatch.setattr(_sqlite3, "connect", lambda *a, **kw: _FakeConn())

        mod = self._reload_module()
        assert mod._projects_for_sessions(["sid-a"]) == {}
        err = capsys.readouterr().err
        assert "project_state batch lookup failed" in err

    def test_projects_for_sessions_silent_on_operational_error(
        self, tmp_path, monkeypatch, capsys
    ):
        """OperationalError (lock contention, missing-table-on-fresh-install)
        is the expected-recoverable case and must stay silent - it self-heals
        on the next SessionStart fire. Locks in the OperationalError /
        broader-Error split documented in the docstring.
        """
        import sqlite3 as _sqlite3

        self._redirect_state(monkeypatch, tmp_path)

        def _locked_connect(*args, **kwargs):
            raise _sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(_sqlite3, "connect", _locked_connect)

        mod = self._reload_module()
        assert mod._projects_for_sessions(["sid-a"]) == {}
        err = capsys.readouterr().err
        assert "project_state" not in err


# ── activity_tracker ──────────────────────────────────────────────────────


class TestActivityTracker:
    """Tests for the UserPromptSubmit heartbeat hook (activity_tracker.py).

    The hook is a thin wrapper around a subprocess invocation; its contract
    is exactly the argv, env, and exception-swallowing behavior. These tests
    are the rename tripwire for the bundled-orbit-db wiring: any mechanical
    rename sweep that renames the ``orbit_db`` module or the bundled
    ``orbit-db`` directory must update the literals here too, or the hook
    breaks silently on every prompt.
    """

    def _reload_module(self):
        import importlib
        import hooks.activity_tracker as mod

        importlib.reload(mod)
        return mod

    def _feed_stdin(self, monkeypatch, payload: dict) -> None:
        monkeypatch.setattr("sys.stdin", StringIO(json.dumps(payload)))

    def test_invokes_orbit_db_heartbeat_auto_with_exact_argv(self, monkeypatch):
        """argv must be exactly [sys.executable, "-m", "orbit_db", "heartbeat-auto"].

        This is the rename tripwire. The literal "orbit_db" is the Python
        module name spawned by the subprocess; the literal "heartbeat-auto"
        is the CLI subcommand on that module. A rename sweep that misses
        either string here would silently break time tracking for every
        prompt without raising - the hook swallows OSError from a missing
        module.
        """
        recorded: dict = {}

        def _recorder(argv, **kwargs):
            recorded["argv"] = argv
            recorded["kwargs"] = kwargs
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        monkeypatch.setattr(subprocess, "run", _recorder)
        self._feed_stdin(
            monkeypatch,
            {"prompt": "do something real", "session_id": "sid-x", "cwd": "/tmp"},
        )

        mod = self._reload_module()
        mod.main()

        assert recorded["argv"] == [sys.executable, "-m", "orbit_db", "heartbeat-auto"]

    def test_subprocess_env_carries_bundled_orbit_db_on_pythonpath(self, monkeypatch):
        """PYTHONPATH passed to the subprocess must contain the bundled
        ``orbit-db`` directory path segment.

        The marketplace install ships orbit-db source inside the plugin tree
        rather than installing it to site-packages, so the subprocess can
        only import it if the bundled dir is on PYTHONPATH. A rename of
        either the bundled dir name or the env-var injection logic must
        be caught here.
        """
        recorded: dict = {}

        def _recorder(argv, **kwargs):
            recorded["env"] = kwargs.get("env")
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        monkeypatch.setattr(subprocess, "run", _recorder)
        self._feed_stdin(
            monkeypatch,
            {"prompt": "do something real", "session_id": "sid-x", "cwd": "/tmp"},
        )

        mod = self._reload_module()
        mod.main()

        env = recorded["env"]
        assert env is not None, "subprocess.run must be called with an explicit env="
        assert "PYTHONPATH" in env
        bundled = str(mod._BUNDLED_ORBIT_DB)
        # The bundled dir must appear as a discrete segment of PYTHONPATH
        # (split on os.pathsep) so existing PYTHONPATH entries can coexist
        # without breaking the subprocess import.
        segments = env["PYTHONPATH"].split(os.pathsep)
        assert bundled in segments, (
            f"PYTHONPATH segments {segments!r} must include bundled orbit-db dir "
            f"{bundled!r} so the subprocess can import the module."
        )

    def test_subprocess_env_carries_claude_session_id(self, monkeypatch):
        """CLAUDE_SESSION_ID is the only signal the heartbeat subprocess has
        for which session this prompt belongs to. Lock the env var name and
        the value pass-through so a rename of the env var (or accidentally
        dropping the wiring) is caught.
        """
        recorded: dict = {}

        def _recorder(argv, **kwargs):
            recorded["env"] = kwargs.get("env")
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        monkeypatch.setattr(subprocess, "run", _recorder)
        self._feed_stdin(
            monkeypatch,
            {"prompt": "real work", "session_id": "abc-123", "cwd": "/tmp"},
        )

        mod = self._reload_module()
        mod.main()

        assert recorded["env"]["CLAUDE_SESSION_ID"] == "abc-123"

    def test_oserror_from_subprocess_is_swallowed(self, monkeypatch):
        """The hook contract documents ``except (TimeoutExpired, OSError): pass``
        so a missing python interpreter, broken venv, or fork() failure does
        not crash Claude Code on prompt submit. Without this test, a refactor
        that narrows the except clause would slip through review and start
        propagating exceptions on every prompt.
        """

        def _exploder(argv, **kwargs):
            raise OSError("simulated fork failure")

        monkeypatch.setattr(subprocess, "run", _exploder)
        self._feed_stdin(
            monkeypatch,
            {"prompt": "work", "session_id": "sid-x", "cwd": "/tmp"},
        )

        mod = self._reload_module()
        # Must not raise. The hook is a fire-and-forget side effect; any
        # exception here would interrupt the user's prompt submission.
        mod.main()

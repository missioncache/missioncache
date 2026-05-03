"""Integration tests for session_start, pre_compact, and stop hooks.

Tests mock orbit_db and use tmp_path for file I/O.
"""

import json
import os
import re
import time
from datetime import datetime
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


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

    def test_writes_pending_task_json(self, tmp_path, monkeypatch):
        """write_pending_task creates a valid JSON file."""
        state_dir = tmp_path / ".claude" / "hooks" / "state"
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        # Import after monkeypatch so Path.home() is patched
        import importlib
        import hooks.session_start as mod

        importlib.reload(mod)
        mod.write_pending_task("test-task", "/some/path")

        pending_file = state_dir / "pending-task.json"
        assert pending_file.exists()

        data = json.loads(pending_file.read_text())
        assert data["taskName"] == "test-task"
        assert data["cwd"] == "/some/path"
        assert "timestamp" in data

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
        """Full main() flow: new sid inherits the project bound to the previous sid."""
        import sqlite3 as _sqlite3

        db_path = self._redirect_state(monkeypatch, tmp_path)
        cwd = tmp_path / "resume" / "repo"
        cwd.mkdir(parents=True)
        monkeypatch.chdir(cwd)
        monkeypatch.setattr("os.getcwd", lambda: str(cwd))
        monkeypatch.setenv("CLAUDE_SESSION_ID", "new-sid")

        self._seed_pointer(tmp_path, cwd, "prev-sid")
        self._seed_project_state(tmp_path, [("prev-sid", "carried-over")])

        mod = self._reload_module()
        # Replace TaskDB on the real orbit_db module with a no-task mock so
        # find_task_for_cwd returns None (we're testing the pickup path, not
        # the existing task-detection path).
        import orbit_db  # type: ignore[import-not-found]

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = None
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

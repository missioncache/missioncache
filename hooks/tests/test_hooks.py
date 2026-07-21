"""Integration tests for session_start, pre_compact, and stop hooks.

Tests mock missioncache_db and use tmp_path for file I/O.
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

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = mock_task
        mock_db.get_task_time.return_value = 0
        mock_db.format_duration.return_value = "0m"

        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-42")
        monkeypatch.setattr("os.getcwd", lambda: "/fake/repo")

        # Real (empty) data root: a bare MagicMock root would make every
        # .exists() truthy on the MISSIONCACHE_ROOT resolution path.
        with patch.dict("sys.modules", {"missioncache_db": MagicMock(TaskDB=lambda: mock_db, MISSIONCACHE_ROOT=tmp_path / "mcroot-empty")}):
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

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = mock_task
        mock_db.get_task_time.return_value = 3600
        mock_db.format_duration.return_value = "1h 0m"

        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-99")
        monkeypatch.setattr("os.getcwd", lambda: "/repo")

        # Real (empty) data root - see test_find_task_for_cwd_integration.
        with patch.dict("sys.modules", {"missioncache_db": MagicMock(TaskDB=lambda: mock_db, MISSIONCACHE_ROOT=tmp_path / "mcroot-empty")}):
            import importlib
            import hooks.session_start as mod

            importlib.reload(mod)
            mod.main()

        output = capsys.readouterr().out
        assert "context-task" in output
        assert "PROJ-999" in output
        assert "1h 0m" in output


class TestSessionStartStrictBinding:
    """A session is bound to a project ONLY by an explicit action
    (/missioncache:load, /missioncache:new) or by sitting under
    ~/.missioncache/active/<task>/. The old "inherit whatever project last
    ran in this cwd" path was removed because a repo root is shared across
    unrelated work: inheriting on cwd alone silently mis-attributed
    heartbeats/time to a repo-mate task and self-perpetuated across sessions.

    These tests pin the new contract: resume does NOT auto-bind, and instead
    surfaces a one-line hint nudging the user to /missioncache:load.
    """

    @staticmethod
    def _redirect_state(monkeypatch, home: Path) -> Path:
        import missioncache_db  # type: ignore[import-not-found]

        monkeypatch.setattr("pathlib.Path.home", lambda: home)
        db_path = home / ".claude" / "hooks-state.db"
        monkeypatch.setattr(missioncache_db, "HOOKS_STATE_DB_PATH", db_path)
        return db_path

    @classmethod
    def _seed_project_state(cls, home: Path, rows: list[tuple[str, str]]) -> Path:
        import sqlite3 as _sqlite3
        from missioncache_db import init_hooks_state_db_schema  # type: ignore[import-not-found]

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

    # ── regression: resume no longer auto-inherits a repo-mate project ────

    def test_resume_does_not_inherit_previous_cwd_owner_project(
        self, tmp_path, monkeypatch
    ):
        """THE bug: a new session resuming at a repo root where a previous
        session was bound to project X must NOT inherit X - neither in
        project_state nor as a projects/<sid>.json pointer. This is what
        leaked unrelated sessions' heartbeats onto a repo-mate task.
        """
        import sqlite3 as _sqlite3

        db_path = self._redirect_state(monkeypatch, tmp_path)
        cwd = tmp_path / "repo"
        cwd.mkdir(parents=True)
        monkeypatch.chdir(cwd)
        monkeypatch.setattr("os.getcwd", lambda: str(cwd))
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        _patch_stdin_payload(
            monkeypatch, {"session_id": "new-sid", "source": "resume"}
        )

        # Previous session "prev-sid" owned this cwd and was bound to "avc".
        # Under the old inherit logic, new-sid would copy "avc".
        cwd_key = str(cwd).replace("/", "-")
        pointer_dir = tmp_path / ".claude" / "hooks" / "state" / "cwd-session"
        pointer_dir.mkdir(parents=True, exist_ok=True)
        (pointer_dir / f"{cwd_key}.json").write_text(
            json.dumps({"sessionId": "prev-sid", "cwd": str(cwd), "updatedAt": "x"})
        )
        self._seed_project_state(tmp_path, [("prev-sid", "avc")])

        mod = self._reload_module()
        import missioncache_db  # type: ignore[import-not-found]

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = None
        mock_db.get_repos.return_value = []  # keep the hint path quiet here
        # get_task_by_name=None makes the OLD inherit's cwd-compat gate fall to
        # its conservative "inherit anyway" branch, so the old code WOULD bind
        # avc onto new-sid. This is what makes the assertion below a real
        # regression check: it fails against the pre-fix code, passes after.
        mock_db.get_task_by_name.return_value = None
        monkeypatch.setattr(missioncache_db, "TaskDB", lambda: mock_db)
        mod.main()

        # new-sid must have NO project_state row...
        conn = _sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT project_name FROM project_state WHERE session_id = ?",
                ("new-sid",),
            ).fetchone()
        finally:
            conn.close()
        assert row is None, "resume must NOT auto-bind a repo-mate project"

        # ...and NO per-session pointer (which would route heartbeats).
        pointer = (
            tmp_path / ".claude" / "hooks" / "state" / "projects" / "new-sid.json"
        )
        assert not pointer.exists(), "resume must NOT write projects/<sid>.json"

    def test_resume_emits_hint_when_repo_has_active_projects(
        self, tmp_path, monkeypatch, capsys
    ):
        """Replacement for the inherit: on an unbound resume, nudge the user
        to /missioncache:load instead of guessing."""
        self._redirect_state(monkeypatch, tmp_path)
        cwd = tmp_path / "repo"
        cwd.mkdir(parents=True)
        monkeypatch.chdir(cwd)
        monkeypatch.setattr("os.getcwd", lambda: str(cwd))
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        _patch_stdin_payload(
            monkeypatch, {"session_id": "new-sid", "source": "resume"}
        )

        mod = self._reload_module()
        import missioncache_db  # type: ignore[import-not-found]

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = None
        mock_db.get_repos.return_value = [
            SimpleNamespace(id=7, path=str(cwd), short_name="repo")
        ]
        mock_db.get_active_tasks.return_value = [
            SimpleNamespace(name="avc-in-house-testing"),
            SimpleNamespace(name="tigen-nightly-stabilization"),
        ]
        monkeypatch.setattr(missioncache_db, "TaskDB", lambda: mock_db)
        mod.main()

        out = capsys.readouterr().out
        assert "avc-in-house-testing" in out
        assert "/missioncache:load" in out
        # It must call get_active_tasks scoped to the matched repo, not globally.
        mock_db.get_active_tasks.assert_called_once_with(7)

    def test_compact_emits_hint_when_unbound(self, tmp_path, monkeypatch, capsys):
        """A compact normally keeps the same session_id, so a bound session
        resolves via find_task_for_cwd and skips this branch. But if a
        compacted session has no binding (e.g. never bound, or a future
        Claude Code re-mints the id on compact), it gets the same nudge as
        resume rather than a silent blank statusline."""
        self._redirect_state(monkeypatch, tmp_path)
        cwd = tmp_path / "repo"
        cwd.mkdir(parents=True)
        monkeypatch.chdir(cwd)
        monkeypatch.setattr("os.getcwd", lambda: str(cwd))
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        _patch_stdin_payload(
            monkeypatch, {"session_id": "new-sid", "source": "compact"}
        )

        mod = self._reload_module()
        import missioncache_db  # type: ignore[import-not-found]

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = None
        mock_db.get_repos.return_value = [
            SimpleNamespace(id=7, path=str(cwd), short_name="repo")
        ]
        mock_db.get_active_tasks.return_value = [
            SimpleNamespace(name="avc-in-house-testing")
        ]
        monkeypatch.setattr(missioncache_db, "TaskDB", lambda: mock_db)
        mod.main()

        out = capsys.readouterr().out
        assert "avc-in-house-testing" in out
        assert "/missioncache:load" in out

    def test_no_hint_on_startup(self, tmp_path, monkeypatch, capsys):
        """The hint is scoped to continuation events (resume/compact). A fresh
        startup in a repo with active projects stays silent (no inherit, no
        nudge)."""
        self._redirect_state(monkeypatch, tmp_path)
        cwd = tmp_path / "repo"
        cwd.mkdir(parents=True)
        monkeypatch.chdir(cwd)
        monkeypatch.setattr("os.getcwd", lambda: str(cwd))
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        _patch_stdin_payload(
            monkeypatch, {"session_id": "new-sid", "source": "startup"}
        )

        mod = self._reload_module()
        import missioncache_db  # type: ignore[import-not-found]

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = None
        mock_db.get_repos.return_value = [
            SimpleNamespace(id=7, path=str(cwd), short_name="repo")
        ]
        mock_db.get_active_tasks.return_value = [SimpleNamespace(name="avc")]
        monkeypatch.setattr(missioncache_db, "TaskDB", lambda: mock_db)
        mod.main()

        out = capsys.readouterr().out
        assert "/missioncache:load" not in out
        mock_db.get_active_tasks.assert_not_called()

    def test_no_hint_when_task_resolves(self, tmp_path, monkeypatch, capsys):
        """When find_task_for_cwd resolves a task (explicit pointer or cwd under
        ~/.missioncache/active/<task>/), the Active Task path runs and no hint
        fires - the session is legitimately bound."""
        self._redirect_state(monkeypatch, tmp_path)
        cwd = tmp_path / "repo"
        cwd.mkdir(parents=True)
        monkeypatch.chdir(cwd)
        monkeypatch.setattr("os.getcwd", lambda: str(cwd))
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        _patch_stdin_payload(
            monkeypatch, {"session_id": "new-sid", "source": "resume"}
        )

        mod = self._reload_module()
        import missioncache_db  # type: ignore[import-not-found]

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = SimpleNamespace(
            id=1, name="bound-task", status="active", jira_key=None,
            repo_id=None, full_path="active/bound-task",
        )
        mock_db.get_task_time.return_value = 0
        mock_db.format_duration.return_value = "0m"
        monkeypatch.setattr(missioncache_db, "TaskDB", lambda: mock_db)
        mod.main()

        out = capsys.readouterr().out
        assert "Active Task Detected" in out
        assert "/missioncache:load <name>" not in out
        mock_db.get_active_tasks.assert_not_called()

    # ── _resume_hint_for_cwd unit tests ───────────────────────────────────

    def test_hint_none_when_cwd_outside_all_repos(self, tmp_path):
        mod = self._reload_module()
        db = MagicMock()
        db.get_repos.return_value = [
            SimpleNamespace(id=1, path=str(tmp_path / "other"), short_name="other")
        ]
        assert mod._resume_hint_for_cwd(db, str(tmp_path / "elsewhere")) is None

    def test_hint_none_when_repo_has_no_active_tasks(self, tmp_path):
        mod = self._reload_module()
        cwd = tmp_path / "repo"
        cwd.mkdir()
        db = MagicMock()
        db.get_repos.return_value = [
            SimpleNamespace(id=1, path=str(cwd), short_name="repo")
        ]
        db.get_active_tasks.return_value = []
        assert mod._resume_hint_for_cwd(db, str(cwd)) is None

    def test_hint_picks_most_specific_repo(self, tmp_path):
        mod = self._reload_module()
        parent = tmp_path / "work"
        child = parent / "repo"
        child.mkdir(parents=True)
        db = MagicMock()
        db.get_repos.return_value = [
            SimpleNamespace(id=1, path=str(parent), short_name="work"),
            SimpleNamespace(id=2, path=str(child), short_name="repo"),
        ]
        db.get_active_tasks.return_value = [SimpleNamespace(name="t")]
        result = mod._resume_hint_for_cwd(db, str(child))
        assert result is not None
        # most-specific repo (child, id=2) drives the active-task lookup
        db.get_active_tasks.assert_called_once_with(2)
        assert "repo" in result

    def test_hint_caps_listed_projects(self, tmp_path):
        mod = self._reload_module()
        cwd = tmp_path / "repo"
        cwd.mkdir()
        db = MagicMock()
        db.get_repos.return_value = [
            SimpleNamespace(id=1, path=str(cwd), short_name="repo")
        ]
        db.get_active_tasks.return_value = [
            SimpleNamespace(name=f"t{i}") for i in range(8)
        ]
        result = mod._resume_hint_for_cwd(db, str(cwd))
        assert result is not None
        assert "`t0`" in result and "`t4`" in result
        assert "`t5`" not in result
        assert "(+3 more)" in result


class TestGetSessionContextValidation:
    """Direct unit tests for ``get_session_context`` validation + precedence.

    These tests exercise ``get_session_context`` in isolation to lock the
    security and precedence invariants (path-traversal rejection, env-var vs
    stdin precedence) that the surrounding ``main()`` flow relies on.
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
    3. Writes a sticky error file on terminal failure for /missioncache:load to
       surface on next resume.
    """

    def _setup_task(self, tmp_path, ctx_seed=None):
        """Build a task dir + mock task/repo. Returns (task_dir, ctx_file, mocks).

        The task dir lives under the DATA ROOT (tmp mcroot), NOT under the
        repo path - matching the real ~/.missioncache layout. The original
        fixture placed it under the mock repo path, which replicated the
        legacy path join the hook wrongly used, so every test passed while
        the hook silently no-oped in production (found 2026-07-15).
        """
        task_dir = tmp_path / "mcroot" / "active" / "compact-task"
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
        # No repo mock on purpose: the hook must never resolve MissionCache
        # files through the repo, and it no longer reads repos at all.
        return task_dir, ctx_file, mock_task

    def _run(self, monkeypatch, mock_db, transcript_path=None, tmp_path=None, session_id=None):
        """Reload pre_compact with stdin payload and mock missioncache_db.

        The mock carries the REAL context_health module: the hook routes its
        Recent Changes prepend through
        ``missioncache_db.context_health.prepend_recent_changes``, and the
        tests assert on real file content, so that path must not be mocked.
        MISSIONCACHE_ROOT is a real path (the fixture's data root) - a
        MagicMock here would make every ``.exists()`` truthy and mask
        resolution bugs.
        """
        from missioncache_db import context_health as real_context_health
        from missioncache_db import read_session_binding as real_read_session_binding

        payload = {"transcript_path": str(transcript_path) if transcript_path else "", "cwd": "/fake/cwd"}
        if session_id:
            payload["session_id"] = session_id
        monkeypatch.setattr("sys.stdin", StringIO(json.dumps(payload)))
        with patch.dict(
            "sys.modules",
            {
                "missioncache_db": MagicMock(
                    TaskDB=lambda: mock_db,
                    context_health=real_context_health,
                    # Real function on purpose: the bound-vs-unbound sticky
                    # tests verify real binding-file path construction and
                    # parsing (a MagicMock here would unpack garbage and
                    # silently take the no-binding path).
                    read_session_binding=real_read_session_binding,
                    MISSIONCACHE_ROOT=(tmp_path or Path("/nonexistent")) / "mcroot",
                )
            },
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
        _task_dir, ctx_file, mock_task = self._setup_task(tmp_path)

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = mock_task
        mock_db.process_heartbeats.return_value = 0

        self._run(monkeypatch, mock_db, tmp_path=tmp_path)

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
        _task_dir, ctx_file, mock_task = self._setup_task(tmp_path)

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

        self._run(monkeypatch, mock_db, transcript_path=transcript, tmp_path=tmp_path)

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

        _task_dir, ctx_file, _ = self._setup_task(tmp_path)
        original_content = ctx_file.read_text()

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.side_effect = sqlite3.OperationalError(
            "database is locked"
        )

        mod = self._run(monkeypatch, mock_db, tmp_path=tmp_path)

        assert mod.ERROR_FILE.exists(), "sticky error file should be written"
        sticky = json.loads(mod.ERROR_FILE.read_text())
        assert "database is locked" in sticky["reason"]
        assert "find_task_for_cwd" in sticky["reason"]
        # context.md should be untouched - DB lookup never succeeded
        assert ctx_file.read_text() == original_content

    def test_bound_session_unresolvable_writes_sticky_error(
        self, tmp_path, monkeypatch
    ):
        """A session WITH a binding on file whose resolution returns None is a
        bug condition (duplicate name, archived task, stale binding) and must
        surface via the sticky error, not repeat the silent bail that hid the
        cwd-veto resolution bug (found 2026-07-16)."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        binding_dir = tmp_path / ".claude" / "hooks" / "state" / "projects"
        binding_dir.mkdir(parents=True)
        (binding_dir / "sess-x.json").write_text(
            json.dumps({"projectName": "ghost-proj", "sessionId": "sess-x"})
        )

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = None

        mod = self._run(monkeypatch, mock_db, tmp_path=tmp_path, session_id="sess-x")

        assert mod.ERROR_FILE.exists(), (
            "bound-but-unresolvable session must write a sticky error"
        )
        sticky = json.loads(mod.ERROR_FILE.read_text())
        assert "ghost-proj" in sticky["reason"]
        assert sticky["task_name"] == "ghost-proj"

    def test_unbound_session_no_task_stays_silent(self, tmp_path, monkeypatch):
        """A session with NO binding and no task is the benign everyday case
        (session simply not on a project) - no sticky error."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = None

        mod = self._run(monkeypatch, mock_db, tmp_path=tmp_path, session_id="sess-y")

        assert not mod.ERROR_FILE.exists(), (
            "unbound session must not write a sticky error"
        )

    def test_corrupt_binding_writes_sticky_error(self, tmp_path, monkeypatch):
        """A binding file that EXISTS but cannot be parsed is a bug condition
        (the session was bound; snapshots are not being saved) - it must
        alarm, not masquerade as the benign unbound case."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        binding_dir = tmp_path / ".claude" / "hooks" / "state" / "projects"
        binding_dir.mkdir(parents=True)
        (binding_dir / "sess-z.json").write_text("{corrupt json!")

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = None

        mod = self._run(monkeypatch, mock_db, tmp_path=tmp_path, session_id="sess-z")

        assert mod.ERROR_FILE.exists(), (
            "present-but-unreadable binding must write a sticky error"
        )
        sticky = json.loads(mod.ERROR_FILE.read_text())
        assert "unreadable" in sticky["reason"]
        assert "sess-z" in sticky["reason"]

    def test_successful_run_clears_prior_sticky_error(
        self, tmp_path, monkeypatch
    ):
        """A successful run removes any leftover sticky error file from a
        previous failed compaction so /missioncache:load does not surface stale warnings."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        _task_dir, _ctx_file, mock_task = self._setup_task(tmp_path)

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

        mod = self._run(monkeypatch, mock_db, tmp_path=tmp_path)

        assert not mod.ERROR_FILE.exists(), (
            "successful run must clear prior sticky error file"
        )

    def test_db_lock_recovers_on_retry(self, tmp_path, monkeypatch):
        """Lock once, succeed on second attempt → no sticky error, snapshot lands."""
        import sqlite3

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        monkeypatch.setattr("time.sleep", lambda *_: None)

        _task_dir, ctx_file, mock_task = self._setup_task(tmp_path)
        original = ctx_file.read_text()

        mock_db = MagicMock()
        # First call raises locked, second call returns the task
        mock_db.find_task_for_cwd.side_effect = [
            sqlite3.OperationalError("database is locked"),
            mock_task,
        ]

        mod = self._run(monkeypatch, mock_db, tmp_path=tmp_path)

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

        with patch.dict("sys.modules", {"missioncache_db": MagicMock(TaskDB=lambda: mock_db)}):
            import importlib
            import hooks.stop as mod

            importlib.reload(mod)
            mod.main()

    def test_detects_edits_shows_reminder(self, tmp_path, monkeypatch, capsys):
        """stop shows missioncache reminder when transcript contains Write/Edit tool uses.

        Uses the real Claude Code transcript shape: a top-level ``assistant``
        record whose ``message.content`` is a list of blocks, one a compact-JSON
        ``tool_use`` block. Written with compact separators (no space after the
        colon) to match how Claude Code serializes transcripts.
        """
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            # A record with an explicit null message must not abort the scan:
            # ``rec.get("message", {})`` returns None here (the key exists), so
            # a naive ``.get("content")`` chain would AttributeError and the
            # outer except would silently skip the reminder. The Edit below it
            # must still be seen.
            json.dumps({"type": "user", "message": None}, separators=(",", ":"))
            + "\n"
            + json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "editing now"},
                            {"type": "tool_use", "id": "t1", "name": "Edit", "input": {}},
                        ]
                    },
                },
                separators=(",", ":"),
            )
            + "\n"
            + json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "tool_use", "id": "t2", "name": "Write", "input": {}},
                        ]
                    },
                },
                separators=(",", ":"),
            )
            + "\n"
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
        assert "missioncache:save" in err.lower() or "MissionCache Reminder" in err

    def test_no_reminder_when_no_edits(self, tmp_path, monkeypatch, capsys):
        """stop does not show reminder when transcript has no Write/Edit tool uses."""
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "tool_use", "id": "r1", "name": "Read", "input": {}},
                        ]
                    },
                },
                separators=(",", ":"),
            )
            + "\n"
        )

        mock_db = MagicMock()

        self._run_stop(
            monkeypatch,
            {"transcript_path": str(transcript), "cwd": str(tmp_path)},
            mock_db,
        )

        err = capsys.readouterr().err
        assert "MissionCache Reminder" not in err


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
        """Create fake MissionCache project files under tmp_path's fake HOME.

        Points Path.home() at tmp_path so the hook's orbit_root resolution
        (~/.missioncache) lands in our sandbox. Returns a fake task object
        ready to be plugged into `mock_db.find_task_for_cwd.return_value`.
        """
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        orbit_dir = tmp_path / ".missioncache" / "active" / "fake-task"
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

        module_patch = {"missioncache_db": MagicMock(TaskDB=lambda: mock_db)}
        with patch.dict("sys.modules", module_patch):
            import importlib
            import hooks.task_tracker as mod

            importlib.reload(mod)
            mod.main()

    def test_no_active_project_silent(self, monkeypatch, capsys):
        """Returns silently when there's no MissionCache project for the cwd."""
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
        assert "MissionCache task tracking divergence" in out
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
            {"session_id": "s1", "cwd": "/tmp", "prompt": "/missioncache:save"},
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

        Mirrors the layout that missioncache_db's scan_repo treats as a subtask
        marker. Verifies the hook falls back to the non-prefixed filenames
        when the prefixed form is absent.
        """
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        # Subtask dir: active/parent-task/sub-task with plain tasks.md/context.md
        subtask_dir = (
            tmp_path / ".missioncache" / "active" / "parent-task" / "sub-task"
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

    def test_divergence_deduped_within_session(
        self, tmp_path, monkeypatch, capsys
    ):
        """Same divergence set surfaces once per session, not on every prompt."""
        task = self._setup_project(
            tmp_path,
            monkeypatch,
            tasks_content="- [ ] 2. Framework wiring review\n",
            context_content="### Task 2: Framework wiring review\nfindings\n",
        )
        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = task
        stdin = {"session_id": "s1", "cwd": str(tmp_path), "prompt": "hello"}

        # First prompt: divergence surfaces.
        self._run_tracker(monkeypatch, dict(stdin), mock_db)
        assert "Task 2: Framework wiring review" in capsys.readouterr().out

        # Second prompt, identical divergence state: stays silent.
        self._run_tracker(monkeypatch, dict(stdin), mock_db)
        assert capsys.readouterr().out == ""

    def test_divergence_refires_when_set_changes(
        self, tmp_path, monkeypatch, capsys
    ):
        """A changed divergent set re-surfaces within the same session."""
        task = self._setup_project(
            tmp_path,
            monkeypatch,
            tasks_content="- [ ] 2. First\n- [ ] 3. Second\n",
            context_content="### Task 2: First\nf\n### Task 3: Second\nf\n",
        )
        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = task
        stdin = {"session_id": "s1", "cwd": str(tmp_path), "prompt": "hello"}

        self._run_tracker(monkeypatch, dict(stdin), mock_db)
        assert "Task 2: First" in capsys.readouterr().out

        # Same set again -> silent.
        self._run_tracker(monkeypatch, dict(stdin), mock_db)
        assert capsys.readouterr().out == ""

        # Mark task 2 done: divergent set shrinks to {3}, so it re-fires.
        tasks_file = (
            tmp_path
            / ".missioncache"
            / "active"
            / "fake-task"
            / "fake-task-tasks.md"
        )
        tasks_file.write_text("- [x] 2. First\n- [ ] 3. Second\n")
        self._run_tracker(monkeypatch, dict(stdin), mock_db)
        out = capsys.readouterr().out
        assert "Task 3: Second" in out
        assert "Task 2:" not in out

    def test_divergence_refires_after_clearing_and_recurring(
        self, tmp_path, monkeypatch, capsys
    ):
        """Clearing then re-introducing the same divergence re-fires the reminder."""
        task = self._setup_project(
            tmp_path,
            monkeypatch,
            tasks_content="- [ ] 2. Framework wiring review\n",
            context_content="### Task 2: Framework wiring review\nfindings\n",
        )
        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = task
        stdin = {"session_id": "s1", "cwd": str(tmp_path), "prompt": "hello"}
        tasks_file = (
            tmp_path
            / ".missioncache"
            / "active"
            / "fake-task"
            / "fake-task-tasks.md"
        )

        # First divergence fires and records state.
        self._run_tracker(monkeypatch, dict(stdin), mock_db)
        assert "Task 2: Framework wiring review" in capsys.readouterr().out

        # Divergence clears (box checked): silent, and stored state is reset.
        tasks_file.write_text("- [x] 2. Framework wiring review\n")
        self._run_tracker(monkeypatch, dict(stdin), mock_db)
        assert capsys.readouterr().out == ""

        # Same divergence recurs (box unchecked again): fires again.
        tasks_file.write_text("- [ ] 2. Framework wiring review\n")
        self._run_tracker(monkeypatch, dict(stdin), mock_db)
        assert "Task 2: Framework wiring review" in capsys.readouterr().out

    def test_hierarchical_numbering_divergence(
        self, tmp_path, monkeypatch, capsys
    ):
        """Hierarchical task numbers (1.2.) match both pending and heading regexes."""
        task = self._setup_project(
            tmp_path,
            monkeypatch,
            tasks_content="- [x] 1.1. Done sub\n- [ ] 1.2. Wire the thing\n",
            context_content=(
                "### Task 1.1: Done sub\nf\n### Task 1.2: Wire the thing\nf\n"
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
        assert "Task 1.2: Wire the thing" in out
        assert "Task 1.1:" not in out

    # ── staleness signal (context saved after tasks last changed) ─────────

    _STALE_TASKS = (
        "# Fake - Tasks\n\n"
        "**Last Updated:** 2026-07-21 10:00\n\n"
        "- [ ] 1. First thing\n"
        "- [ ] 2. Second thing\n"
        "- [ ] Unnumbered extra\n"
    )
    _STALE_CONTEXT = (
        "# Fake - Context\n"
        "**Last Updated:** 2026-07-21 12:30\n\n"
        "## Recent Changes\n\n"
        "### 2026-07-21 12:30\nProgress recorded here\n"
    )

    def test_stale_fires_when_context_saved_after_tasks(
        self, tmp_path, monkeypatch, capsys
    ):
        """Context header newer than tasks header + pending items = staleness
        reminder listing the pending tasks and both timestamps."""
        task = self._setup_project(
            tmp_path,
            monkeypatch,
            tasks_content=self._STALE_TASKS,
            context_content=self._STALE_CONTEXT,
        )
        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = task

        self._run_tracker(
            monkeypatch,
            {"session_id": "s1", "cwd": str(tmp_path), "prompt": "hello"},
            mock_db,
        )

        out = capsys.readouterr().out
        assert "tasks file may be stale" in out
        assert "2026-07-21 12:30" in out
        assert "2026-07-21 10:00" in out
        assert "Task 1: First thing" in out
        assert "Task 2: Second thing" in out
        assert "and 1 more unchecked item(s)" in out
        assert "update_tasks_file" in out

    def test_stale_silent_when_tasks_header_newer(
        self, tmp_path, monkeypatch, capsys
    ):
        """Tasks header newer than context header = silent, even though the
        context file's MTIME is newer (headers take priority over mtime)."""
        task = self._setup_project(
            tmp_path,
            monkeypatch,
            tasks_content=self._STALE_TASKS.replace(
                "2026-07-21 10:00", "2026-07-21 13:00"
            ),
            context_content=self._STALE_CONTEXT,
            context_newer=True,  # mtime says context is newer; header wins
        )
        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = task

        self._run_tracker(
            monkeypatch,
            {"session_id": "s1", "cwd": str(tmp_path), "prompt": "hello"},
            mock_db,
        )

        assert capsys.readouterr().out == ""

    def test_stale_deduped_per_context_stamp(
        self, tmp_path, monkeypatch, capsys
    ):
        """The staleness reminder fires once per context save, not per prompt."""
        task = self._setup_project(
            tmp_path,
            monkeypatch,
            tasks_content=self._STALE_TASKS,
            context_content=self._STALE_CONTEXT,
        )
        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = task
        stdin = {"session_id": "s1", "cwd": str(tmp_path), "prompt": "hello"}

        self._run_tracker(monkeypatch, dict(stdin), mock_db)
        assert "tasks file may be stale" in capsys.readouterr().out

        self._run_tracker(monkeypatch, dict(stdin), mock_db)
        assert capsys.readouterr().out == ""

    def test_stale_refires_after_new_context_save_and_clears_on_catchup(
        self, tmp_path, monkeypatch, capsys
    ):
        """Full lifecycle: fire -> tasks catch up (silent, marker cleared) ->
        a later context save re-fires."""
        task = self._setup_project(
            tmp_path,
            monkeypatch,
            tasks_content=self._STALE_TASKS,
            context_content=self._STALE_CONTEXT,
        )
        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = task
        stdin = {"session_id": "s1", "cwd": str(tmp_path), "prompt": "hello"}
        project_dir = tmp_path / ".missioncache" / "active" / "fake-task"
        tasks_file = project_dir / "fake-task-tasks.md"
        context_file = project_dir / "fake-task-context.md"

        self._run_tracker(monkeypatch, dict(stdin), mock_db)
        assert "tasks file may be stale" in capsys.readouterr().out

        # Tasks file updated after the context save: silent.
        tasks_file.write_text(
            self._STALE_TASKS.replace("2026-07-21 10:00", "2026-07-21 12:45")
        )
        self._run_tracker(monkeypatch, dict(stdin), mock_db)
        assert capsys.readouterr().out == ""

        # A later context save makes it stale again: re-fires.
        context_file.write_text(
            self._STALE_CONTEXT.replace("2026-07-21 12:30", "2026-07-21 14:00")
        )
        self._run_tracker(monkeypatch, dict(stdin), mock_db)
        assert "tasks file may be stale" in capsys.readouterr().out

    def test_stale_mtime_fallback_when_headers_missing(
        self, tmp_path, monkeypatch, capsys
    ):
        """Without Last Updated headers, mtime ordering drives the signal."""
        task = self._setup_project(
            tmp_path,
            monkeypatch,
            tasks_content="- [ ] 1. Only pending thing\n",
            context_content="## Recent Changes\n\nsome progress\n",
            context_newer=True,
        )
        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = task

        self._run_tracker(
            monkeypatch,
            {"session_id": "s1", "cwd": str(tmp_path), "prompt": "hello"},
            mock_db,
        )

        out = capsys.readouterr().out
        assert "tasks file may be stale" in out
        assert "Task 1: Only pending thing" in out

    def test_precise_divergence_wins_over_staleness(
        self, tmp_path, monkeypatch, capsys
    ):
        """When both signals are present, only the precise block is printed."""
        task = self._setup_project(
            tmp_path,
            monkeypatch,
            tasks_content=self._STALE_TASKS,
            context_content=self._STALE_CONTEXT
            + "\n### Task 1: First thing\nfindings\n",
        )
        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = task

        self._run_tracker(
            monkeypatch,
            {"session_id": "s1", "cwd": str(tmp_path), "prompt": "hello"},
            mock_db,
        )

        out = capsys.readouterr().out
        assert "task tracking divergence" in out
        assert "tasks file may be stale" not in out

    def test_stale_equal_headers_tiebreak_on_mtime(
        self, tmp_path, monkeypatch, capsys
    ):
        """Equal minute-resolution headers fall back to mtime ordering."""
        equal_tasks = self._STALE_TASKS.replace(
            "2026-07-21 10:00", "2026-07-21 12:30"
        )
        # Context mtime newer than tasks: stale fires despite equal headers.
        task = self._setup_project(
            tmp_path,
            monkeypatch,
            tasks_content=equal_tasks,
            context_content=self._STALE_CONTEXT,
            context_newer=True,
        )
        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = task

        self._run_tracker(
            monkeypatch,
            {"session_id": "s1", "cwd": str(tmp_path), "prompt": "hello"},
            mock_db,
        )
        assert "tasks file may be stale" in capsys.readouterr().out

        # Tasks mtime newer (the normal context-then-tasks save flow): silent.
        project_dir = tmp_path / ".missioncache" / "active" / "fake-task"
        os.utime(project_dir / "fake-task-tasks.md", (3000, 3000))
        self._run_tracker(
            monkeypatch,
            {"session_id": "s2", "cwd": str(tmp_path), "prompt": "hello"},
            mock_db,
        )
        assert capsys.readouterr().out == ""

    def test_stale_refires_for_second_save_in_same_minute(
        self, tmp_path, monkeypatch, capsys
    ):
        """Two context saves sharing a header minute are distinct save
        generations (mtime_ns dedup), so the second still gets its reminder."""
        task = self._setup_project(
            tmp_path,
            monkeypatch,
            tasks_content=self._STALE_TASKS,
            context_content=self._STALE_CONTEXT,
        )
        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = task
        stdin = {"session_id": "s1", "cwd": str(tmp_path), "prompt": "hello"}
        context_file = (
            tmp_path / ".missioncache" / "active" / "fake-task"
            / "fake-task-context.md"
        )

        self._run_tracker(monkeypatch, dict(stdin), mock_db)
        assert "tasks file may be stale" in capsys.readouterr().out

        # Re-save the context with the SAME header stamp (new mtime_ns).
        context_file.write_text(self._STALE_CONTEXT + "\nmore progress\n")
        self._run_tracker(monkeypatch, dict(stdin), mock_db)
        assert "tasks file may be stale" in capsys.readouterr().out

    def test_stale_fires_after_ignored_precise_when_new_save_lands(
        self, tmp_path, monkeypatch, capsys
    ):
        """An unchanged (ignored) precise divergence set must not starve the
        staleness signal: a later context save still surfaces it."""
        context_with_heading = (
            self._STALE_CONTEXT + "\n### Task 1: First thing\nfindings\n"
        )
        task = self._setup_project(
            tmp_path,
            monkeypatch,
            tasks_content=self._STALE_TASKS,
            context_content=context_with_heading,
        )
        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = task
        stdin = {"session_id": "s1", "cwd": str(tmp_path), "prompt": "hello"}
        context_file = (
            tmp_path / ".missioncache" / "active" / "fake-task"
            / "fake-task-context.md"
        )

        # First prompt: precise block only (it stamps this save generation).
        self._run_tracker(monkeypatch, dict(stdin), mock_db)
        out = capsys.readouterr().out
        assert "task tracking divergence" in out
        assert "tasks file may be stale" not in out

        # Same state again: fully silent.
        self._run_tracker(monkeypatch, dict(stdin), mock_db)
        assert capsys.readouterr().out == ""

        # A NEW context save (same divergent set, still stale): the staleness
        # reminder fires even though the precise set never changed.
        context_file.write_text(
            context_with_heading.replace("2026-07-21 12:30", "2026-07-21 14:00")
        )
        self._run_tracker(monkeypatch, dict(stdin), mock_db)
        out = capsys.readouterr().out
        assert "tasks file may be stale" in out
        assert "task tracking divergence" not in out

    def test_legacy_list_state_file_still_dedups(
        self, tmp_path, monkeypatch, capsys
    ):
        """A pre-existing list-shaped dedup file (old format) suppresses the
        same precise divergence set instead of being discarded."""
        task = self._setup_project(
            tmp_path,
            monkeypatch,
            tasks_content="- [ ] 2. Framework wiring review\n",
            context_content="### Task 2: Framework wiring review\nfindings\n",
            # Tasks mtime newer: keeps the staleness signal out of the
            # picture so this test isolates the precise-signal dedup.
            context_newer=False,
        )
        state_dir = tmp_path / ".claude" / "hooks" / "state"
        state_dir.mkdir(parents=True)
        (state_dir / "divergence-s1.json").write_text("[2]")

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = task

        self._run_tracker(
            monkeypatch,
            {"session_id": "s1", "cwd": str(tmp_path), "prompt": "hello"},
            mock_db,
        )

        assert capsys.readouterr().out == ""


# ── session_start task discipline reminder ────────────────────────────────


class TestSessionStartTaskDiscipline:
    """Verify the session_start hook includes the trimmed task-tracking pointer."""

    def test_output_includes_discipline_reminder(
        self, tmp_path, monkeypatch, capsys
    ):
        """session_start output points at /missioncache:load and update_tasks_file,
        and the MissionCache-files Tip renders iff the task dir resolves under
        the DATA ROOT. The task dir lives under mcroot and a decoy exists
        under the repo path, so a regression to the legacy repo-path join
        cannot pass: the mock module would lack a usable MISSIONCACHE_ROOT
        only if resolution went through the repo again (mutation guard for
        the session_start half of the 2026-07-15 path fix)."""
        # Redirect Path.home() to tmp_path so the hook's state-file writes
        # (pending-task.json, projects/<session>.json) land in our sandbox
        # instead of polluting the real ~/.claude/hooks/state/.
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        # Real on-disk task dir under the DATA ROOT - never under the repo.
        mcroot = tmp_path / "mcroot"
        task_dir = mcroot / "active" / "my-task"
        task_dir.mkdir(parents=True)
        # Deliberately NO active/my-task under the repo path: the legacy
        # repo-join world must find nothing.
        repo_path = tmp_path / "repo"
        repo_path.mkdir()

        mock_task = SimpleNamespace(
            id=1,
            name="my-task",
            status="active",
            jira_key=None,
            repo_id=10,
            full_path="active/my-task",
        )

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = mock_task
        mock_db.get_task_time.return_value = 0
        mock_db.format_duration.return_value = "0m"

        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-discipline-test")
        monkeypatch.setattr("os.getcwd", lambda: str(repo_path))

        with patch.dict(
            "sys.modules",
            {
                "missioncache_db": MagicMock(
                    TaskDB=lambda: mock_db,
                    MISSIONCACHE_ROOT=mcroot,
                )
            },
        ):
            import importlib
            import hooks.session_start as mod

            importlib.reload(mod)
            mod.main()

        output = capsys.readouterr().out
        assert "/missioncache:load" in output
        assert "update_tasks_file" in output
        # The Tip block proves the dir resolved under MISSIONCACHE_ROOT.
        assert "**MissionCache files:**" in output
        assert str(task_dir) in output

    def test_taskcreate_note_is_unconditional(
        self, tmp_path, monkeypatch, capsys
    ):
        """The 'ignore built-in TaskCreate/task tools' note fires whenever a
        task is active, even when the task's files dir does not resolve (so
        the MissionCache-files Tip block is skipped). The divergence hook only
        nudges this on divergence, so session_start must state it proactively.
        """
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        # The data root exists but holds no dir for this task -> the Tip
        # block never runs, so the note must come from the unconditional
        # line, not the Tip.
        mock_task = SimpleNamespace(
            id=1,
            name="my-task",
            status="active",
            jira_key=None,
            repo_id=None,
            full_path="active/my-task",
        )

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = mock_task
        mock_db.get_task_time.return_value = 0
        mock_db.format_duration.return_value = "0m"

        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-unconditional")
        monkeypatch.setattr("os.getcwd", lambda: str(tmp_path))

        with patch.dict(
            "sys.modules",
            {
                "missioncache_db": MagicMock(
                    TaskDB=lambda: mock_db,
                    # Real (empty) path: a MagicMock root would make
                    # .exists() truthy and wrongly render the Tip.
                    MISSIONCACHE_ROOT=tmp_path / "mcroot-empty",
                )
            },
        ):
            import importlib
            import hooks.session_start as mod

            importlib.reload(mod)
            mod.main()

        output = capsys.readouterr().out
        # The Tip block did not run (task dir absent under the data root)...
        assert "**MissionCache files:**" not in output
        # ...but the unconditional TaskCreate note still appears.
        assert "TaskCreate" in output
        assert "update_tasks_file" in output


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
        """Redirect Path.home() + HOOKS_STATE_DB_PATH into a tmp home."""
        import missioncache_db  # type: ignore[import-not-found]

        monkeypatch.setattr("pathlib.Path.home", lambda: home)
        db_path = home / ".claude" / "hooks-state.db"
        monkeypatch.setattr(missioncache_db, "HOOKS_STATE_DB_PATH", db_path)
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
        from missioncache_db import init_hooks_state_db_schema  # type: ignore[import-not-found]

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
        assert "/missioncache:load" in warning

    def test_format_warning_handles_unbound_self(self, tmp_path, monkeypatch):
        self._redirect_state(monkeypatch, tmp_path)
        mod = self._reload_module()
        warning = mod._format_collision_warning(None, {"sid-x-12345": "beta"})
        assert "beta" in warning
        assert "/missioncache:load" in warning

    # ── main() integration: skip pickup under ambiguity ───────────────────


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
        import missioncache_db  # type: ignore[import-not-found]

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = None
        monkeypatch.setattr(missioncache_db, "TaskDB", lambda: mock_db)
        mod.main()

        out = capsys.readouterr().out
        assert "Parallel MissionCache Session Warning" in out
        # Structural assertions: "alpha" must appear as the bound-self
        # project (in the intro line), "beta" as the bullet for the other
        # session. Bare token checks would also pass if alpha/beta swapped
        # roles, which is the exact contract we want this test to enforce.
        assert "bound to MissionCache project `alpha`" in out
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
        import missioncache_db  # type: ignore[import-not-found]

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = None
        monkeypatch.setattr(missioncache_db, "TaskDB", lambda: mock_db)
        mod.main()

        out = capsys.readouterr().out
        assert "Parallel MissionCache Session Warning" not in out

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
        import missioncache_db  # type: ignore[import-not-found]

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = None
        monkeypatch.setattr(missioncache_db, "TaskDB", lambda: mock_db)
        mod.main()

        out = capsys.readouterr().out
        assert "Parallel MissionCache Session Warning" not in out

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

        import missioncache_db  # type: ignore[import-not-found]

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = None
        monkeypatch.setattr(missioncache_db, "TaskDB", lambda: mock_db)
        mod.main()

        out = capsys.readouterr().out
        assert "Parallel MissionCache Session Warning" not in out

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

        import missioncache_db  # type: ignore[import-not-found]

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = None
        monkeypatch.setattr(missioncache_db, "TaskDB", lambda: mock_db)
        mod.main()

        out = capsys.readouterr().out
        assert "Parallel MissionCache Session Warning" in out
        assert "- `beta`" in out

    # ── Codex P1: exclude resumed-from session from parallel detection ────


    # ── Compact source coverage ───────────────────────────────────────────


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
    are the rename tripwire for the bundled-missioncache-db wiring: any mechanical
    rename sweep that renames the ``missioncache_db`` module or the bundled
    ``missioncache-db`` directory must update the literals here too, or the hook
    breaks silently on every prompt.
    """

    def _reload_module(self):
        import importlib
        import hooks.activity_tracker as mod

        importlib.reload(mod)
        return mod

    def _feed_stdin(self, monkeypatch, payload: dict) -> None:
        monkeypatch.setattr("sys.stdin", StringIO(json.dumps(payload)))

    def test_invokes_missioncache_db_heartbeat_auto_with_exact_argv(self, monkeypatch):
        """argv must be exactly [sys.executable, "-m", "missioncache_db", "heartbeat-auto"].

        This is the rename tripwire. The literal "missioncache_db" is the Python
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

        assert recorded["argv"] == [sys.executable, "-m", "missioncache_db", "heartbeat-auto"]

    def test_subprocess_env_carries_bundled_missioncache_db_on_pythonpath(self, monkeypatch):
        """PYTHONPATH passed to the subprocess must contain the bundled
        ``missioncache-db`` directory path segment.

        The marketplace install ships missioncache-db source inside the plugin tree
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
        bundled = str(mod._BUNDLED_MISSIONCACHE_DB)
        # The bundled dir must appear as a discrete segment of PYTHONPATH
        # (split on os.pathsep) so existing PYTHONPATH entries can coexist
        # without breaking the subprocess import.
        segments = env["PYTHONPATH"].split(os.pathsep)
        assert bundled in segments, (
            f"PYTHONPATH segments {segments!r} must include bundled missioncache-db dir "
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

"""Integration tests for session-to-project binding in create_orbit_files / create_task.

Verifies the atomic-binding behavior added to fix the "blank statusline after
/orbit:new" bug. The slash command's prior client-side bash binding was
skippable in practice; moving the binding into the MCP tool eliminates that
failure mode by making it impossible to create a project without binding.

The binding writes two artifacts:
  1. ``project_state`` row in ``~/.claude/hooks-state.db`` (statusline reads this)
  2. ``~/.claude/hooks/state/projects/<sid>.json`` per-session pointer
     (``find_task_for_cwd`` reads this so /orbit:save works at repo root)

Both are best-effort: validation/IO failure logs a warning and returns
``session_bound=False`` in the tool response, but does NOT fail task creation.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sqlite3

import pytest

from mcp_orbit import db as db_module
from mcp_orbit import tools_docs, tools_tasks


@pytest.fixture
def isolated_orbit(tmp_path, monkeypatch):
    """Bind ORBIT_ROOT, DB_PATH, Path.home(), and HOOKS_STATE_DB_PATH to tmp.

    Mirrors test_rename.py's fixture but also redirects orbit_db's
    HOOKS_STATE_DB_PATH so the binding writes land under tmp and don't
    contaminate the user's real ~/.claude/hooks-state.db.
    """
    orbit_root = tmp_path / ".orbit"
    orbit_root.mkdir()
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    db_path = tmp_path / "tasks.db"
    hooks_db_path = fake_home / ".claude" / "hooks-state.db"

    import orbit_db
    from mcp_orbit import config, orbit

    monkeypatch.setattr(config.settings, "orbit_root", orbit_root)
    monkeypatch.setattr(config.settings, "db_path", db_path)
    monkeypatch.setattr(orbit, "settings", config.settings)
    monkeypatch.setattr(orbit_db, "ORBIT_ROOT", orbit_root)
    monkeypatch.setattr(orbit_db, "DB_PATH", db_path)
    monkeypatch.setattr(orbit_db, "HOOKS_STATE_DB_PATH", hooks_db_path)
    monkeypatch.setattr(orbit_db, "_LEGACY_DB", tmp_path / "no-legacy-db")
    monkeypatch.setattr(orbit_db, "_LEGACY_ORBIT_ROOT", tmp_path / "no-legacy-orbit")
    monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: fake_home))
    monkeypatch.setattr(db_module, "_db", None)

    return tmp_path, orbit_root, fake_home, hooks_db_path


def _read_project_state(hooks_db_path: pathlib.Path, session_id: str) -> str | None:
    """Return the project_name bound to ``session_id``, or None if no row."""
    if not hooks_db_path.exists():
        return None
    conn = sqlite3.connect(str(hooks_db_path))
    try:
        row = conn.execute(
            "SELECT project_name FROM project_state WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        # project_state table doesn't exist yet
        return None
    finally:
        conn.close()
    return row[0] if row else None


def _read_per_session_pointer(home: pathlib.Path, session_id: str) -> dict | None:
    """Return the per-session pointer JSON, or None if absent."""
    pointer = home / ".claude" / "hooks" / "state" / "projects" / f"{session_id}.json"
    if not pointer.exists():
        return None
    return json.loads(pointer.read_text())


# ── create_orbit_files binding ────────────────────────────────────────────


class TestCreateOrbitFilesBinding:
    """The coding branch of /orbit:new - binding fires when session_id provided."""

    def test_binds_session_when_session_id_provided(self, isolated_orbit):
        """Happy path: session_id resolves to a UUID, project_state gets written.

        Use a clearly-synthetic UUID (all-zeros) so a future isolation
        break would land on a phantom row instead of polluting the
        developer's real session in ~/.claude/hooks-state.db.
        """
        tmp_path, _orbit_root, fake_home, hooks_db = isolated_orbit
        repo_path = tmp_path / "myrepo"
        repo_path.mkdir()
        sid = "00000000-0000-0000-0000-000000000000"

        result = asyncio.run(
            tools_docs.create_orbit_files(
                repo_path=str(repo_path),
                project_name="my-project",
                session_id=sid,
                resolve_git_root=False,
            )
        )

        assert result.get("success") is True
        assert result.get("session_bound") is True, (
            "session_bound should be True when a valid session_id is provided"
        )
        assert _read_project_state(hooks_db, sid) == "my-project"
        pointer = _read_per_session_pointer(fake_home, sid)
        assert pointer is not None
        assert pointer["projectName"] == "my-project"
        assert pointer["sessionId"] == sid

    def test_no_binding_when_session_id_omitted(self, isolated_orbit):
        """No session_id -> no binding, but task creation still succeeds.

        Backward-compat: existing callers that don't pass session_id (CLI
        tests, scripts) keep working. session_bound=False signals the no-op.
        """
        tmp_path, _orbit_root, fake_home, hooks_db = isolated_orbit
        repo_path = tmp_path / "myrepo"
        repo_path.mkdir()

        result = asyncio.run(
            tools_docs.create_orbit_files(
                repo_path=str(repo_path),
                project_name="no-bind-project",
                resolve_git_root=False,
            )
        )

        assert result.get("success") is True
        assert result.get("session_bound") is False
        # project_state DB shouldn't even exist or should have no row
        assert _read_project_state(hooks_db, "any-sid") is None

    def test_invalid_session_id_skipped_silently(self, isolated_orbit):
        """A path-traversal-shaped session_id is rejected; task creation succeeds.

        Defense in depth: session_id flows into a filename component
        (projects/<sid>.json). Without validation, ``"../../../tmp/pwn"``
        would write outside ~/.claude/. Helper rejects on charset.
        """
        tmp_path, _orbit_root, fake_home, hooks_db = isolated_orbit
        repo_path = tmp_path / "myrepo"
        repo_path.mkdir()

        result = asyncio.run(
            tools_docs.create_orbit_files(
                repo_path=str(repo_path),
                project_name="hostile-sid-project",
                session_id="../../../tmp/pwn",
                resolve_git_root=False,
            )
        )

        # Task creation succeeds (binding failure is best-effort)
        assert result.get("success") is True
        assert result.get("session_bound") is False
        # No DB row for the hostile sid
        assert _read_project_state(hooks_db, "../../../tmp/pwn") is None
        # No pointer file written outside the projects/ dir
        traversal_target = pathlib.Path("/tmp/pwn.json")
        assert not traversal_target.exists(), (
            "path-traversal session_id must not write outside the project pointer dir"
        )

    def test_binding_overwrites_prior_session_binding(self, isolated_orbit):
        """Calling create_orbit_files for the same sid twice updates the binding.

        ON CONFLICT DO UPDATE in the upsert. Important when a session that
        already had a project bound (e.g., via /orbit:go for project A)
        creates a NEW project B - the statusline should follow to B.
        """
        tmp_path, _orbit_root, fake_home, hooks_db = isolated_orbit
        repo_path = tmp_path / "myrepo"
        repo_path.mkdir()
        sid = "shared-sid-1234"

        # Create first project
        asyncio.run(
            tools_docs.create_orbit_files(
                repo_path=str(repo_path),
                project_name="first-project",
                session_id=sid,
                resolve_git_root=False,
            )
        )
        assert _read_project_state(hooks_db, sid) == "first-project"

        # Create second project with same sid
        asyncio.run(
            tools_docs.create_orbit_files(
                repo_path=str(repo_path),
                project_name="second-project",
                session_id=sid,
                resolve_git_root=False,
            )
        )
        assert _read_project_state(hooks_db, sid) == "second-project"


# ── create_task binding (non-coding branch) ───────────────────────────────


class TestCreateTaskBinding:
    """The non-coding branch of /orbit:new - which had NO binding before this fix."""

    def test_non_coding_task_binds_session(self, isolated_orbit):
        """Non-coding /orbit:new now binds the session, fixing the prior gap.

        Before this fix, commands/new.md's non-coding branch had no
        binding step at all - the statusline was guaranteed blank for any
        non-coding project. The MCP-side binding makes coding and
        non-coding paths uniform.
        """
        _tmp_path, _orbit_root, fake_home, hooks_db = isolated_orbit
        sid = "noncoding-sid-abc"

        result = asyncio.run(
            tools_tasks.create_task(
                name="ops-followup",
                task_type="non-coding",
                jira_key="PROJ-100",
                session_id=sid,
            )
        )

        assert result.get("error") is not True
        assert result.get("session_bound") is True
        assert _read_project_state(hooks_db, sid) == "ops-followup"
        pointer = _read_per_session_pointer(fake_home, sid)
        assert pointer is not None and pointer["projectName"] == "ops-followup"

    def test_coding_task_via_create_task_also_binds(self, isolated_orbit):
        """The coding branch of create_task (parallel to create_orbit_files) binds too.

        Defensive: create_task with task_type='coding' is reachable from
        callers other than /orbit:new (tests, manual MCP). The binding
        contract should be uniform across both task types.
        """
        tmp_path, _orbit_root, _fake_home, hooks_db = isolated_orbit
        repo_path = tmp_path / "myrepo"
        repo_path.mkdir()
        sid = "coding-sid-xyz"

        result = asyncio.run(
            tools_tasks.create_task(
                name="coding-via-create-task",
                task_type="coding",
                repo_path=str(repo_path),
                session_id=sid,
            )
        )

        assert result.get("error") is not True
        assert result.get("session_bound") is True
        assert _read_project_state(hooks_db, sid) == "coding-via-create-task"

    def test_no_binding_when_session_id_omitted(self, isolated_orbit):
        """create_task without session_id reports session_bound=False, doesn't fail."""
        _tmp_path, _orbit_root, _fake_home, hooks_db = isolated_orbit

        result = asyncio.run(
            tools_tasks.create_task(
                name="no-bind-task",
                task_type="non-coding",
            )
        )

        assert result.get("error") is not True
        assert result.get("session_bound") is False
        assert _read_project_state(hooks_db, "anything") is None


# ── _bind_session_to_project unit tests ───────────────────────────────────


class TestBindSessionToProject:
    """Direct unit tests for the helper - covers branches the MCP tools rarely hit.

    These exercise the validator + writer in isolation so refactors that
    accidentally widen the validation surface (e.g., dropping the path-
    traversal check) get caught at this layer instead of leaking through
    to the integration layer.
    """

    def test_returns_false_for_empty_session_id(self, isolated_orbit):
        from mcp_orbit.helpers import _bind_session_to_project

        assert _bind_session_to_project("", "any-project") is False
        assert _bind_session_to_project(None, "any-project") is False

    def test_returns_false_for_empty_project_name(self, isolated_orbit):
        from mcp_orbit.helpers import _bind_session_to_project

        assert _bind_session_to_project("valid-sid-1234", "") is False

    def test_returns_false_for_session_id_with_path_traversal(self, isolated_orbit):
        from mcp_orbit.helpers import _bind_session_to_project

        assert _bind_session_to_project("../etc/passwd", "any") is False
        assert _bind_session_to_project("a/b", "any") is False

    def test_returns_false_for_oversized_session_id(self, isolated_orbit):
        """129-char id is one over the bound (128); rejects."""
        from mcp_orbit.helpers import _bind_session_to_project

        assert _bind_session_to_project("a" * 129, "any") is False

    def test_accepts_uuid_shaped_session_id(self, isolated_orbit):
        """The Claude Code UUID format passes validation.

        Synthetic all-zeros UUID; never collide with a real session_id.
        """
        from mcp_orbit.helpers import _bind_session_to_project

        _tmp_path, _orbit_root, _fake_home, hooks_db = isolated_orbit
        sid = "00000000-0000-0000-0000-000000000000"
        assert _bind_session_to_project(sid, "valid-project") is True
        assert _read_project_state(hooks_db, sid) == "valid-project"

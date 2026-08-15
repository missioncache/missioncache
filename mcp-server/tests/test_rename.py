"""Integration tests for the rename_task MCP tool.

Exercises the wrapper layer end-to-end: missioncache-db's rename primitive runs
in real, the MCP tool catches its exceptions and translates them to
structured error responses.
"""

from __future__ import annotations

import asyncio
import pathlib

import pytest

from mcp_missioncache import db as db_module
from mcp_missioncache import tools_tasks


@pytest.fixture
def isolated_orbit(tmp_path, monkeypatch):
    """Bind MISSIONCACHE_ROOT, DB_PATH, and Path.home() to a tmp dir.

    Mirrors the fixture in test_repo_resolution.py - the rename primitive
    writes to MISSIONCACHE_ROOT (missioncache-db) and reads/writes ~/.claude/hooks/state
    (session pointer sweep), so both need sandboxing.
    """
    root_dir = tmp_path / ".missioncache"
    root_dir.mkdir()
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    db_path = tmp_path / "tasks.db"

    import missioncache_db
    from mcp_missioncache import config, project_files

    monkeypatch.setattr(config.settings, "root", root_dir)
    monkeypatch.setattr(config.settings, "db_path", db_path)
    monkeypatch.setattr(project_files, "settings", config.settings)
    monkeypatch.setattr(missioncache_db, "MISSIONCACHE_ROOT", root_dir)
    monkeypatch.setattr(missioncache_db, "DB_PATH", db_path)
    # Point the migration guard's legacy paths at non-existent tmp
    # locations so the user's real ~/.claude/ doesn't trigger the
    # MissionCacheMigrationRequired guard during tests.
    monkeypatch.setattr(missioncache_db, "_LEGACY_CLAUDE_DB", tmp_path / "no-legacy-db")
    monkeypatch.setattr(missioncache_db, "_LEGACY_CLAUDE_ORBIT_ROOT", tmp_path / "no-legacy-orbit")
    monkeypatch.setattr(missioncache_db, "_LEGACY_ORBIT_DB", tmp_path / "no-legacy-orbit-db")
    monkeypatch.setattr(missioncache_db, "_LEGACY_ORBIT_ROOT", tmp_path / "no-legacy-orbit-root")
    monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: fake_home))

    monkeypatch.setattr(db_module, "_db", None)

    return tmp_path, root_dir


def _seed(root_dir: pathlib.Path, name: str, repo_path: pathlib.Path) -> int:
    """Create an active coding task with the standard 3 MissionCache files on disk."""
    db = db_module.get_db()
    repo_path.mkdir(parents=True, exist_ok=True)
    repo_id = db.add_repo(str(repo_path), short_name=repo_path.name)
    task = db.create_task(name=name, task_type="coding", repo_id=repo_id)
    with db.connection() as conn:
        conn.execute(
            "UPDATE tasks SET full_path = ? WHERE id = ?",
            (f"active/{name}", task.id),
        )
        conn.commit()
    project_dir = root_dir / "active" / name
    project_dir.mkdir(parents=True)
    titlecase = name.replace("-", " ").title()
    (project_dir / f"{name}-plan.md").write_text(
        f"# {titlecase} - Plan\n\nbody\n"
    )
    (project_dir / f"{name}-context.md").write_text(
        f"# {titlecase} - Context\n\nbody\n"
    )
    (project_dir / f"{name}-tasks.md").write_text(
        f"# {titlecase} - Tasks\n\n- [ ] 1. do thing\n"
    )
    return task.id


# ── happy path via project_name ───────────────────────────────────────────


class TestRenameTaskMCPHappyPath:
    def test_rename_by_project_name(self, isolated_orbit):
        tmp, root_dir = isolated_orbit
        repo = tmp / "repo"
        tid = _seed(root_dir, "old-mcp", repo)

        result = asyncio.run(
            tools_tasks.rename_task(
                new_name="new-mcp",
                project_name="old-mcp",
            )
        )

        assert result.get("success") is True
        assert result["changed"] is True
        assert result["task_id"] == tid
        assert result["name"] == "new-mcp"
        assert result["old_name"] == "old-mcp"
        assert result["normalized"] is False
        assert (root_dir / "active" / "new-mcp").exists()
        assert not (root_dir / "active" / "old-mcp").exists()

    def test_rename_by_task_id_normalizes_input(self, isolated_orbit):
        tmp, root_dir = isolated_orbit
        repo = tmp / "repo"
        tid = _seed(root_dir, "id-rename", repo)

        result = asyncio.run(
            tools_tasks.rename_task(new_name="  Renamed-By-ID  ", task_id=tid)
        )

        assert result["name"] == "renamed-by-id"
        assert result["normalized"] is True


# ── validation / collision / state error responses ────────────────────────


class TestRenameTaskMCPErrorMapping:
    def test_missing_task_returns_TASK_NOT_FOUND(self, isolated_orbit):
        result = asyncio.run(
            tools_tasks.rename_task(new_name="x", task_id=99999)
        )
        assert result.get("error") is True
        assert result["code"] == "TASK_NOT_FOUND"

    def test_neither_id_nor_name_returns_VALIDATION_ERROR(self, isolated_orbit):
        result = asyncio.run(tools_tasks.rename_task(new_name="x"))
        assert result.get("error") is True
        assert result["code"] == "VALIDATION_ERROR"

    def test_bad_chars_in_new_name_returns_VALIDATION_ERROR(self, isolated_orbit):
        tmp, root_dir = isolated_orbit
        _seed(root_dir, "valid-source", tmp / "repo")

        result = asyncio.run(
            tools_tasks.rename_task(
                new_name="bad name with spaces", project_name="valid-source"
            )
        )
        assert result.get("error") is True
        assert result["code"] == "VALIDATION_ERROR"
        assert "lowercase letters" in result["message"]

    def test_collision_returns_ALREADY_EXISTS(self, isolated_orbit):
        tmp, root_dir = isolated_orbit
        _seed(root_dir, "alpha", tmp / "repo")
        _seed(root_dir, "bravo", tmp / "repo")

        result = asyncio.run(
            tools_tasks.rename_task(new_name="bravo", project_name="alpha")
        )
        assert result.get("error") is True
        assert result["code"] == "ALREADY_EXISTS"

    def test_running_auto_returns_INVALID_STATE(self, isolated_orbit):
        tmp, root_dir = isolated_orbit
        tid = _seed(root_dir, "with-auto", tmp / "repo")
        db = db_module.get_db()
        with db.connection() as conn:
            conn.execute(
                "INSERT INTO auto_executions (task_id, status, started_at) "
                "VALUES (?, 'running', datetime('now'))",
                (tid,),
            )
            conn.commit()

        result = asyncio.run(
            tools_tasks.rename_task(new_name="post-auto", project_name="with-auto")
        )
        assert result.get("error") is True
        assert result["code"] == "INVALID_STATE"


class TestRenameLiveSessionsContract:
    """A rename moves the context file out from under any live peer session, so
    it carries the cross-session notification contract - looked up by the NEW
    name, since the rename's pointer sweep has already rewritten project_state.
    """

    def test_rename_reports_live_sessions_under_the_new_name(
        self, isolated_orbit, monkeypatch
    ):
        import missioncache_db
        from mcp_missioncache import helpers

        tmp, root_dir = isolated_orbit
        _seed(root_dir, "old-notify", tmp / "repo")
        seen = {"project_names": []}

        def _fake(project_name, exclude_session_id=None):
            seen["project_names"].append(project_name)
            return [{"session_id": "sid-peer", "title": project_name, "last_active": "now"}]

        monkeypatch.setattr(missioncache_db, "live_sessions_for_project", _fake)
        monkeypatch.setattr(helpers, "_resolve_session_id", lambda _=None: "sid-me")

        result = asyncio.run(
            tools_tasks.rename_task(new_name="new-notify", project_name="old-notify")
        )
        assert result["success"] is True
        # Both names are queried (sweep-failure tolerance); new name first.
        assert seen["project_names"] == ["new-notify", "old-notify"]
        assert result["live_sessions"][0]["title"] == "new-notify"

    def test_peers_stranded_under_the_old_name_still_notified(
        self, isolated_orbit, monkeypatch
    ):
        """The project_state sweep inside db.rename_task is best-effort: on
        failure the rename still returns changed=True with a warning, and the
        peer rows keep the OLD name. That peer is exactly the one that most
        needs to hear its directory moved, so the lookup covers both names
        and dedupes by session id."""
        import missioncache_db
        from mcp_missioncache import helpers

        tmp, root_dir = isolated_orbit
        _seed(root_dir, "old-stranded", tmp / "repo")

        def _fake(project_name, exclude_session_id=None):
            if project_name == "old-stranded":  # sweep never moved these rows
                return [
                    {"session_id": "sid-a", "title": "old-stranded", "last_active": "now"},
                    {"session_id": "sid-b", "title": "old-stranded-2", "last_active": "now"},
                ]
            return [  # sid-a also visible under the new name: dedupe keeps one
                {"session_id": "sid-a", "title": "old-stranded", "last_active": "now"}
            ]

        monkeypatch.setattr(missioncache_db, "live_sessions_for_project", _fake)
        monkeypatch.setattr(helpers, "_resolve_session_id", lambda _=None: "sid-me")

        result = asyncio.run(
            tools_tasks.rename_task(new_name="new-stranded", project_name="old-stranded")
        )
        assert result["success"] is True
        assert sorted(p["session_id"] for p in result["live_sessions"]) == [
            "sid-a",
            "sid-b",
        ]
        assert len(result["live_sessions"]) == 2

    def test_noop_rename_does_not_notify(self, isolated_orbit, monkeypatch):
        """changed=False means nothing moved - a notification would be noise."""
        import missioncache_db
        from mcp_missioncache import helpers

        tmp, root_dir = isolated_orbit
        _seed(root_dir, "same-name", tmp / "repo")
        monkeypatch.setattr(
            missioncache_db,
            "live_sessions_for_project",
            lambda *a, **k: [{"session_id": "s", "title": "t", "last_active": "now"}],
        )
        monkeypatch.setattr(helpers, "_resolve_session_id", lambda _=None: "sid-me")

        result = asyncio.run(
            tools_tasks.rename_task(new_name="same-name", project_name="same-name")
        )
        assert result["success"] is True
        assert result["changed"] is False
        assert "live_sessions" not in result


class TestCompleteReopenLiveSessionsContract:
    """complete_task and reopen_task move the entire project directory between
    active/ and completed/ - the strongest form of "the files moved out from
    under a live peer" - so both carry the notification contract.
    """

    @pytest.fixture
    def one_peer(self, monkeypatch):
        import missioncache_db
        from mcp_missioncache import helpers

        calls = []

        def _fake(project_name, exclude_session_id=None):
            calls.append(project_name)
            return [{"session_id": "sid-peer", "title": project_name,
                     "last_active": "now"}]

        monkeypatch.setattr(missioncache_db, "live_sessions_for_project", _fake)
        monkeypatch.setattr(helpers, "_resolve_session_id", lambda _=None: "sid-me")
        return calls

    def test_complete_then_reopen_both_notify(self, isolated_orbit, one_peer):
        tmp, root_dir = isolated_orbit
        _seed(root_dir, "lifecycle-proj", tmp / "repo")

        completed = asyncio.run(
            tools_tasks.complete_task(project_name="lifecycle-proj")
        )
        assert completed.get("error") is not True
        assert completed["live_sessions"][0]["session_id"] == "sid-peer"

        reopened = asyncio.run(
            tools_tasks.reopen_task(project_name="lifecycle-proj")
        )
        assert reopened.get("error") is not True
        assert reopened["live_sessions"][0]["session_id"] == "sid-peer"
        assert one_peer == ["lifecycle-proj", "lifecycle-proj"]

    def test_no_peers_no_key(self, isolated_orbit, monkeypatch):
        import missioncache_db
        from mcp_missioncache import helpers

        tmp, root_dir = isolated_orbit
        _seed(root_dir, "quiet-proj", tmp / "repo")
        monkeypatch.setattr(
            missioncache_db, "live_sessions_for_project", lambda *a, **k: []
        )
        monkeypatch.setattr(helpers, "_resolve_session_id", lambda _=None: "sid-me")
        completed = asyncio.run(tools_tasks.complete_task(project_name="quiet-proj"))
        assert completed.get("error") is not True
        assert "live_sessions" not in completed

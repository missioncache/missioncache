"""Integration tests for the rename_task MCP tool.

Exercises the wrapper layer end-to-end: orbit-db's rename primitive runs
in real, the MCP tool catches its exceptions and translates them to
structured error responses.
"""

from __future__ import annotations

import asyncio
import pathlib

import pytest

from mcp_orbit import db as db_module
from mcp_orbit import tools_tasks


@pytest.fixture
def isolated_orbit(tmp_path, monkeypatch):
    """Bind ORBIT_ROOT, DB_PATH, and Path.home() to a tmp dir.

    Mirrors the fixture in test_repo_resolution.py - the rename primitive
    writes to ORBIT_ROOT (orbit-db) and reads/writes ~/.claude/hooks/state
    (session pointer sweep), so both need sandboxing.
    """
    orbit_root = tmp_path / ".orbit"
    orbit_root.mkdir()
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    db_path = tmp_path / "tasks.db"

    import orbit_db
    from mcp_orbit import config, orbit

    monkeypatch.setattr(config.settings, "orbit_root", orbit_root)
    monkeypatch.setattr(config.settings, "db_path", db_path)
    monkeypatch.setattr(orbit, "settings", config.settings)
    monkeypatch.setattr(orbit_db, "ORBIT_ROOT", orbit_root)
    monkeypatch.setattr(orbit_db, "DB_PATH", db_path)
    # Point the migration guard's legacy paths at non-existent tmp
    # locations so the user's real ~/.claude/ doesn't trigger the
    # OrbitMigrationRequired guard during tests.
    monkeypatch.setattr(orbit_db, "_LEGACY_DB", tmp_path / "no-legacy-db")
    monkeypatch.setattr(orbit_db, "_LEGACY_ORBIT_ROOT", tmp_path / "no-legacy-orbit")
    monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: fake_home))

    monkeypatch.setattr(db_module, "_db", None)

    return tmp_path, orbit_root


def _seed(orbit_root: pathlib.Path, name: str, repo_path: pathlib.Path) -> int:
    """Create an active coding task with the standard 3 orbit files on disk."""
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
    project_dir = orbit_root / "active" / name
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
        tmp, orbit_root = isolated_orbit
        repo = tmp / "repo"
        tid = _seed(orbit_root, "old-mcp", repo)

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
        assert (orbit_root / "active" / "new-mcp").exists()
        assert not (orbit_root / "active" / "old-mcp").exists()

    def test_rename_by_task_id_normalizes_input(self, isolated_orbit):
        tmp, orbit_root = isolated_orbit
        repo = tmp / "repo"
        tid = _seed(orbit_root, "id-rename", repo)

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
        tmp, orbit_root = isolated_orbit
        _seed(orbit_root, "valid-source", tmp / "repo")

        result = asyncio.run(
            tools_tasks.rename_task(
                new_name="bad name with spaces", project_name="valid-source"
            )
        )
        assert result.get("error") is True
        assert result["code"] == "VALIDATION_ERROR"
        assert "lowercase letters" in result["message"]

    def test_collision_returns_ALREADY_EXISTS(self, isolated_orbit):
        tmp, orbit_root = isolated_orbit
        _seed(orbit_root, "alpha", tmp / "repo")
        _seed(orbit_root, "bravo", tmp / "repo")

        result = asyncio.run(
            tools_tasks.rename_task(new_name="bravo", project_name="alpha")
        )
        assert result.get("error") is True
        assert result["code"] == "ALREADY_EXISTS"

    def test_running_auto_returns_INVALID_STATE(self, isolated_orbit):
        tmp, orbit_root = isolated_orbit
        tid = _seed(orbit_root, "with-auto", tmp / "repo")
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

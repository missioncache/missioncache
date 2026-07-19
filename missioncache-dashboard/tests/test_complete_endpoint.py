"""Tests for the dashboard complete / reopen endpoints.

Calls the endpoint functions directly (no TestClient / lifespan boot)
with a sandboxed missioncache-db, mirroring test_rename_endpoint.py.
The composition itself is covered in
missioncache-db/tests/test_complete_reopen_project.py - this file locks
in the wire-up: 200 happy paths, 404 / 409 mappings, the DuckDB sync
trigger, and the round trip complete -> reopen.
"""

from __future__ import annotations

import asyncio
import pathlib

import pytest
from fastapi import HTTPException

import missioncache_db
from missioncache_dashboard import server


@pytest.fixture
def sandboxed(tmp_path, monkeypatch):
    missioncache_root = tmp_path / ".missioncache"
    (missioncache_root / "active").mkdir(parents=True)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    db_path = tmp_path / "tasks.db"

    monkeypatch.setattr(missioncache_db, "MISSIONCACHE_ROOT", missioncache_root)
    monkeypatch.setattr(missioncache_db, "DB_PATH", db_path)
    monkeypatch.setattr(missioncache_db, "_LEGACY_CLAUDE_DB", tmp_path / "n1")
    monkeypatch.setattr(missioncache_db, "_LEGACY_CLAUDE_ORBIT_ROOT", tmp_path / "n2")
    monkeypatch.setattr(missioncache_db, "_LEGACY_ORBIT_DB", tmp_path / "n3")
    monkeypatch.setattr(missioncache_db, "_LEGACY_ORBIT_ROOT", tmp_path / "n4")
    monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: fake_home))

    class _FakeAnalyticsDB:
        def __init__(self):
            self.sync_calls = 0

        def sync_from_sqlite(self):
            self.sync_calls += 1
            return {"sessions": 0, "heartbeats": 0, "tasks": 0}

    fake = _FakeAnalyticsDB()
    monkeypatch.setattr(server, "get_db", lambda: fake)
    return tmp_path, missioncache_root, fake


def _seed_active(missioncache_root: pathlib.Path, name: str) -> int:
    db = missioncache_db.TaskDB(db_path=missioncache_db.DB_PATH)
    db.initialize()
    task = db.create_task(name=name, task_type="coding", repo_id=None)
    project_dir = missioncache_root / "active" / name
    project_dir.mkdir(parents=True)
    (project_dir / f"{name}-context.md").write_text("# ctx\n")
    return task.id


def test_complete_moves_files_and_syncs(sandboxed):
    _tmp, root, fake = sandboxed
    tid = _seed_active(root, "done-proj")

    result = asyncio.run(server.complete_task_endpoint(tid))

    assert result["success"] is True
    assert result["new_status"] == "completed"
    assert (root / "completed" / "done-proj").is_dir()
    assert fake.sync_calls == 1


def test_complete_unknown_id_404(sandboxed):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(server.complete_task_endpoint(99999))
    assert exc.value.status_code == 404


def test_complete_already_completed_409(sandboxed):
    _tmp, root, _fake = sandboxed
    tid = _seed_active(root, "twice-proj")
    asyncio.run(server.complete_task_endpoint(tid))

    with pytest.raises(HTTPException) as exc:
        asyncio.run(server.complete_task_endpoint(tid))
    assert exc.value.status_code == 409


def test_reopen_round_trip(sandboxed):
    _tmp, root, fake = sandboxed
    tid = _seed_active(root, "back-proj")
    asyncio.run(server.complete_task_endpoint(tid))

    result = asyncio.run(server.reopen_task_endpoint(tid))

    assert result["success"] is True
    assert result["new_status"] == "active"
    assert (root / "active" / "back-proj").is_dir()
    assert not (root / "completed" / "back-proj").exists()
    assert fake.sync_calls == 2


def test_reopen_active_project_409(sandboxed):
    _tmp, root, _fake = sandboxed
    tid = _seed_active(root, "still-active")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(server.reopen_task_endpoint(tid))
    assert exc.value.status_code == 409

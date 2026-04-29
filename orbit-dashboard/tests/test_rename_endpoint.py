"""Tests for the dashboard rename_task_endpoint.

Calls the endpoint function directly (no TestClient / lifespan boot)
with a sandboxed orbit-db so we don't touch the user's real ~/.orbit/.
The HTTPException raise paths are inspected directly via pytest.raises.

Most rename behavior is covered in orbit-db/tests/test_rename.py and
mcp-server/tests/test_rename.py - this file just locks in the
endpoint's wire-up: 200 happy path, 400 / 404 / 409 error mappings,
and the post-rename DuckDB sync trigger.
"""

from __future__ import annotations

import asyncio
import pathlib

import pytest
from fastapi import HTTPException

import orbit_db
from orbit_dashboard import server


@pytest.fixture
def sandboxed(tmp_path, monkeypatch):
    """Sandbox orbit-db's filesystem layout and ~/.claude/ for the
    duration of the test so neither the server nor the rename primitive
    touches the user's real data."""
    orbit_root = tmp_path / ".orbit"
    orbit_root.mkdir()
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    db_path = tmp_path / "tasks.db"

    monkeypatch.setattr(orbit_db, "ORBIT_ROOT", orbit_root)
    monkeypatch.setattr(orbit_db, "DB_PATH", db_path)
    monkeypatch.setattr(orbit_db, "_LEGACY_DB", tmp_path / "no-legacy-db")
    monkeypatch.setattr(orbit_db, "_LEGACY_ORBIT_ROOT", tmp_path / "no-legacy-orbit")
    monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: fake_home))

    # The endpoint also calls server.get_db() to trigger a DuckDB sync.
    # Track call count so tests can verify the trigger actually fires
    # (a no-op fake silently masks regressions where the call is removed).
    class _FakeAnalyticsDB:
        def __init__(self):
            self.sync_calls = 0

        def sync_from_sqlite(self):
            self.sync_calls += 1
            return {"sessions": 0, "heartbeats": 0, "tasks": 0}

    fake = _FakeAnalyticsDB()
    monkeypatch.setattr(server, "get_db", lambda: fake)

    return tmp_path, orbit_root, fake


def _seed_active(orbit_root: pathlib.Path, name: str, repo_path: pathlib.Path) -> int:
    db = orbit_db.TaskDB(db_path=orbit_db.DB_PATH)
    db.initialize()
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


def _call(task_id: int, body: dict):
    return asyncio.run(server.rename_task_endpoint(task_id, body))


# ── happy path ────────────────────────────────────────────────────────────


def test_rename_endpoint_returns_canonical_name(sandboxed):
    tmp, orbit_root, _fake = sandboxed
    tid = _seed_active(orbit_root, "old-api", tmp / "repo")

    result = _call(tid, {"new_name": "  New-API-Name  "})

    assert result["success"] is True
    assert result["task_id"] == tid
    assert result["name"] == "new-api-name"
    assert result["normalized"] is True
    assert (orbit_root / "active" / "new-api-name").exists()


# ── error mapping ────────────────────────────────────────────────────────


def test_missing_body_field_returns_400(sandboxed):
    with pytest.raises(HTTPException) as exc:
        _call(123, {})
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "VALIDATION_ERROR"


def test_non_string_new_name_returns_400(sandboxed):
    with pytest.raises(HTTPException) as exc:
        _call(123, {"new_name": 42})
    assert exc.value.status_code == 400


def test_unknown_task_id_returns_404(sandboxed):
    with pytest.raises(HTTPException) as exc:
        _call(99999, {"new_name": "valid"})
    assert exc.value.status_code == 404
    assert exc.value.detail["code"] == "TASK_NOT_FOUND"


def test_invalid_kebab_returns_400_validation_error(sandboxed):
    tmp, orbit_root, _fake = sandboxed
    tid = _seed_active(orbit_root, "valid-source", tmp / "repo")
    with pytest.raises(HTTPException) as exc:
        _call(tid, {"new_name": "bad name with spaces"})
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "VALIDATION_ERROR"
    assert "lowercase letters" in exc.value.detail["message"]


def test_collision_returns_409_already_exists(sandboxed):
    tmp, orbit_root, _fake = sandboxed
    _seed_active(orbit_root, "alpha", tmp / "repo")
    tid_b = _seed_active(orbit_root, "bravo", tmp / "repo")
    with pytest.raises(HTTPException) as exc:
        _call(tid_b, {"new_name": "alpha"})
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "ALREADY_EXISTS"


def test_running_auto_returns_409_invalid_state(sandboxed):
    tmp, orbit_root, _fake = sandboxed
    tid = _seed_active(orbit_root, "with-auto", tmp / "repo")
    db = orbit_db.TaskDB(db_path=orbit_db.DB_PATH)
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO auto_executions (task_id, status, started_at) "
            "VALUES (?, 'running', datetime('now'))",
            (tid,),
        )
        conn.commit()
    with pytest.raises(HTTPException) as exc:
        _call(tid, {"new_name": "post-auto"})
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "INVALID_STATE"


# ── XSS-safety (server-side parameterization smoke test) ─────────────────


def test_xss_payload_in_new_name_rejected_by_validator(sandboxed):
    """The regex rejects HTML/script content. Even if it didn't, SQL
    parameterization and the dashboard's textContent rendering would
    catch it - but failing fast at validation is cleaner."""
    tmp, orbit_root, _fake = sandboxed
    tid = _seed_active(orbit_root, "xss-target", tmp / "repo")
    with pytest.raises(HTTPException) as exc:
        _call(tid, {"new_name": "<script>alert(1)</script>"})
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "VALIDATION_ERROR"


# ── DuckDB sync trigger (the dashboard's read path consistency seam) ──────


def test_rename_triggers_duckdb_sync(sandboxed):
    """A bare no-op fake ``sync_from_sqlite`` would silently mask a
    regression that removes the call entirely from the endpoint. Track
    the call count and assert it incremented on a successful rename."""
    tmp, orbit_root, fake = sandboxed
    tid = _seed_active(orbit_root, "sync-source", tmp / "repo")

    assert fake.sync_calls == 0
    body = _call(tid, {"new_name": "sync-target"})

    assert body["success"] is True
    assert fake.sync_calls == 1
    # Empty warnings on the happy path.
    assert body.get("warnings") == []


def test_rename_returns_warning_when_duckdb_sync_fails(sandboxed, monkeypatch):
    """When the post-rename DuckDB sync raises, the endpoint must still
    return 200 (the SQLite write succeeded) but surface a warning so the
    caller knows the dashboard list will be stale until next periodic
    sync. The previous behavior swallowed silently."""
    tmp, orbit_root, fake = sandboxed
    tid = _seed_active(orbit_root, "sync-fail-source", tmp / "repo")

    def boom():
        raise RuntimeError("simulated duckdb lock contention")

    monkeypatch.setattr(fake, "sync_from_sqlite", boom)

    body = _call(tid, {"new_name": "sync-fail-target"})

    assert body["success"] is True
    assert "warnings" in body
    assert any("Dashboard list refresh failed" in w for w in body["warnings"]), (
        f"Expected dashboard refresh warning, got {body['warnings']!r}"
    )

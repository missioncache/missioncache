"""Tests for the dashboard set_task_category_endpoint.

Calls the endpoint function directly (no TestClient / lifespan boot)
with a sandboxed missioncache-db, mirroring test_rename_endpoint.py.

Category validation itself is covered in missioncache-db/tests/ - this
file locks in the endpoint's wire-up: 200 happy path (set + clear),
400 / 404 error mappings, and the post-change DuckDB sync trigger.
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
    """Sandbox missioncache-db's filesystem layout (test_rename_endpoint pattern)."""
    mc_root = tmp_path / ".missioncache"
    mc_root.mkdir()
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    db_path = tmp_path / "tasks.db"

    monkeypatch.setattr(missioncache_db, "MISSIONCACHE_ROOT", mc_root)
    monkeypatch.setattr(missioncache_db, "DB_PATH", db_path)
    monkeypatch.setattr(missioncache_db, "_LEGACY_CLAUDE_DB", tmp_path / "no-legacy-db")
    monkeypatch.setattr(missioncache_db, "_LEGACY_CLAUDE_ORBIT_ROOT", tmp_path / "no-legacy-orbit")
    monkeypatch.setattr(missioncache_db, "_LEGACY_ORBIT_DB", tmp_path / "no-legacy-orbit-db")
    monkeypatch.setattr(missioncache_db, "_LEGACY_ORBIT_ROOT", tmp_path / "no-legacy-orbit-root")
    monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: fake_home))

    class _FakeAnalyticsDB:
        def __init__(self):
            self.sync_calls = 0

        def sync_from_sqlite(self):
            self.sync_calls += 1
            return {"sessions": 0, "heartbeats": 0, "tasks": 0}

    fake = _FakeAnalyticsDB()
    monkeypatch.setattr(server, "get_db", lambda: fake)

    return tmp_path, fake


def _seed_task(name: str, category: str | None = None) -> int:
    db = missioncache_db.TaskDB(db_path=missioncache_db.DB_PATH)
    db.initialize()
    task = db.create_task(name=name, category=category)
    db.close()
    return task.id


def _stored_category(task_id: int) -> str | None:
    db = missioncache_db.TaskDB(db_path=missioncache_db.DB_PATH)
    try:
        return db.get_task(task_id).category
    finally:
        db.close()


def _call(task_id: int, body):
    return asyncio.run(server.set_task_category_endpoint(task_id, body))


# ── happy path ────────────────────────────────────────────────────────────


def test_set_category_persists_and_echoes(sandboxed):
    tid = _seed_task("categorize-me")

    result = _call(tid, {"category": "ui"})

    assert result["success"] is True
    assert result["task_id"] == tid
    assert result["category"] == "ui"
    assert _stored_category(tid) == "ui"


def test_null_category_clears(sandboxed):
    tid = _seed_task("clear-me", category="bug")

    result = _call(tid, {"category": None})

    assert result["success"] is True
    assert result["category"] is None
    assert _stored_category(tid) is None


# ── error mapping ────────────────────────────────────────────────────────


def test_missing_category_key_returns_400(sandboxed):
    with pytest.raises(HTTPException) as exc:
        _call(123, {})
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "VALIDATION_ERROR"


def test_non_string_category_returns_400(sandboxed):
    with pytest.raises(HTTPException) as exc:
        _call(123, {"category": 42})
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "VALIDATION_ERROR"


def test_unknown_task_id_returns_404(sandboxed):
    _seed_task("someone-else")  # ensure DB exists
    with pytest.raises(HTTPException) as exc:
        _call(99999, {"category": "ui"})
    assert exc.value.status_code == 404
    assert exc.value.detail["code"] == "TASK_NOT_FOUND"


def test_invalid_category_returns_400_and_preserves_stored_value(sandboxed):
    """Server-side taxonomy validation is THE guard - the frontend
    selector is not a validation layer, and a hostile value must never
    reach the DB (it would land in rendered markup)."""
    tid = _seed_task("keep-me", category="infra")
    with pytest.raises(HTTPException) as exc:
        _call(tid, {"category": "<script>alert(1)</script>"})
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "VALIDATION_ERROR"
    assert _stored_category(tid) == "infra"


# ── DuckDB sync trigger ──────────────────────────────────────────────────


def test_category_change_triggers_duckdb_sync(sandboxed):
    _tmp, fake = sandboxed
    tid = _seed_task("sync-source")

    assert fake.sync_calls == 0
    body = _call(tid, {"category": "docs"})

    assert body["success"] is True
    assert fake.sync_calls == 1
    assert body["warnings"] == []


def test_category_change_returns_warning_when_sync_fails(sandboxed, monkeypatch):
    _tmp, fake = sandboxed
    tid = _seed_task("sync-fail-source")

    def boom():
        raise RuntimeError("simulated duckdb lock contention")

    monkeypatch.setattr(fake, "sync_from_sqlite", boom)

    body = _call(tid, {"category": "perf"})

    assert body["success"] is True
    assert _stored_category(tid) == "perf"
    assert any("Dashboard list refresh failed" in w for w in body["warnings"])


def test_category_change_warns_when_sync_reports_error_key(sandboxed, monkeypatch):
    """sync_from_sqlite can also report a top-level 'error' key (e.g. its
    internal try/except caught the failure) - that shape must warn too."""
    _tmp, fake = sandboxed
    tid = _seed_task("sync-error-source")

    monkeypatch.setattr(fake, "sync_from_sqlite", lambda: {"error": "duckdb exploded"})

    body = _call(tid, {"category": "perf"})

    assert body["success"] is True
    assert any("Dashboard list refresh incomplete" in w for w in body["warnings"])


def test_category_change_warns_on_per_row_sync_failures(sandboxed, monkeypatch):
    """sync_from_sqlite reports per-row upsert failures in its RESULT dict
    (it does not raise) - the regression that froze the read path silently:
    the FK-bearing legacy DuckDB schema rejected every upsert of a task row
    referenced by sessions, and the endpoint's except-only handling never
    saw it. The endpoint must read the result and surface a warning."""
    _tmp, fake = sandboxed
    tid = _seed_task("sync-partial-source")

    monkeypatch.setattr(
        fake, "sync_from_sqlite", lambda: {"tasks_synced": 15, "tasks_sync_failed": 2}
    )

    body = _call(tid, {"category": "perf"})

    assert body["success"] is True
    assert any("Dashboard list refresh incomplete" in w for w in body["warnings"])
    assert any("2 task rows failed" in w for w in body["warnings"])


# ── custom categories CRUD (/api/categories) ─────────────────────────────


def _add_category(body):
    return asyncio.run(server.add_category_endpoint(body))


def _delete_category(name):
    return asyncio.run(server.delete_category_endpoint(name))


def _list_categories():
    return asyncio.run(server.list_categories())


class TestCustomCategoryEndpoints:
    def test_add_list_delete_roundtrip(self, sandboxed):
        _seed_task("ensure-db")  # initialize the sandboxed DB

        created = _add_category({"name": "research", "emoji": "🔬", "color": "#4dabf7"})
        assert created["success"] is True
        assert created["name"] == "research"

        listing = _list_categories()
        assert "research" in [c["name"] for c in listing["custom"]]
        assert listing["built_in"][0] == "bug"  # taxonomy order preserved

        removed = _delete_category("research")
        assert removed["success"] is True
        assert _list_categories()["custom"] == []

    def test_add_invalid_fields_return_400(self, sandboxed):
        _seed_task("ensure-db")
        for bad in (
            {"name": "bad name", "emoji": "🔬", "color": "#4dabf7"},
            {"name": "ok-name", "emoji": "", "color": "#4dabf7"},
            {"name": "ok-name", "emoji": "plaintext", "color": "#4dabf7"},
            {"name": "ok-name", "emoji": "🔬", "color": "red"},
            {"name": "bug", "emoji": "🐛", "color": "#4dabf7"},  # reserved
        ):
            with pytest.raises(HTTPException) as exc:
                _add_category(bad)
            assert exc.value.status_code == 400
            assert exc.value.detail["code"] == "VALIDATION_ERROR"

    def test_add_non_dict_body_returns_400(self, sandboxed):
        """Reachable in tests (direct function call bypasses FastAPI body
        validation) and locks the endpoint's own type guard."""
        _seed_task("ensure-db")
        with pytest.raises(HTTPException) as exc:
            _add_category(["not", "a", "dict"])
        assert exc.value.status_code == 400
        assert exc.value.detail["code"] == "VALIDATION_ERROR"

    def test_add_duplicate_returns_409(self, sandboxed):
        _seed_task("ensure-db")
        _add_category({"name": "research", "emoji": "🔬", "color": "#4dabf7"})
        with pytest.raises(HTTPException) as exc:
            _add_category({"name": "research", "emoji": "🧪", "color": "#51cf66"})
        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "ALREADY_EXISTS"

    def test_delete_unknown_returns_404(self, sandboxed):
        _seed_task("ensure-db")
        with pytest.raises(HTTPException) as exc:
            _delete_category("no-such-category")
        assert exc.value.status_code == 404

    def test_put_category_endpoint_accepts_custom(self, sandboxed):
        """The task-category endpoint accepts custom values - the primitive
        it delegates to validates against built-ins PLUS customs."""
        tid = _seed_task("custom-target")
        _add_category({"name": "research", "emoji": "🔬", "color": "#4dabf7"})

        result = _call(tid, {"category": "research"})

        assert result["success"] is True
        assert result["category"] == "research"
        assert _stored_category(tid) == "research"

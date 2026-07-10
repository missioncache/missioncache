"""SQLite -> DuckDB sync carries the category column (Task 80's early-risk check).

Runs the REAL AnalyticsDB.sync_from_sqlite() against a tmp SQLite seeded via
TaskDB and a tmp DuckDB file - no fakes - because the sync enumerates columns
explicitly and a missed column syncs silently as NULL forever.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from missioncache_db import TaskDB
from missioncache_dashboard.lib import analytics_db


@pytest.fixture
def synced_pair(tmp_path, monkeypatch):
    """A seeded tmp SQLite (source) + empty tmp DuckDB (target) pair."""
    sqlite_path = tmp_path / "tasks.db"
    duckdb_path = tmp_path / "tasks.duckdb"
    monkeypatch.setattr(analytics_db, "SQLITE_PATH", sqlite_path)

    source = TaskDB(db_path=sqlite_path)
    source.initialize()
    target = analytics_db.AnalyticsDB(db_path=duckdb_path)
    yield source, target
    source.close()
    target.close()


def _duck_category(target, name):
    with target.connection() as conn:
        row = conn.execute(
            "SELECT category FROM tasks WHERE name = ?", (name,)
        ).fetchone()
    return row[0] if row else None


class TestCategorySync:
    def test_sync_carries_category(self, synced_pair):
        """A categorized SQLite task arrives in DuckDB with its category."""
        source, target = synced_pair
        source.create_task("categorized", category="ui")
        source.create_task("uncategorized")

        result = target.sync_from_sqlite()

        assert result.get("error") is None
        assert _duck_category(target, "categorized") == "ui"
        assert _duck_category(target, "uncategorized") is None

    def test_resync_carries_category_update(self, synced_pair):
        """set_task_category after an initial sync propagates on the next sync
        (the upsert's DO UPDATE branch, which Phase 3's edit endpoint relies on)."""
        source, target = synced_pair
        task = source.create_task("recategorized")
        target.sync_from_sqlite()
        assert _duck_category(target, "recategorized") is None

        source.set_task_category(task.id, "infra")
        target.sync_from_sqlite()
        assert _duck_category(target, "recategorized") == "infra"

    def test_existing_duckdb_without_category_column_migrates(
        self, tmp_path, monkeypatch
    ):
        """A pre-category DuckDB file gains the column via the idempotent ALTER.

        Simulates a dashboard install whose tasks.duckdb predates the feature
        by dropping the column, then reopening - _ensure_core_tables must
        restore it and the next sync must fill it.
        """
        import duckdb

        sqlite_path = tmp_path / "tasks.db"
        duckdb_path = tmp_path / "tasks.duckdb"
        monkeypatch.setattr(analytics_db, "SQLITE_PATH", sqlite_path)

        source = TaskDB(db_path=sqlite_path)
        source.initialize()
        source.create_task("legacy-duck-task", category="docs")

        # Build the pre-category DuckDB shape.
        seed = analytics_db.AnalyticsDB(db_path=duckdb_path)
        with seed.connection() as conn:
            conn.execute("ALTER TABLE tasks DROP COLUMN category")
        seed.close()
        raw = duckdb.connect(str(duckdb_path))
        cols = {r[1] for r in raw.execute("PRAGMA table_info('tasks')").fetchall()}
        raw.close()
        assert "category" not in cols, "precondition: column dropped"

        reopened = analytics_db.AnalyticsDB(db_path=duckdb_path)
        try:
            result = reopened.sync_from_sqlite()
            assert result.get("error") is None
            assert _duck_category(reopened, "legacy-duck-task") == "docs"
        finally:
            reopened.close()
            source.close()

    def test_task_to_dict_exposes_category(self, synced_pair):
        """The API serialization path (/api/tasks/active) includes category."""
        source, target = synced_pair
        source.create_task("api-visible", category="perf")
        target.sync_from_sqlite()

        task = target.get_task_by_name("api-visible")
        assert task is not None
        assert task.category == "perf"
        assert task.to_dict()["category"] == "perf"


class TestMigrateScriptCategory:
    """migrate_to_duckdb.py is a SEPARATE implementation from analytics_db's
    sync (its own schema DDL + column list) and is the documented recovery
    procedure - drift there breaks recovery silently."""

    def test_migrate_script_carries_category(self, tmp_path):
        import importlib.util

        import duckdb

        script = (
            Path(__file__).resolve().parents[1] / "migrate_to_duckdb.py"
        )
        spec = importlib.util.spec_from_file_location("migrate_to_duckdb", script)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        source = TaskDB(db_path=tmp_path / "tasks.db")
        source.initialize()
        source.create_task("migrated-task", category="security")
        source.create_task("uncategorized-task")
        source.close()

        import sqlite3

        sqlite_conn = sqlite3.connect(str(tmp_path / "tasks.db"))
        sqlite_conn.row_factory = sqlite3.Row
        duck_conn = duckdb.connect(str(tmp_path / "fresh.duckdb"))
        try:
            mod.create_duckdb_schema(duck_conn)
            migrated = mod.migrate_tasks(sqlite_conn, duck_conn)
            assert migrated == 2
            rows = dict(
                duck_conn.execute("SELECT name, category FROM tasks").fetchall()
            )
            assert rows["migrated-task"] == "security"
            assert rows["uncategorized-task"] is None
        finally:
            sqlite_conn.close()
            duck_conn.close()


class TestTaxonomyFrontendSync:
    """CATEGORIES must stay in sync with the frontend icon/color maps - the
    frontend cannot import Python, so this guard test is the only enforcement.
    A category missing from the maps degrades silently to the generic coding
    icon."""

    @staticmethod
    def _js_object_keys(html: str, const_name: str) -> set[str]:
        start = html.index(f"const {const_name} = {{")
        end = html.index("};", start)
        block = html[start:end]
        return set(re.findall(r"^\s*(\w+):", block, re.M))

    def test_frontend_maps_cover_all_categories(self):
        from missioncache_db import CATEGORIES

        html = (
            Path(__file__).resolve().parents[1]
            / "missioncache_dashboard"
            / "index.html"
        ).read_text()

        icons = self._js_object_keys(html, "TASK_ICONS")
        colors = self._js_object_keys(html, "TASK_ICON_COLORS")

        assert set(CATEGORIES) == icons, (
            f"TASK_ICONS keys drifted from CATEGORIES: "
            f"missing={set(CATEGORIES) - icons}, extra={icons - set(CATEGORIES)}"
        )
        assert set(CATEGORIES) == colors, (
            f"TASK_ICON_COLORS keys drifted from CATEGORIES: "
            f"missing={set(CATEGORIES) - colors}, extra={colors - set(CATEGORIES)}"
        )

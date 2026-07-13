"""/api/tasks/active must not drop children of a completed parent.

Spec source: the fork feature contract - completing a parent while its
fork children are active keeps the children visible; the endpoint promotes
them to top-level instead of leaving them under a parent id nothing renders.
Runs the real AnalyticsDB sync (test_category_sync pattern) and calls the
endpoint function directly.
"""

from __future__ import annotations

import asyncio

import pytest

from missioncache_db import TaskDB
from missioncache_dashboard import server
from missioncache_dashboard.lib import analytics_db


@pytest.fixture
def synced_pair(tmp_path, monkeypatch):
    sqlite_path = tmp_path / "tasks.db"
    duckdb_path = tmp_path / "tasks.duckdb"
    monkeypatch.setattr(analytics_db, "SQLITE_PATH", sqlite_path)

    source = TaskDB(db_path=sqlite_path)
    source.initialize()
    target = analytics_db.AnalyticsDB(db_path=duckdb_path)
    yield source, target
    source.close()
    target.close()


@pytest.fixture
def api(synced_pair, monkeypatch):
    source, target = synced_pair
    monkeypatch.setattr(server, "get_db", lambda: target)
    monkeypatch.setattr(server, "_get_jsonl_task_times", lambda ids: {})
    return source, target


def _names(result):
    return {t["name"] for t in result["tasks"]}


class TestActiveTasksOrphanPromotion:
    def test_child_of_completed_parent_surfaces_top_level(self, api):
        source, target = api
        parent = source.create_task("parent-proj")
        child = source.create_task("child-proj")
        source.set_task_parent(child.id, parent.id)
        source.update_task_status(parent.id, "completed")
        target.sync_from_sqlite()

        result = asyncio.run(server.api_tasks_active())
        assert "child-proj" in _names(result)
        assert "parent-proj" not in _names(result)

    def test_child_of_active_parent_stays_grouped(self, api):
        source, target = api
        parent = source.create_task("parent-proj")
        child = source.create_task("child-proj")
        source.set_task_parent(child.id, parent.id)
        target.sync_from_sqlite()

        result = asyncio.run(server.api_tasks_active())
        assert "parent-proj" in _names(result)
        assert "child-proj" not in _names(result)
        parent_dict = next(t for t in result["tasks"] if t["name"] == "parent-proj")
        assert [c["name"] for c in parent_dict["subtasks"]] == ["child-proj"]

    def test_completed_grandparent_promotes_only_middle(self, api):
        """completed-P -> active-A -> active-B: A is promoted top-level and
        KEEPS B as its subtask (with combined time), B is not promoted."""
        source, target = api
        p = source.create_task("grand-p")
        a = source.create_task("mid-a")
        b = source.create_task("leaf-b")
        source.set_task_parent(a.id, p.id)
        source.set_task_parent(b.id, a.id)
        source.update_task_status(p.id, "completed")
        target.sync_from_sqlite()

        result = asyncio.run(server.api_tasks_active())
        names = _names(result)
        assert "mid-a" in names
        assert "grand-p" not in names
        assert "leaf-b" not in names  # stays under mid-a
        mid = next(t for t in result["tasks"] if t["name"] == "mid-a")
        assert [c["name"] for c in mid["subtasks"]] == ["leaf-b"]

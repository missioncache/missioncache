"""SQLite -> DuckDB sync prunes tasks deleted from the source.

Spec source: the 2026-07-30 duplicate-projects investigation. The sync's
upsert loop never deletes, and prune_task only runs from the dashboard's own
delete endpoint - so a task deleted by any other path (missioncache-db CLI,
the MCP server, hand SQL) lingered in the DuckDB read path forever and
rendered as a ghost row in the projects table (observed live: two ghosts,
ids deleted from SQLite weeks earlier). The sync must reconcile: after
upserting, mirror rows whose id no longer exists in SQLite are removed,
children first.

Same no-fakes setup as test_category_sync: real TaskDB source, real
AnalyticsDB target.
"""

from __future__ import annotations

import pytest

from missioncache_db import TaskDB
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


def _duck_ids(target, table="tasks", col="id"):
    with target.connection() as conn:
        return {r[0] for r in conn.execute(f"SELECT {col} FROM {table}").fetchall()}


class TestGhostTaskPrune:
    def test_deleted_task_is_pruned_on_next_sync(self, synced_pair):
        """A task deleted from SQLite after a sync disappears from DuckDB on
        the following sync instead of lingering as a ghost."""
        source, target = synced_pair
        keeper = source.create_task("keeper")
        ghost = source.create_task("ghost")
        target.sync_from_sqlite()
        assert _duck_ids(target) == {keeper.id, ghost.id}

        source.delete_task(ghost.id)
        result = target.sync_from_sqlite()

        assert result.get("error") is None
        assert _duck_ids(target) == {keeper.id}, \
            "The deleted task must leave the mirror; the survivor must stay"

    def test_prune_cascades_to_child_rows(self, synced_pair):
        """The ghost's dependent rows leave with it - an orphaned child row
        would break FK integrity and skew analytics aggregates."""
        source, target = synced_pair
        ghost = source.create_task("ghost-with-children")
        source.record_heartbeat(task_id=ghost.id)
        target.sync_from_sqlite()
        assert ghost.id in _duck_ids(target)

        source.delete_task(ghost.id)
        target.sync_from_sqlite()

        assert ghost.id not in _duck_ids(target)
        assert ghost.id not in _duck_ids(target, table="heartbeats", col="task_id"), \
            "Child heartbeats must be pruned with the task"

    def test_sync_without_deletions_prunes_nothing(self, synced_pair):
        """Negative guard: a normal sync must not touch live rows."""
        source, target = synced_pair
        a = source.create_task("alpha")
        b = source.create_task("beta")
        target.sync_from_sqlite()
        target.sync_from_sqlite()

        assert _duck_ids(target) == {a.id, b.id}

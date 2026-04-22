"""Integration tests for TaskDB CRUD operations.

Tests use a real SQLite database in tmp_path.
"""

import json

import pytest

from orbit_db import TaskDB


@pytest.fixture
def db(tmp_path):
    """TaskDB backed by a temporary SQLite database."""
    db_path = tmp_path / "test.db"
    db = TaskDB(db_path=db_path)
    db.initialize()
    yield db
    db.close()


# ── initialize ────────────────────────────────────────────────────────────


class TestInitialize:
    def test_creates_tables(self, db):
        """initialize() should create all required tables."""
        with db.connection() as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            table_names = {row["name"] for row in rows}

        for expected in ("repositories", "tasks", "heartbeats", "sessions", "config", "task_updates"):
            assert expected in table_names, f"Missing table: {expected}"

    def test_auto_init_without_explicit_initialize(self, tmp_path):
        """A bare TaskDB() + query should work without calling initialize() first.

        Regression: the `orbit-db list-active` CLI (and any other first-time
        caller) used to crash with `sqlite3.OperationalError: no such table:
        tasks` because __init__ only created an empty DB file. Fresh connection
        opens now auto-run the idempotent schema DDL.
        """
        db_path = tmp_path / "fresh.db"
        fresh_db = TaskDB(db_path=db_path)  # no .initialize() call
        try:
            # Any method that hits the tasks table used to crash here.
            tasks = fresh_db.get_active_tasks()
            assert tasks == []
        finally:
            fresh_db.close()


# ── create_task ───────────────────────────────────────────────────────────


class TestCreateTask:
    def test_create_coding_task(self, db):
        """create_task with type='coding' stores correct type and path prefix."""
        task = db.create_task("my-coding-task", task_type="coding")
        assert task is not None
        assert task.name == "my-coding-task"
        assert task.task_type == "coding"
        assert task.status == "active"
        assert task.full_path.startswith("manual/")

    def test_create_non_coding_task(self, db):
        """create_task with type='non-coding' stores correct type and global prefix."""
        task = db.create_task("sprint-planning", task_type="non-coding")
        assert task is not None
        assert task.name == "sprint-planning"
        assert task.task_type == "non-coding"
        assert task.full_path.startswith("global/")

    def test_create_non_coding_with_repo_raises(self, db):
        """Non-coding tasks cannot be associated with a repository."""
        with pytest.raises(ValueError, match="Non-coding tasks cannot"):
            db.create_task("standup", task_type="non-coding", repo_id=1)


# ── get_task / get_task_by_name ───────────────────────────────────────────


class TestGetTask:
    def test_get_task_by_id(self, db):
        """get_task returns the task matching the given ID."""
        created = db.create_task("lookup-task")
        fetched = db.get_task(created.id)
        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.name == "lookup-task"

    def test_get_task_not_found(self, db):
        """get_task returns None for non-existent ID."""
        assert db.get_task(99999) is None

    def test_get_task_by_name(self, db):
        """get_task_by_name returns the correct task."""
        db.create_task("named-task")
        fetched = db.get_task_by_name("named-task")
        assert fetched is not None
        assert fetched.name == "named-task"

    def test_get_task_by_name_not_found(self, db):
        """get_task_by_name returns None when name doesn't exist."""
        assert db.get_task_by_name("does-not-exist") is None


# ── complete_task / reopen_task ───────────────────────────────────────────


class TestCompleteAndReopen:
    def test_complete_task(self, db):
        """update_task_status to 'completed' sets status and triggers completed_at."""
        task = db.create_task("finish-me")
        updated = db.update_task_status(task.id, "completed")
        assert updated is not None
        assert updated.status == "completed"
        assert updated.completed_at is not None

    def test_reopen_task(self, db):
        """reopen_task sets status back to active and clears completed_at."""
        task = db.create_task("reopen-me")
        db.update_task_status(task.id, "completed")
        reopened = db.reopen_task(task.id)
        assert reopened is not None
        assert reopened.status == "active"
        assert reopened.completed_at is None


# ── add_task_update / get_task_updates ────────────────────────────────────


class TestTaskUpdates:
    def test_add_and_get_updates(self, db):
        """add_task_update inserts a note; get_task_updates retrieves it."""
        task = db.create_task("update-task")
        update_id = db.add_task_update(task.id, "Did something important")
        assert update_id > 0

        updates = db.get_task_updates(task.id)
        assert len(updates) >= 1
        assert updates[0]["note"] == "Did something important"


# ── config get/set ────────────────────────────────────────────────────────


class TestConfig:
    def test_config_get_set(self, db):
        """set_config stores a value; get_config retrieves it."""
        db.set_config("test_key", "test_value")
        assert db.get_config("test_key") == "test_value"

    def test_config_get_default(self, db):
        """get_config returns default when key doesn't exist."""
        assert db.get_config("nonexistent", "fallback") == "fallback"

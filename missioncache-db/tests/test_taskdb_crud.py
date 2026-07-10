"""Integration tests for TaskDB CRUD operations.

Tests use a real SQLite database in tmp_path.
"""

import json
import os
import subprocess
import sys

import pytest

from missioncache_db import TaskDB


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

        Regression: the `missioncache-db list-active` CLI (and any other first-time
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

    def test_create_task_with_category(self, db):
        """create_task stores a valid category and get_task round-trips it."""
        task = db.create_task("dashboard-filters", category="ui")
        assert task.category == "ui"
        assert db.get_task(task.id).category == "ui"

    def test_create_task_without_category_is_null(self, db):
        """category defaults to None (uncategorized)."""
        task = db.create_task("no-category-task")
        assert task.category is None

    def test_create_task_invalid_category_raises(self, db):
        """create_task rejects categories outside the taxonomy."""
        with pytest.raises(ValueError, match="Invalid category"):
            db.create_task("bad-cat-task", category="not-a-category")


# ── set_task_category ─────────────────────────────────────────────────────


class TestSetTaskCategory:
    def test_set_category(self, db):
        """set_task_category updates the stored category."""
        task = db.create_task("categorize-me")
        updated = db.set_task_category(task.id, "infra")
        assert updated.category == "infra"
        assert db.get_task(task.id).category == "infra"

    def test_clear_category(self, db):
        """Passing None clears the category back to uncategorized."""
        task = db.create_task("clear-me", category="bug")
        updated = db.set_task_category(task.id, None)
        assert updated.category is None

    def test_set_invalid_category_raises(self, db):
        """set_task_category rejects categories outside the taxonomy."""
        task = db.create_task("still-valid")
        with pytest.raises(ValueError, match="Invalid category"):
            db.set_task_category(task.id, "nonsense")
        assert db.get_task(task.id).category is None

    def test_set_category_missing_task_raises(self, db):
        """set_task_category on a non-existent task id raises."""
        with pytest.raises(ValueError, match="not found"):
            db.set_task_category(99999, "docs")


# ── category column migration ─────────────────────────────────────────────


class TestCategoryMigration:
    def test_existing_db_without_category_column_migrates(self, tmp_path):
        """initialize() adds the category column to a pre-category DB.

        Simulates a DB created before the category feature by dropping the
        column, then re-running initialize() - the idempotent ALTER must
        restore it and leave existing rows NULL (the dashboard's heuristic
        fallback renders those; set-category fills them by hand).
        """
        db_path = tmp_path / "legacy.db"
        db = TaskDB(db_path=db_path)
        db.initialize()
        db.create_task("pre-migration-task")
        with db.connection() as conn:
            conn.execute("ALTER TABLE tasks DROP COLUMN category")
            conn.commit()
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
            assert "category" not in cols, "precondition: column dropped"
        db.close()

        reopened = TaskDB(db_path=db_path)
        reopened.initialize()
        try:
            task = reopened.get_task_by_name("pre-migration-task")
            assert task.category is None
            reopened.set_task_category(task.id, "feature")
            assert reopened.get_task(task.id).category == "feature"
        finally:
            reopened.close()

    def test_bare_taskdb_migrates_on_first_open(self, tmp_path):
        """A bare TaskDB() (no initialize call) migrates the column on open.

        Regression: the ALTER originally lived only in initialize(), so the
        CLI / hooks path (which never calls initialize) crashed with
        "no such column: category" on the first create_task against an
        un-migrated DB. Connection-open auto-init must run the column
        migrations too.
        """
        db_path = tmp_path / "legacy.db"
        db = TaskDB(db_path=db_path)
        db.initialize()
        with db.connection() as conn:
            conn.execute("ALTER TABLE tasks DROP COLUMN category")
            conn.commit()
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
            assert "category" not in cols, "precondition: column dropped"
        db.close()

        bare = TaskDB(db_path=db_path)  # no .initialize() call
        try:
            task = bare.create_task("cli-created", category="infra")
            assert task.category == "infra"
        finally:
            bare.close()


# ── category survives lifecycle operations ────────────────────────────────


class TestCategoryLifecycle:
    def test_rescan_preserves_category(self, db, tmp_path):
        """A repo rescan must not touch a stored category.

        The /missioncache:new path registers the row via scan, then sets the
        category afterwards. Routine rescans (dashboard startup, /api/sync)
        re-run _sync_task_from_dir, whose UPDATE branch is COALESCE-only on
        other fields - this locks in that category stays out of that UPDATE.
        """
        import missioncache_db

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        repo_id = db.add_repo(str(repo_dir))
        task_dir = missioncache_db.MISSIONCACHE_ROOT / "active" / "scanned-project"
        task_dir.mkdir(parents=True)
        (task_dir / "context.md").write_text("# scanned-project - Context\n")

        [task] = db.scan_repo(repo_id)
        assert task.category is None
        db.set_task_category(task.id, "ui")

        db.scan_repo(repo_id)

        assert db.get_task(task.id).category == "ui"

    def test_complete_and_reopen_preserve_category(self, db):
        """Status walks leave category untouched."""
        task = db.create_task("lifecycle-task", category="infra")
        db.update_task_status(task.id, "completed")
        assert db.get_task(task.id).category == "infra"
        db.reopen_task(task.id)
        assert db.get_task(task.id).category == "infra"


# ── category CLI ──────────────────────────────────────────────────────────


def _run_cli(root, *args):
    """Subprocess CLI runner (test_portability.py pattern): the child process
    resolves its DB from the MISSIONCACHE_ROOT env var."""
    env = {**os.environ, "MISSIONCACHE_ROOT": str(root)}
    code = "from missioncache_db import main; main()"
    return subprocess.run(
        [sys.executable, "-c", code, *args],
        capture_output=True, text=True, env=env,
    )


class TestCategoryCLI:
    @pytest.fixture
    def cli_root(self, tmp_path):
        root = tmp_path / ".missioncache"
        (root / "active").mkdir(parents=True)
        return root

    def test_create_task_with_category_flag(self, cli_root):
        r = _run_cli(cli_root, "create-task", "--category", "ui", "cli-cat-task")
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["category"] == "ui"

    def test_set_category_and_case_insensitive_none(self, cli_root):
        task_id = json.loads(
            _run_cli(cli_root, "create-task", "clearable-task").stdout
        )["id"]

        r = _run_cli(cli_root, "set-category", str(task_id), "docs")
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["category"] == "docs"

        # The clear sentinel folds case: "None"/"NONE" clear too.
        r = _run_cli(cli_root, "set-category", str(task_id), "None")
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["category"] is None

    def test_set_category_rejects_unknown_value(self, cli_root):
        task_id = json.loads(
            _run_cli(cli_root, "create-task", "reject-task").stdout
        )["id"]
        r = _run_cli(cli_root, "set-category", str(task_id), "bogus")
        assert r.returncode == 1
        assert "Invalid category" in r.stdout


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

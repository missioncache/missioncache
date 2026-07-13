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


# ── custom categories ─────────────────────────────────────────────────────


class TestCustomCategories:
    def test_add_list_remove_roundtrip(self, db):
        created = db.add_custom_category("research", "🔬", "#4dabf7")
        assert created == {"name": "research", "emoji": "🔬", "color": "#4dabf7"}
        assert db.list_custom_categories() == [created]
        assert db.custom_category_names() == frozenset({"research"})

        assert db.remove_custom_category("research") is True
        assert db.list_custom_categories() == []
        assert db.remove_custom_category("research") is False

    def test_name_is_normalized_and_validated(self, db):
        """Names trim + lowercase; bad shapes are rejected."""
        assert db.add_custom_category("  Research  ", "🔬", "#4dabf7")["name"] == "research"
        for bad in ("", "-leading-hyphen", "has space", "UPPER!", "a" * 25):
            with pytest.raises(ValueError, match="Invalid category name"):
                db.add_custom_category(bad, "🔬", "#4dabf7")

    def test_reserved_names_rejected(self, db):
        """Built-in taxonomy names and the 'none' clear sentinel collide."""
        with pytest.raises(ValueError, match="reserved"):
            db.add_custom_category("bug", "🐛", "#ff6b6b")
        with pytest.raises(ValueError, match="reserved"):
            db.add_custom_category("none", "🚫", "#ff6b6b")

    def test_emoji_and_color_validated(self, db):
        with pytest.raises(ValueError, match="emoji is required"):
            db.add_custom_category("no-emoji", "", "#4dabf7")
        with pytest.raises(ValueError, match="emoji is required"):
            db.add_custom_category("long-emoji", "🔬" * 17, "#4dabf7")
        # Strict hex: the color lands in style attributes, so anything
        # looser (names, rgb(), url()) is a CSS injection channel.
        for bad in ("blue", "#fff", "#12345g", "url(x)", "#4dabf7;background:red"):
            with pytest.raises(ValueError, match="RRGGBB"):
                db.add_custom_category("bad-color", "🎨", bad)

    def test_emoji_content_not_just_length(self, db):
        """The field is named emoji and must hold emoji: plain ASCII text
        and markup fragments (even mixed with a real emoji) are rejected,
        so render-time escaping is never the only XSS defense."""
        for bad in ("abcdefgh", "<img/", "hi!!", "🔬<img", '🔬"x'):
            with pytest.raises(ValueError, match="emoji characters"):
                db.add_custom_category("text-emoji", bad, "#4dabf7")
        # Multi-emoji and multi-codepoint ZWJ sequences are legitimate.
        assert db.add_custom_category("dual", "🔬🧪", "#4dabf7")["emoji"] == "🔬🧪"
        assert db.add_custom_category("family", "👨‍👩‍👧", "#4dabf7")

    def test_remove_normalizes_name_like_add(self, db):
        """DELETE 'UI ' removes the row stored as 'ui' - remove uses the
        same strip+lower normalization as add."""
        db.add_custom_category("research", "🔬", "#4dabf7")
        assert db.remove_custom_category("  ReSearch  ") is True
        assert db.list_custom_categories() == []

    def test_duplicate_rejected(self, db):
        db.add_custom_category("research", "🔬", "#4dabf7")
        with pytest.raises(ValueError, match="already exists"):
            db.add_custom_category("research", "🧪", "#51cf66")

    def test_custom_category_assignable_to_tasks(self, db):
        """create_task and set_task_category accept custom categories."""
        db.add_custom_category("research", "🔬", "#4dabf7")
        task = db.create_task("custom-at-create", category="research")
        assert task.category == "research"

        other = db.create_task("custom-via-set")
        assert db.set_task_category(other.id, "research").category == "research"

    def test_unknown_category_still_rejected(self, db):
        db.add_custom_category("research", "🔬", "#4dabf7")
        with pytest.raises(ValueError, match="Invalid category"):
            db.create_task("nope", category="not-defined")

    def test_removal_orphans_keep_value(self, db):
        """Deleting a custom category leaves assigned tasks untouched (they
        render with default styling until it is re-added), but new
        assignments of the removed name are rejected."""
        db.add_custom_category("research", "🔬", "#4dabf7")
        task = db.create_task("orphan-me", category="research")
        db.remove_custom_category("research")

        assert db.get_task(task.id).category == "research"
        with pytest.raises(ValueError, match="Invalid category"):
            db.set_task_category(task.id, "research")

    def test_table_appears_on_reopen_of_pre_custom_db(self, tmp_path):
        """A pre-custom-categories DB gains the table on reopen via the
        idempotent SCHEMA_SQL executescript at connection open - no
        migration step (unlike the category COLUMN, which needed an ALTER).
        Locks the CHANGELOG claim to the column-migration test precedent."""
        db_path = tmp_path / "legacy.db"
        db = TaskDB(db_path=db_path)
        db.initialize()
        with db.connection() as conn:
            conn.execute("DROP TABLE custom_categories")
            conn.commit()
        db.close()

        bare = TaskDB(db_path=db_path)  # no .initialize() call
        try:
            created = bare.add_custom_category("research", "🔬", "#4dabf7")
            assert created["name"] == "research"
        finally:
            bare.close()


# ── set_task_jira ─────────────────────────────────────────────────────────


class TestSetTaskJira:
    def test_set_jira(self, db):
        """set_task_jira stores the key and get_task round-trips it."""
        task = db.create_task("jira-me")
        updated = db.set_task_jira(task.id, "PROJ-12345")
        assert updated.jira_key == "PROJ-12345"
        assert db.get_task(task.id).jira_key == "PROJ-12345"

    def test_clear_jira(self, db):
        """Passing None clears the JIRA key."""
        task = db.create_task("clear-jira", jira_key="PROJ-1")
        updated = db.set_task_jira(task.id, None)
        assert updated.jira_key is None

    def test_set_jira_missing_task_raises(self, db):
        """set_task_jira on a non-existent task id raises."""
        with pytest.raises(ValueError, match="not found"):
            db.set_task_jira(99999, "PROJ-1")


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


# ── fork parent linkage ───────────────────────────────────────────────────


class TestSetTaskParent:
    def test_set_and_clear_parent(self, db):
        """set_task_parent links a child to a parent and None clears it."""
        parent = db.create_task("parent-proj")
        child = db.create_task("child-proj")

        linked = db.set_task_parent(child.id, parent.id)
        assert linked.parent_id == parent.id

        cleared = db.set_task_parent(child.id, None)
        assert cleared.parent_id is None

    def test_idempotent(self, db):
        """Setting the same parent twice is a no-op, not an error."""
        parent = db.create_task("parent-proj")
        child = db.create_task("child-proj")
        db.set_task_parent(child.id, parent.id)
        again = db.set_task_parent(child.id, parent.id)
        assert again.parent_id == parent.id

    def test_self_parent_rejected(self, db):
        """A task cannot be its own parent."""
        task = db.create_task("loner")
        with pytest.raises(ValueError, match="its own parent"):
            db.set_task_parent(task.id, task.id)

    def test_missing_parent_rejected(self, db):
        """Linking to a nonexistent parent raises."""
        child = db.create_task("child-proj")
        with pytest.raises(ValueError, match="Parent task 99999 not found"):
            db.set_task_parent(child.id, 99999)

    def test_missing_child_rejected(self, db):
        """Linking a nonexistent child raises."""
        parent = db.create_task("parent-proj")
        with pytest.raises(ValueError, match="Task 99999 not found"):
            db.set_task_parent(99999, parent.id)


class TestHierarchicalOrphans:
    def test_child_of_completed_parent_surfaces_top_level(self, db):
        """A completed parent must not swallow its active children: they
        surface in top_level so every caller still sees them."""
        parent = db.create_task("parent-proj")
        child = db.create_task("child-proj")
        db.set_task_parent(child.id, parent.id)

        db.update_task_status(parent.id, "completed")

        hierarchy = db.get_active_tasks_hierarchical()
        top_names = {t.name for t in hierarchy["top_level"]}
        assert "child-proj" in top_names
        assert parent.id not in hierarchy["children"]

    def test_child_of_active_parent_stays_grouped(self, db):
        """While the parent is active the child groups under it."""
        parent = db.create_task("parent-proj")
        child = db.create_task("child-proj")
        db.set_task_parent(child.id, parent.id)

        hierarchy = db.get_active_tasks_hierarchical()
        top_names = {t.name for t in hierarchy["top_level"]}
        assert "child-proj" not in top_names
        assert [t.name for t in hierarchy["children"][parent.id]] == ["child-proj"]


class TestSetTaskParentCycles:
    def test_two_node_cycle_rejected(self, db):
        """A -> B then B -> A must raise."""
        a = db.create_task("aaa")
        b = db.create_task("bbb")
        db.set_task_parent(a.id, b.id)
        with pytest.raises(ValueError, match="cycle"):
            db.set_task_parent(b.id, a.id)

    def test_three_node_cycle_rejected(self, db):
        """A -> B -> C then C -> A must raise."""
        a = db.create_task("aaa")
        b = db.create_task("bbb")
        c = db.create_task("ccc")
        db.set_task_parent(a.id, b.id)
        db.set_task_parent(b.id, c.id)
        with pytest.raises(ValueError, match="cycle"):
            db.set_task_parent(c.id, a.id)

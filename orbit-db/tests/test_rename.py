"""Tests for TaskDB.rename_task.

Covers the full rename primitive: normalization, validation, no-op,
DB+FS collision detection, active-auto guard, file rename, H1 rewrite
(matched + edited), DB+FS atomicity, and session-pointer sweep.

Tests sandbox both ORBIT_ROOT (via monkeypatching the module constant)
and ~/.claude/ (via monkeypatching Path.home) so they never touch the
user's real orbit data.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3

import pytest

import orbit_db
from orbit_db import (
    AutoRunActiveError,
    FilesystemCollisionError,
    NameCollisionError,
    TaskDB,
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """TaskDB + sandboxed ORBIT_ROOT + sandboxed Path.home().

    Returns (db, orbit_root, fake_home) so tests can introspect both
    the filesystem state and the seeded session pointers.
    """
    orbit_root = tmp_path / "orbit"
    orbit_root.mkdir()
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    monkeypatch.setattr(orbit_db, "ORBIT_ROOT", orbit_root)
    monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: fake_home))

    db_path = tmp_path / "tasks.db"
    db = TaskDB(db_path=db_path)
    db.initialize()
    yield db, orbit_root, fake_home
    db.close()


def _seed_active_project(
    db: TaskDB,
    orbit_root: pathlib.Path,
    name: str,
    repo_path: str = "/tmp/test-repo",
):
    """Create an active coding task with the standard 3 orbit files on disk."""
    repo_id = db.add_repo(repo_path, short_name="test-repo")
    task = db.create_task(name=name, task_type="coding", repo_id=repo_id)

    # create_task uses full_path = "manual/<name>"; for filesystem tests we
    # need the active layout. Update full_path directly to "active/<name>"
    # to mirror what scan_repos would produce.
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
    return task.id, project_dir


# ── happy path ────────────────────────────────────────────────────────────


class TestRenameHappyPath:
    def test_renames_dir_files_h1_and_db_row(self, env):
        db, orbit_root, _ = env
        tid, old_dir = _seed_active_project(db, orbit_root, "kafka-consumer-fix")

        result = db.rename_task(tid, "consumer-resilience")

        # Response shape
        assert result["success"] is True
        assert result["changed"] is True
        assert result["name"] == "consumer-resilience"
        assert result["old_name"] == "kafka-consumer-fix"
        assert result["normalized"] is False
        assert result["full_path"] == "active/consumer-resilience"
        assert sorted(result["files_renamed"]) == [
            "consumer-resilience-context.md",
            "consumer-resilience-plan.md",
            "consumer-resilience-tasks.md",
        ]
        assert sorted(result["h1_rewritten"]) == [
            "consumer-resilience-context.md",
            "consumer-resilience-plan.md",
            "consumer-resilience-tasks.md",
        ]
        assert result["h1_skipped"] == []

        # Filesystem
        new_dir = orbit_root / "active" / "consumer-resilience"
        assert not old_dir.exists()
        assert new_dir.exists()
        assert (new_dir / "consumer-resilience-plan.md").read_text().startswith(
            "# Consumer Resilience - Plan"
        )
        assert (
            new_dir / "consumer-resilience-context.md"
        ).read_text().startswith("# Consumer Resilience - Context")
        assert (new_dir / "consumer-resilience-tasks.md").read_text().startswith(
            "# Consumer Resilience - Tasks"
        )

        # DB
        renamed = db.get_task(tid)
        assert renamed.name == "consumer-resilience"
        assert renamed.full_path == "active/consumer-resilience"

    def test_keeps_task_id_so_heartbeats_survive(self, env):
        db, orbit_root, _ = env
        tid, _ = _seed_active_project(db, orbit_root, "old-name")
        db.record_heartbeat(tid)

        db.rename_task(tid, "new-name")

        # task_id is stable, so heartbeat lookup still works
        with db.connection() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM heartbeats WHERE task_id = ?", (tid,)
            ).fetchone()[0]
        assert count == 1


# ── normalization ─────────────────────────────────────────────────────────


class TestRenameNormalization:
    def test_lowercase_input_marks_normalized_true(self, env):
        db, orbit_root, _ = env
        tid, _ = _seed_active_project(db, orbit_root, "old-name")

        result = db.rename_task(tid, "Kafka-Fix")

        assert result["name"] == "kafka-fix"
        assert result["normalized"] is True
        assert (orbit_root / "active" / "kafka-fix").exists()

    def test_whitespace_trimmed_marks_normalized_true(self, env):
        db, orbit_root, _ = env
        tid, _ = _seed_active_project(db, orbit_root, "old-name")

        result = db.rename_task(tid, "  fixed-name  ")

        assert result["name"] == "fixed-name"
        assert result["normalized"] is True

    def test_already_canonical_marks_normalized_false(self, env):
        db, orbit_root, _ = env
        tid, _ = _seed_active_project(db, orbit_root, "old-name")

        result = db.rename_task(tid, "fixed-name")

        assert result["normalized"] is False


# ── validation rejections ─────────────────────────────────────────────────


class TestRenameValidation:
    def test_empty_after_trim_rejected(self, env):
        db, orbit_root, _ = env
        tid, _ = _seed_active_project(db, orbit_root, "old-name")
        with pytest.raises(ValueError, match="cannot be empty"):
            db.rename_task(tid, "   ")

    def test_leading_hyphen_rejected_with_specific_message(self, env):
        db, orbit_root, _ = env
        tid, _ = _seed_active_project(db, orbit_root, "old-name")
        with pytest.raises(ValueError, match="must start with a letter or digit"):
            db.rename_task(tid, "-bad")

    def test_internal_space_rejected(self, env):
        db, orbit_root, _ = env
        tid, _ = _seed_active_project(db, orbit_root, "old-name")
        with pytest.raises(ValueError, match="lowercase letters, digits, and hyphens"):
            db.rename_task(tid, "kafka fix")

    def test_uppercase_after_normalize_pass(self, env):
        """Uppercase input is normalized to lowercase, so it does NOT raise."""
        db, orbit_root, _ = env
        tid, _ = _seed_active_project(db, orbit_root, "old-name")
        result = db.rename_task(tid, "KAFKA-FIX")
        assert result["name"] == "kafka-fix"

    def test_underscore_rejected(self, env):
        db, orbit_root, _ = env
        tid, _ = _seed_active_project(db, orbit_root, "old-name")
        with pytest.raises(ValueError, match="lowercase letters, digits, and hyphens"):
            db.rename_task(tid, "kafka_fix")

    def test_path_traversal_segment_rejected(self, env):
        db, orbit_root, _ = env
        tid, _ = _seed_active_project(db, orbit_root, "old-name")
        with pytest.raises(ValueError, match="lowercase letters, digits, and hyphens"):
            db.rename_task(tid, "../escape")


# ── no-op and missing task ────────────────────────────────────────────────


class TestRenameNoOp:
    def test_same_name_returns_changed_false_without_touching_disk(self, env):
        db, orbit_root, _ = env
        tid, project_dir = _seed_active_project(db, orbit_root, "same-name")
        plan_mtime_before = (project_dir / "same-name-plan.md").stat().st_mtime

        result = db.rename_task(tid, "same-name")

        assert result["changed"] is False
        assert result["files_renamed"] == []
        # File untouched
        assert (
            project_dir / "same-name-plan.md"
        ).stat().st_mtime == plan_mtime_before

    def test_uppercase_input_matching_canonical_is_noop(self, env):
        """Normalized uppercase input that equals current name -> no-op
        but normalized=True still set."""
        db, orbit_root, _ = env
        tid, _ = _seed_active_project(db, orbit_root, "same-name")

        result = db.rename_task(tid, "Same-Name")

        assert result["changed"] is False
        assert result["normalized"] is True


class TestRenameMissingTask:
    def test_unknown_task_id_raises(self, env):
        db, _, _ = env
        with pytest.raises(ValueError, match="No project found"):
            db.rename_task(99999, "anything")


# ── collision detection ───────────────────────────────────────────────────


class TestRenameCollisions:
    def test_db_collision_within_same_repo(self, env):
        db, orbit_root, _ = env
        tid_a, _ = _seed_active_project(db, orbit_root, "task-a")
        _seed_active_project(db, orbit_root, "task-b")

        with pytest.raises(NameCollisionError, match="already exists"):
            db.rename_task(tid_a, "task-b")

    def test_filesystem_collision_when_orphan_dir_exists(self, env):
        """A directory at the target path but no DB row -> filesystem
        collision (caught after DB collision check passes)."""
        db, orbit_root, _ = env
        tid, _ = _seed_active_project(db, orbit_root, "task-a")
        (orbit_root / "active" / "orphan-dir").mkdir(parents=True)

        with pytest.raises(FilesystemCollisionError, match="already exists"):
            db.rename_task(tid, "orphan-dir")


# ── active orbit-auto guard ───────────────────────────────────────────────


class TestRenameAutoGuard:
    def test_running_auto_execution_blocks_rename(self, env):
        db, orbit_root, _ = env
        tid, _ = _seed_active_project(db, orbit_root, "active-auto")
        with db.connection() as conn:
            conn.execute(
                "INSERT INTO auto_executions (task_id, status, started_at) "
                "VALUES (?, 'running', datetime('now'))",
                (tid,),
            )
            conn.commit()

        with pytest.raises(AutoRunActiveError, match="orbit-auto is running"):
            db.rename_task(tid, "new-name")

    def test_completed_auto_execution_does_not_block(self, env):
        db, orbit_root, _ = env
        tid, _ = _seed_active_project(db, orbit_root, "ok-auto")
        with db.connection() as conn:
            conn.execute(
                "INSERT INTO auto_executions (task_id, status, started_at) "
                "VALUES (?, 'completed', datetime('now'))",
                (tid,),
            )
            conn.commit()

        # No exception
        result = db.rename_task(tid, "renamed-after-auto")
        assert result["changed"] is True


# ── H1 rewrite scope ──────────────────────────────────────────────────────


class TestRenameH1Rewrite:
    def test_edited_h1_is_skipped_not_overwritten(self, env):
        db, orbit_root, _ = env
        tid, project_dir = _seed_active_project(db, orbit_root, "edited-proj")

        # User has edited the plan H1 - rename should LEAVE IT ALONE
        edited_h1_content = "# My Custom Plan Title\n\nbody\n"
        (project_dir / "edited-proj-plan.md").write_text(edited_h1_content)

        result = db.rename_task(tid, "renamed-proj")

        new_dir = orbit_root / "active" / "renamed-proj"
        # Plan H1 untouched
        assert (
            new_dir / "renamed-proj-plan.md"
        ).read_text() == edited_h1_content
        # Reported as skipped
        assert "renamed-proj-plan.md" in result["h1_skipped"]
        # Other two H1s match the template, so they ARE rewritten
        assert "renamed-proj-context.md" in result["h1_rewritten"]
        assert "renamed-proj-tasks.md" in result["h1_rewritten"]


# ── non-coding tasks (no orbit dir) ───────────────────────────────────────


class TestRenameNonCoding:
    def test_db_only_rename_when_no_filesystem_dir(self, env):
        db, orbit_root, _ = env
        task = db.create_task(name="non-coding-task", task_type="non-coding")
        # create_task gives full_path = "global/<name>" for non-coding;
        # there is no on-disk dir.

        result = db.rename_task(task.id, "renamed-non-coding")

        assert result["success"] is True
        assert result["changed"] is True
        assert result["files_renamed"] == []
        assert result["h1_rewritten"] == []
        # DB updated
        renamed = db.get_task(task.id)
        assert renamed.name == "renamed-non-coding"
        assert renamed.full_path == "global/renamed-non-coding"


# ── session-pointer sweep ─────────────────────────────────────────────────


class TestRenameSessionSweep:
    def test_pending_task_json_updated(self, env):
        db, orbit_root, fake_home = env
        tid, _ = _seed_active_project(db, orbit_root, "old-pointer")

        state_dir = fake_home / ".claude" / "hooks" / "state"
        state_dir.mkdir(parents=True)
        pending = state_dir / "pending-task.json"
        pending.write_text(json.dumps({"projectName": "old-pointer", "cwd": "/tmp"}))

        result = db.rename_task(tid, "new-pointer")

        assert result["sessions_updated"] >= 1
        data = json.loads(pending.read_text())
        assert data["projectName"] == "new-pointer"

    def test_per_session_projects_json_updated(self, env):
        db, orbit_root, fake_home = env
        tid, _ = _seed_active_project(db, orbit_root, "old-session")

        projects_dir = fake_home / ".claude" / "hooks" / "state" / "projects"
        projects_dir.mkdir(parents=True)
        sid_match = projects_dir / "abc.json"
        sid_match.write_text(
            json.dumps({"projectName": "old-session", "sessionId": "abc"})
        )
        sid_other = projects_dir / "def.json"
        sid_other.write_text(
            json.dumps({"projectName": "different-project", "sessionId": "def"})
        )

        db.rename_task(tid, "new-session")

        # Matching pointer updated
        assert json.loads(sid_match.read_text())["projectName"] == "new-session"
        # Non-matching pointer untouched
        assert (
            json.loads(sid_other.read_text())["projectName"] == "different-project"
        )

    def test_hooks_state_db_project_state_updated(self, env):
        db, orbit_root, fake_home = env
        tid, _ = _seed_active_project(db, orbit_root, "old-statusline")

        hooks_db_path = fake_home / ".claude" / "hooks-state.db"
        hooks_db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(hooks_db_path))
        conn.execute(
            "CREATE TABLE project_state ("
            "session_id TEXT PRIMARY KEY, "
            "project_name TEXT NOT NULL, "
            "updated_at TEXT)"
        )
        conn.execute(
            "INSERT INTO project_state VALUES (?, ?, datetime('now'))",
            ("session-xyz", "old-statusline"),
        )
        conn.commit()
        conn.close()

        result = db.rename_task(tid, "new-statusline")

        conn = sqlite3.connect(str(hooks_db_path))
        row = conn.execute(
            "SELECT project_name FROM project_state WHERE session_id = ?",
            ("session-xyz",),
        ).fetchone()
        conn.close()
        assert row[0] == "new-statusline"
        assert result["sessions_updated"] >= 1


# ── subtask rejection ─────────────────────────────────────────────────────


class TestRenameSubtask:
    def test_subtask_rename_refused(self, env):
        db, orbit_root, _ = env
        # Create parent
        parent_id, _ = _seed_active_project(db, orbit_root, "parent-task")

        # Create subtask manually pointing at parent_id
        with db.connection() as conn:
            conn.execute(
                "INSERT INTO tasks (repo_id, name, full_path, type, parent_id, status) "
                "VALUES (?, ?, ?, 'coding', ?, 'active')",
                (
                    db.get_task(parent_id).repo_id,
                    "child-task",
                    "active/parent-task/child-task",
                    parent_id,
                ),
            )
            conn.commit()
            child_id = conn.execute(
                "SELECT id FROM tasks WHERE name = 'child-task'"
            ).fetchone()[0]

        with pytest.raises(ValueError, match="Subtask rename is not supported"):
            db.rename_task(child_id, "renamed-child")


# ── full FS rollback on DB UPDATE failure ─────────────────────────────────


class TestRenameRollback:
    """The DB-UPDATE-fails-after-FS-succeeds branch is the highest-risk
    code path in the procedure. Force it via monkeypatch and assert that
    every recorded FS mutation (outer dir, inner files, H1 contents) is
    reversed before the original exception propagates.
    """

    def test_db_update_failure_rolls_back_all_fs_work(
        self, env, monkeypatch
    ):
        db, orbit_root, _ = env
        tid, project_dir = _seed_active_project(
            db, orbit_root, "rollback-source"
        )
        original_files = {
            f.name: f.read_text() for f in project_dir.iterdir() if f.is_file()
        }
        assert "rollback-source-plan.md" in original_files

        # Patch connection() so the SECOND open (the UPDATE block, after
        # FS work has already committed) raises a sqlite3.OperationalError.
        # The first connection() call is the pre-flight collision check;
        # we let that one through.
        real_connection = db.connection
        call_count = {"n": 0}

        from contextlib import contextmanager

        @contextmanager
        def patched_connection():
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise sqlite3.OperationalError("forced for rollback test")
            with real_connection() as conn:
                yield conn

        monkeypatch.setattr(db, "connection", patched_connection)

        with pytest.raises(sqlite3.OperationalError, match="forced for rollback"):
            db.rename_task(tid, "rollback-target")

        # Outer directory restored at original path; new dir is gone.
        assert (orbit_root / "active" / "rollback-source").is_dir()
        assert not (orbit_root / "active" / "rollback-target").exists()

        # Inner files restored under original names.
        restored_dir = orbit_root / "active" / "rollback-source"
        for name, content in original_files.items():
            f = restored_dir / name
            assert f.exists(), f"inner file {name} not restored"
            assert f.read_text() == content, f"H1 for {name} not restored"

        # DB row unchanged.
        task = db.get_task(tid)
        assert task.name == "rollback-source"
        assert task.full_path == "active/rollback-source"


# ── iteration-log file rename (auto-running tasks) ────────────────────────


class TestRenameIterationLog:
    """Auto-running tasks generate an iteration-log file. Renaming such
    a task must rename the iteration-log alongside the standard three.
    Without explicit coverage, an inadvertent removal of the suffix from
    the rename loop would not regress any existing test.
    """

    def test_iteration_log_renamed_alongside_standard_files(self, env):
        db, orbit_root, _ = env
        tid, project_dir = _seed_active_project(db, orbit_root, "auto-running")
        # Add the iteration-log file the seed helper does NOT create.
        (project_dir / "auto-running-iteration-log.md").write_text(
            "# Auto Running - Iteration Log\n\nrun #1 entry\n"
        )

        result = db.rename_task(tid, "auto-renamed")

        assert sorted(result["files_renamed"]) == [
            "auto-renamed-context.md",
            "auto-renamed-iteration-log.md",
            "auto-renamed-plan.md",
            "auto-renamed-tasks.md",
        ]
        new_dir = orbit_root / "active" / "auto-renamed"
        assert (new_dir / "auto-renamed-iteration-log.md").exists()
        assert not (project_dir / "auto-running-iteration-log.md").exists()


# ── response includes warnings field ──────────────────────────────────────


class TestRenameWarnings:
    """The response shape now includes a ``warnings`` list so callers can
    surface best-effort failures (session sweep, dashboard sync). Lock in
    the contract that the field is always present and empty on the happy
    path.
    """

    def test_happy_path_returns_empty_warnings(self, env):
        db, orbit_root, _ = env
        tid, _ = _seed_active_project(db, orbit_root, "no-warnings-source")

        result = db.rename_task(tid, "no-warnings-target")

        assert "warnings" in result
        assert result["warnings"] == []

    def test_noop_returns_empty_warnings(self, env):
        db, orbit_root, _ = env
        tid, _ = _seed_active_project(db, orbit_root, "noop-warnings")

        result = db.rename_task(tid, "noop-warnings")

        assert result["changed"] is False
        assert "warnings" in result
        assert result["warnings"] == []


# ── parent rename updates descendant full_path rows ───────────────────────


class TestRenameWithSubtasks:
    """Subtask rows embed the parent's full_path as a prefix (e.g.
    ``active/parent/child``). When the parent is renamed, the FS dirs
    move as subdirectories of the parent, but the DB rows keep the old
    prefix unless explicitly updated. Without this fix, ``scan_repos``
    re-discovers the moved subtasks at the new path and inserts duplicate
    rows.
    """

    def test_parent_rename_updates_child_full_paths(self, env):
        db, orbit_root, _ = env
        parent_id, _ = _seed_active_project(db, orbit_root, "parent-task")

        # Create two subtasks with the parent-embedded full_path convention
        # that ``_sync_task_from_dir`` produces.
        parent = db.get_task(parent_id)
        with db.connection() as conn:
            for child_name in ("child-one", "child-two"):
                conn.execute(
                    "INSERT INTO tasks (repo_id, name, full_path, type, "
                    "parent_id, status) VALUES (?, ?, ?, 'coding', ?, 'active')",
                    (
                        parent.repo_id,
                        child_name,
                        f"active/parent-task/{child_name}",
                        parent_id,
                    ),
                )
            conn.commit()

        result = db.rename_task(parent_id, "renamed-parent")

        assert result["changed"] is True
        assert result["full_path"] == "active/renamed-parent"

        # Children's full_path rewrote with the new prefix.
        with db.connection() as conn:
            children = conn.execute(
                "SELECT name, full_path FROM tasks WHERE parent_id = ? "
                "ORDER BY name",
                (parent_id,),
            ).fetchall()
        assert [(c["name"], c["full_path"]) for c in children] == [
            ("child-one", "active/renamed-parent/child-one"),
            ("child-two", "active/renamed-parent/child-two"),
        ]


# ── inner-file target collision (would otherwise overwrite user data) ────


class TestRenameInnerFileCollision:
    """POSIX rename(2) silently overwrites a regular file at the target.
    If the source dir somehow contains a file with the new prefix already
    (user manually renamed one of the four without using this primitive,
    or copied a file in), the inner rename loop would clobber it. The
    pre-flight check raises ``FilesystemCollisionError`` before any FS
    mutation happens, so no rollback is required.
    """

    def test_existing_target_prefixed_file_blocks_rename(self, env):
        db, orbit_root, _ = env
        tid, project_dir = _seed_active_project(db, orbit_root, "inner-source")

        # Plant a target-prefixed file alongside the standard four.
        squatter = project_dir / "inner-target-plan.md"
        squatter.write_text("# user-created content; must not be lost\n")

        with pytest.raises(
            FilesystemCollisionError, match="inner-target-plan.md"
        ):
            db.rename_task(tid, "inner-target")

        # Source dir untouched.
        assert project_dir.is_dir()
        assert (project_dir / "inner-source-plan.md").exists()
        # Squatter content preserved verbatim.
        assert squatter.read_text() == "# user-created content; must not be lost\n"
        # No outer dir created.
        assert not (orbit_root / "active" / "inner-target").exists()
        # DB row unchanged.
        assert db.get_task(tid).name == "inner-source"

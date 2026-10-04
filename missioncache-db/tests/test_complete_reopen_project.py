"""Tests for the complete_project / reopen_project primitives.

Spec source: the completion contract carried over from the MCP
complete_task tool (whose behavior these primitives now single-source):
status flips with completed_at trigger-stamped, the project directory
moves active/ -> completed/ (back on reopen), an already-completed (or
not-completed) task is an INVALID_STATE error, an unknown id is
NOT_FOUND, completing a parent with active forks succeeds and carries an
advisory warning, and full_path deliberately stays as-is after the move.
"""

import missioncache_db as mdb


def _with_root(monkeypatch, tmp_path):
    root = tmp_path / "missioncache-root"
    (root / "active").mkdir(parents=True)
    monkeypatch.setattr(mdb, "MISSIONCACHE_ROOT", root)
    return root


class TestCompleteProject:
    def test_completes_and_moves_directory(self, task_db, tmp_path, monkeypatch):
        root = _with_root(monkeypatch, tmp_path)
        task = task_db.create_task("proj-a", task_type="coding", repo_id=None)
        (root / "active" / "proj-a").mkdir()
        (root / "active" / "proj-a" / "proj-a-context.md").write_text("# ctx\n")

        result = task_db.complete_project(task.id)

        assert not result.get("error")
        assert result["new_status"] == "completed"
        assert result["previous_status"] == "active"
        assert result["files_moved"] is True
        assert (root / "completed" / "proj-a" / "proj-a-context.md").is_file()
        assert not (root / "active" / "proj-a").exists()
        assert task_db.get_task(task.id).status == "completed"
        assert task_db.get_task(task.id).completed_at  # trigger stamped it

    def test_unknown_id_is_not_found(self, task_db):
        result = task_db.complete_project(99999)
        assert result["error"] and result["code"] == "NOT_FOUND"

    def test_already_completed_is_invalid_state(self, task_db, tmp_path, monkeypatch):
        _with_root(monkeypatch, tmp_path)
        task = task_db.create_task("proj-b", task_type="coding", repo_id=None)
        task_db.complete_project(task.id)

        result = task_db.complete_project(task.id)
        assert result["error"] and result["code"] == "INVALID_STATE"

    def test_missing_directory_completes_without_move(self, task_db, tmp_path, monkeypatch):
        _with_root(monkeypatch, tmp_path)
        task = task_db.create_task("proj-c", task_type="coding", repo_id=None)

        result = task_db.complete_project(task.id)
        assert not result.get("error")
        assert result["files_moved"] is False
        assert task_db.get_task(task.id).status == "completed"

    def test_active_fork_children_produce_warning(self, task_db, tmp_path, monkeypatch):
        _with_root(monkeypatch, tmp_path)
        parent = task_db.create_task("parent-p", task_type="coding", repo_id=None)
        child = task_db.create_task("child-p", task_type="coding", repo_id=None)
        with task_db.connection() as conn:
            conn.execute(
                "UPDATE tasks SET parent_id = ? WHERE id = ?", (parent.id, child.id)
            )
            conn.commit()

        result = task_db.complete_project(parent.id)
        assert not result.get("error")
        assert result["active_children_count"] == 1
        assert "child-p" in result["warning"]

    def test_full_path_untouched_after_move(self, task_db, tmp_path, monkeypatch):
        root = _with_root(monkeypatch, tmp_path)
        task = task_db.create_task("proj-d", task_type="coding", repo_id=None)
        (root / "active" / "proj-d").mkdir()

        task_db.complete_project(task.id)
        assert task_db.get_task(task.id).full_path == task.full_path


class TestReopenProject:
    def test_reopens_and_moves_directory_back(self, task_db, tmp_path, monkeypatch):
        root = _with_root(monkeypatch, tmp_path)
        task = task_db.create_task("proj-e", task_type="coding", repo_id=None)
        (root / "active" / "proj-e").mkdir()
        task_db.complete_project(task.id)
        assert (root / "completed" / "proj-e").is_dir()

        result = task_db.reopen_project(task.id)
        assert not result.get("error")
        assert result["new_status"] == "active"
        assert (root / "active" / "proj-e").is_dir()
        assert not (root / "completed" / "proj-e").exists()
        reopened = task_db.get_task(task.id)
        assert reopened.status == "active"
        assert reopened.completed_at is None

    def test_not_completed_is_invalid_state(self, task_db, tmp_path, monkeypatch):
        _with_root(monkeypatch, tmp_path)
        task = task_db.create_task("proj-f", task_type="coding", repo_id=None)
        result = task_db.reopen_project(task.id)
        assert result["error"] and result["code"] == "INVALID_STATE"

    def test_unknown_id_is_not_found(self, task_db):
        result = task_db.reopen_project(99999)
        assert result["error"] and result["code"] == "NOT_FOUND"


class TestMovesWaitForWriters:
    """A project directory must not move while a writer holds its file lock.

    Writers take the ``<file>.lock`` sidecar of the context or tasks file. A
    move in the middle of a write used to break the writer's ``os.replace``,
    because the temp file's directory was already gone.
    """

    def test_complete_waits_for_a_writer_holding_the_context_lock(
        self, task_db, tmp_path, monkeypatch
    ):
        """complete_project runs on this thread (the DB connection is
        thread-bound). A writer thread holds the context lock for a moment
        and records whether the directory was still in place when it let go."""
        import sys
        import threading

        import pytest

        from missioncache_db import filelock

        if sys.platform == "win32":
            pytest.skip("Windows drains writers and releases before the move")
        root = _with_root(monkeypatch, tmp_path)
        task = task_db.create_task("proj-w", task_type="coding", repo_id=None)
        project = root / "active" / "proj-w"
        project.mkdir()
        ctx = project / "proj-w-context.md"
        ctx.write_text("# ctx\n")

        held = threading.Event()
        seen = {}

        def writer():
            with filelock.sidecar_lock(ctx):
                held.set()
                threading.Event().wait(0.5)
                seen["still_there"] = project.exists()

        thread = threading.Thread(target=writer)
        thread.start()
        assert held.wait(5)
        task_db.complete_project(task.id)
        thread.join(5)

        assert seen["still_there"] is True, "the move must wait for the writer"
        assert not project.exists()
        assert (root / "completed" / "proj-w" / "proj-w-context.md").is_file()
        assert task_db.get_task(task.id).status == "completed"

    def test_a_failed_move_leaves_the_project_active(self, task_db, tmp_path, monkeypatch):
        """Files first, status second: a move that fails must not leave the
        project marked completed with its files still in active/."""
        import pytest

        from missioncache_db import filelock

        root = _with_root(monkeypatch, tmp_path)
        task = task_db.create_task("proj-f", task_type="coding", repo_id=None)
        (root / "active" / "proj-f").mkdir()

        def refuse(src, dst, attempts=8):
            raise PermissionError("held open")

        monkeypatch.setattr(filelock, "move_with_retry", refuse)
        with pytest.raises(PermissionError):
            task_db.complete_project(task.id)
        assert task_db.get_task(task.id).status == "active"


class TestProjectDirLockOnWindows:
    def test_windows_releases_the_locks_before_the_move(self, tmp_path, monkeypatch):
        """Windows will not rename a directory with a file open in it, our own
        lock files included, so there the locks only drain the writers."""
        from missioncache_db import filelock

        events = []
        (tmp_path / "p-context.md").write_text("x")
        (tmp_path / "p-journal.md").write_text("x")

        class FakeLocks:
            def __init__(self, files):
                events.append(("lock", sorted(p.name for p in files)))

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                events.append(("unlock",))
                return False

        monkeypatch.setattr(filelock, "sidecar_locks", FakeLocks)
        monkeypatch.setattr(filelock, "_HAVE_FCNTL", False)
        with filelock.project_dir_locked(tmp_path):
            events.append(("move",))
        assert events == [("lock", ["p-context.md"]), ("unlock",), ("move",)]

    def test_posix_holds_the_locks_across_the_move(self, tmp_path, monkeypatch):
        from missioncache_db import filelock

        events = []
        (tmp_path / "p-tasks.md").write_text("x")

        class FakeLocks:
            def __init__(self, files):
                events.append(("lock",))

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                events.append(("unlock",))
                return False

        monkeypatch.setattr(filelock, "sidecar_locks", FakeLocks)
        monkeypatch.setattr(filelock, "_HAVE_FCNTL", True)
        with filelock.project_dir_locked(tmp_path):
            events.append(("move",))
        assert events == [("lock",), ("move",), ("unlock",)]

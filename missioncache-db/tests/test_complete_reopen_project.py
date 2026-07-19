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

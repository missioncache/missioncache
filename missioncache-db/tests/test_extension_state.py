"""Tests for get_extension_state - the editor extension's one-call data layer.

Contract: all active projects sorted newest-first, per-project progress from
the tasks file, context-save mtime, dir_match against the directory's git
root, and update-check.json passthrough.
"""

import json

import pytest

import missioncache_db
from missioncache_db import TaskDB


@pytest.fixture
def db(tmp_path, monkeypatch):
    """TaskDB on tmp with MISSIONCACHE_ROOT redirected to tmp."""
    monkeypatch.setattr(missioncache_db, "MISSIONCACHE_ROOT", tmp_path)
    db = TaskDB(db_path=tmp_path / "t.db")
    db.initialize()
    yield db
    db.close()


def _project_with_files(db, tmp_path, name, repo_path):
    repo_id = db.add_repo(repo_path, name + "-repo")
    task = db.create_task(name, repo_id=repo_id)
    task_dir = tmp_path / task.full_path
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / f"{name}-tasks.md").write_text(
        "# T\n\n**Status:** In Progress\n\n- [x] 1. done thing\n- [ ] 2. open thing\n",
        encoding="utf-8",
    )
    (task_dir / f"{name}-context.md").write_text("# C\n", encoding="utf-8")
    return task


class TestExtensionState:
    def test_empty_db(self, db):
        state = db.get_extension_state()
        assert state["projects"] == []
        assert state["schema"] == 1
        assert state["update_available"] is False

    def test_progress_and_context_mtime(self, db, tmp_path):
        _project_with_files(db, tmp_path, "ext-a", "/tmp/ext-a-repo")
        state = db.get_extension_state()
        (proj,) = state["projects"]
        assert proj["has_docs"] is True
        assert proj["completed_count"] == 1
        assert proj["total_count"] == 2
        assert proj["completion_pct"] == 50
        assert proj["context_saved_at"] is not None

    def test_dir_match_resolves_git_root(self, db, tmp_path):
        repo = tmp_path / "repo-root"
        (repo / ".git").mkdir(parents=True)
        sub = repo / "src" / "deep"
        sub.mkdir(parents=True)
        _project_with_files(db, tmp_path, "ext-b", str(repo))
        state = db.get_extension_state(str(sub))
        (proj,) = state["projects"]
        assert proj["dir_match"] is True
        assert state["directory"] == str(repo)

    def test_non_git_dir_matches_exact_only(self, db, tmp_path):
        plain = tmp_path / "plain-dir"
        plain.mkdir()
        _project_with_files(db, tmp_path, "ext-c", str(plain))
        assert db.get_extension_state(str(plain))["projects"][0]["dir_match"] is True
        other = tmp_path / "other-dir"
        other.mkdir()
        assert db.get_extension_state(str(other))["projects"][0]["dir_match"] is False

    def test_sorted_by_last_worked_desc(self, db, tmp_path):
        a = _project_with_files(db, tmp_path, "ext-old", "/tmp/r1")
        b = _project_with_files(db, tmp_path, "ext-new", "/tmp/r2")
        with db.connection() as conn:
            conn.execute(
                "UPDATE tasks SET last_worked_on = '2026-01-01 10:00:00' WHERE id = ?",
                (a.id,),
            )
            conn.execute(
                "UPDATE tasks SET last_worked_on = '2026-08-19 10:00:00' WHERE id = ?",
                (b.id,),
            )
            conn.commit()
        names = [p["name"] for p in db.get_extension_state()["projects"]]
        assert names == ["ext-new", "ext-old"]

    def test_update_check_passthrough(self, db, tmp_path):
        (tmp_path / "update-check.json").write_text(
            json.dumps({"update_available": True, "command": "uvx missioncache-install --update"}),
            encoding="utf-8",
        )
        state = db.get_extension_state()
        assert state["update_available"] is True
        assert "missioncache-install" in state["update_command"]

    def test_corrupt_update_check_ignored(self, db, tmp_path):
        (tmp_path / "update-check.json").write_text("{not json", encoding="utf-8")
        assert db.get_extension_state()["update_available"] is False

    def test_project_without_files(self, db):
        # No files on disk: has_docs False, counts null, no file paths -
        # the extension hides the open-file menu entries on exactly this.
        repo_id = db.add_repo("/tmp/ext-nofiles-repo", "nofiles")
        db.create_task("ext-nofiles", repo_id=repo_id)
        (proj,) = db.get_extension_state()["projects"]
        assert proj["has_docs"] is False
        assert proj["completed_count"] is None
        assert proj["tasks_file"] is None
        assert proj["context_file"] is None

    def test_non_active_full_path_resolves_files(self, db, tmp_path):
        # create_task places projects under manual/<name> - the payload must
        # carry the real resolved paths, never an assumed active/ prefix.
        task = _project_with_files(db, tmp_path, "ext-manual", "/tmp/ext-manual-repo")
        assert task.full_path.startswith("manual/")
        (proj,) = db.get_extension_state()["projects"]
        assert proj["tasks_file"] == str(tmp_path / task.full_path / "ext-manual-tasks.md")
        assert proj["context_file"] == str(tmp_path / task.full_path / "ext-manual-context.md")
        assert proj["context_saved_at"] is not None

    def test_update_check_non_dict_json_ignored(self, db, tmp_path):
        (tmp_path / "update-check.json").write_text("[1, 2, 3]", encoding="utf-8")
        assert db.get_extension_state()["update_available"] is False

    def test_fork_parent_name(self, db, tmp_path):
        parent = _project_with_files(db, tmp_path, "ext-parent", "/tmp/rp")
        child = _project_with_files(db, tmp_path, "ext-child", "/tmp/rc")
        with db.connection() as conn:
            conn.execute("UPDATE tasks SET parent_id = ? WHERE id = ?", (parent.id, child.id))
            conn.commit()
        by_name = {p["name"]: p for p in db.get_extension_state()["projects"]}
        assert by_name["ext-child"]["fork_of"] == "ext-parent"
        assert by_name["ext-parent"]["fork_of"] is None

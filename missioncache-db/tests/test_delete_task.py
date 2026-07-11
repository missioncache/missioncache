"""Tests for TaskDB.delete_task (record removal + optional file removal)."""

import shutil

import pytest

import missioncache_db
from missioncache_db import AutoRunActiveError, SubtasksExistError


def _make(db, name):
    """A non-coding task (no repo needed); full_path = global/<name>."""
    return db.create_task(name, task_type="non-coding")


def _set_full_path(db, task_id, full_path):
    with db.connection() as conn:
        conn.execute("UPDATE tasks SET full_path = ? WHERE id = ?", (full_path, task_id))
        conn.commit()


def test_delete_removes_row_and_cascades_child_rows(task_db):
    t = _make(task_db, "gone")
    task_db.add_task_update(t.id, "a note")  # a task_updates child row
    res = task_db.delete_task(t.id)
    assert res["deleted"] is True
    assert res["files_deleted"] is False
    assert task_db.get_task(t.id) is None
    with task_db.connection() as conn:
        remaining = conn.execute(
            "SELECT COUNT(*) AS c FROM task_updates WHERE task_id = ?", (t.id,)
        ).fetchone()["c"]
    assert remaining == 0  # cascaded via FK


def test_delete_missing_task_raises(task_db):
    with pytest.raises(ValueError, match="No project found"):
        task_db.delete_task(999999)


def test_delete_blocks_when_subtasks_exist(task_db):
    parent = _make(task_db, "parent")
    child = _make(task_db, "child")
    with task_db.connection() as conn:
        conn.execute(
            "UPDATE tasks SET parent_id = ? WHERE id = ?", (parent.id, child.id)
        )
        conn.commit()
    with pytest.raises(SubtasksExistError):
        task_db.delete_task(parent.id)
    assert task_db.get_task(parent.id) is not None  # untouched


def test_delete_files_false_keeps_dir(task_db, tmp_path, monkeypatch):
    monkeypatch.setattr(missioncache_db, "MISSIONCACHE_ROOT", tmp_path)
    t = _make(task_db, "keepfiles")
    d = tmp_path / t.full_path
    d.mkdir(parents=True)
    (d / f"{t.name}-context.md").write_text("x")
    res = task_db.delete_task(t.id, delete_files=False)
    assert res["files_deleted"] is False
    assert d.exists()


def test_delete_files_true_removes_dir(task_db, tmp_path, monkeypatch):
    monkeypatch.setattr(missioncache_db, "MISSIONCACHE_ROOT", tmp_path)
    t = _make(task_db, "dropfiles")
    d = tmp_path / t.full_path
    d.mkdir(parents=True)
    (d / f"{t.name}-context.md").write_text("x")
    res = task_db.delete_task(t.id, delete_files=True)
    assert res["files_deleted"] is True
    assert not d.exists()


def test_delete_files_true_no_dir_is_clean(task_db, tmp_path, monkeypatch):
    monkeypatch.setattr(missioncache_db, "MISSIONCACHE_ROOT", tmp_path)
    t = _make(task_db, "nodir")  # dir never created
    res = task_db.delete_task(t.id, delete_files=True)
    assert res["files_deleted"] is False
    assert res["warnings"] == []
    assert task_db.get_task(t.id) is None


def test_delete_files_refuses_out_of_root(task_db, tmp_path, monkeypatch):
    # A corrupted full_path that resolves outside the root must be refused, not
    # deleted: the DB row goes (deleted=True) but the out-of-root dir survives
    # and a warning is returned.
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(missioncache_db, "MISSIONCACHE_ROOT", root)
    t = _make(task_db, "escaper")
    _set_full_path(task_db, t.id, "../evil")
    evil = tmp_path / "evil"
    evil.mkdir()
    (evil / "keep.md").write_text("do not delete me")
    res = task_db.delete_task(t.id, delete_files=True)
    assert res["deleted"] is True
    assert res["files_deleted"] is False
    assert res["warnings"] and "outside" in res["warnings"][0].lower()
    assert evil.exists()  # untouched
    assert task_db.get_task(t.id) is None


def test_delete_files_refuses_root_itself(task_db, tmp_path, monkeypatch):
    monkeypatch.setattr(missioncache_db, "MISSIONCACHE_ROOT", tmp_path)
    t = _make(task_db, "rooter")
    _set_full_path(task_db, t.id, "")  # target resolves to the root itself
    res = task_db.delete_task(t.id, delete_files=True)
    assert res["files_deleted"] is False
    assert res["warnings"] and "root" in res["warnings"][0].lower()
    assert tmp_path.exists()


def test_delete_files_rmtree_failure_warns(task_db, tmp_path, monkeypatch):
    monkeypatch.setattr(missioncache_db, "MISSIONCACHE_ROOT", tmp_path)
    t = _make(task_db, "rmfail")
    d = tmp_path / t.full_path
    d.mkdir(parents=True)
    (d / f"{t.name}-context.md").write_text("x")

    def _boom(*a, **k):
        raise OSError("permission denied")

    monkeypatch.setattr(shutil, "rmtree", _boom)
    res = task_db.delete_task(t.id, delete_files=True)
    # Row is gone, file removal failed but was warned (not raised).
    assert res["deleted"] is True
    assert res["files_deleted"] is False
    assert len(res["warnings"]) == 1
    assert task_db.get_task(t.id) is None


def test_delete_blocked_by_running_auto_run(task_db):
    t = _make(task_db, "autorun")
    with task_db.connection() as conn:
        conn.execute(
            "INSERT INTO auto_executions (task_id, status, started_at) "
            "VALUES (?, 'running', datetime('now'))",
            (t.id,),
        )
        conn.commit()
    with pytest.raises(AutoRunActiveError, match="missioncache-auto is running"):
        task_db.delete_task(t.id)
    assert task_db.get_task(t.id) is not None  # row intact


def test_delete_not_blocked_by_completed_auto_run(task_db):
    t = _make(task_db, "autodone")
    with task_db.connection() as conn:
        conn.execute(
            "INSERT INTO auto_executions (task_id, status, started_at) "
            "VALUES (?, 'completed', datetime('now'))",
            (t.id,),
        )
        conn.commit()
    res = task_db.delete_task(t.id)
    assert res["deleted"] is True
    assert task_db.get_task(t.id) is None


def test_delete_happy_path_returns_name_and_full_path(task_db):
    t = _make(task_db, "named")
    res = task_db.delete_task(t.id)
    assert res["name"] == "named"
    assert res["full_path"] == t.full_path

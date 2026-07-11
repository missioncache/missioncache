"""Tests for the ``missioncache-db cleanup`` CLI subcommand.

Spec source: cleanup is documented as GENERAL DB maintenance (module docstring
"Archive orphans, resolve dupes, normalize paths"; the MCP server lists it under
DB maintenance). It must therefore be safe on any user's DB:

- B1 must not archive a coding task just because it has no on-disk directory.
  Tasks created via ``create-task`` carry a ``manual/<name>`` full_path and never
  get a directory, so a missing dir is expected, not orphaned.
- B2 must never delete the live ``active/`` / ``completed/`` roots under
  MISSIONCACHE_ROOT.

Runs ``main()`` in-process with ``sys.argv`` and the ``MISSIONCACHE_ROOT`` /
``DB_PATH`` module globals monkeypatched to tmp (the cleanup branch reads those
globals at call time), mirroring the health-command tests.
"""

import sys

import missioncache_db
from missioncache_db import TaskDB


def _run_cleanup(monkeypatch, root):
    monkeypatch.setattr(missioncache_db, "MISSIONCACHE_ROOT", root)
    monkeypatch.setattr(missioncache_db, "DB_PATH", root / "tasks.db")
    monkeypatch.setattr(sys, "argv", ["missioncache-db", "cleanup"])
    missioncache_db.main()


class TestCleanupCommand:
    def test_manual_coding_task_not_archived(self, monkeypatch, tmp_path):
        (tmp_path / "active").mkdir()
        (tmp_path / "completed").mkdir()
        db = TaskDB(db_path=tmp_path / "tasks.db")
        db.initialize()
        task = db.create_task("manual-coding-proj")
        assert task.full_path.startswith("manual/")
        db.close()

        _run_cleanup(monkeypatch, tmp_path)

        db2 = TaskDB(db_path=tmp_path / "tasks.db")
        reloaded = db2.get_task(task.id)
        db2.close()
        assert reloaded is not None
        assert reloaded.status == "active"

    def test_live_roots_survive_when_empty(self, monkeypatch, tmp_path):
        (tmp_path / "active").mkdir()
        (tmp_path / "completed").mkdir()
        db = TaskDB(db_path=tmp_path / "tasks.db")
        db.initialize()
        db.close()

        _run_cleanup(monkeypatch, tmp_path)

        assert (tmp_path / "active").is_dir()
        assert (tmp_path / "completed").is_dir()

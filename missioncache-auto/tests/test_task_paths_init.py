"""Tests for the lazy ``from missioncache_db import MISSIONCACHE_ROOT`` sites.

Two production sites import ``MISSIONCACHE_ROOT`` lazily, INSIDE the function
body, instead of at module import time:

- ``missioncache_auto.models.TaskPaths.from_task_name`` (models.py ~line 175)
- ``missioncache_auto.init_task.init_task``           (init_task.py ~line 39)

Lazy imports survive a mechanical rename of the symbol name elsewhere in
the codebase without anything noticing until the function is actually
called. These tests pin the contract that:

- ``TaskPaths.from_task_name`` reads ``MISSIONCACHE_ROOT`` from the missioncache_db
  module at call time and roots all paths under ``<MISSIONCACHE_ROOT>/active/<task>``.
- ``init_task`` reads ``MISSIONCACHE_ROOT`` from the missioncache_db module at call
  time and writes the task tree under ``<MISSIONCACHE_ROOT>/active/<task>``.

Both tests monkeypatch ``missioncache_db.MISSIONCACHE_ROOT`` to a tmp_path so the lazy
import resolves to the patched value and nothing escapes to the user's
real ``~/.orbit`` directory. Mirrors the monkeypatch isolation pattern
from ``test_db_logger_init.py``.
"""

import missioncache_db

from missioncache_auto import init_task as init_task_module
from missioncache_auto.init_task import init_task
from missioncache_auto.models import TaskPaths


class TestTaskPathsFromTaskName:
    def test_paths_root_at_patched_orbit_root(self, tmp_path, monkeypatch):
        """Lazy import resolves to the patched MISSIONCACHE_ROOT at call time.

        Every path on the returned TaskPaths must sit under
        <patched-MISSIONCACHE_ROOT>/active/<task-name>/.
        """
        fake_root = tmp_path / "fake-orbit"
        monkeypatch.setattr(missioncache_db, "MISSIONCACHE_ROOT", fake_root)

        paths = TaskPaths.from_task_name("sample-task")

        expected_task_dir = fake_root / "active" / "sample-task"
        assert paths.task_dir == expected_task_dir
        assert paths.tasks_file == expected_task_dir / "sample-task-tasks.md"
        assert paths.context_file == expected_task_dir / "sample-task-context.md"
        assert paths.auto_log == expected_task_dir / "sample-task-auto-log.md"
        assert paths.prompts_dir == expected_task_dir / "prompts"
        assert paths.state_dir == expected_task_dir / ".orbit-parallel-state"
        assert paths.logs_dir == expected_task_dir / "logs"

    def test_no_disk_side_effects(self, tmp_path, monkeypatch):
        """``from_task_name`` is pure: it returns paths but creates nothing."""
        fake_root = tmp_path / "fake-orbit"
        monkeypatch.setattr(missioncache_db, "MISSIONCACHE_ROOT", fake_root)

        TaskPaths.from_task_name("sample-task")

        assert not fake_root.exists()


class TestInitTaskOrbitRoot:
    def test_creates_task_dir_under_patched_orbit_root(
        self, tmp_path, monkeypatch
    ):
        """init_task writes under the patched MISSIONCACHE_ROOT, not the real ~/.orbit.

        Exercises the same lazy import site (init_task.py ~line 39) and
        proves the file tree lands under tmp_path so any rename of
        MISSIONCACHE_ROOT downstream surfaces as a failed write here.
        """
        fake_root = tmp_path / "fake-orbit"
        monkeypatch.setattr(missioncache_db, "MISSIONCACHE_ROOT", fake_root)

        task_dir = init_task("sample-task", "test description")

        expected = fake_root / "active" / "sample-task"
        assert task_dir == expected
        assert task_dir.is_dir()
        assert (task_dir / "sample-task-tasks.md").is_file()
        assert (task_dir / "sample-task-context.md").is_file()
        assert (task_dir / "sample-task-plan.md").is_file()

    def test_written_files_use_template_substitutions(
        self, tmp_path, monkeypatch
    ):
        """Each template's ``{task_name}`` / ``{description}`` slots are
        actually substituted - a rename that breaks the format placeholders
        would produce raw ``{...}`` in the output."""
        fake_root = tmp_path / "fake-orbit"
        monkeypatch.setattr(missioncache_db, "MISSIONCACHE_ROOT", fake_root)

        task_dir = init_task("sample-task", "my-desc")

        tasks_content = (task_dir / "sample-task-tasks.md").read_text()
        context_content = (task_dir / "sample-task-context.md").read_text()

        assert "sample-task" in tasks_content
        assert "my-desc" in tasks_content
        assert "sample-task" in context_content
        assert "my-desc" in context_content
        # No unsubstituted placeholders left over.
        assert "{task_name}" not in tasks_content
        assert "{description}" not in tasks_content

    def test_raises_when_task_dir_already_exists(self, tmp_path, monkeypatch):
        """Second init_task call against the same name surfaces
        FileExistsError - the only error contract this function exposes."""
        import pytest

        fake_root = tmp_path / "fake-orbit"
        monkeypatch.setattr(missioncache_db, "MISSIONCACHE_ROOT", fake_root)

        init_task("sample-task")
        with pytest.raises(FileExistsError):
            init_task("sample-task")

    def test_module_does_not_eagerly_import_orbit_root(self):
        """init_task.py must NOT bind MISSIONCACHE_ROOT at module scope.

        The whole point of the lazy ``from missioncache_db import MISSIONCACHE_ROOT``
        inside the function body is that tests (and the migration guard)
        can monkeypatch ``missioncache_db.MISSIONCACHE_ROOT`` and have the new value
        take effect. If someone refactors that to a top-level import,
        this test breaks - which is the signal we want.
        """
        assert not hasattr(init_task_module, "MISSIONCACHE_ROOT")

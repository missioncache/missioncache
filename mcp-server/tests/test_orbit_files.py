"""Integration tests for orbit file create/update operations.

Tests use tmp_path for all file I/O and monkeypatch to redirect orbit_root.
"""

import re

import pytest

from mcp_orbit.config import Settings
from mcp_orbit.errors import ErrorCode, OrbitError, OrbitFileNotFoundError
from mcp_orbit.orbit import (
    create_orbit_files,
    get_orbit_files,
    parse_task_progress,
    update_context_file,
    update_tasks_file,
)


@pytest.fixture(autouse=True)
def _redirect_orbit_root(tmp_path, monkeypatch):
    """Point orbit_root to tmp_path so file operations don't touch real filesystem."""
    test_settings = Settings(orbit_root=tmp_path / "orbit")
    monkeypatch.setattr("mcp_orbit.orbit.settings", test_settings)


# ── create_orbit_files ────────────────────────────────────────────────────


class TestCreateOrbitFiles:
    def test_creates_three_files(self, tmp_path):
        """create_orbit_files produces plan, context, and tasks files."""
        result = create_orbit_files(
            task_name="test-task",
            description="A test project",
            tasks=["Set up repo", "Write code"],
        )

        assert result.plan_file is not None
        assert result.context_file is not None
        assert result.tasks_file is not None

        # Files should actually exist on disk
        from pathlib import Path

        assert Path(result.plan_file).exists()
        assert Path(result.context_file).exists()
        assert Path(result.tasks_file).exists()

    def test_template_placeholders_filled(self, tmp_path):
        """No raw {{placeholder}} tokens should remain in generated files."""
        result = create_orbit_files(
            task_name="test-task",
            description="Filled description",
            jira_key="PROJ-1234",
            branch="feature/test-task",
            tasks=["First task"],
        )

        from pathlib import Path

        for fpath in (result.plan_file, result.context_file, result.tasks_file):
            content = Path(fpath).read_text()
            leftover = re.findall(r"\{\{[a-z_]+\}\}", content)
            assert leftover == [], f"Unfilled placeholders in {fpath}: {leftover}"

    def test_duplicate_raises_already_exists(self, tmp_path):
        """Re-creating a project with the same name raises ALREADY_EXISTS."""
        create_orbit_files(task_name="dup-task", tasks=["one"])

        with pytest.raises(OrbitError) as excinfo:
            create_orbit_files(task_name="dup-task", tasks=["two"])

        assert excinfo.value.code == ErrorCode.ALREADY_EXISTS
        assert "dup-task" in excinfo.value.message
        assert "existing_files" in excinfo.value.details

    def test_duplicate_preserves_original_files(self, tmp_path):
        """The ALREADY_EXISTS guard runs BEFORE any write, so files are intact."""
        first = create_orbit_files(task_name="preserve-task", tasks=["original"])

        from pathlib import Path

        original_tasks = Path(first.tasks_file).read_text()

        with pytest.raises(OrbitError):
            create_orbit_files(task_name="preserve-task", tasks=["clobber"])

        assert Path(first.tasks_file).read_text() == original_tasks
        assert "original" in original_tasks
        assert "clobber" not in original_tasks

    def test_force_overwrites_existing(self, tmp_path):
        """force=True bypasses the ALREADY_EXISTS guard and rewrites files."""
        from pathlib import Path

        first = create_orbit_files(task_name="force-task", tasks=["v1"])
        original = Path(first.tasks_file).read_text()
        assert "v1" in original

        second = create_orbit_files(
            task_name="force-task", tasks=["v2"], force=True
        )
        rewritten = Path(second.tasks_file).read_text()
        assert "v2" in rewritten
        assert "v1" not in rewritten

    def test_guard_catches_legacy_unprefixed_filenames(self, tmp_path):
        """ALREADY_EXISTS fires when the dir has only legacy unprefixed files.

        get_orbit_files reads both prefixed and legacy names; the guard
        must check both, otherwise fresh prefixed files would shadow
        existing legacy content at read time.
        """
        from mcp_orbit.orbit import get_task_dir

        task_dir = get_task_dir("legacy-task")
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "plan.md").write_text("# legacy plan content")
        (task_dir / "context.md").write_text("# legacy context content")
        (task_dir / "tasks.md").write_text("- [ ] legacy task")

        with pytest.raises(OrbitError) as excinfo:
            create_orbit_files(task_name="legacy-task", tasks=["new"])

        assert excinfo.value.code == ErrorCode.ALREADY_EXISTS


# ── get_orbit_files ──────────────────────────────────────────────────────


class TestGetOrbitFiles:
    def test_finds_files_in_active_dir(self, tmp_path):
        create_orbit_files(task_name="active-task", tasks=["x"])

        result = get_orbit_files("active-task")

        assert result.plan_file is not None
        assert result.context_file is not None
        assert result.tasks_file is not None
        assert "active/active-task" in result.task_dir

    def test_finds_files_in_completed_dir(self, tmp_path):
        """When a project is archived to completed/, get_orbit_files finds it.

        Reproduces MAJOR-10 from the QA report - a fresh /orbit:go on a
        completed project used to report has_orbit_files=False because the
        lookup only scanned active/.
        """
        from mcp_orbit.orbit import settings

        create_orbit_files(task_name="archived-task", tasks=["done"])

        active_dir = settings.orbit_root / "active" / "archived-task"
        completed_dir = settings.orbit_root / "completed" / "archived-task"
        completed_dir.parent.mkdir(parents=True, exist_ok=True)
        active_dir.rename(completed_dir)
        assert not active_dir.exists()
        assert completed_dir.exists()

        result = get_orbit_files("archived-task")

        assert result.plan_file is not None
        assert result.context_file is not None
        assert result.tasks_file is not None
        assert "completed/archived-task" in result.task_dir

    def test_returns_empty_paths_when_nothing_exists(self, tmp_path):
        result = get_orbit_files("nonexistent-task")

        assert result.plan_file is None
        assert result.context_file is None
        assert result.tasks_file is None

    def test_active_takes_priority_over_completed(self, tmp_path):
        """If a project exists in both active/ AND completed/ (e.g., reopened
        without deleting the archived copy), the active version wins."""
        from mcp_orbit.orbit import settings

        create_orbit_files(task_name="dual-task", tasks=["active-version"])

        completed_dir = settings.orbit_root / "completed" / "dual-task"
        completed_dir.mkdir(parents=True, exist_ok=True)
        (completed_dir / "dual-task-tasks.md").write_text("completed-version")

        result = get_orbit_files("dual-task")

        assert result.tasks_file is not None
        assert "active/dual-task" in result.task_dir
        from pathlib import Path

        assert "active-version" in Path(result.tasks_file).read_text()


# ── update_context_file ──────────────────────────────────────────────────


class TestUpdateContextFile:
    def test_updates_timestamp(self, tmp_path, sample_context_md):
        """update_context_file refreshes the Last Updated timestamp."""
        ctx_file = tmp_path / "context.md"
        ctx_file.write_text(sample_context_md)

        updated = update_context_file(str(ctx_file))
        assert "**Last Updated:**" in updated
        # Should NOT contain the old timestamp
        assert "2026-04-01 10:00" not in updated

    def test_appends_recent_changes(self, tmp_path, sample_context_md):
        """update_context_file with recent_changes adds entries to Recent Changes."""
        ctx_file = tmp_path / "context.md"
        ctx_file.write_text(sample_context_md)

        updated = update_context_file(
            str(ctx_file),
            recent_changes=["Added new module", "Fixed tests"],
        )

        assert "Added new module" in updated
        assert "Fixed tests" in updated


# ── update_tasks_file ────────────────────────────────────────────────────


class TestUpdateTasksFile:
    def test_marks_task_completed(self, tmp_path, sample_tasks_md):
        """update_tasks_file marks matching task descriptions as [x]."""
        tasks_file = tmp_path / "tasks.md"
        tasks_file.write_text(sample_tasks_md)

        result = update_tasks_file(
            str(tasks_file),
            completed_tasks=["Implement core logic"],
        )

        content = tasks_file.read_text()
        # The task "3. Implement core logic" should now be checked
        assert re.search(r"- \[x\].*Implement core logic", content, re.IGNORECASE)
        assert len(result["updates_made"]) > 0

    def test_updates_progress_percentage(self, tmp_path, sample_tasks_md):
        """update_tasks_file returns progress with correct completion percentage."""
        tasks_file = tmp_path / "tasks.md"
        tasks_file.write_text(sample_tasks_md)

        result = update_tasks_file(
            str(tasks_file),
            completed_tasks=["Implement core logic"],
        )

        progress = result["progress"]
        assert progress is not None
        # Originally 2/5 completed, now 3/5 = 60%
        assert progress["completion_pct"] == 60
        assert progress["completed_items"] == 3
        assert progress["total_items"] == 5

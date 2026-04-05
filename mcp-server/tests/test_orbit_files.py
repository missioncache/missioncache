"""Integration tests for orbit file create/update operations.

Tests use tmp_path for all file I/O and monkeypatch to redirect orbit_root.
"""

import re

import pytest

from mcp_orbit.config import Settings
from mcp_orbit.errors import OrbitFileNotFoundError
from mcp_orbit.orbit import (
    create_orbit_files,
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

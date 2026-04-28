"""Tests for the active orbit-task MCP tools.

Covers ``set_active_orbit_tasks`` and ``clear_active_orbit_tasks``: input
validation, tasks.md presence/absence, unknown vs already-completed
numbers, idempotent replace, and end-to-end pointer round-trip.

The tools are async wrappers; we call them via ``asyncio.run`` to keep
the test suite synchronous.
"""

from __future__ import annotations

import asyncio
import pathlib

import pytest

from mcp_orbit import active_task, tools_active


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def project_dir(tmp_path, monkeypatch):
    """Create a fake orbit project under a sandboxed orbit_root."""
    orbit_root = tmp_path / ".orbit"
    project = orbit_root / "active" / "demo-project"
    project.mkdir(parents=True)
    (project / "demo-project-tasks.md").write_text(
        "# Demo Project Tasks\n"
        "- [ ] 8. Draft Show HN post\n"
        "- [ ] 54. Parent\n"
        "  - [ ] 54a. Sub a\n"
        "  - [ ] 54b. Sub b\n"
        "- [x] 9. Already done\n"
    )

    # Re-bind ORBIT_ROOT-like settings used by orbit.get_orbit_files.
    from mcp_orbit import config

    monkeypatch.setattr(config.settings, "orbit_root", orbit_root)
    monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(
        active_task,
        "STATE_DIR",
        tmp_path / ".claude" / "hooks" / "state" / "active-orbit-task",
    )
    return project


# ── set_active_orbit_tasks ───────────────────────────────────────────────


class TestSetActiveOrbitTasks:
    def test_writes_pointer_for_valid_single_task(self, project_dir):
        result = asyncio.run(
            tools_active.set_active_orbit_tasks(
                project_name="demo-project",
                task_numbers=["54a"],
                session_id="sess-1",
            )
        )
        assert result["success"] is True
        assert result["task_numbers"] == ["54a"]

        pointer = active_task.read_pointer("sess-1")
        assert pointer["project_name"] == "demo-project"
        assert pointer["task_numbers"] == ["54a"]

    def test_writes_pointer_for_multiple_valid_tasks(self, project_dir):
        result = asyncio.run(
            tools_active.set_active_orbit_tasks(
                project_name="demo-project",
                task_numbers=["8", "54a", "54b"],
                session_id="sess-1",
            )
        )
        assert result["success"] is True
        assert active_task.read_pointer("sess-1")["task_numbers"] == [
            "8", "54a", "54b"
        ]

    def test_replaces_existing_pointer(self, project_dir):
        asyncio.run(
            tools_active.set_active_orbit_tasks(
                project_name="demo-project",
                task_numbers=["54a"],
                session_id="sess-1",
            )
        )
        asyncio.run(
            tools_active.set_active_orbit_tasks(
                project_name="demo-project",
                task_numbers=["54b"],
                session_id="sess-1",
            )
        )
        # Latest call wins.
        assert active_task.read_pointer("sess-1")["task_numbers"] == ["54b"]

    def test_empty_list_is_a_clear(self, project_dir):
        asyncio.run(
            tools_active.set_active_orbit_tasks(
                project_name="demo-project",
                task_numbers=["54a"],
                session_id="sess-1",
            )
        )
        result = asyncio.run(
            tools_active.set_active_orbit_tasks(
                project_name="demo-project",
                task_numbers=[],
                session_id="sess-1",
            )
        )
        assert result["success"] is True
        assert result["task_numbers"] == []
        assert active_task.read_pointer("sess-1") is None

    def test_unknown_number_returns_validation_error(self, project_dir):
        result = asyncio.run(
            tools_active.set_active_orbit_tasks(
                project_name="demo-project",
                task_numbers=["999"],
                session_id="sess-1",
            )
        )
        assert result["error"] is True
        assert result["code"] == "VALIDATION_ERROR"
        assert result["details"]["unknown_numbers"] == ["999"]
        # Pointer should NOT be written on validation failure.
        assert active_task.read_pointer("sess-1") is None

    def test_already_completed_number_returns_validation_error(self, project_dir):
        result = asyncio.run(
            tools_active.set_active_orbit_tasks(
                project_name="demo-project",
                task_numbers=["9"],  # marked [x] in fixture
                session_id="sess-1",
            )
        )
        assert result["error"] is True
        assert result["code"] == "VALIDATION_ERROR"
        assert result["details"]["already_completed_numbers"] == ["9"]
        assert active_task.read_pointer("sess-1") is None

    def test_mix_of_unknown_and_completed_reports_both(self, project_dir):
        result = asyncio.run(
            tools_active.set_active_orbit_tasks(
                project_name="demo-project",
                task_numbers=["999", "9", "54a"],
                session_id="sess-1",
            )
        )
        assert result["error"] is True
        assert result["details"]["unknown_numbers"] == ["999"]
        assert result["details"]["already_completed_numbers"] == ["9"]
        # Even though 54a is valid, the call fails atomically.
        assert active_task.read_pointer("sess-1") is None

    def test_missing_session_id_rejected(self, project_dir):
        result = asyncio.run(
            tools_active.set_active_orbit_tasks(
                project_name="demo-project",
                task_numbers=["54a"],
                session_id="",
            )
        )
        assert result["error"] is True
        assert result["code"] == "VALIDATION_ERROR"

    def test_missing_project_returns_file_not_found(self, project_dir):
        result = asyncio.run(
            tools_active.set_active_orbit_tasks(
                project_name="no-such-project",
                task_numbers=["54a"],
                session_id="sess-1",
            )
        )
        assert result["error"] is True
        assert result["code"] == "FILE_NOT_FOUND"

    def test_path_traversal_project_name_rejected(self, project_dir):
        """``project_name`` flows into ``orbit.get_orbit_files`` which builds a
        path under ORBIT_ROOT. Reject traversal-shaped names at the boundary."""
        result = asyncio.run(
            tools_active.set_active_orbit_tasks(
                project_name="../../etc",
                task_numbers=["54a"],
                session_id="sess-1",
            )
        )
        assert result["error"] is True
        assert result["code"] == "VALIDATION_ERROR"
        assert active_task.read_pointer("sess-1") is None


# ── clear_active_orbit_tasks ─────────────────────────────────────────────


class TestClearActiveOrbitTasks:
    def test_clears_existing_pointer(self, project_dir):
        active_task.write_pointer("sess-1", "demo-project", ["54a"])
        result = asyncio.run(
            tools_active.clear_active_orbit_tasks(session_id="sess-1")
        )
        assert result["success"] is True
        assert result["cleared"] is True
        assert active_task.read_pointer("sess-1") is None

    def test_returns_cleared_false_when_nothing_to_clear(self, project_dir):
        result = asyncio.run(
            tools_active.clear_active_orbit_tasks(session_id="never-set")
        )
        assert result["success"] is True
        assert result["cleared"] is False

    def test_missing_session_id_rejected(self, project_dir):
        result = asyncio.run(tools_active.clear_active_orbit_tasks(session_id=""))
        assert result["error"] is True
        assert result["code"] == "VALIDATION_ERROR"


# ── update_tasks_file -> active-task auto-clear hook ─────────────────────


class TestUpdateTasksFileAutoClear:
    """The ``update_tasks_file`` MCP wrapper sweeps active-task pointers.

    When a checklist item transitions from [ ] to [x], any session pointer
    listing that number should drop it (and the pointer file is deleted
    when the set drains to empty). Driven by the diff-based
    ``completed_numbers`` returned from ``orbit.update_tasks_file``.
    """

    def test_auto_clear_removes_completed_number_from_pointer(self, project_dir):
        from mcp_orbit import tools_docs

        active_task.write_pointer("sess-1", "demo-project", ["54a", "54b"])

        result = asyncio.run(
            tools_docs.update_tasks_file(
                tasks_file=str(project_dir / "demo-project-tasks.md"),
                completed_tasks=["Sub a"],
            )
        )
        assert result["success"] is True
        assert result["completed_numbers"] == ["54a"]
        assert result["active_pointers_cleared_for_sessions"] == ["sess-1"]

        pointer = active_task.read_pointer("sess-1")
        assert pointer["task_numbers"] == ["54b"]

    def test_auto_clear_removes_pointer_file_when_set_drains(self, project_dir):
        from mcp_orbit import tools_docs

        active_task.write_pointer("sess-1", "demo-project", ["54a"])

        asyncio.run(
            tools_docs.update_tasks_file(
                tasks_file=str(project_dir / "demo-project-tasks.md"),
                completed_tasks=["Sub a"],
            )
        )
        assert active_task.read_pointer("sess-1") is None

    def test_other_project_pointers_untouched(self, project_dir):
        """Different project name = different pointer scope."""
        from mcp_orbit import tools_docs

        active_task.write_pointer("sess-1", "demo-project", ["54a"])
        active_task.write_pointer("sess-2", "other-project", ["54a"])

        asyncio.run(
            tools_docs.update_tasks_file(
                tasks_file=str(project_dir / "demo-project-tasks.md"),
                completed_tasks=["Sub a"],
            )
        )

        assert active_task.read_pointer("sess-1") is None
        # Other project pointer survives even though it shares the number "54a".
        assert active_task.read_pointer("sess-2")["task_numbers"] == ["54a"]

    def test_no_completions_means_no_sweep(self, project_dir):
        from mcp_orbit import tools_docs

        active_task.write_pointer("sess-1", "demo-project", ["54a"])

        result = asyncio.run(
            tools_docs.update_tasks_file(
                tasks_file=str(project_dir / "demo-project-tasks.md"),
                notes=["unrelated"],
            )
        )
        assert result["completed_numbers"] == []
        assert result["active_pointers_cleared_for_sessions"] == []
        assert active_task.read_pointer("sess-1")["task_numbers"] == ["54a"]

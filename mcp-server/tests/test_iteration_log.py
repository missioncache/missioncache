"""Tests for iteration_log.py - pure functions where possible, tmp_path for file reads."""

from pathlib import Path

from mcp_orbit.iteration_log import (
    _is_task_completed,
    _task_id_to_display,
    get_iteration_log_path,
    get_iteration_status,
    get_prompts_status,
)


# --- _task_id_to_display ---


class TestTaskIdToDisplay:
    def test_single_digit(self):
        """'01' converts to '1' (strips leading zero)."""
        assert _task_id_to_display("01") == "1"

    def test_subtask(self):
        """'01-02' converts to '1.2' (dot-separated)."""
        assert _task_id_to_display("01-02") == "1.2"


# --- get_iteration_status ---


class TestGetIterationStatus:
    def test_nonexistent_log(self, tmp_path):
        """Returns default dict when log file doesn't exist."""
        result = get_iteration_status(tmp_path, "no-such-task")
        assert result["exists"] is False
        assert result["iterations"] == 0
        assert result["completed"] is False

    def test_parses_iterations(self, tmp_path, sample_iteration_log):
        """Counts iteration entries correctly."""
        log_path = tmp_path / "my-task-iteration-log.md"
        log_path.write_text(sample_iteration_log)
        result = get_iteration_status(tmp_path, "my-task")
        assert result["exists"] is True
        assert result["iterations"] == 3
        assert result["last_status"] == "SUCCESS"
        assert result["started"] == "2026-04-01"
        assert result["max_iterations"] == 20

    def test_completed_marker(self, tmp_path, sample_iteration_log_completed):
        """Detects COMPLETED marker in log."""
        log_path = tmp_path / "my-task-iteration-log.md"
        log_path.write_text(sample_iteration_log_completed)
        result = get_iteration_status(tmp_path, "my-task")
        assert result["completed"] is True
        assert result["timed_out"] is False


# --- _is_task_completed ---


class TestIsTaskCompleted:
    def test_completed_task(self, tmp_path, sample_tasks_md):
        """Returns True for a checked task."""
        tasks_file = tmp_path / "tasks.md"
        tasks_file.write_text(sample_tasks_md)
        assert _is_task_completed(tasks_file, "01") is True

    def test_pending_task(self, tmp_path, sample_tasks_md):
        """Returns False for an unchecked task."""
        tasks_file = tmp_path / "tasks.md"
        tasks_file.write_text(sample_tasks_md)
        assert _is_task_completed(tasks_file, "03") is False

    def test_missing_file(self, tmp_path):
        """Returns False when tasks file doesn't exist."""
        tasks_file = tmp_path / "nonexistent.md"
        assert _is_task_completed(tasks_file, "01") is False


# --- get_prompts_status ---


class TestGetPromptsStatus:
    def test_no_prompts_dir(self, tmp_path):
        """Returns exists=False when prompts/ doesn't exist."""
        result = get_prompts_status(tmp_path, "my-task")
        assert result["exists"] is False
        assert result["total"] == 0

    def test_prompts_with_tasks(
        self, tmp_path, sample_prompt_content, sample_tasks_md
    ):
        """Cross-references prompt task_id against tasks.md checkboxes."""
        # Set up prompts dir with one prompt
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "task-01-prompt.md").write_text(sample_prompt_content)

        # Set up tasks file (task 1 is completed in sample_tasks_md)
        (tmp_path / "my-task-tasks.md").write_text(sample_tasks_md)

        result = get_prompts_status(tmp_path, "my-task")
        assert result["exists"] is True
        assert result["total"] == 1
        assert result["completed"] == 1
        assert result["remaining"] == 0
        assert result["next_prompt"] is None


# --- get_iteration_log_path ---


class TestGetIterationLogPath:
    def test_path_construction(self):
        """Builds correct log path from task_dir and task_name."""
        result = get_iteration_log_path("/home/user/.claude/orbit/active/my-task", "my-task")
        assert result == Path("/home/user/.claude/orbit/active/my-task/my-task-iteration-log.md")

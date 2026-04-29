"""Pure function tests for orbit.py - no I/O, no mocking."""

import pytest

from mcp_orbit.errors import ValidationError
from mcp_orbit.orbit import (
    _append_to_section,
    _update_section,
    format_tasks_markdown,
    parse_task_progress,
    validate_task_name,
)


# --- validate_task_name ---


class TestValidateTaskName:
    def test_valid_name(self):
        """Lowercase alphanumeric with hyphens is accepted."""
        validate_task_name("my-task-1")

    def test_uppercase_rejected(self):
        """Uppercase letters are rejected."""
        with pytest.raises(ValidationError, match="lowercase letters"):
            validate_task_name("My-Task")

    def test_spaces_rejected(self):
        """Spaces in name are rejected."""
        with pytest.raises(ValidationError, match="lowercase letters"):
            validate_task_name("my task")

    def test_empty_rejected(self):
        """Empty string is rejected."""
        with pytest.raises(ValidationError, match="cannot be empty"):
            validate_task_name("")

    def test_starts_with_hyphen_rejected(self):
        """Name starting with hyphen is rejected."""
        with pytest.raises(ValidationError, match="must start with a letter or digit"):
            validate_task_name("-my-task")


# --- format_tasks_markdown ---


class TestFormatTasksMarkdown:
    def test_flat_list(self):
        """Flat string list produces numbered checkboxes."""
        md, count = format_tasks_markdown(["Write tests", "Add docs"])
        assert count == 2
        assert "- [ ] 1. Write tests" in md
        assert "- [ ] 2. Add docs" in md

    def test_hierarchical(self):
        """Dict with subtasks produces parent.child numbering."""
        tasks = [
            {"title": "Infrastructure", "subtasks": ["Set up CI", "Add linting"]},
        ]
        md, count = format_tasks_markdown(tasks)
        # Subtasks are counted, parent is not (it has subtasks)
        assert count == 2
        assert "- [ ] 1. Infrastructure" in md
        assert "  - [ ] 1.1. Set up CI" in md
        assert "  - [ ] 1.2. Add linting" in md

    def test_empty_list(self):
        """Empty list returns TBD placeholder with count 0."""
        md, count = format_tasks_markdown([])
        assert md == "- [ ] TBD"
        assert count == 0


# --- parse_task_progress ---


class TestParseTaskProgress:
    def test_mixed_completion(self, sample_tasks_md):
        """Mixed completed/pending items are counted correctly."""
        progress = parse_task_progress(sample_tasks_md)
        assert progress.completed_items == 2
        assert progress.total_items == 5
        assert progress.completion_pct == 40

    def test_all_done(self, sample_tasks_md_all_done):
        """All completed gives 100%."""
        progress = parse_task_progress(sample_tasks_md_all_done)
        assert progress.completed_items == 3
        assert progress.total_items == 3
        assert progress.completion_pct == 100
        assert progress.remaining_summary is None

    def test_remaining_summary(self, sample_tasks_md):
        """Remaining summary captures first pending items."""
        progress = parse_task_progress(sample_tasks_md)
        assert progress.remaining_summary is not None
        assert "Implement core logic" in progress.remaining_summary
        assert "Write tests" in progress.remaining_summary
        assert "Add documentation" in progress.remaining_summary


# --- _update_section ---


class TestUpdateSection:
    def test_existing_section(self, sample_context_md):
        """Replaces content of an existing section."""
        result = _update_section(
            sample_context_md, "Next Steps", "1. New step one\n2. New step two"
        )
        assert "1. New step one" in result
        assert "2. New step two" in result
        # Old content should be gone
        assert "Implement core logic" not in result

    def test_new_section(self):
        """Appends a new section when it doesn't exist."""
        content = "# My Doc\n\nSome content.\n"
        result = _update_section(content, "New Section", "New content here")
        assert "## New Section" in result
        assert "New content here" in result


# --- _append_to_section ---


class TestAppendToSection:
    def test_append_existing(self, sample_context_md):
        """Appends to an existing section without removing old content."""
        result = _append_to_section(
            sample_context_md,
            "Key Architectural Decisions",
            "- Use SQLite for storage",
        )
        # Old content preserved
        assert "Use pydantic for models" in result
        # New content appended
        assert "Use SQLite for storage" in result

    def test_append_new_section(self):
        """Creates a new section when it doesn't exist."""
        content = "# My Doc\n\nSome content.\n"
        result = _append_to_section(content, "Notes", "- Important note")
        assert "## Notes" in result
        assert "- Important note" in result

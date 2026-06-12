"""Tests for orbit_auto.task_parser module."""

from pathlib import Path

from orbit_auto.task_parser import (
    extract_prompt_content,
    get_task_progress,
    get_uncompleted_tasks,
    is_all_tasks_completed,
    mark_task_completed,
    parse_prompt_yaml,
    parse_tasks_md,
)


class TestParseTasksMd:
    def test_basic_parsing(self, tmp_path, tasks_md_content):
        f = tmp_path / "tasks.md"
        f.write_text(tasks_md_content)
        tasks = parse_tasks_md(f)
        assert len(tasks) == 4
        assert tasks[0].number == "1"
        assert tasks[0].is_completed is True
        assert tasks[1].number == "2"
        assert tasks[1].is_completed is False
        assert tasks[3].is_wait is True

    def test_empty_file(self, tmp_path):
        f = tmp_path / "tasks.md"
        f.write_text("")
        tasks = parse_tasks_md(f)
        assert tasks == []

    def test_nonexistent_file(self, tmp_path):
        f = tmp_path / "nope.md"
        tasks = parse_tasks_md(f)
        assert tasks == []


class TestGetUncompletedTasks:
    def test_returns_only_uncompleted(self, tmp_path, tasks_md_content):
        f = tmp_path / "tasks.md"
        f.write_text(tasks_md_content)
        uncompleted = get_uncompleted_tasks(f)
        assert len(uncompleted) == 3
        assert all(not t.is_completed for t in uncompleted)


class TestGetTaskProgress:
    def test_progress_counts(self, tmp_path, tasks_md_content):
        f = tmp_path / "tasks.md"
        f.write_text(tasks_md_content)
        completed, total = get_task_progress(f)
        assert completed == 1
        assert total == 4


class TestIsAllTasksCompleted:
    def test_not_all_completed(self, tmp_path, tasks_md_content):
        f = tmp_path / "tasks.md"
        f.write_text(tasks_md_content)
        assert is_all_tasks_completed(f) is False

    def test_all_completed(self, tmp_path):
        f = tmp_path / "tasks.md"
        f.write_text("- [x] 1. Done\n- [x] 2. Also done\n")
        assert is_all_tasks_completed(f) is True

    def test_empty_file_is_completed(self, tmp_path):
        f = tmp_path / "tasks.md"
        f.write_text("")
        assert is_all_tasks_completed(f) is True


class TestParsePromptYaml:
    def test_full_frontmatter(self, tmp_path, prompt_yaml_content):
        f = tmp_path / "prompt.md"
        f.write_text(prompt_yaml_content)
        info = parse_prompt_yaml(f)
        assert info is not None
        assert info.task_id == "01"
        assert info.task_title == "Setup project"
        assert info.dependencies == ["02", "03"]
        assert info.agents == ["python-pro"]
        assert info.skills == ["pytest-patterns"]
        assert "<acceptance_criteria>" in info.content

    def test_no_frontmatter(self, tmp_path):
        f = tmp_path / "prompt.md"
        f.write_text("Just a prompt without frontmatter.")
        info = parse_prompt_yaml(f)
        assert info is None

    def test_minimal_frontmatter(self, tmp_path):
        f = tmp_path / "prompt.md"
        f.write_text('---\ntask_id: "05"\n---\nDo the thing.\n')
        info = parse_prompt_yaml(f)
        assert info is not None
        assert info.task_id == "05"
        assert info.task_title == ""
        assert info.dependencies == []
        assert info.content == "Do the thing."

    def test_inline_deps(self, tmp_path):
        content = (
            "---\n"
            'task_id: "03"\n'
            'task_title: "Third"\n'
            'dependencies: ["01", "02"]\n'
            "---\n"
            "Prompt body\n"
        )
        f = tmp_path / "prompt.md"
        f.write_text(content)
        info = parse_prompt_yaml(f)
        assert info is not None
        assert info.dependencies == ["01", "02"]

    def test_nonexistent_file(self, tmp_path):
        f = tmp_path / "nope.md"
        info = parse_prompt_yaml(f)
        assert info is None

    def test_missing_task_id_returns_none(self, tmp_path):
        f = tmp_path / "prompt.md"
        f.write_text('---\ntask_title: "No ID"\n---\nBody\n')
        info = parse_prompt_yaml(f)
        assert info is None


class TestExtractPromptContent:
    def test_with_frontmatter(self, tmp_path):
        f = tmp_path / "prompt.md"
        f.write_text('---\ntask_id: "01"\n---\nThe actual prompt.')
        content = extract_prompt_content(f)
        assert content == "The actual prompt."

    def test_without_frontmatter(self, tmp_path):
        f = tmp_path / "prompt.md"
        f.write_text("No frontmatter here, just content.")
        content = extract_prompt_content(f)
        assert content == "No frontmatter here, just content."

    def test_nonexistent_file(self, tmp_path):
        f = tmp_path / "nope.md"
        content = extract_prompt_content(f)
        assert content == ""


class TestMarkTaskCompleted:
    def test_marks_task(self, tmp_path, tasks_md_content):
        f = tmp_path / "tasks.md"
        f.write_text(tasks_md_content)
        result = mark_task_completed(f, "2")
        assert result is True
        updated = f.read_text()
        assert "- [x] 2." in updated

    def test_task_not_found(self, tmp_path, tasks_md_content):
        f = tmp_path / "tasks.md"
        f.write_text(tasks_md_content)
        result = mark_task_completed(f, "99")
        assert result is False

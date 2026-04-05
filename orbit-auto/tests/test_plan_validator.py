"""Tests for orbit_auto.plan_validator module."""

from orbit_auto.dag import DAG
from orbit_auto.plan_validator import Severity, ValidationIssue, has_errors, validate_plan


def _write_prompt(prompts_dir, task_id, title="Task", deps=None, has_criteria=True):
    """Helper to write a valid prompt file."""
    deps_str = ""
    if deps:
        deps_list = ", ".join(f'"{d}"' for d in deps)
        deps_str = f"dependencies: [{deps_list}]\n"

    criteria = ""
    if has_criteria:
        criteria = "\n<acceptance_criteria>\n- It works\n</acceptance_criteria>\n"

    content = (
        f"---\n"
        f'task_id: "{task_id}"\n'
        f'task_title: "{title}"\n'
        f"{deps_str}"
        f"---\n"
        f"Do the task.{criteria}"
    )
    prompts_dir.mkdir(parents=True, exist_ok=True)
    (prompts_dir / f"task-{task_id}-prompt.md").write_text(content)


class TestValidatePlanValid:
    def test_valid_prompts_no_issues(self, tmp_path):
        prompts_dir = tmp_path / "prompts"
        tasks_file = tmp_path / "tasks.md"
        tasks_file.write_text("- [ ] 1. First task\n- [ ] 2. Second task\n")

        _write_prompt(prompts_dir, "01", "First task")
        _write_prompt(prompts_dir, "02", "Second task", deps=["01"])

        dag = DAG.build_from_adjacency_list({"01": [], "02": ["01"]})
        issues = validate_plan(prompts_dir, tasks_file, dag)
        errors = [i for i in issues if i.severity == Severity.ERROR]
        assert errors == []


class TestMissingFrontmatter:
    def test_detects_missing_frontmatter(self, tmp_path):
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        tasks_file = tmp_path / "tasks.md"
        tasks_file.write_text("- [ ] 1. First task\n")

        # Write a prompt without frontmatter
        (prompts_dir / "task-01-prompt.md").write_text("No YAML here.\n")

        dag = DAG.build_from_adjacency_list({"01": []})
        issues = validate_plan(prompts_dir, tasks_file, dag)
        errors = [i for i in issues if i.severity == Severity.ERROR]
        assert len(errors) == 1
        assert "Missing or invalid YAML frontmatter" in errors[0].message


class TestBrokenDependencies:
    def test_detects_broken_dep_reference(self, tmp_path):
        prompts_dir = tmp_path / "prompts"
        tasks_file = tmp_path / "tasks.md"
        tasks_file.write_text("- [ ] 1. First task\n")

        _write_prompt(prompts_dir, "01", "First", deps=["99"])

        dag = DAG.build_from_adjacency_list({"01": ["99"]})
        issues = validate_plan(prompts_dir, tasks_file, dag)
        errors = [i for i in issues if i.severity == Severity.ERROR]
        assert len(errors) == 1
        assert "'99' does not exist" in errors[0].message


class TestMissingAcceptanceCriteria:
    def test_detects_missing_criteria(self, tmp_path):
        prompts_dir = tmp_path / "prompts"
        tasks_file = tmp_path / "tasks.md"
        tasks_file.write_text("- [ ] 1. First task\n")

        _write_prompt(prompts_dir, "01", "First", has_criteria=False)

        dag = DAG.build_from_adjacency_list({"01": []})
        issues = validate_plan(prompts_dir, tasks_file, dag)
        warnings = [i for i in issues if i.severity == Severity.WARNING]
        criteria_warnings = [w for w in warnings if "acceptance_criteria" in w.message]
        assert len(criteria_warnings) == 1


class TestMissingTaskId:
    def test_missing_task_id_is_error(self, tmp_path):
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        tasks_file = tmp_path / "tasks.md"
        tasks_file.write_text("- [ ] 1. First task\n")

        # Prompt with frontmatter but no task_id
        (prompts_dir / "task-01-prompt.md").write_text(
            '---\ntask_title: "No ID"\n---\nBody\n'
        )

        dag = DAG.build_from_adjacency_list({"01": []})
        issues = validate_plan(prompts_dir, tasks_file, dag)
        errors = [i for i in issues if i.severity == Severity.ERROR]
        assert len(errors) == 1
        assert "Missing or invalid YAML frontmatter" in errors[0].message


class TestHasErrors:
    def test_has_errors_true(self):
        issues = [ValidationIssue(Severity.ERROR, "broken")]
        assert has_errors(issues) is True

    def test_has_errors_false_with_warnings_only(self):
        issues = [ValidationIssue(Severity.WARNING, "minor")]
        assert has_errors(issues) is False

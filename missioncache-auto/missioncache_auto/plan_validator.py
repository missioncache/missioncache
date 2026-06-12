"""
Plan validation for orbit-auto parallel execution.

Runs after DAG build, before user confirmation. No Claude call needed -
pure deterministic checks on prompt files and task structure.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from orbit_auto.dag import DAG
from orbit_auto.task_parser import parse_prompt_yaml, parse_tasks_md


class Severity(Enum):
    ERROR = "error"
    WARNING = "warning"


@dataclass
class ValidationIssue:
    severity: Severity
    message: str


def validate_plan(
    prompts_dir: Path,
    tasks_file: Path,
    dag: DAG,
) -> list[ValidationIssue]:
    """
    Validate prompt files and task structure before execution.

    Checks:
    1. Required YAML frontmatter fields (task_id, task_title)
    2. Dependencies reference existing task IDs
    3. Tasks without corresponding prompts (missing prompts)
    4. Prompts without corresponding tasks (orphan prompts)
    5. Acceptance criteria present in each prompt

    Returns list of ValidationIssue (empty = all good).
    """
    issues: list[ValidationIssue] = []
    dag_task_ids = set(dag.tasks)

    # Parse tasks.md for cross-referencing
    tasks_md_numbers: set[str] = set()
    if tasks_file.exists():
        for task in parse_tasks_md(tasks_file):
            if not task.is_completed:
                tasks_md_numbers.add(f"{int(task.number):02d}")

    # Validate each prompt file
    prompt_files = sorted(prompts_dir.glob("task-*-prompt.md"))
    prompt_task_ids: set[str] = set()

    for prompt_file in prompt_files:
        info = parse_prompt_yaml(prompt_file)

        if info is None:
            issues.append(
                ValidationIssue(
                    Severity.ERROR,
                    f"{prompt_file.name}: Missing or invalid YAML frontmatter",
                )
            )
            continue

        prompt_task_ids.add(info.task_id)

        # Check required fields
        if not info.task_title:
            issues.append(
                ValidationIssue(
                    Severity.WARNING,
                    f"Task {info.task_id}: Missing task_title in frontmatter",
                )
            )

        # Check dependency consistency
        for dep in info.dependencies:
            if dep not in dag_task_ids:
                issues.append(
                    ValidationIssue(
                        Severity.ERROR,
                        f"Task {info.task_id}: Dependency '{dep}' does not exist",
                    )
                )

        # Check acceptance criteria
        if "<acceptance_criteria>" not in info.content:
            issues.append(
                ValidationIssue(
                    Severity.WARNING,
                    f"Task {info.task_id}: No <acceptance_criteria> section",
                )
            )

    # Check for tasks.md entries without prompts (only uncompleted)
    for task_num in sorted(tasks_md_numbers - prompt_task_ids):
        issues.append(
            ValidationIssue(
                Severity.WARNING,
                f"Task {task_num}: Listed in tasks.md but no prompt file found",
            )
        )

    # Check for orphan prompts (prompt exists but not in tasks.md)
    if tasks_md_numbers:
        for task_id in sorted(prompt_task_ids - tasks_md_numbers):
            # Only warn if tasks.md has entries - otherwise tasks.md might not
            # use the same numbering
            issues.append(
                ValidationIssue(
                    Severity.WARNING,
                    f"Task {task_id}: Prompt exists but not found in tasks.md",
                )
            )

    return issues


def has_errors(issues: list[ValidationIssue]) -> bool:
    """Check if any issues are errors (not just warnings)."""
    return any(i.severity == Severity.ERROR for i in issues)

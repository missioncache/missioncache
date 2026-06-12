"""
Task initialization for Orbit Auto.

Creates the directory structure and template files for a new task.
"""

from datetime import datetime
from pathlib import Path

from orbit_auto.templates import CONTEXT_TEMPLATE, PLAN_TEMPLATE, TASKS_TEMPLATE


def init_task(
    task_name: str,
    description: str = "",
    project_root: Path | None = None,
) -> Path:
    """
    Initialize a new task with template files.

    Creates:
    - ~/.orbit/active/<task-name>/
    - ~/.orbit/active/<task-name>/<task-name>-tasks.md
    - ~/.orbit/active/<task-name>/<task-name>-context.md
    - ~/.orbit/active/<task-name>/<task-name>-plan.md

    Args:
        task_name: Name of the task (used as directory name)
        description: Optional task description
        project_root: Project root directory (unused, kept for API compat)

    Returns:
        Path to the created task directory

    Raises:
        FileExistsError: If task directory already exists
    """
    # Create in centralized orbit root
    from orbit_db import ORBIT_ROOT

    orbit_active = ORBIT_ROOT / "active"
    orbit_active.mkdir(parents=True, exist_ok=True)

    # Create task directory
    task_dir = orbit_active / task_name
    if task_dir.exists():
        raise FileExistsError(f"Task directory already exists: {task_dir}")

    task_dir.mkdir()

    # Get current date
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Create tasks file
    tasks_content = TASKS_TEMPLATE.format(
        task_name=task_name,
        date=date_str,
        description=description or f"Description for {task_name}",
    )
    tasks_file = task_dir / f"{task_name}-tasks.md"
    tasks_file.write_text(tasks_content)

    # Create context file
    context_content = CONTEXT_TEMPLATE.format(
        task_name=task_name,
        date=date_str,
        description=description or f"Context for {task_name}",
    )
    context_file = task_dir / f"{task_name}-context.md"
    context_file.write_text(context_content)

    # Create plan file
    plan_content = PLAN_TEMPLATE.format(
        task_name=task_name,
        date=date_str,
        description=description or f"Implementation plan for {task_name}",
    )
    plan_file = task_dir / f"{task_name}-plan.md"
    plan_file.write_text(plan_content)

    return task_dir

"""
Runnable task calculation for Orbit Auto.

Determines which tasks can currently run based on:
- Task mode (auto/inter)
- Task completion status
- Dependencies (explicit and sequential)

This mirrors the logic in the orbit MCP server's get_runnable_tasks()
to enable orbit-auto to filter tasks without needing MCP connectivity.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple


@dataclass
class TaskModeInfo:
    """Information about a task including its mode and blocking status."""

    task_id: str
    title: str
    mode: str | None  # "auto", "inter", or None (defaults to inter)
    completed: bool
    dependencies: list[str]
    is_blocked: bool = False
    blocked_by: str | None = None
    blocker_mode: str | None = None


class RunnableResult(NamedTuple):
    """Result of checking which tasks can run."""

    runnable: list[TaskModeInfo]
    blocked: list[TaskModeInfo]
    blocked_by_inter: list[TaskModeInfo]
    completed: list[TaskModeInfo]
    all_tasks: list[TaskModeInfo]


def parse_task_modes(tasks_file: Path) -> list[TaskModeInfo]:
    """Parse tasks.md to extract task mode information.

    Reads the tasks file and extracts:
    - Task completion status from checkboxes
    - Mode markers like `[auto]`, `[inter]`, `[auto:depends=1,3]`
    - Explicit dependencies

    Args:
        tasks_file: Path to the tasks.md file

    Returns:
        List of TaskModeInfo objects
    """
    import re

    if not tasks_file.exists():
        return []

    content = tasks_file.read_text()
    results = []

    # Pattern for checkbox items with optional mode markers
    # Matches: - [ ] 1. Task description `[auto]` or `[auto:depends=1,3]`
    pattern = re.compile(
        r"^\s*-\s*\[([ xX])\]\s*"  # Checkbox: - [ ] or - [x]
        r"(\d+(?:\.\d+)?)\.\s*"  # Task number: 1. or 1.2.
        r"(.+?)$",  # Rest of line (title + optional mode)
        re.MULTILINE,
    )

    for match in pattern.finditer(content):
        checkbox = match.group(1)
        task_id = match.group(2)
        rest = match.group(3).strip()

        completed = checkbox.lower() == "x"

        # Parse mode marker from the rest of the line
        mode = None
        dependencies: list[str] = []
        title = rest

        # Look for mode marker at end: `[auto]` or `[inter]` or `[auto:depends=1,3]`
        mode_pattern = re.search(r"`\[(auto|inter)(?::depends=([^\]]+))?\]`\s*$", rest)
        if mode_pattern:
            mode = mode_pattern.group(1)
            if mode_pattern.group(2):
                deps_str = mode_pattern.group(2)
                dependencies = [d.strip() for d in deps_str.split(",") if d.strip()]
            title = rest[: mode_pattern.start()].strip()

        results.append(
            TaskModeInfo(
                task_id=task_id,
                title=title,
                mode=mode,
                completed=completed,
                dependencies=dependencies,
            )
        )

    return results


def _get_sequential_dependencies(task_id: str, all_tasks: list[TaskModeInfo]) -> list[str]:
    """Get implicit sequential dependencies for a task.

    Task N depends on task N-1 unless it has explicit dependencies.
    For hierarchical tasks like 1.2, it depends on 1.1.

    Args:
        task_id: The task ID to get dependencies for
        all_tasks: All tasks in the file

    Returns:
        List of task IDs that this task implicitly depends on
    """
    # Find the task to check if it has explicit dependencies
    task = next((t for t in all_tasks if t.task_id == task_id), None)
    if task and task.dependencies:
        # Task has explicit dependencies, no implicit ones
        return []

    # Build set of all task IDs
    all_task_ids = {t.task_id for t in all_tasks}

    # Parse task_id into components
    if "." in task_id:
        # Hierarchical: 1.2 depends on 1.1
        parts = task_id.rsplit(".", 1)
        parent = parts[0]
        sub_num = int(parts[1])
        if sub_num > 1:
            return [f"{parent}.{sub_num - 1}"]
        else:
            # 1.1 depends on task 1 (the parent) if it exists
            return [parent] if parent in all_task_ids else []
    else:
        # Simple: task 2 depends on task 1
        try:
            num = int(task_id)
            if num > 1:
                prev_id = str(num - 1)
                return [prev_id] if prev_id in all_task_ids else []
        except ValueError:
            pass

    return []


def get_runnable_tasks(tasks_file: Path) -> RunnableResult:
    """Get tasks that can currently run based on mode and dependencies.

    Analyzes the tasks file to determine:
    - Which auto tasks are ready to run (dependencies satisfied)
    - Which tasks are blocked (and by what)
    - Which tasks are blocked specifically by interactive tasks

    Args:
        tasks_file: Path to the tasks.md file

    Returns:
        RunnableResult with runnable, blocked, blocked_by_inter, completed lists
    """
    task_modes = parse_task_modes(tasks_file)

    if not task_modes:
        return RunnableResult(
            runnable=[],
            blocked=[],
            blocked_by_inter=[],
            completed=[],
            all_tasks=[],
        )

    # Build lookup
    task_by_id = {tm.task_id: tm for tm in task_modes}

    runnable: list[TaskModeInfo] = []
    blocked: list[TaskModeInfo] = []
    blocked_by_inter: list[TaskModeInfo] = []
    completed: list[TaskModeInfo] = []

    for tm in task_modes:
        if tm.completed:
            completed.append(tm)
            continue

        # Only autonomous tasks can be "runnable" for orbit-auto
        if tm.mode != "auto":
            continue

        # Check dependencies
        is_blocked = False
        blocker_id = None
        blocker_mode = None

        # Get all dependencies (explicit + sequential)
        all_deps = _get_sequential_dependencies(tm.task_id, task_modes)
        # Add explicit dependencies
        all_deps.extend(tm.dependencies)
        # Deduplicate while preserving order
        all_deps = list(dict.fromkeys(all_deps))

        for dep_id in all_deps:
            dep_task = task_by_id.get(dep_id)
            if not dep_task:
                continue  # Unknown dependency, skip

            if not dep_task.completed:
                is_blocked = True
                blocker_id = dep_id
                blocker_mode = dep_task.mode if dep_task.mode else "inter"
                break

        # Update the task info with blocking status
        tm.is_blocked = is_blocked
        tm.blocked_by = blocker_id
        tm.blocker_mode = blocker_mode

        if is_blocked:
            blocked.append(tm)
            if blocker_mode == "inter":
                blocked_by_inter.append(tm)
        else:
            runnable.append(tm)

    return RunnableResult(
        runnable=runnable,
        blocked=blocked,
        blocked_by_inter=blocked_by_inter,
        completed=completed,
        all_tasks=task_modes,
    )


def get_blocking_summary(tasks_file: Path) -> dict:
    """Get a summary of task blocking status.

    Args:
        tasks_file: Path to the tasks.md file

    Returns:
        Dict with runnable_count, blocked_count, blocked_by_inter_count,
        and first_inter_blocker if applicable
    """
    result = get_runnable_tasks(tasks_file)

    # Find the first interactive task that's blocking other tasks
    first_inter_blocker = None
    if result.blocked_by_inter:
        # Get the blocker ID from the first blocked task
        blocker_id = result.blocked_by_inter[0].blocked_by
        if blocker_id:
            task_by_id = {t.task_id: t for t in result.all_tasks}
            blocker = task_by_id.get(blocker_id)
            if blocker:
                first_inter_blocker = {
                    "task_id": blocker.task_id,
                    "title": blocker.title,
                }

    return {
        "runnable_count": len(result.runnable),
        "blocked_count": len(result.blocked),
        "blocked_by_inter_count": len(result.blocked_by_inter),
        "completed_count": len(result.completed),
        "first_inter_blocker": first_inter_blocker,
    }

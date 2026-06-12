"""Iteration log integration for autonomous task execution.

Progress tracking is done ONLY via checkboxes in the tasks.md file.
Prompts do not have status fields - tasks.md is the single source of truth.
"""

import re
from datetime import datetime
from pathlib import Path
from typing import Any


def get_iteration_log_path(task_dir: str | Path, task_name: str) -> Path:
    """Get path to the iteration log file."""
    return Path(task_dir) / f"{task_name}-iteration-log.md"


def log_iteration(
    task_dir: str | Path,
    task_name: str,
    iteration: int,
    status: str,
    task_title: str | None = None,
    what_done: list[str] | None = None,
    files_modified: list[str] | None = None,
    validation: dict[str, str] | None = None,
    error_details: str | None = None,
    next_steps: list[str] | None = None,
) -> str:
    """Log an iteration to the iteration log.

    Args:
        task_dir: Task directory path
        task_name: Task name
        iteration: Iteration number
        status: SUCCESS, FAILED, or BLOCKED
        task_title: Title of the task being worked on
        what_done: List of what was done/attempted
        files_modified: List of modified files
        validation: Dict of validation results {tests: PASS, typecheck: PASS, etc.}
        error_details: Error details if status is FAILED
        next_steps: Suggested next steps if FAILED

    Returns:
        The log entry that was written
    """
    log_path = get_iteration_log_path(task_dir, task_name)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Build the log entry
    entry = f"\n## Iteration {iteration}"
    if task_title:
        entry += f" - {task_title}"
    entry += f"\n**Status:** {status}\n"
    entry += f"**Time:** {timestamp}\n\n"

    if what_done:
        header = (
            "### What was done" if status == "SUCCESS" else "### What was attempted"
        )
        entry += f"{header}\n"
        for item in what_done:
            entry += f"- {item}\n"
        entry += "\n"

    if files_modified:
        entry += "### Files modified\n"
        for f in files_modified:
            entry += f"- {f}\n"
        entry += "\n"

    if validation:
        entry += "### Validation\n"
        for check, result in validation.items():
            entry += f"- {check}: {result}\n"
        entry += "\n"

    if error_details:
        entry += f"### Error details\n{error_details}\n\n"

    if next_steps:
        entry += "### Next steps to try\n"
        for step in next_steps:
            entry += f"- {step}\n"
        entry += "\n"

    # Append to log
    if log_path.exists():
        with open(log_path, "a") as f:
            f.write(entry)
    else:
        # Create new log with header
        log_path.write_text(f"""# Iteration Log - {task_name}

**Started:** {timestamp.split()[0]}
**Max Iterations:** 20

---
{entry}""")

    return entry


def log_completion(
    task_dir: str | Path,
    task_name: str,
    total_iterations: int,
    duration_seconds: int,
) -> str:
    """Log task completion to the iteration log."""
    log_path = get_iteration_log_path(task_dir, task_name)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    entry = f"""
---

# COMPLETED
**Finished:** {timestamp}
**Total iterations:** {total_iterations}
**Duration:** {duration_seconds}s
"""

    if log_path.exists():
        with open(log_path, "a") as f:
            f.write(entry)

    return entry


def log_timeout(
    task_dir: str | Path,
    task_name: str,
    max_iterations: int,
    duration_seconds: int,
) -> str:
    """Log timeout to the iteration log."""
    log_path = get_iteration_log_path(task_dir, task_name)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    entry = f"""
---

# TIMEOUT
**Stopped:** {timestamp}
**Reached max iterations:** {max_iterations}
**Duration:** {duration_seconds}s
"""

    if log_path.exists():
        with open(log_path, "a") as f:
            f.write(entry)

    return entry


def get_iteration_status(task_dir: str | Path, task_name: str) -> dict[str, Any]:
    """Get the current iteration loop status from the log file.

    Returns:
        Dict with status info: {
            exists: bool,
            started: str | None,
            max_iterations: int | None,
            iterations: int,
            last_status: str | None,
            completed: bool,
            timed_out: bool,
            blocked: bool,
        }
    """
    log_path = get_iteration_log_path(task_dir, task_name)

    if not log_path.exists():
        return {
            "exists": False,
            "started": None,
            "max_iterations": None,
            "iterations": 0,
            "last_status": None,
            "completed": False,
            "timed_out": False,
            "blocked": False,
        }

    content = log_path.read_text()

    # Parse started time
    started_match = re.search(r"\*\*Started:\*\* (.+)", content)
    started = started_match.group(1) if started_match else None

    # Parse max iterations
    max_match = re.search(r"\*\*Max Iterations:\*\* (\d+)", content)
    max_iterations = int(max_match.group(1)) if max_match else None

    # Count iterations
    iterations = len(re.findall(r"## Iteration \d+", content))

    # Get last status
    status_matches = list(
        re.finditer(r"\*\*Status:\*\* (SUCCESS|FAILED|BLOCKED)", content)
    )
    last_status = status_matches[-1].group(1) if status_matches else None

    # Check for completion/timeout
    completed = "# COMPLETED" in content
    timed_out = "# TIMEOUT" in content
    blocked = "BLOCKED" in content and not completed

    return {
        "exists": True,
        "started": started,
        "max_iterations": max_iterations,
        "iterations": iterations,
        "last_status": last_status,
        "completed": completed,
        "timed_out": timed_out,
        "blocked": blocked,
    }


def _task_id_to_display(task_id: str) -> str:
    """Convert task_id format to display format.

    "01" -> "1"
    "01-02" -> "1.2"
    """
    if "-" in task_id:
        parts = task_id.split("-")
        return f"{int(parts[0])}.{int(parts[1])}"
    return str(int(task_id))


def _is_task_completed(tasks_file: Path, task_id: str) -> bool:
    """Check if a task is marked as completed in the tasks file.

    Returns True if the checkbox is marked [x], False if [ ] or not found.
    """
    if not tasks_file.exists():
        return False

    content = tasks_file.read_text()
    display_id = _task_id_to_display(task_id)

    # Escape dots for regex
    id_escaped = display_id.replace(".", r"\.")

    # Check if task is marked completed: "- [x] N." or "- [x] N:"
    if re.search(rf"^\s*- \[x\] {id_escaped}[.:]", content, re.MULTILINE):
        return True

    return False


def get_prompts_status(
    task_dir: str | Path, task_name: str | None = None
) -> dict[str, Any]:
    """Get status of optimized prompts for a task.

    Progress is tracked via checkboxes in the tasks.md file, not via status fields
    in prompt files.

    Returns:
        Dict with prompt status: {
            exists: bool,
            total: int,
            completed: int,  # Tasks marked [x] in tasks.md
            remaining: int,  # Tasks still [ ] in tasks.md
            next_prompt: str | None,  # Path to next prompt for uncompleted task
        }
    """
    task_dir = Path(task_dir)
    prompts_dir = task_dir / "prompts"

    if not prompts_dir.exists():
        return {
            "exists": False,
            "total": 0,
            "completed": 0,
            "remaining": 0,
            "next_prompt": None,
        }

    prompt_files = list(prompts_dir.glob("task-*-prompt.md"))

    if not prompt_files:
        return {
            "exists": True,
            "total": 0,
            "completed": 0,
            "remaining": 0,
            "next_prompt": None,
        }

    # Find tasks file
    if task_name:
        tasks_file = task_dir / f"{task_name}-tasks.md"
    else:
        # Try to infer from directory name
        tasks_file = task_dir / f"{task_dir.name}-tasks.md"

    completed = 0
    remaining = 0
    next_prompt = None

    for pf in sorted(prompt_files):
        content = pf.read_text()

        # Extract task_id from YAML frontmatter
        task_id_match = re.search(
            r"^task_id:\s*[\"']?([^\"'\n]+)[\"']?", content, re.MULTILINE
        )
        if task_id_match:
            task_id = task_id_match.group(1).strip()

            if _is_task_completed(tasks_file, task_id):
                completed += 1
            else:
                remaining += 1
                if next_prompt is None:
                    next_prompt = str(pf)
        else:
            # No task_id found, count as remaining
            remaining += 1
            if next_prompt is None:
                next_prompt = str(pf)

    return {
        "exists": True,
        "total": len(prompt_files),
        "completed": completed,
        "remaining": remaining,
        "next_prompt": next_prompt,
    }

"""
Task file parsing utilities.

Handles parsing of tasks.md files, YAML frontmatter in prompts,
and updating task status/timestamps.
"""

import re
from datetime import datetime
from pathlib import Path
from typing import NamedTuple


class TaskInfo(NamedTuple):
    """Information about a task extracted from tasks.md."""

    number: str
    title: str
    is_completed: bool
    is_wait: bool  # Has [WAIT] marker
    line_number: int


class PromptInfo(NamedTuple):
    """Information extracted from a prompt file's YAML frontmatter."""

    task_id: str
    task_title: str
    dependencies: list[str]
    agents: list[str]
    skills: list[str]
    content: str  # Prompt content without frontmatter
    tdd: bool | None = None  # Per-task TDD override (True/False/None=use global)


def parse_tasks_md(tasks_file: Path) -> list[TaskInfo]:
    """
    Parse tasks.md file and extract task information.

    Looks for checkbox patterns like:
    - [ ] 1. Task title
    - [x] 2. Completed task
    - [ ] [WAIT] 3. Blocked task
    """
    if not tasks_file.exists():
        return []

    tasks = []
    content = tasks_file.read_text()

    # Pattern matches: - [ ] or - [x] followed by optional [WAIT], then number
    # Examples: "- [ ] 1. Title", "- [x] 2: Title", "- [ ] [WAIT] 3. Title"
    pattern = r"^\s*- \[([ x])\]\s*(\[WAIT\])?\s*(\d+)[.:]\s*(.+)$"

    for line_num, line in enumerate(content.split("\n"), 1):
        match = re.match(pattern, line)
        if match:
            is_completed = match.group(1) == "x"
            is_wait = match.group(2) is not None
            number = match.group(3)
            title = match.group(4).strip()

            tasks.append(
                TaskInfo(
                    number=number,
                    title=title,
                    is_completed=is_completed,
                    is_wait=is_wait,
                    line_number=line_num,
                )
            )

    return tasks


def get_uncompleted_tasks(tasks_file: Path) -> list[TaskInfo]:
    """Get only uncompleted tasks from tasks.md."""
    return [t for t in parse_tasks_md(tasks_file) if not t.is_completed]


def get_first_uncompleted_task(tasks_file: Path) -> TaskInfo | None:
    """Get the first uncompleted task from tasks.md."""
    uncompleted = get_uncompleted_tasks(tasks_file)
    return uncompleted[0] if uncompleted else None


def is_all_tasks_completed(tasks_file: Path) -> bool:
    """Check if all tasks in tasks.md are completed."""
    tasks = parse_tasks_md(tasks_file)
    return all(t.is_completed for t in tasks) if tasks else True


def get_task_progress(tasks_file: Path) -> tuple[int, int]:
    """Get (completed, total) task counts."""
    tasks = parse_tasks_md(tasks_file)
    completed = sum(1 for t in tasks if t.is_completed)
    return completed, len(tasks)


def parse_prompt_yaml(prompt_file: Path) -> PromptInfo | None:
    """
    Parse YAML frontmatter from a prompt file.

    Expected format:
    ---
    task_id: "01"
    task_title: "Add priority field"
    dependencies: ["01", "02"]
    agents:
      - python-pro
    skills:
      - pytest-patterns
    ---
    <actual prompt content>
    """
    if not prompt_file.exists():
        return None

    content = prompt_file.read_text()

    # Check for YAML frontmatter
    if not content.startswith("---"):
        return None

    # Find the closing ---
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None

    yaml_content = parts[1].strip()
    prompt_content = parts[2].strip()

    # Parse YAML manually (simple key-value parsing)
    task_id = ""
    task_title = ""
    dependencies: list[str] = []
    agents: list[str] = []
    skills: list[str] = []
    tdd: bool | None = None

    current_list: list[str] | None = None

    for line in yaml_content.split("\n"):
        line = line.strip()
        if not line:
            continue

        # Check for list item
        if line.startswith("- "):
            if current_list is not None:
                item = line[2:].strip().strip("\"'")
                current_list.append(item)
            continue

        # Check for key-value pair
        if ":" in line:
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip().strip("\"'")

            if key == "task_id":
                task_id = value
                current_list = None
            elif key == "task_title":
                task_title = value
                current_list = None
            elif key == "dependencies":
                # Could be inline array or multi-line
                if value.startswith("["):
                    # Inline array: ["01", "02"]
                    deps_match = re.findall(r'["\']([^"\']+)["\']', value)
                    dependencies = [d.strip() for d in deps_match]
                    current_list = None
                else:
                    current_list = dependencies
            elif key == "agents":
                if value.startswith("["):
                    agents = [
                        a.strip().strip("\"'") for a in value.strip("[]").split(",") if a.strip()
                    ]
                    current_list = None
                else:
                    current_list = agents
            elif key == "skills":
                if value.startswith("["):
                    skills = [
                        s.strip().strip("\"'") for s in value.strip("[]").split(",") if s.strip()
                    ]
                    current_list = None
                else:
                    current_list = skills
            elif key == "tdd":
                tdd = value.lower() in ("true", "yes", "1")
                current_list = None
            else:
                current_list = None

    if not task_id:
        return None

    return PromptInfo(
        task_id=task_id,
        task_title=task_title,
        dependencies=dependencies,
        agents=agents,
        skills=skills,
        content=prompt_content,
        tdd=tdd,
    )


def get_prompt_for_task(prompts_dir: Path, task_number: str) -> Path | None:
    """
    Get the prompt file for a given task number.

    Task number is converted to padded format: 1 -> task-01-prompt.md
    """
    task_id_padded = f"{int(task_number):02d}"
    prompt_file = prompts_dir / f"task-{task_id_padded}-prompt.md"
    return prompt_file if prompt_file.exists() else None


def validate_prompts_exist(prompts_dir: Path, tasks_file: Path) -> list[str]:
    """
    Validate that all uncompleted tasks have corresponding prompt files.

    Returns list of missing prompt file descriptions.
    """
    uncompleted = get_uncompleted_tasks(tasks_file)
    missing = []

    for task in uncompleted:
        prompt_file = get_prompt_for_task(prompts_dir, task.number)
        if prompt_file is None:
            missing.append(f"task-{int(task.number):02d}-prompt.md (task {task.number})")

    return missing


def mark_task_completed(tasks_file: Path, task_number: str) -> bool:
    """
    Mark a task as completed in tasks.md by changing [ ] to [x].

    Returns True if task was found and marked.
    """
    if not tasks_file.exists():
        return False

    content = tasks_file.read_text()

    # Pattern: - [ ] N. or - [ ] N: (with optional leading whitespace)
    pattern = rf"^(\s*)- \[ \] ({task_number}[.:])"
    replacement = r"\1- [x] \2"

    new_content, count = re.subn(pattern, replacement, content, flags=re.MULTILINE)

    if count > 0:
        tasks_file.write_text(new_content)
        return True
    return False


def update_timestamps(tasks_file: Path, context_file: Path | None = None) -> None:
    """Update Last Updated timestamps in task files."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    for file_path in [tasks_file, context_file]:
        if file_path and file_path.exists():
            content = file_path.read_text()
            pattern = r"^\*\*Last Updated:\*\*.*$"
            replacement = f"**Last Updated:** {timestamp}"
            new_content = re.sub(pattern, replacement, content, flags=re.MULTILINE)
            file_path.write_text(new_content)


def update_remaining_summary(tasks_file: Path, summary: str) -> None:
    """Update the Remaining field in tasks.md with natural language summary."""
    if not tasks_file.exists():
        return

    content = tasks_file.read_text()
    pattern = r"^\*\*Remaining:\*\*.*$"
    replacement = f"**Remaining:** {summary}"
    new_content = re.sub(pattern, replacement, content, flags=re.MULTILINE)
    tasks_file.write_text(new_content)


def extract_prompt_content(prompt_file: Path) -> str:
    """Extract prompt content, stripping YAML frontmatter."""
    if not prompt_file.exists():
        return ""

    content = prompt_file.read_text()

    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            return parts[2].strip()

    return content.strip()

"""Orbit file operations."""

import re
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Any

from .config import settings
from .errors import OrbitFileNotFoundError, ValidationError
from .models import OrbitFiles, TaskProgress

_TASK_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def validate_task_name(name: str) -> None:
    """Validate task name is safe for filesystem and git branch use.

    Raises ValidationError if name contains unsafe characters.
    """
    if not name or not _TASK_NAME_RE.match(name):
        raise ValidationError(
            "Task name must be lowercase alphanumeric with hyphens (e.g. 'my-task-1')",
            field="task_name",
        )


def get_timestamp() -> str:
    """Get current local timestamp."""
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def format_tasks_markdown(tasks: list) -> tuple[str, int]:
    """Format tasks list into numbered markdown.

    Supports two formats:
    1. Flat list: ["task1", "task2"] -> numbered tasks
    2. Hierarchical: [{"title": "Parent", "subtasks": ["sub1", "sub2"]}, ...] -> parent.child numbering

    Args:
        tasks: List of task strings or dicts with title/subtasks

    Returns:
        Tuple of (markdown string, total task count)
    """
    if not tasks:
        return "- [ ] TBD", 0

    lines = []
    total_count = 0

    for i, task in enumerate(tasks, start=1):
        if isinstance(task, dict):
            # Hierarchical: {"title": "Parent task", "subtasks": ["sub1", "sub2"]}
            title = task.get("title", "")
            subtasks = task.get("subtasks", [])

            if subtasks:
                # Parent task with subtasks
                lines.append(f"- [ ] {i}. {title}")
                for j, subtask in enumerate(subtasks, start=1):
                    lines.append(f"  - [ ] {i}.{j}. {subtask}")
                    total_count += 1
            else:
                # Just a parent without subtasks (treat as flat)
                lines.append(f"- [ ] {i}. {title}")
                total_count += 1
        else:
            # Flat: just a string
            lines.append(f"- [ ] {i}. {task}")
            total_count += 1

    return "\n".join(lines), total_count


def get_task_dir(task_name: str, active: bool = True) -> Path:
    """Get the task directory path under ORBIT_ROOT."""
    subdir = settings.active_dir_name if active else settings.completed_dir_name
    return settings.orbit_root / subdir / task_name


def get_orbit_files(task_name: str, full_path: str | None = None) -> OrbitFiles:
    """Get paths to all orbit files for a task.

    Args:
        task_name: Task name (used for file naming conventions)
        full_path: Optional path relative to orbit_root (e.g., 'active/parent/subtask').
                   If provided, uses this instead of constructing from task_name.
                   This is required for subtasks which are nested under parent tasks.
    """
    if full_path:
        # Use the full path directly (supports subtasks)
        task_dir = settings.orbit_root / full_path
    else:
        # Fall back to constructing from task_name (top-level tasks only)
        task_dir = get_task_dir(task_name)

    # Try both naming conventions
    plan_candidates = [
        task_dir / f"{task_name}-plan.md",
        task_dir / "plan.md",
    ]
    context_candidates = [
        task_dir / f"{task_name}-context.md",
        task_dir / "context.md",
    ]
    tasks_candidates = [
        task_dir / f"{task_name}-tasks.md",
        task_dir / "tasks.md",
    ]

    def find_file(candidates: list[Path]) -> str | None:
        for c in candidates:
            if c.exists():
                return str(c)
        return None

    prompts_dir = task_dir / "prompts"

    return OrbitFiles(
        task_dir=str(task_dir),
        plan_file=find_file(plan_candidates),
        context_file=find_file(context_candidates),
        tasks_file=find_file(tasks_candidates),
        prompts_dir=str(prompts_dir) if prompts_dir.exists() else None,
    )


def create_orbit_files(
    task_name: str,
    description: str = "TBD",
    jira_key: str | None = None,
    branch: str | None = None,
    tasks: list[str] | None = None,
    plan_content: dict[str, str] | None = None,
) -> OrbitFiles:
    """Create orbit files for a task under ORBIT_ROOT.

    Args:
        task_name: Task name (kebab-case)
        description: Short description for context.md
        jira_key: Optional JIRA ticket
        branch: Optional git branch
        tasks: List of task descriptions for tasks.md
        plan_content: Optional dict with plan sections (summary, goals, etc.)

    Returns:
        OrbitFiles with paths to created files
    """
    validate_task_name(task_name)
    task_dir = get_task_dir(task_name)
    task_dir.mkdir(parents=True, exist_ok=True)

    timestamp = get_timestamp()
    templates = resources.files("mcp_orbit.templates")

    # Create context.md
    context_template = templates.joinpath("context.md").read_text()
    context_content = context_template.replace(
        "{{task_name}}", task_name.replace("-", " ").title()
    )
    context_content = context_content.replace("{{timestamp}}", timestamp)
    context_content = context_content.replace("{{description}}", description)

    context_file = task_dir / f"{task_name}-context.md"
    context_file.write_text(context_content)

    # Create tasks.md
    tasks_template = templates.joinpath("tasks.md").read_text()
    tasks_content = tasks_template.replace(
        "{{task_name}}", task_name.replace("-", " ").title()
    )
    tasks_content = tasks_content.replace("{{timestamp}}", timestamp)

    if tasks:
        tasks_md, total_count = format_tasks_markdown(tasks)
        tasks_content = tasks_content.replace("{{tasks}}", tasks_md)
        remaining = f"{total_count} tasks pending"
    else:
        tasks_content = tasks_content.replace("{{tasks}}", "- [ ] TBD")
        remaining = "TBD"

    tasks_content = tasks_content.replace("{{remaining}}", remaining)

    tasks_file = task_dir / f"{task_name}-tasks.md"
    tasks_file.write_text(tasks_content)

    # Create plan.md
    plan_template = templates.joinpath("plan.md").read_text()
    plan_content = plan_content or {}

    plan_md = plan_template.replace(
        "{{task_name}}", task_name.replace("-", " ").title()
    )
    plan_md = plan_md.replace("{{timestamp}}", timestamp)
    plan_md = plan_md.replace("{{jira_key}}", jira_key or "")
    plan_md = plan_md.replace("{{branch}}", branch or f"feature/{task_name}")
    plan_md = plan_md.replace("{{summary}}", plan_content.get("summary", "TBD"))
    plan_md = plan_md.replace(
        "{{research_findings}}",
        plan_content.get("research_findings", "N/A - research phase skipped"),
    )
    plan_md = plan_md.replace("{{goals}}", plan_content.get("goals", "TBD"))
    plan_md = plan_md.replace(
        "{{success_criteria}}", plan_content.get("success_criteria", "TBD")
    )
    plan_md = plan_md.replace("{{approach}}", plan_content.get("approach", "TBD"))
    plan_md = plan_md.replace("{{files}}", plan_content.get("files", "TBD"))
    plan_md = plan_md.replace(
        "{{dependencies}}", plan_content.get("dependencies", "None")
    )
    plan_md = plan_md.replace("{{risks}}", plan_content.get("risks", "None"))

    plan_file = task_dir / f"{task_name}-plan.md"
    plan_file.write_text(plan_md)

    return OrbitFiles(
        task_dir=str(task_dir),
        plan_file=str(plan_file),
        context_file=str(context_file),
        tasks_file=str(tasks_file),
        prompts_dir=None,
    )


def update_context_file(
    context_file: str | Path,
    next_steps: list[str] | None = None,
    recent_changes: list[str] | None = None,
    key_decisions: list[str] | None = None,
    gotchas: list[str] | None = None,
    key_files: dict[str, str] | None = None,
) -> str:
    """Update sections in a context.md file atomically.

    Args:
        context_file: Path to context.md
        next_steps: List of next steps to add/replace
        recent_changes: List of recent changes to add
        key_decisions: List of decisions to add
        gotchas: List of gotchas to add
        key_files: Dict of file paths to descriptions

    Returns:
        Updated file content
    """
    path = Path(context_file)
    if not path.exists():
        raise OrbitFileNotFoundError(str(path))

    content = path.read_text()
    timestamp = get_timestamp()

    # Update Last Updated timestamp
    content = re.sub(
        r"\*\*Last Updated:\*\* .+",
        f"**Last Updated:** {timestamp}",
        content,
    )

    # Update Next Steps section
    if next_steps:
        next_steps_md = "\n".join(
            f"{i + 1}. {step}" for i, step in enumerate(next_steps)
        )
        content = _update_section(content, "Next Steps", next_steps_md)

    # Update Recent Changes section
    if recent_changes:
        changes_md = "\n".join(f"- {change}" for change in recent_changes)
        # Try to update existing section or append
        if "## Recent Changes" in content:
            content = _update_section(
                content, f"Recent Changes ({timestamp})", changes_md
            )
        else:
            content = _update_section(
                content, "Recent Changes", f"### {timestamp}\n\n{changes_md}"
            )

    # Update Key Decisions section
    if key_decisions:
        decisions_md = "\n".join(f"- {d}" for d in key_decisions)
        content = _append_to_section(
            content, "Key Architectural Decisions", decisions_md
        )

    # Update Gotchas section
    if gotchas:
        gotchas_md = "\n".join(f"- {g}" for g in gotchas)
        content = _append_to_section(content, "Gotchas", gotchas_md)

    # Update Key Files section
    if key_files:
        files_md = "\n".join(
            f"| `{path}` | {desc} |" for path, desc in key_files.items()
        )
        content = _append_to_section(content, "Key Files", files_md)

    path.write_text(content)
    return content


def update_tasks_file(
    tasks_file: str | Path,
    completed_tasks: list[str] | None = None,
    new_tasks: list[str] | None = None,
    remaining_summary: str | None = None,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    """Update a tasks.md file.

    Args:
        tasks_file: Path to tasks.md
        completed_tasks: List of task descriptions to mark as [x]
        new_tasks: List of new tasks to add
        remaining_summary: New summary for Remaining field
        notes: Notes to add

    Returns:
        Dict with update summary
    """
    path = Path(tasks_file)
    if not path.exists():
        raise OrbitFileNotFoundError(str(path))

    content = path.read_text()
    timestamp = get_timestamp()
    updates_made = []

    # Update Last Updated timestamp
    content = re.sub(
        r"\*\*Last Updated:\*\* .+",
        f"**Last Updated:** {timestamp}",
        content,
    )

    # Mark tasks as completed
    if completed_tasks:
        for task_desc in completed_tasks:
            # Escape regex special chars in task description
            escaped = re.escape(task_desc)
            # Match the checkbox pattern with the task description
            pattern = rf"- \[\s*\]([^\n]*{escaped}[^\n]*)"
            if re.search(pattern, content, re.IGNORECASE):
                content = re.sub(pattern, r"- [x]\1", content, flags=re.IGNORECASE)
                updates_made.append(f"Completed: {task_desc[:50]}...")

    # Add new tasks (before Phase 2/Validation section)
    if new_tasks:
        # Find the highest existing task number to continue numbering
        existing_numbers = re.findall(
            r"^\s*[-*]\s*\[[x\s]\]\s*(\d+)\.", content, re.MULTILINE
        )
        next_num = max([int(n) for n in existing_numbers], default=0) + 1

        new_tasks_lines = []
        for i, task in enumerate(new_tasks):
            new_tasks_lines.append(f"- [ ] {next_num + i}. {task}")
        new_tasks_md = "\n".join(new_tasks_lines)

        # Find a good insertion point (before Phase 2 or Validation)
        insertion_patterns = [
            r"(## Phase 2)",
            r"(## Validation)",
            r"(## Notes)",
        ]
        inserted = False
        for pattern in insertion_patterns:
            if re.search(pattern, content):
                content = re.sub(pattern, f"{new_tasks_md}\n\n\\1", content)
                inserted = True
                break

        if not inserted:
            content += f"\n{new_tasks_md}\n"

        updates_made.append(f"Added {len(new_tasks)} new tasks")

    # Update Remaining summary
    if remaining_summary:
        content = re.sub(
            r"\*\*Remaining:\*\* .+",
            f"**Remaining:** {remaining_summary}",
            content,
        )
        updates_made.append(f"Updated remaining: {remaining_summary[:50]}...")

    # Add notes
    if notes:
        notes_md = "\n".join(f"- {n}" for n in notes)
        content = _append_to_section(content, "Notes", notes_md)
        updates_made.append(f"Added {len(notes)} notes")

    path.write_text(content)

    # Calculate progress
    progress = parse_task_progress(content)

    return {
        "file": str(path),
        "updates_made": updates_made,
        "progress": progress.model_dump() if progress else None,
    }


def parse_task_progress(content: str) -> TaskProgress:
    """Parse progress from tasks.md content."""
    # Match markdown checklist items: - [ ] or - [x]
    completed_pattern = r"^\s*[-*]\s*\[x\]"
    pending_pattern = r"^\s*[-*]\s*\[\s*\]"

    completed = len(
        re.findall(completed_pattern, content, re.MULTILINE | re.IGNORECASE)
    )
    pending = len(re.findall(pending_pattern, content, re.MULTILINE))

    total = completed + pending
    pct = int((completed / total * 100) if total > 0 else 0)

    # Extract remaining items as summary (first few pending items)
    remaining_items = re.findall(r"^\s*[-*]\s*\[\s*\]\s*(.+)$", content, re.MULTILINE)
    remaining_summary = None
    if remaining_items:
        # Take first 2-3 items as summary
        summary_items = remaining_items[:3]
        remaining_summary = "; ".join(item.strip() for item in summary_items)
        if len(remaining_items) > 3:
            remaining_summary += f" (+{len(remaining_items) - 3} more)"

    return TaskProgress(
        completion_pct=pct,
        total_items=total,
        completed_items=completed,
        remaining_summary=remaining_summary,
    )


def _update_section(content: str, section_name: str, new_content: str) -> str:
    """Replace content of a section (from ## heading to next ## heading)."""
    pattern = rf"(## {re.escape(section_name)}[^\n]*\n)(.+?)(?=\n## |\Z)"

    if re.search(pattern, content, re.DOTALL):
        return re.sub(pattern, rf"\1\n{new_content}\n\n", content, flags=re.DOTALL)
    else:
        # Section doesn't exist, append it
        return content + f"\n## {section_name}\n\n{new_content}\n"


def _append_to_section(content: str, section_name: str, new_content: str) -> str:
    """Append content to an existing section."""
    pattern = rf"(## {re.escape(section_name)}[^\n]*\n)(.+?)(?=\n## |\Z)"

    match = re.search(pattern, content, re.DOTALL)
    if match:
        existing = match.group(2).strip()
        combined = f"{existing}\n{new_content}"
        return re.sub(pattern, rf"\1{combined}\n\n", content, flags=re.DOTALL)
    else:
        # Section doesn't exist, create it
        return content + f"\n## {section_name}\n\n{new_content}\n"

"""Orbit file operations."""

import contextlib
import fcntl
import os
import re
from collections.abc import Callable, Iterator
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Any

from orbit_db import validate_task_name as _orbit_db_validate_task_name

from .config import settings
from .errors import ErrorCode, OrbitError, OrbitFileNotFoundError, ValidationError
from .models import OrbitFiles, TaskProgress
from .tasks_parse import parse_tasks_md


# NOTE: ``_file_lock`` and ``_atomic_update_text`` below are duplicated in
# ``hooks/pre_compact.py`` to keep the PreCompact hook self-contained
# (avoids dragging mcp_orbit's transitive imports into the hook hot path).
# If you change locking semantics here, mirror the change in the hook.


@contextlib.contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive lock on a sidecar lockfile next to ``path``.

    The lockfile (``<path>.lock``) is a long-lived sidecar; we never delete
    it because creation/deletion under contention is racy.
    """
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lockfd:
        fcntl.flock(lockfd.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lockfd.fileno(), fcntl.LOCK_UN)


def _atomic_update_text(path: Path, transform: Callable[[str], str]) -> str:
    """Atomically update a text file under exclusive lock.

    Acquires a flock on a sidecar lockfile, reads current content, applies the
    transform, writes the result to ``<path>.tmp``, and atomically replaces
    the target via ``os.replace``. A crash mid-write leaves the original file
    intact; concurrent callers serialize on the lockfile so their
    read-modify-write cycles do not interleave.
    """
    with _file_lock(path):
        content = path.read_text()
        new_content = transform(content)
        tmp_path = path.with_name(path.name + ".tmp")
        tmp_path.write_text(new_content)
        os.replace(tmp_path, path)
        return new_content


def validate_task_name(name: str) -> None:
    """Validate task name is safe for filesystem and git branch use.

    Delegates to ``orbit_db.validate_task_name`` (the single source of
    truth for the regex and per-branch error messages) and re-raises
    its ``ValueError`` as the structured ``ValidationError`` that mcp
    callers and tests already expect. Keeping the wrap thin here means
    a future tightening of the rule lands in one place (orbit-db) and
    propagates to every surface.
    """
    try:
        _orbit_db_validate_task_name(name)
    except ValueError as e:
        raise ValidationError(str(e), field="task_name") from e


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

    When ``full_path`` is given (e.g. ``active/parent/subtask`` for nested
    subtasks), it is authoritative. Otherwise, search the active directory
    first, then the completed directory. This lets ``/orbit:go`` and the
    /orbit:save flow find archived projects without prompting the user to
    "create files" - which would otherwise overwrite the archived content.
    """
    if full_path:
        candidate_dirs = [settings.orbit_root / full_path]
    else:
        candidate_dirs = [
            get_task_dir(task_name, active=True),
            get_task_dir(task_name, active=False),
        ]

    def find_file(candidates: list[Path]) -> str | None:
        for c in candidates:
            if c.exists():
                return str(c)
        return None

    chosen_dir = candidate_dirs[0]
    plan_file = context_file = tasks_file = None
    for task_dir in candidate_dirs:
        p = find_file([task_dir / f"{task_name}-plan.md", task_dir / "plan.md"])
        c = find_file(
            [task_dir / f"{task_name}-context.md", task_dir / "context.md"]
        )
        t = find_file([task_dir / f"{task_name}-tasks.md", task_dir / "tasks.md"])
        if p or c or t:
            chosen_dir = task_dir
            plan_file, context_file, tasks_file = p, c, t
            break

    prompts_dir = chosen_dir / "prompts"

    return OrbitFiles(
        task_dir=str(chosen_dir),
        plan_file=plan_file,
        context_file=context_file,
        tasks_file=tasks_file,
        prompts_dir=str(prompts_dir) if prompts_dir.exists() else None,
    )


def create_orbit_files(
    task_name: str,
    description: str = "TBD",
    jira_key: str | None = None,
    branch: str | None = None,
    tasks: list[str] | None = None,
    plan_content: dict[str, str] | None = None,
    force: bool = False,
) -> OrbitFiles:
    """Create orbit files for a task under ORBIT_ROOT.

    Args:
        task_name: Task name (kebab-case)
        description: Short description for context.md
        jira_key: Optional JIRA ticket
        branch: Optional git branch
        tasks: List of task descriptions for tasks.md
        plan_content: Optional dict with plan sections (summary, goals, etc.)
        force: If True, overwrite existing files. If False (default), raise
            OrbitError(ALREADY_EXISTS) when any of plan/context/tasks already
            exist on disk for this task. Prevents silent data loss when the
            same name is reused.

    Returns:
        OrbitFiles with paths to created files
    """
    validate_task_name(task_name)
    task_dir = get_task_dir(task_name)

    if not force:
        # Include both prefixed AND legacy unprefixed filenames - get_orbit_files
        # accepts both, so the guard must too. Otherwise creating a task whose
        # dir already has only legacy files would write fresh prefixed files,
        # and the legacy content would be hidden by the read-time precedence.
        existing = [
            p
            for p in (
                task_dir / f"{task_name}-plan.md",
                task_dir / f"{task_name}-context.md",
                task_dir / f"{task_name}-tasks.md",
                task_dir / "plan.md",
                task_dir / "context.md",
                task_dir / "tasks.md",
            )
            if p.exists()
        ]
        if existing:
            raise OrbitError(
                ErrorCode.ALREADY_EXISTS,
                f"Orbit files for '{task_name}' already exist. "
                f"Pass force=True to overwrite, or pick a different name.",
                {
                    "task_name": task_name,
                    "task_dir": str(task_dir),
                    "existing_files": [str(p) for p in existing],
                },
            )

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

    def _transform(content: str) -> str:
        # Stamp inside the lock so serialized writers each get a fresh
        # timestamp instead of all sharing the function-entry value.
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

        # Update Recent Changes section - consolidate into one `## Recent Changes`
        # heading with dated `###` sub-sections prepended (newest first). Before,
        # each save appended a new top-level `## Recent Changes (timestamp)` which
        # fragmented the file with N unmerged sections.
        if recent_changes:
            changes_md = "\n".join(f"- {change}" for change in recent_changes)
            new_subsection = f"### {timestamp}\n\n{changes_md}\n"
            # Tolerates old-style `## Recent Changes (timestamp)` heading so
            # pre-existing context files keep working without migration.
            heading_pattern = r"(## Recent Changes[^\n]*\n)"
            match = re.search(heading_pattern, content)
            if match:
                heading_end = match.end()
                content = (
                    content[:heading_end]
                    + f"\n{new_subsection}\n"
                    + content[heading_end:]
                )
            else:
                content = content + f"\n## Recent Changes\n\n{new_subsection}"

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
                f"| `{filename}` | {desc} |"
                for filename, desc in key_files.items()
            )
            content = _append_to_section(content, "Key Files", files_md)

        return content

    return _atomic_update_text(path, _transform)


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
        Dict with update summary including ``completed_numbers``: the
        checklist numbers (e.g. ``["54a", "56"]``) of items that were
        unchecked before this call and are now checked. Used by callers
        to drive cross-cutting cleanup like clearing active-task pointers.
    """
    path = Path(tasks_file)
    if not path.exists():
        raise OrbitFileNotFoundError(str(path))

    updates_made: list[str] = []
    completed_numbers_seen: list[str] = []

    def _transform(content: str) -> str:
        # Stamp inside the lock so serialized writers each get a fresh
        # timestamp instead of all sharing the function-entry value.
        timestamp = get_timestamp()

        # Snapshot pre-transform unchecked items so we can diff after
        # marking completions and report the actual numbers transitioned.
        pre_unchecked = {
            item.number for item in parse_tasks_md(content) if not item.checked
        }

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
                    content = re.sub(
                        pattern, r"- [x]\1", content, flags=re.IGNORECASE
                    )
                    updates_made.append(f"Completed: {task_desc[:50]}...")

        # Diff post-transform: any number that was [ ] before and is [x]
        # now is a real transition. This catches edits regardless of how
        # the caller phrased ``completed_tasks`` (description, fragment,
        # etc.) and ignores items that were already checked beforehand.
        post_checked = {
            item.number for item in parse_tasks_md(content) if item.checked
        }
        completed_numbers_seen.extend(sorted(pre_unchecked & post_checked))

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
            updates_made.append(f"Updated remaining: {remaining_summary}")

        # Add notes
        if notes:
            notes_md = "\n".join(f"- {n}" for n in notes)
            content = _append_to_section(content, "Notes", notes_md)
            updates_made.append(f"Added {len(notes)} notes")

        return content

    new_content = _atomic_update_text(path, _transform)

    # Calculate progress from the just-written content
    progress = parse_task_progress(new_content)

    return {
        "file": str(path),
        "updates_made": updates_made,
        "progress": progress.model_dump() if progress else None,
        "completed_numbers": completed_numbers_seen,
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
    """Append content to an existing section.

    Strips template placeholders (lines that are exactly `- TBD` or `1. TBD`)
    so the first real write replaces the template rather than sitting alongside it.
    """
    pattern = rf"(## {re.escape(section_name)}[^\n]*\n)(.+?)(?=\n## |\Z)"

    match = re.search(pattern, content, re.DOTALL)
    if match:
        existing_lines = [
            line for line in match.group(2).strip().splitlines()
            if line.strip() not in ("- TBD", "1. TBD")
        ]
        existing = "\n".join(existing_lines)
        combined = f"{existing}\n{new_content}" if existing else new_content
        return re.sub(pattern, rf"\1{combined}\n\n", content, flags=re.DOTALL)
    else:
        # Section doesn't exist, create it
        return content + f"\n## {section_name}\n\n{new_content}\n"

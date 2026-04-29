"""Shared helper functions used across tool modules."""

import asyncio
import logging
import urllib.request
from pathlib import Path

from . import orbit
from .config import settings
from .db import Task, get_db
from .errors import TaskNotFoundError, ValidationError
from .models import TaskDetail, TaskProgress, TaskSummary

logger = logging.getLogger(__name__)


async def _notify_dashboard_task_created() -> None:
    """Fire-and-forget POST to the dashboard so it syncs immediately.

    The dashboard polls SQLite every 60 seconds; this shaves that lag
    off the user-visible "created a project, doesn't show up yet" case.
    Silently swallows every failure - the dashboard is optional, may
    not be running, and we never want to fail a tool call over it.
    """
    url = f"{settings.dashboard_url}/api/hooks/task-created"

    def _post() -> None:
        try:
            req = urllib.request.Request(
                url,
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=0.5)
        except Exception:
            pass

    await asyncio.to_thread(_post)


def _resolve_to_git_root(path: str) -> str:
    """Walk parents of ``path`` looking for ``.git``; return the git root.

    Mirrors ``git rev-parse --show-toplevel`` semantics - the first
    ancestor (closest to ``path``) that contains a ``.git`` entry
    (directory or file, the latter for submodules) is the git root.
    Falls back to the resolved input if no ancestor has ``.git``
    before the filesystem root, so non-git project locations stay
    supported.

    Used at the MCP-tool boundary (``create_orbit_files``,
    ``set_task_repo``) to enforce git-root resolution server-side
    instead of trusting callers to do it. Slash command guidance
    can be skipped silently by the model; tool-level enforcement
    cannot. Callers that legitimately want a sub-package within a
    monorepo to be the project boundary should pass
    ``resolve_git_root=False`` to the tool rather than calling this
    helper directly.

    Symlinks are followed once via ``Path.resolve()`` before the walk
    so a symlink to a subdir of a git repo lands on the real path.
    ``OSError`` mid-walk (permission denied on ``.git`` probing) is
    treated as "no git root found" and returns the resolved input.
    """
    current = Path(path).expanduser().resolve()

    walker = current
    while walker != walker.parent:
        try:
            if (walker / ".git").exists():
                return str(walker)
        except OSError:
            return str(current)
        walker = walker.parent
    return str(current)


def _validate_path(
    path: str, field_name: str = "path", must_be_under: Path | None = None
) -> Path:
    """Validate and resolve a filesystem path.

    Checks for empty strings and null bytes, then resolves the path.
    If must_be_under is provided, verifies the resolved path is contained
    within that directory.

    Raises:
        ValidationError: If path is empty, contains null bytes, or resolves
            outside the required root directory.
    """
    if not path or not path.strip():
        raise ValidationError(f"{field_name} cannot be empty", field=field_name)
    if "\x00" in path:
        raise ValidationError(f"{field_name} contains null bytes", field=field_name)
    resolved = Path(path).resolve()
    if must_be_under is not None:
        root = must_be_under.resolve()
        if resolved != root and not str(resolved).startswith(str(root) + "/"):
            raise ValidationError(
                f"{field_name} must be within {root}", field=field_name
            )
    return resolved


def _resolve_task_dir(
    db, task_id: int | None, task_name: str | None
) -> tuple[Path, str]:
    """Resolve task directory and name from task_id or task_name.

    Returns:
        Tuple of (task_dir, task_name).

    Raises:
        TaskNotFoundError: If task cannot be found.
    """
    task = None
    if task_id:
        task = db.get_task(task_id)
    elif task_name:
        task = db.get_task_by_name(task_name)

    if not task:
        identifier = task_id if task_id is not None else (task_name or "unknown")
        raise TaskNotFoundError(identifier)

    task_dir = settings.orbit_root / task.full_path
    return task_dir, task.name


def _task_to_summary(
    task: Task, db=None, time_seconds: int | None = None
) -> TaskSummary:
    """Convert a Task to TaskSummary with time info.

    Args:
        task: Task object to convert.
        db: Database instance (auto-resolved if None).
        time_seconds: Pre-fetched time in seconds. If None, fetches from DB.
    """
    if db is None:
        db = get_db()

    # Get time tracking info (use pre-fetched if available)
    if time_seconds is None:
        time_seconds = db.get_task_time(task.id)
    time_formatted = db.format_duration(time_seconds)

    # Get effective last updated (uses file mtime if more recent)
    effective_last = db.get_effective_last_updated(task)
    last_worked_ago = db.format_time_ago(effective_last)

    # Get repo info if available
    repo_name = None
    repo_path = None
    if task.repo_id:
        repo = db.get_repo(task.repo_id)
        if repo:
            repo_name = repo.short_name
            repo_path = repo.path

    # Check if orbit files exist
    has_orbit_files = False
    if task.full_path:
        task_dir = settings.orbit_root / task.full_path
        has_orbit_files = task_dir.exists() and any(
            (task_dir / f).exists()
            for f in [
                f"{task.name}-context.md",
                f"{task.name}-tasks.md",
                f"{task.name}-plan.md",
                "context.md",
                "tasks.md",
            ]
        )

    return TaskSummary(
        id=task.id,
        name=task.name,
        status=task.status,
        type=task.task_type,
        repo_name=repo_name,
        repo_path=repo_path,
        jira_key=task.jira_key,
        tags=task.tags,
        time_total_seconds=time_seconds,
        time_formatted=time_formatted,
        last_worked_on=effective_last,
        last_worked_ago=last_worked_ago,
        has_orbit_files=has_orbit_files,
    )


def _task_to_detail(
    task: Task, include_subtasks: bool = True, include_updates: bool = True
) -> TaskDetail:
    """Convert a Task to TaskDetail with full information."""
    db = get_db()

    # Get base summary fields
    summary = _task_to_summary(task, db)

    # Parse progress from orbit files
    progress = None
    if task.full_path:
        progress = _parse_task_progress(
            settings.orbit_root / task.full_path, task.name
        )

    # Get subtasks if this is a parent task
    subtasks = []
    if include_subtasks:
        hierarchy = db.get_active_tasks_hierarchical(task.repo_id)
        if task.id in hierarchy.get("children", {}):
            for subtask in hierarchy["children"][task.id]:
                subtasks.append(_task_to_summary(subtask, db))

    # Get recent updates for non-coding tasks
    recent_updates = []
    if include_updates and task.task_type == "non-coding":
        recent_updates = db.get_task_updates(task.id, limit=5)

    return TaskDetail(
        **summary.model_dump(by_alias=True),
        full_path=task.full_path,
        parent_id=task.parent_id,
        branch=task.branch,
        pr_url=task.pr_url,
        created_at=task.created_at,
        updated_at=task.updated_at,
        completed_at=task.completed_at,
        progress=progress,
        subtasks=subtasks,
        recent_updates=recent_updates,
    )


def _parse_task_progress(task_dir: Path, task_name: str) -> TaskProgress | None:
    """Parse progress from the tasks.md file."""
    if not task_dir.exists():
        return None

    # Try both naming conventions
    tasks_files = [
        task_dir / f"{task_name}-tasks.md",
        task_dir / "tasks.md",
    ]

    for tasks_file in tasks_files:
        if tasks_file.exists():
            try:
                content = tasks_file.read_text()
                return orbit.parse_task_progress(content)
            except Exception as e:
                logger.warning(f"Failed to parse {tasks_file}: {e}")
                continue

    return None

"""Task lifecycle MCP tools - listing, retrieval, CRUD, non-coding updates."""

import logging
import shutil
from typing import Annotated

from pydantic import Field

from . import orbit
from .app import mcp
from .config import settings
from .db import get_db
from .errors import OrbitError, InvalidStateError, TaskNotFoundError
from .helpers import (
    _notify_dashboard_task_created,
    _task_to_detail,
    _task_to_summary,
    _validate_path,
)
from .models import (
    CompleteTaskResult,
    CreateTaskResult,
    ListTasksResult,
    ReopenTaskResult,
)

logger = logging.getLogger(__name__)


def _build_summaries(tasks, db, include_time: bool):
    """Convert Task objects to TaskSummary list with optional batch time lookup."""
    if include_time and tasks:
        task_ids = [t.id for t in tasks]
        times = db.get_batch_task_times(task_ids)
        return [
            _task_to_summary(task, db, time_seconds=times.get(task.id, 0))
            for task in tasks
        ]
    return [_task_to_summary(task, db) for task in tasks]


# =============================================================================
# TASK LISTING
# =============================================================================


@mcp.tool()
async def list_active_tasks(
    repo_path: Annotated[
        str | None, Field(description="Filter by repo path (optional)")
    ] = None,
    task_type: Annotated[
        str | None, Field(description="Filter by type: 'coding' or 'non-coding'")
    ] = None,
    include_time: Annotated[
        bool, Field(description="Include time tracking info")
    ] = True,
    prioritize_by_repo: Annotated[
        bool,
        Field(
            description="When repo_path is set, return repo tasks in 'tasks' and "
            "non-repo tasks in 'other_tasks' instead of filtering"
        ),
    ] = False,
) -> dict:
    """
    List all active tasks with time tracking and progress info.

    Returns tasks sorted by last worked on (most recent first).
    Much faster than multiple tool calls - single DB query with batch time lookup.

    When prioritize_by_repo=True and repo_path is set, returns two lists:
    - tasks: projects belonging to the given repo (shown first)
    - other_tasks: all other active projects
    """
    db = get_db()

    try:
        repo_id = None
        if repo_path:
            repo = db.get_repo_by_path(repo_path)
            if repo:
                repo_id = repo.id

        if prioritize_by_repo and repo_id:
            # Two-tier: fetch all tasks, split by repo membership
            all_tasks = db.get_active_tasks(None)

            if task_type:
                all_tasks = [t for t in all_tasks if t.task_type == task_type]

            repo_tasks = [t for t in all_tasks if t.repo_id == repo_id]
            other_tasks = [t for t in all_tasks if t.repo_id != repo_id]

            repo_summaries = _build_summaries(repo_tasks, db, include_time)
            other_summaries = _build_summaries(other_tasks, db, include_time)

            return ListTasksResult(
                tasks=repo_summaries,
                total_count=len(repo_summaries) + len(other_summaries),
                filter_applied=f"prioritized repo={repo_path}",
                other_tasks=other_summaries if other_summaries else None,
            ).model_dump()
        else:
            # Original behavior: filter by repo or return all
            tasks = db.get_active_tasks(repo_id)

            if task_type:
                tasks = [t for t in tasks if t.task_type == task_type]

            summaries = _build_summaries(tasks, db, include_time)

            filter_desc = []
            if repo_path:
                filter_desc.append(f"repo={repo_path}")
            if task_type:
                filter_desc.append(f"type={task_type}")

            return ListTasksResult(
                tasks=summaries,
                total_count=len(summaries),
                filter_applied=", ".join(filter_desc) if filter_desc else None,
            ).model_dump()

    except Exception as e:
        logger.exception("Error listing tasks")
        return {"error": True, "message": str(e)}


@mcp.tool()
async def list_completed_tasks(
    days: Annotated[int, Field(description="Number of days to look back")] = 7,
    limit: Annotated[int, Field(description="Maximum tasks to return")] = 20,
) -> dict:
    """List recently completed tasks."""
    db = get_db()

    try:
        tasks = db.get_recent_completed(days=days)[:limit]

        summaries = [_task_to_summary(task, db) for task in tasks]

        return ListTasksResult(
            tasks=summaries,
            total_count=len(summaries),
            filter_applied=f"completed within {days} days",
        ).model_dump()

    except Exception as e:
        logger.exception("Error listing completed tasks")
        return {"error": True, "message": str(e)}


# =============================================================================
# TASK RETRIEVAL
# =============================================================================


@mcp.tool()
async def get_task(
    task_id: Annotated[int | None, Field(description="Task ID")] = None,
    project_name: Annotated[
        str | None, Field(description="Project name (alternative to ID)")
    ] = None,
    include_subtasks: Annotated[
        bool, Field(description="Include subtask details")
    ] = True,
    include_updates: Annotated[
        bool, Field(description="Include recent updates for non-coding tasks")
    ] = True,
) -> dict:
    """
    Get full task details including progress, time, and prompt config.

    Provide either task_id OR project_name (not both).
    Returns all information needed for /continue-task in a single call.
    """
    db = get_db()

    try:
        task = None

        if task_id:
            task = db.get_task(task_id)
        elif project_name:
            task = db.get_task_by_name(project_name)
        else:
            return {
                "error": True,
                "code": "VALIDATION_ERROR",
                "message": "Provide task_id or project_name",
            }

        if not task:
            raise TaskNotFoundError(task_id or project_name)

        detail = _task_to_detail(task, include_subtasks, include_updates)
        return detail.model_dump()

    except OrbitError as e:
        return e.to_dict()
    except Exception as e:
        logger.exception("Error getting task")
        return {"error": True, "message": str(e)}


@mcp.tool()
async def find_task_for_directory(
    directory: Annotated[str, Field(description="Directory path to find task for")],
    session_id: Annotated[
        str | None,
        Field(
            description=(
                "Claude session ID. Strongly recommended: without it, matching "
                "falls through to cwd-pattern only, which fails when cwd is the "
                "repo root. Resolve via the filesystem (most-recently-modified "
                "transcript in ~/.claude/projects/<sanitized-cwd>/); see "
                "commands/save.md for the canonical pattern."
            )
        ),
    ] = None,
) -> dict:
    """
    Find the active task for a given directory.

    Lookup priority (see orbit_db.find_task_for_cwd):
    1. pending-project.json (cwd match)
    2. projects/<session_id>.json - requires session_id arg
    3. cwd under ~/.claude/orbit/active/<task>/

    Callers that invoke this from arbitrary cwds (e.g. the repo root) MUST
    pass session_id for priority 2 to fire. The 4 orbit slash commands all
    do this; copy their pattern rather than omitting the arg.
    """
    db = get_db()

    try:
        _validate_path(directory, "directory")
        task = db.find_task_for_cwd(directory, session_id)

        if not task:
            return {"found": False, "task": None}

        detail = _task_to_detail(task, include_subtasks=False, include_updates=False)
        return {"found": True, "task": detail.model_dump()}

    except OrbitError as e:
        return e.to_dict()
    except Exception as e:
        logger.exception("Error finding task for directory")
        return {"error": True, "message": str(e)}


# =============================================================================
# TASK LIFECYCLE
# =============================================================================


@mcp.tool()
async def create_task(
    name: Annotated[str, Field(description="Task name (e.g., 'kafka-consumer-fix')")],
    task_type: Annotated[
        str, Field(description="Type: 'coding' or 'non-coding'")
    ] = "coding",
    repo_path: Annotated[
        str | None, Field(description="Repository path (required for coding tasks)")
    ] = None,
    jira_key: Annotated[
        str | None, Field(description="JIRA ticket ID (e.g., 'PROJ-12345')")
    ] = None,
) -> dict:
    """
    Create a new task in the database.

    For coding tasks, also creates the orbit/active/<name>/ directory.
    For non-coding tasks, no directory is created.
    """
    db = get_db()

    try:
        # Validate inputs
        orbit.validate_task_name(name)
        if task_type not in ("coding", "non-coding"):
            return {
                "error": True,
                "code": "VALIDATION_ERROR",
                "message": "type must be 'coding' or 'non-coding'",
            }

        repo_id = None
        orbit_path = None

        if task_type == "coding":
            if not repo_path:
                return {
                    "error": True,
                    "code": "VALIDATION_ERROR",
                    "message": "repo_path required for coding tasks",
                }
            _validate_path(repo_path, "repo_path")

            repo = db.get_repo_by_path(repo_path)
            if not repo:
                # Auto-register repo
                repo_id = db.add_repo(repo_path)
            else:
                repo_id = repo.id

            # Create active/<name>/ directory under ORBIT_ROOT
            task_dir = settings.orbit_root / settings.active_dir_name / name
            task_dir.mkdir(parents=True, exist_ok=True)
            orbit_path = str(task_dir)

        # Create task in DB
        task = db.create_task(
            name=name,
            task_type=task_type,
            repo_id=repo_id,
            jira_key=jira_key,
        )

        await _notify_dashboard_task_created()

        return CreateTaskResult(
            task_id=task.id,
            task_name=task.name,
            task_type=task.task_type,
            orbit_path=orbit_path,
        ).model_dump()

    except OrbitError as e:
        return e.to_dict()
    except Exception as e:
        logger.exception("Error creating task")
        return {"error": True, "message": str(e)}


@mcp.tool()
async def complete_task(
    task_id: Annotated[int | None, Field(description="Task ID")] = None,
    project_name: Annotated[
        str | None, Field(description="Project name (alternative to ID)")
    ] = None,
    move_files: Annotated[bool, Field(description="Move orbit files to completed/")] = True,
) -> dict:
    """
    Mark a task as completed.

    For coding tasks, optionally moves orbit files from active/ to completed/.
    """
    db = get_db()

    try:
        task = None

        if task_id:
            task = db.get_task(task_id)
        elif project_name:
            task = db.get_task_by_name(project_name, status="active")
        else:
            return {
                "error": True,
                "code": "VALIDATION_ERROR",
                "message": "Provide task_id or project_name",
            }

        if not task:
            raise TaskNotFoundError(task_id or project_name)

        if task.status == "completed":
            raise InvalidStateError(
                "Task is already completed", current_state="completed"
            )

        previous_status = task.status

        # Update status
        updated_task = db.update_task_status(task.id, "completed")

        # Move files if requested
        if move_files and task.task_type == "coding":
            source = settings.orbit_root / task.full_path
            if source.exists():
                dest = settings.orbit_root / settings.completed_dir_name / task.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(dest))
                logger.info(f"Moved {source} to {dest}")

        # Get final time
        time_total = db.get_task_time(task.id)

        return CompleteTaskResult(
            task_id=task.id,
            task_name=task.name,
            previous_status=previous_status,
            new_status="completed",
            completed_at=updated_task.completed_at or "",
            time_total_formatted=db.format_duration(time_total),
        ).model_dump()

    except OrbitError as e:
        return e.to_dict()
    except Exception as e:
        logger.exception("Error completing task")
        return {"error": True, "message": str(e)}


@mcp.tool()
async def reopen_task(
    task_id: Annotated[int | None, Field(description="Task ID")] = None,
    project_name: Annotated[
        str | None, Field(description="Project name (alternative to ID)")
    ] = None,
    move_files: Annotated[
        bool, Field(description="Move orbit files from completed/ to active/")
    ] = True,
) -> dict:
    """
    Reopen a completed task.

    For coding tasks, optionally moves orbit files from completed/ back to active/.
    """
    db = get_db()

    try:
        task = None

        if task_id:
            task = db.get_task(task_id)
        elif project_name:
            task = db.get_task_by_name(project_name, status="completed")
        else:
            return {
                "error": True,
                "code": "VALIDATION_ERROR",
                "message": "Provide task_id or project_name",
            }

        if not task:
            raise TaskNotFoundError(task_id or project_name)

        if task.status != "completed":
            raise InvalidStateError(
                "Task is not completed",
                current_state=task.status,
                expected_state="completed",
            )

        previous_status = task.status

        # Move files back if requested
        if move_files and task.task_type == "coding":
            source = settings.orbit_root / settings.completed_dir_name / task.name
            if source.exists():
                dest = settings.orbit_root / settings.active_dir_name / task.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(dest))
                logger.info(f"Moved {source} to {dest}")

        # Reopen task
        updated_task = db.reopen_task(task.id)

        return ReopenTaskResult(
            task_id=task.id,
            task_name=task.name,
            previous_status=previous_status,
            new_status="active",
        ).model_dump()

    except OrbitError as e:
        return e.to_dict()
    except Exception as e:
        logger.exception("Error reopening task")
        return {"error": True, "message": str(e)}


# =============================================================================
# NON-CODING TASK UPDATES
# =============================================================================


@mcp.tool()
async def add_task_update(
    task_id: Annotated[int, Field(description="Task ID")],
    note: Annotated[str, Field(description="Update note")],
) -> dict:
    """
    Add a timestamped update to a task.

    Primarily for non-coding tasks to track progress notes.
    """
    db = get_db()

    try:
        task = db.get_task(task_id)
        if not task:
            raise TaskNotFoundError(task_id)

        update_id = db.add_task_update(task_id, note)

        return {
            "update_id": update_id,
            "task_id": task_id,
            "task_name": task.name,
            "note": note,
        }

    except OrbitError as e:
        return e.to_dict()
    except Exception as e:
        logger.exception("Error adding task update")
        return {"error": True, "message": str(e)}


@mcp.tool()
async def get_task_updates(
    task_id: Annotated[int, Field(description="Task ID")],
    limit: Annotated[int, Field(description="Maximum updates to return")] = 20,
) -> dict:
    """Get updates for a task."""
    db = get_db()

    try:
        task = db.get_task(task_id)
        if not task:
            raise TaskNotFoundError(task_id)

        updates = db.get_task_updates(task_id, limit)

        return {
            "task_id": task_id,
            "task_name": task.name,
            "updates": updates,
            "total_count": len(updates),
        }

    except OrbitError as e:
        return e.to_dict()
    except Exception as e:
        logger.exception("Error getting task updates")
        return {"error": True, "message": str(e)}

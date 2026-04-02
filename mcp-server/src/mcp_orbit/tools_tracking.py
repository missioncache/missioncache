"""Time tracking and repository management MCP tools."""

import logging
from typing import Annotated

from pydantic import Field

from .app import mcp
from .db import get_db, repo_to_dict
from .errors import OrbitError, TaskNotFoundError
from .helpers import _validate_path
from .models import HeartbeatResult, ProcessHeartbeatsResult

logger = logging.getLogger(__name__)


# =============================================================================
# TIME TRACKING
# =============================================================================


@mcp.tool()
async def record_heartbeat(
    task_id: Annotated[int | None, Field(description="Task ID (if known)")] = None,
    directory: Annotated[
        str | None, Field(description="Directory to auto-detect task from")
    ] = None,
    session_id: Annotated[str | None, Field(description="Claude session ID")] = None,
    context: Annotated[dict | None, Field(description="Optional context data")] = None,
) -> dict:
    """
    Record a heartbeat for time tracking.

    Provide either task_id OR directory for auto-detection.
    This is called automatically by hooks, but can be called manually.
    """
    db = get_db()

    try:
        if task_id:
            task = db.get_task(task_id)
            if not task:
                raise TaskNotFoundError(task_id)
            hb_id = db.record_heartbeat(task_id, session_id, context)
        elif directory:
            _validate_path(directory, "directory")
            hb_id = db.record_heartbeat_auto(directory, session_id, context)
            if not hb_id:
                return {
                    "recorded": False,
                    "message": "No active task found for directory",
                }
            task = db.find_task_for_cwd(directory, session_id)
        else:
            return {
                "error": True,
                "code": "VALIDATION_ERROR",
                "message": "Provide task_id or directory",
            }

        return HeartbeatResult(
            heartbeat_id=hb_id,
            task_id=task.id if task else 0,
            task_name=task.name if task else "",
        ).model_dump()

    except OrbitError as e:
        return e.to_dict()
    except Exception as e:
        logger.exception("Error recording heartbeat")
        return {"error": True, "message": str(e)}


@mcp.tool()
async def process_heartbeats() -> dict:
    """
    Aggregate unprocessed heartbeats into sessions.

    Call this periodically to update time tracking.
    Normally called automatically by the update hook.
    """
    db = get_db()

    try:
        count = db.process_heartbeats()

        return ProcessHeartbeatsResult(
            processed_count=count,
        ).model_dump()

    except Exception as e:
        logger.exception("Error processing heartbeats")
        return {"error": True, "message": str(e)}


@mcp.tool()
async def get_task_time(
    task_id: Annotated[int, Field(description="Task ID")],
    period: Annotated[
        str, Field(description="Period: 'all', 'today', or 'week'")
    ] = "all",
) -> dict:
    """
    Get time spent on a task.

    Returns total time and formatted string.
    """
    db = get_db()

    try:
        task = db.get_task(task_id)
        if not task:
            raise TaskNotFoundError(task_id)

        seconds = db.get_task_time(task_id, period)
        sessions = db.get_task_session_count(task_id)

        return {
            "task_id": task_id,
            "task_name": task.name,
            "period": period,
            "total_seconds": seconds,
            "formatted": db.format_duration(seconds),
            "session_count": sessions,
        }

    except OrbitError as e:
        return e.to_dict()
    except Exception as e:
        logger.exception("Error getting task time")
        return {"error": True, "message": str(e)}


# =============================================================================
# REPOSITORY MANAGEMENT
# =============================================================================


@mcp.tool()
async def list_repos(
    active_only: Annotated[bool, Field(description="Only show active repos")] = True,
) -> dict:
    """List tracked repositories."""
    db = get_db()

    try:
        repos = db.get_repos(active_only=active_only)

        return {
            "repos": [repo_to_dict(r) for r in repos],
            "total_count": len(repos),
        }

    except Exception as e:
        logger.exception("Error listing repos")
        return {"error": True, "message": str(e)}


@mcp.tool()
async def add_repo(
    path: Annotated[str, Field(description="Repository path")],
    short_name: Annotated[
        str | None, Field(description="Short name for display")
    ] = None,
) -> dict:
    """Add a repository to track."""
    db = get_db()

    try:
        _validate_path(path, "path")
        repo_id = db.add_repo(path, short_name)
        repo = db.get_repo(repo_id)

        return {
            "repo_id": repo_id,
            "path": repo.path if repo else path,
            "short_name": repo.short_name if repo else short_name,
        }

    except OrbitError as e:
        return e.to_dict()
    except Exception as e:
        logger.exception("Error adding repo")
        return {"error": True, "message": str(e)}


@mcp.tool()
async def scan_repos(
    repo_id: Annotated[
        int | None, Field(description="Specific repo ID to scan (or all if None)")
    ] = None,
) -> dict:
    """
    Scan repositories for orbit tasks and sync with database.

    Discovers tasks from ~/.claude/orbit/ directories.
    """
    db = get_db()

    try:
        if repo_id:
            tasks = db.scan_repo(repo_id)
        else:
            tasks = db.scan_all_repos()

        return {
            "scanned_count": len(tasks),
            "tasks": [
                {"id": t.id, "name": t.name, "repo_id": t.repo_id} for t in tasks
            ],
        }

    except Exception as e:
        logger.exception("Error scanning repos")
        return {"error": True, "message": str(e)}

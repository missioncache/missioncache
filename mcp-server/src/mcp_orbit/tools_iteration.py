"""Iteration log integration MCP tools - iteration logging and status tracking."""

import logging
from typing import Annotated

from pydantic import Field

from . import iteration_log
from .app import mcp
from .db import get_db
from .errors import OrbitError
from .helpers import _resolve_task_dir

logger = logging.getLogger(__name__)


@mcp.tool()
async def log_iteration(
    task_id: Annotated[int | None, Field(description="Task ID")] = None,
    project_name: Annotated[
        str | None, Field(description="Project name (if task_id not known)")
    ] = None,
    iteration: Annotated[int, Field(description="Iteration number")] = 1,
    status: Annotated[
        str, Field(description="Status: SUCCESS, FAILED, or BLOCKED")
    ] = "SUCCESS",
    task_title: Annotated[
        str | None, Field(description="Title of the task being worked on")
    ] = None,
    what_done: Annotated[
        list[str] | None, Field(description="What was done/attempted")
    ] = None,
    files_modified: Annotated[
        list[str] | None, Field(description="Files modified")
    ] = None,
    validation: Annotated[
        dict | None, Field(description="Validation results {test: PASS, etc.}")
    ] = None,
    error_details: Annotated[
        str | None, Field(description="Error details if FAILED")
    ] = None,
    next_steps: Annotated[
        list[str] | None, Field(description="Suggested next steps if FAILED")
    ] = None,
) -> dict:
    """
    Log an iteration to the iteration log file.

    Used by the iteration loop for tracking progress and debugging.
    """
    db = get_db()

    try:
        task_dir, resolved_name = _resolve_task_dir(db, task_id, project_name)

        entry = iteration_log.log_iteration(
            task_dir=task_dir,
            task_name=resolved_name,
            iteration=iteration,
            status=status,
            task_title=task_title,
            what_done=what_done,
            files_modified=files_modified,
            validation=validation,
            error_details=error_details,
            next_steps=next_steps,
        )

        return {
            "success": True,
            "task_name": resolved_name,
            "iteration": iteration,
            "status": status,
            "log_file": str(iteration_log.get_iteration_log_path(task_dir, resolved_name)),
        }

    except OrbitError as e:
        return e.to_dict()
    except Exception as e:
        logger.exception("Error logging iteration")
        return {"error": True, "message": str(e)}


@mcp.tool()
async def log_iteration_completion(
    task_id: Annotated[int | None, Field(description="Task ID")] = None,
    project_name: Annotated[str | None, Field(description="Project name")] = None,
    total_iterations: Annotated[
        int, Field(description="Total iterations completed")
    ] = 1,
    duration_seconds: Annotated[
        int, Field(description="Total duration in seconds")
    ] = 0,
    timed_out: Annotated[bool, Field(description="Whether loop timed out")] = False,
) -> dict:
    """
    Log completion or timeout to the iteration log.
    """
    db = get_db()

    try:
        task_dir, resolved_name = _resolve_task_dir(db, task_id, project_name)

        if timed_out:
            entry = iteration_log.log_timeout(
                task_dir=task_dir,
                task_name=resolved_name,
                max_iterations=total_iterations,
                duration_seconds=duration_seconds,
            )
        else:
            entry = iteration_log.log_completion(
                task_dir=task_dir,
                task_name=resolved_name,
                total_iterations=total_iterations,
                duration_seconds=duration_seconds,
            )

        return {
            "success": True,
            "task_name": resolved_name,
            "completed": not timed_out,
            "timed_out": timed_out,
            "total_iterations": total_iterations,
            "duration_seconds": duration_seconds,
        }

    except OrbitError as e:
        return e.to_dict()
    except Exception as e:
        logger.exception("Error logging iteration completion")
        return {"error": True, "message": str(e)}


@mcp.tool()
async def get_iteration_status(
    task_id: Annotated[int | None, Field(description="Task ID")] = None,
    project_name: Annotated[str | None, Field(description="Project name")] = None,
) -> dict:
    """
    Get the current iteration loop status for a task.

    Returns iteration count, last status, completion/timeout state,
    and prompt status if optimized prompts exist.
    """
    db = get_db()

    try:
        task_dir, resolved_name = _resolve_task_dir(db, task_id, project_name)

        # Get iteration log status
        status = iteration_log.get_iteration_status(task_dir, resolved_name)

        # Get prompts status
        prompts = iteration_log.get_prompts_status(task_dir)

        return {
            "task_name": resolved_name,
            "iteration_log": status,
            "prompts": prompts,
        }

    except OrbitError as e:
        return e.to_dict()
    except Exception as e:
        logger.exception("Error getting iteration status")
        return {"error": True, "message": str(e)}

"""Orbit file operation MCP tools - create, get, update orbit files."""

import logging
from pathlib import Path
from typing import Annotated

from pydantic import Field

from . import orbit
from .app import mcp
from .config import settings
from .db import get_db
from .errors import OrbitError, OrbitFileNotFoundError, TaskNotFoundError
from .helpers import _validate_path

logger = logging.getLogger(__name__)


@mcp.tool()
async def create_orbit_files(
    repo_path: Annotated[str, Field(description="Repository path")],
    project_name: Annotated[str, Field(description="Project name (kebab-case)")],
    description: Annotated[
        str, Field(description="Short description (max 12 words)")
    ] = "TBD",
    jira_key: Annotated[str | None, Field(description="JIRA ticket ID")] = None,
    branch: Annotated[str | None, Field(description="Git branch name")] = None,
    tasks: Annotated[
        list[str] | None, Field(description="List of task descriptions")
    ] = None,
    plan: Annotated[
        dict | None, Field(description="Plan content: {summary, goals, approach, etc.}")
    ] = None,
) -> dict:
    """
    Create orbit files for a new task.

    Creates files under ~/.claude/orbit/active/<task-name>/.
    The repo_path is used to register the repository in the DB.

    Returns paths to all created files.
    """
    db = get_db()

    try:
        _validate_path(repo_path, "repo_path")

        # Ensure repo is registered
        repo = db.get_repo_by_path(repo_path)
        if not repo:
            repo_id = db.add_repo(repo_path)
        else:
            repo_id = repo.id

        # Create the files under ORBIT_ROOT
        files = orbit.create_orbit_files(
            task_name=project_name,
            description=description,
            jira_key=jira_key,
            branch=branch,
            tasks=tasks,
            plan_content=plan,
        )

        # Scan to register task in database
        db.scan_all_repos()

        # Find the created task by its known full_path (avoids name-only ambiguity)
        task = db.find_task_by_full_path(f"active/{project_name}")
        if not task:
            task = db.get_task_by_name(project_name)
        if task and task.repo_id != repo_id:
            db.update_task_repo(task.id, repo_id)

        return {
            "success": True,
            "task_id": task.id if task else None,
            "task_name": project_name,
            "files": files.model_dump(),
        }

    except OrbitError as e:
        return e.to_dict()
    except Exception as e:
        logger.exception("Error creating orbit files")
        return {"error": True, "message": str(e)}


@mcp.tool()
async def get_orbit_files(
    task_id: Annotated[int | None, Field(description="Task ID")] = None,
    project_name: Annotated[str | None, Field(description="Project name")] = None,
) -> dict:
    """
    Get paths to orbit files for a task.

    Returns existing file paths (plan.md, context.md, tasks.md, prompts/).
    Files are resolved under ~/.claude/orbit/.
    """
    db = get_db()

    try:
        task = None

        if task_id:
            task = db.get_task(task_id)
            if not task:
                raise TaskNotFoundError(task_id)
        elif project_name:
            task = db.get_task_by_name(project_name)

        if not task and not project_name:
            return {
                "error": True,
                "code": "VALIDATION_ERROR",
                "message": "Provide task_id or project_name",
            }

        name = task.name if task else project_name
        # Pass full_path for subtasks (nested under parent directories)
        full_path = task.full_path if task else None
        files = orbit.get_orbit_files(name, full_path=full_path)

        return {
            "task_id": task.id if task else None,
            "task_name": name,
            "files": files.model_dump(),
        }

    except OrbitError as e:
        return e.to_dict()
    except Exception as e:
        logger.exception("Error getting orbit files")
        return {"error": True, "message": str(e)}


@mcp.tool()
async def update_context_file(
    context_file: Annotated[str, Field(description="Path to context.md file")],
    next_steps: Annotated[
        list[str] | None, Field(description="Next steps to add/replace")
    ] = None,
    recent_changes: Annotated[
        list[str] | None, Field(description="Recent changes to add")
    ] = None,
    key_decisions: Annotated[
        list[str] | None, Field(description="Key decisions to add")
    ] = None,
    gotchas: Annotated[list[str] | None, Field(description="Gotchas to add")] = None,
    key_files: Annotated[
        dict[str, str] | None,
        Field(description="Key files to add: {path: description}"),
    ] = None,
) -> dict:
    """
    Update a context.md file atomically.

    Updates timestamp and specified sections. Much faster than multiple
    Read/Edit calls.
    """
    try:
        _validate_path(context_file, "context_file", must_be_under=settings.orbit_root)
        content = orbit.update_context_file(
            context_file=context_file,
            next_steps=next_steps,
            recent_changes=recent_changes,
            key_decisions=key_decisions,
            gotchas=gotchas,
            key_files=key_files,
        )

        return {
            "success": True,
            "file": context_file,
            "timestamp": orbit.get_timestamp(),
            "sections_updated": [
                s
                for s, v in [
                    ("next_steps", next_steps),
                    ("recent_changes", recent_changes),
                    ("key_decisions", key_decisions),
                    ("gotchas", gotchas),
                    ("key_files", key_files),
                ]
                if v
            ],
        }

    except OrbitError as e:
        return e.to_dict()
    except Exception as e:
        logger.exception("Error updating context file")
        return {"error": True, "message": str(e)}


@mcp.tool()
async def update_tasks_file(
    tasks_file: Annotated[str, Field(description="Path to tasks.md file")],
    completed_tasks: Annotated[
        list[str] | None, Field(description="Task descriptions to mark as [x]")
    ] = None,
    new_tasks: Annotated[
        list[str] | None, Field(description="New tasks to add")
    ] = None,
    remaining_summary: Annotated[
        str | None, Field(description="New Remaining summary (max 15 words)")
    ] = None,
    notes: Annotated[list[str] | None, Field(description="Notes to add")] = None,
) -> dict:
    """
    Update a tasks.md file.

    Marks tasks as completed, adds new tasks, updates Remaining summary.
    Returns progress info.
    """
    try:
        _validate_path(tasks_file, "tasks_file", must_be_under=settings.orbit_root)
        result = orbit.update_tasks_file(
            tasks_file=tasks_file,
            completed_tasks=completed_tasks,
            new_tasks=new_tasks,
            remaining_summary=remaining_summary,
            notes=notes,
        )

        return {
            "success": True,
            **result,
        }

    except OrbitError as e:
        return e.to_dict()
    except Exception as e:
        logger.exception("Error updating tasks file")
        return {"error": True, "message": str(e)}


@mcp.tool()
async def get_orbit_progress(
    task_id: Annotated[int | None, Field(description="Task ID")] = None,
    tasks_file: Annotated[
        str | None, Field(description="Direct path to tasks.md")
    ] = None,
) -> dict:
    """
    Get progress info from a tasks.md file.

    Returns completion percentage, completed/total items, and remaining summary.
    """
    db = get_db()

    try:
        file_path = tasks_file

        if task_id and not file_path:
            task = db.get_task(task_id)
            if not task:
                raise TaskNotFoundError(task_id)
            files = orbit.get_orbit_files(task.name, full_path=task.full_path)
            file_path = files.tasks_file

        if not file_path:
            return {
                "error": True,
                "code": "VALIDATION_ERROR",
                "message": "Provide task_id or tasks_file",
            }

        if tasks_file:
            _validate_path(file_path, "tasks_file", must_be_under=settings.orbit_root)

        path = Path(file_path)
        if not path.exists():
            raise OrbitFileNotFoundError(file_path)

        content = path.read_text()
        progress = orbit.parse_task_progress(content)

        return {
            "task_id": task_id,
            "file": file_path,
            "progress": progress.model_dump(),
        }

    except OrbitError as e:
        return e.to_dict()
    except Exception as e:
        logger.exception("Error getting progress")
        return {"error": True, "message": str(e)}

"""MissionCache file operation MCP tools - create, get, update MissionCache files."""

import logging
from pathlib import Path
from typing import Annotated

from pydantic import Field

from missioncache_db import CATEGORIES

from . import active_task, project_files
from .app import mcp
from .config import settings
from .db import get_db
from .errors import MissionCacheError, MissionCacheFileNotFoundError, TaskNotFoundError
from .helpers import (
    _bind_session_to_project,
    _notify_dashboard_task_created,
    _resolve_session_id,
    _resolve_to_git_root,
    _validate_path,
)

logger = logging.getLogger(__name__)


@mcp.tool()
async def create_missioncache_files(
    repo_path: Annotated[str, Field(description="Repository path")],
    project_name: Annotated[str, Field(description="Project name (kebab-case)")],
    description: Annotated[
        str, Field(description="Short description (max 12 words)")
    ] = "TBD",
    jira_key: Annotated[str | None, Field(description="JIRA ticket ID")] = None,
    branch: Annotated[str | None, Field(description="Git branch name")] = None,
    category: Annotated[
        str | None,
        Field(
            description="Project category, derived from the project "
            "description at creation time. One of: " + ", ".join(CATEGORIES)
            + ", or an existing custom category"
        ),
    ] = None,
    tasks: Annotated[
        list[str] | None, Field(description="List of task descriptions")
    ] = None,
    plan: Annotated[
        dict | None, Field(description="Plan content: {summary, goals, approach, etc.}")
    ] = None,
    force: Annotated[
        bool,
        Field(
            description="Overwrite existing MissionCache files. Default False raises "
            "ALREADY_EXISTS to prevent silent data loss."
        ),
    ] = False,
    resolve_git_root: Annotated[
        bool,
        Field(
            description="Walk parents of repo_path to the containing git root "
            "before registering. Default True so any cwd inside a repo lands "
            "at the same registered path. Pass False when a sub-package within "
            "a monorepo is the actual project boundary."
        ),
    ] = True,
    session_id: Annotated[
        str | None,
        Field(
            description="Claude Code session ID (UUID). Binds this session to "
            "the new project so the statusline picks it up immediately. On "
            "Claude Code 2.1.154+ this is resolved automatically from the "
            "CLAUDE_CODE_SESSION_ID this MCP subprocess was spawned with, so "
            "you can omit it. Pass explicitly for older Claude Code or "
            "non-Claude clients; if it cannot be resolved, binding is skipped "
            "(the user can recover via /missioncache:load)."
        ),
    ] = None,
) -> dict:
    """
    Create MissionCache files for a new task.

    Creates files under ~/.missioncache/active/<task-name>/.
    The repo_path is used to register the repository in the DB. By default,
    repo_path is resolved to its containing git root before registration so
    /missioncache:new captures the same path regardless of which subdirectory the
    user invoked it from. Pass resolve_git_root=False to opt out (e.g., when
    each sub-package in a monorepo is its own MissionCache project).

    When ``session_id`` is provided, also writes the project_state row +
    per-session pointer that the statusline reads, atomically with task
    creation. Eliminates the prior failure mode where a separate
    client-side bash binding step could be silently skipped, leaving the
    statusline blank until /missioncache:load was re-run.

    Returns ALREADY_EXISTS error if any of plan/context/tasks already exist
    for this name. Pass force=True to overwrite (destructive - the caller is
    expected to have confirmed with the user).

    Returns paths to all created files plus a ``session_bound`` flag
    indicating whether the statusline binding was written.
    """
    db = get_db()

    try:
        if (
            category is not None
            and category not in CATEGORIES
            and category not in db.custom_category_names()
        ):
            return {
                "error": True,
                "code": "VALIDATION_ERROR",
                "message": f"category must be one of: {', '.join(CATEGORIES)}, "
                "or an existing custom category",
            }

        # Validate the raw input first; otherwise an empty string passed
        # with resolve_git_root=True would silently resolve to the MCP
        # server's cwd via Path("").resolve() and bypass the empty-string
        # / null-byte guards in _validate_path.
        _validate_path(repo_path, "repo_path")
        registered_repo_path = (
            _resolve_to_git_root(repo_path) if resolve_git_root else repo_path
        )

        # Ensure repo is registered
        repo = db.get_repo_by_path(registered_repo_path)
        if not repo:
            repo_id = db.add_repo(registered_repo_path)
        else:
            repo_id = repo.id

        # Create the files under MISSIONCACHE_ROOT
        files = project_files.create_missioncache_files(
            task_name=project_name,
            description=description,
            jira_key=jira_key,
            branch=branch,
            tasks=tasks,
            plan_content=plan,
            force=force,
        )

        # Scan to register task in database
        db.scan_all_repos()

        # Find the created task by its known full_path (avoids name-only ambiguity)
        task = db.find_task_by_full_path(f"active/{project_name}")
        if not task:
            task = db.get_task_by_name(project_name)
        if task and task.repo_id != repo_id:
            db.update_task_repo(task.id, repo_id)

        # This creation path registers the task via scan_all_repos (not
        # create_task), so the category is set on the scanned row after
        # the fact. By this point files and the DB row already exist, so a
        # failure here must NOT fail the whole call - the client would retry
        # into ALREADY_EXISTS. Degrade to uncategorized (recoverable via
        # set-category) and log.
        if task and category is not None:
            try:
                task = db.set_task_category(task.id, category)
            except Exception:
                logger.warning(
                    "Project %s created but category %r could not be set; "
                    "it is uncategorized until set via set-category",
                    project_name,
                    category,
                    exc_info=True,
                )

        await _notify_dashboard_task_created()

        # Bind the current session to the new project so the statusline
        # picks it up immediately, atomically with task creation. None or
        # an invalid session_id silently no-ops; the user can recover by
        # running /missioncache:load. Falls back to the CLAUDE_CODE_SESSION_ID env
        # var so the binding works without the caller resolving the id.
        session_id = _resolve_session_id(session_id)
        session_bound = _bind_session_to_project(session_id, project_name)

        return {
            "success": True,
            "task_id": task.id if task else None,
            "task_name": project_name,
            # Echo what was actually STORED: None when the scan found no row
            # (nothing was persisted) or when the category set degraded.
            "category": task.category if task else None,
            "files": files.model_dump(),
            "repo_path": registered_repo_path,
            "session_bound": session_bound,
        }

    except MissionCacheError as e:
        return e.to_dict()
    except Exception as e:
        logger.exception("Error creating MissionCache files")
        return {"error": True, "message": str(e)}


@mcp.tool()
async def get_missioncache_files(
    task_id: Annotated[int | None, Field(description="Task ID")] = None,
    project_name: Annotated[str | None, Field(description="Project name")] = None,
) -> dict:
    """
    Get paths to MissionCache files for a task.

    Returns existing file paths (plan.md, context.md, tasks.md, prompts/).
    Files are resolved under ~/.missioncache/.
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
        # Pass full_path only for subtasks (nested under parent directories).
        # For top-level tasks, full_path can be stale because complete_task
        # moves the directory to completed/<name> without updating the column.
        # Letting get_missioncache_files do its standard active+completed search
        # avoids returning null files for archived projects.
        full_path = (
            task.full_path if (task and task.parent_id is not None) else None
        )
        files = project_files.get_missioncache_files(name, full_path=full_path)

        return {
            "task_id": task.id if task else None,
            "task_name": name,
            "files": files.model_dump(),
        }

    except MissionCacheError as e:
        return e.to_dict()
    except Exception as e:
        logger.exception("Error getting MissionCache files")
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
        _validate_path(context_file, "context_file", must_be_under=settings.root)
        content = project_files.update_context_file(
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
            "timestamp": project_files.get_timestamp(),
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

    except MissionCacheError as e:
        return e.to_dict()
    except Exception as e:
        logger.exception("Error updating context file")
        return {"error": True, "message": str(e)}


@mcp.tool()
async def update_tasks_file(
    tasks_file: Annotated[str, Field(description="Path to tasks.md file")],
    completed_tasks: Annotated[
        list[str] | None,
        Field(
            description=(
                "Tasks to mark as [x]. Lead each entry with the checklist "
                "number for a reliable match (e.g. '7' or '7. Implement X'); "
                "trailing prose is ignored. Entries matching no item come "
                "back in the 'unmatched' field."
            )
        ),
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
        _validate_path(tasks_file, "tasks_file", must_be_under=settings.root)
        result = project_files.update_tasks_file(
            tasks_file=tasks_file,
            completed_tasks=completed_tasks,
            new_tasks=new_tasks,
            remaining_summary=remaining_summary,
            notes=notes,
        )

        # Auto-clear active-task pointers for any items just transitioned
        # to [x]. Without this, the statusline keeps rendering Task: <foo>
        # after the user (or Claude via update_tasks_file) finished it.
        # Project name is the prefix of <name>-tasks.md; legacy unprefixed
        # tasks.md files yield None and skip the sweep (the parent-dir-name
        # fallback would be unsafe for renamed projects).
        completed_numbers = result.get("completed_numbers") or []
        tasks_path_name = Path(tasks_file).name
        project_name = (
            tasks_path_name[: -len("-tasks.md")]
            if tasks_path_name.endswith("-tasks.md")
            and tasks_path_name != "-tasks.md"
            else None
        )
        cleared_sessions: list[str] = []
        if project_name and completed_numbers:
            cleared_sessions = active_task.remove_task_numbers_everywhere(
                project_name, completed_numbers
            )

        return {
            "success": True,
            **result,
            "active_pointers_cleared_for_sessions": cleared_sessions,
        }

    except MissionCacheError as e:
        return e.to_dict()
    except Exception as e:
        logger.exception("Error updating tasks file")
        return {"error": True, "message": str(e)}


@mcp.tool()
async def get_missioncache_progress(
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
            files = project_files.get_missioncache_files(task.name, full_path=task.full_path)
            file_path = files.tasks_file

        if not file_path:
            return {
                "error": True,
                "code": "VALIDATION_ERROR",
                "message": "Provide task_id or tasks_file",
            }

        if tasks_file:
            _validate_path(file_path, "tasks_file", must_be_under=settings.root)

        path = Path(file_path)
        if not path.exists():
            raise MissionCacheFileNotFoundError(file_path)

        content = path.read_text()
        progress = project_files.parse_task_progress(content)

        return {
            "task_id": task_id,
            "file": file_path,
            "progress": progress.model_dump(),
        }

    except MissionCacheError as e:
        return e.to_dict()
    except Exception as e:
        logger.exception("Error getting progress")
        return {"error": True, "message": str(e)}

"""Task lifecycle MCP tools - listing, retrieval, CRUD, non-coding updates."""

import logging
from typing import Annotated

from pydantic import Field

from missioncache_db import (
    CATEGORIES,
    AutoRunActiveError,
    FilesystemCollisionError,
    NameCollisionError,
)

from . import project_files
from .app import mcp
from .config import settings
from .db import get_db
from .errors import (
    ErrorCode,
    InvalidStateError,
    MissionCacheError,
    TaskNotFoundError,
    ValidationError,
)
from .helpers import (
    SESSION_ID_RESOLVE_HINT,
    _bind_session_to_project,
    _notify_dashboard_task_created,
    _resolve_session_id,
    _resolve_to_git_root,
    _task_to_detail,
    _task_to_summary,
    _validate_path,
)
from .models import (
    CreateTaskResult,
    ListTasksResult,
    RenameTaskResult,
    UpdateTaskResult,
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


def _format_tasks_table(tasks: list) -> str:
    """Format a list of TaskSummary as a plain-text whitespace-aligned table.

    Designed to render cleanly across all MCP clients, including TUIs that
    don't render markdown (Codex). Falls back to a single-line message when
    there are no tasks.
    """
    if not tasks:
        return "(no active tasks)"

    headers = ("ID", "Task", "Repo", "Time", "Last worked")
    rows = [
        (
            str(t.id),
            t.name,
            t.repo_name or "-",
            t.time_formatted or "-",
            t.last_worked_ago or "-",
        )
        for t in tasks
    ]
    widths = [
        max(len(headers[i]), *(len(row[i]) for row in rows))
        for i in range(len(headers))
    ]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    sep = "  ".join("-" * w for w in widths)
    return "\n".join(
        [fmt.format(*headers).rstrip(), sep]
        + [fmt.format(*row).rstrip() for row in rows]
    )


def _format_prioritized_display(
    repo_summaries: list,
    other_summaries: list,
    repo_path: str,
) -> str:
    """Format the prioritize_by_repo two-list display.

    Renders both `tasks` (cwd repo) and `other_tasks` (everything else) so the
    output is useful even when the primary list is empty. Common case the
    user's cwd repo has no tasks attached directly (cwd is a top-level work
    dir; actual projects live in subdirs) - we still want to show the other
    tasks instead of collapsing to "(no active tasks)".
    """
    if not repo_summaries and not other_summaries:
        return "(no active tasks)"
    sections: list[str] = []
    if repo_summaries:
        sections.append(f"Tasks in {repo_path}:")
        sections.append(_format_tasks_table(repo_summaries))
    else:
        sections.append(f"(no active tasks in {repo_path})")
    if other_summaries:
        if repo_summaries:
            sections.append("")
        sections.append("Other active tasks:")
        sections.append(_format_tasks_table(other_summaries))
    return "\n".join(sections)


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

    repo_path is looked up as given first, then falls back to its containing
    git root, so a cwd inside a repo matches the repo registered at its root.
    If repo_path resolves to no registered repo, filter_applied is tagged
    "(not registered)" and the repo-scoped list is empty rather than
    silently returning every project as if it were repo-scoped.

    Display: the response includes a `display` field with a pre-rendered
    plain-text table of `tasks`. When showing this output to a user in a
    chat UI, render the `display` string verbatim - it reads cleanly in
    TUIs that don't render markdown (e.g., Codex). Use the structured
    `tasks` array for follow-up logic (filtering, picking by id, etc.).
    """
    db = get_db()

    try:
        repo_id = None
        if repo_path:
            # Try the path as given (covers a monorepo sub-package
            # registered with resolve_git_root=False), then its git root
            # (covers a cwd that is a subdir of a normally-registered repo).
            repo = db.get_repo_by_path(repo_path) or db.get_repo_by_path(
                _resolve_to_git_root(repo_path)
            )
            if repo:
                repo_id = repo.id

        if prioritize_by_repo and repo_path:
            # Two-tier: fetch all tasks, split by repo membership. When
            # repo_path resolves to no registered repo (repo_id is None),
            # nothing is "in" the repo, so every task falls to other_tasks -
            # the intended prioritization for a cwd with no tracked projects.
            all_tasks = db.get_active_tasks(None)

            if task_type:
                all_tasks = [t for t in all_tasks if t.task_type == task_type]

            repo_tasks = [
                t
                for t in all_tasks
                if repo_id is not None and t.repo_id == repo_id
            ]
            other_tasks = [
                t
                for t in all_tasks
                if not (repo_id is not None and t.repo_id == repo_id)
            ]

            repo_summaries = _build_summaries(repo_tasks, db, include_time)
            other_summaries = _build_summaries(other_tasks, db, include_time)

            filter_applied = f"prioritized repo={repo_path}"
            if repo_id is None:
                filter_applied += " (not registered)"

            return ListTasksResult(
                tasks=repo_summaries,
                total_count=len(repo_summaries) + len(other_summaries),
                filter_applied=filter_applied,
                other_tasks=other_summaries if other_summaries else None,
                display=_format_prioritized_display(
                    repo_summaries, other_summaries, repo_path or ""
                ),
            ).model_dump()
        else:
            # Filter by repo or return all. A repo_path that resolved to no
            # registered repo yields an empty repo-scoped list, not every
            # project - the filter_applied says so instead of claiming a
            # filter that was never applied.
            if repo_path and repo_id is None:
                tasks = []
            else:
                tasks = db.get_active_tasks(repo_id)

            if task_type:
                tasks = [t for t in tasks if t.task_type == task_type]

            summaries = _build_summaries(tasks, db, include_time)

            filter_desc = []
            if repo_path:
                if repo_id is None:
                    filter_desc.append(f"repo={repo_path} (not registered)")
                else:
                    filter_desc.append(f"repo={repo_path}")
            if task_type:
                filter_desc.append(f"type={task_type}")

            return ListTasksResult(
                tasks=summaries,
                total_count=len(summaries),
                filter_applied=", ".join(filter_desc) if filter_desc else None,
                display=_format_tasks_table(summaries),
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
    session_id: Annotated[
        str | None,
        Field(
            description=(
                "Claude Code session ID; binds this session to the project so "
                "the statusline shows it. " + SESSION_ID_RESOLVE_HINT
            )
        ),
    ] = None,
) -> dict:
    """
    Get full task details including progress, time, and prompt config.

    Provide either task_id OR project_name (not both).
    Returns all information needed for /continue-task in a single call.

    When a session id is available - passed explicitly, or resolved from
    the CLAUDE_CODE_SESSION_ID env var on Claude Code 2.1.154+ - also writes
    the project_state row + the ``~/.claude/hooks/state/projects/<sid>.json``
    pointer so the statusline reflects this project immediately, without
    needing the slash-command bash step to fire. Returns a ``session_bound``
    field indicating whether the binding succeeded (True) or failed (False);
    the field is omitted entirely when no session id could be resolved (no
    argument and no env var).
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
        result = detail.model_dump()

        # Atomic session binding when session_id is supplied. Best-effort
        # like the create_missioncache_files pattern: a DB-write failure or
        # invalid session_id silently no-ops; the user can recover by
        # re-running /missioncache:load or invoking the binding explicitly. Falls
        # back to the CLAUDE_CODE_SESSION_ID env var so /missioncache:load binds
        # without the caller resolving the id client-side.
        session_id = _resolve_session_id(session_id)
        if session_id:
            session_bound = _bind_session_to_project(session_id, task.name, task_id=task.id)
            result["session_bound"] = session_bound

        return result

    except MissionCacheError as e:
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

    Lookup priority (see missioncache_db.find_task_for_cwd):
    1. pending-project.json (cwd match)
    2. projects/<session_id>.json - requires session_id arg
    3. cwd under ~/.missioncache/active/<task>/

    Callers that invoke this from arbitrary cwds (e.g. the repo root) MUST
    pass session_id for priority 2 to fire. The 4 MissionCache slash commands all
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

    except MissionCacheError as e:
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
    category: Annotated[
        str | None,
        Field(
            description="Project category, derived from the project "
            "description at creation time. One of: " + ", ".join(CATEGORIES)
            + ", or an existing custom category"
        ),
    ] = None,
    session_id: Annotated[
        str | None,
        Field(
            description="Claude Code session ID; binds this session to the new "
            "task so the statusline shows it. " + SESSION_ID_RESOLVE_HINT
        ),
    ] = None,
) -> dict:
    """
    Create a new task in the database.

    For coding tasks, also creates the missioncache/active/<name>/ directory.
    For non-coding tasks, no directory is created.

    When ``session_id`` is provided, also writes the project_state row +
    per-session pointer that the statusline reads, atomically with task
    creation. Eliminates the prior failure mode where /missioncache:new's
    non-coding branch left the statusline blank because no binding step
    existed for it.
    """
    db = get_db()

    try:
        # Validate inputs
        project_files.validate_task_name(name)
        if task_type not in ("coding", "non-coding"):
            return {
                "error": True,
                "code": "VALIDATION_ERROR",
                "message": "type must be 'coding' or 'non-coding'",
            }
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

        repo_id = None
        missioncache_path = None

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

            # Create active/<name>/ directory under MISSIONCACHE_ROOT
            task_dir = settings.root / settings.active_dir_name / name
            task_dir.mkdir(parents=True, exist_ok=True)
            missioncache_path = str(task_dir)

        # Create task in DB
        task = db.create_task(
            name=name,
            task_type=task_type,
            repo_id=repo_id,
            jira_key=jira_key,
            category=category,
        )

        await _notify_dashboard_task_created()

        # Bind the current session to the new task so the statusline
        # picks it up immediately, atomically with task creation. None
        # or invalid session_id silently no-ops; the user can recover by
        # running /missioncache:load. Falls back to the CLAUDE_CODE_SESSION_ID env
        # var so the binding works without the caller resolving the id.
        session_id = _resolve_session_id(session_id)
        session_bound = _bind_session_to_project(session_id, name, task_id=task.id)

        result = CreateTaskResult(
            task_id=task.id,
            task_name=task.name,
            task_type=task.task_type,
            category=task.category,
            missioncache_path=missioncache_path,
        ).model_dump()
        result["session_bound"] = session_bound
        return result

    except MissionCacheError as e:
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
    move_files: Annotated[bool, Field(description="Move MissionCache files to completed/")] = True,
) -> dict:
    """
    Mark a task as completed.

    For coding tasks, optionally moves MissionCache files from active/ to completed/.
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

        # The composition (status flip, file move, fork advisory) lives in
        # missioncache_db.complete_project - the single source shared with the
        # dashboard's complete endpoint. This tool only resolves arguments
        # and maps error dicts onto the MCP error surface.
        result = db.complete_project(task.id, move_files=move_files)
        if result.get("error"):
            if result.get("code") == "INVALID_STATE":
                raise InvalidStateError(
                    "Task is already completed", current_state="completed"
                )
            raise TaskNotFoundError(task.id)
        return result

    except MissionCacheError as e:
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
        bool, Field(description="Move MissionCache files from completed/ to active/")
    ] = True,
) -> dict:
    """
    Reopen a completed task.

    For coding tasks, optionally moves MissionCache files from completed/ back to active/.
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

        # Composition delegated to missioncache_db.reopen_project (shared with
        # the dashboard's reopen endpoint), mirroring complete_task above.
        result = db.reopen_project(task.id, move_files=move_files)
        if result.get("error"):
            if result.get("code") == "INVALID_STATE":
                raise InvalidStateError(
                    "Task is not completed",
                    current_state=task.status,
                    expected_state="completed",
                )
            raise TaskNotFoundError(task.id)
        return result

    except MissionCacheError as e:
        return e.to_dict()
    except Exception as e:
        logger.exception("Error reopening task")
        return {"error": True, "message": str(e)}


@mcp.tool()
async def rename_task(
    new_name: Annotated[
        str, Field(description="New project name (kebab-case, e.g. 'my-project')")
    ],
    task_id: Annotated[int | None, Field(description="Task ID")] = None,
    project_name: Annotated[
        str | None, Field(description="Current project name (alternative to ID)")
    ] = None,
) -> dict:
    """
    Rename a project / task.

    Updates the DB row, moves the MissionCache directory, renames files inside,
    and rewrites template H1 titles. Time tracking, heartbeats, sessions,
    and JIRA links survive because they're keyed by task_id (integer FK),
    not by name.

    Inputs are normalized (trim + lowercase) before validation. The
    response always reports the canonical stored name in ``name`` -
    callers should display that, not the user-typed input. The
    ``normalized`` flag tells callers whether normalization changed the
    input so they can prefix their confirmation accordingly.

    Provide either task_id OR project_name to identify the project to
    rename.

    Refuses to rename when:
    - Another project in the same repo has the target name (ALREADY_EXISTS)
    - The target MissionCache directory already exists on disk (ALREADY_EXISTS)
    - A missioncache-auto run is in progress on the project (INVALID_STATE)
    - The task is a subtask (VALIDATION_ERROR; rename the parent instead)
    """
    db = get_db()

    try:
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

        try:
            result = db.rename_task(task.id, new_name)
        except NameCollisionError as e:
            raise MissionCacheError(ErrorCode.ALREADY_EXISTS, str(e))
        except FilesystemCollisionError as e:
            raise MissionCacheError(ErrorCode.ALREADY_EXISTS, str(e))
        except AutoRunActiveError as e:
            raise InvalidStateError(str(e), current_state="auto-running")
        except ValueError as e:
            # Validation, missing task, subtask refusal, unexpected
            # full_path - all surface as VALIDATION_ERROR with the
            # original message.
            raise ValidationError(str(e), field="new_name")

        return RenameTaskResult(
            success=result["success"],
            changed=result["changed"],
            task_id=task.id,
            name=result["name"],
            old_name=result["old_name"],
            normalized=result["normalized"],
            full_path=result["full_path"],
            files_renamed=result["files_renamed"],
            h1_rewritten=result["h1_rewritten"],
            h1_skipped=result["h1_skipped"],
            sessions_updated=result["sessions_updated"],
            warnings=result.get("warnings", []),
        ).model_dump()

    except MissionCacheError as e:
        return e.to_dict()
    except Exception as e:
        logger.exception("Error renaming task")
        return {"error": True, "message": str(e)}


@mcp.tool()
async def update_task(
    task_id: Annotated[int | None, Field(description="Task ID")] = None,
    project_name: Annotated[
        str | None, Field(description="Project name (alternative to ID)")
    ] = None,
    jira_key: Annotated[
        str | None,
        Field(
            description="JIRA ticket ID to set (e.g. 'PROJ-12345'). Pass the "
            "literal string 'none' to clear. Omit to leave unchanged."
        ),
    ] = None,
    category: Annotated[
        str | None,
        Field(
            description="Project category to set. One of: "
            + ", ".join(CATEGORIES)
            + ", or an existing custom category. Pass the literal string "
            "'none' to clear. Omit to leave unchanged."
        ),
    ] = None,
) -> dict:
    """
    Update a task's JIRA key and/or category after creation.

    Provide either task_id OR project_name to identify the task, and at
    least one of jira_key / category. The literal string 'none' (any
    case) clears a field, matching the CLI's set-category convention.
    An empty/whitespace jira_key is rejected (pass 'none' to clear).

    All input validation runs BEFORE any write, so invalid input never
    leaves a half-applied update (e.g. jira_key written, category
    rejected). The two writes themselves are separate transactions, not
    one atomic unit.
    """
    db = get_db()

    try:
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

        if jira_key is None and category is None:
            return {
                "error": True,
                "code": "VALIDATION_ERROR",
                "message": "Provide at least one of jira_key or category",
            }

        # 'none' sentinel (any case) clears the field. An empty string is
        # rejected rather than stored ("" would persist as a falsy ghost
        # value) or treated as clear (too easy to send by accident).
        if jira_key is not None and jira_key.strip() == "":
            return {
                "error": True,
                "code": "VALIDATION_ERROR",
                "message": "jira_key cannot be empty; pass 'none' to clear it",
            }
        new_jira = (
            None if jira_key is not None and jira_key.lower() == "none" else jira_key
        )
        new_category = (
            None if category is not None and category.lower() == "none" else category
        )

        if (
            new_category is not None
            and new_category not in CATEGORIES
            and new_category not in db.custom_category_names()
        ):
            return {
                "error": True,
                "code": "VALIDATION_ERROR",
                "message": f"category must be one of: {', '.join(CATEGORIES)}, "
                "or an existing custom category",
            }

        updated = []
        if jira_key is not None:
            task = db.set_task_jira(task.id, new_jira)
            updated.append("jira_key")
        if category is not None:
            task = db.set_task_category(task.id, new_category)
            updated.append("category")

        return UpdateTaskResult(
            task_id=task.id,
            task_name=task.name,
            jira_key=task.jira_key,
            category=task.category,
            updated=updated,
        ).model_dump()

    except MissionCacheError as e:
        return e.to_dict()
    except Exception as e:
        logger.exception("Error updating task")
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

    except MissionCacheError as e:
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

    except MissionCacheError as e:
        return e.to_dict()
    except Exception as e:
        logger.exception("Error getting task updates")
        return {"error": True, "message": str(e)}

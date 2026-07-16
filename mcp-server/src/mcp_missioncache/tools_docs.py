"""MissionCache file operation MCP tools - create, get, update MissionCache files."""

import logging
from datetime import datetime
from pathlib import Path
from typing import Annotated

from pydantic import Field

import missioncache_db
from missioncache_db import CATEGORIES, context_health

from . import active_task, project_files
from .app import mcp
from .config import settings
from .db import get_db
from .errors import MissionCacheError, MissionCacheFileNotFoundError, TaskNotFoundError
from .helpers import (
    SESSION_ID_RESOLVE_HINT,
    _bind_session_to_project,
    _get_bound_project,
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
            description="Claude Code session ID; binds this session to the new "
            "project so the statusline shows it. " + SESSION_ID_RESOLVE_HINT
        ),
    ] = None,
    fork_of: Annotated[
        str | None,
        Field(
            description="Parent project name to fork from. Writes a "
            "'**Fork of:**' header into the new context file; the parent's "
            "context file becomes this project's shared knowledge layer. "
            "The parent should already exist (active or completed)."
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
            fork_of=fork_of,
        )

        # Scan to register task in database. For forks, the scan's reconcile
        # pass also resolves the freshly written "**Fork of:**" header into
        # tasks.parent_id - no separate linking step needed.
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
        session_bound = _bind_session_to_project(
            session_id, project_name, task_id=task.id if task else None
        )

        result = {
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
        if fork_of is not None:
            # Linked means linked to the REQUESTED parent - a non-null
            # parent_id alone could be a stale link preserved by the scan
            # reconcile (e.g. force re-create pointing at an unresolvable
            # new parent while the old link survives).
            linked = False
            if task and task.parent_id is not None:
                try:
                    parent_task = db.get_task(task.parent_id)
                    linked = bool(parent_task and parent_task.name == fork_of)
                except Exception:
                    # A db error here means we can't verify the link, NOT that
                    # it is unlinked - log so a real db problem isn't hidden
                    # behind the benign-sounding "self-heals on next scan".
                    logger.warning(
                        "Fork link verification for %r -> %r failed; reporting "
                        "unlinked, but parent_id may be set",
                        project_name,
                        fork_of,
                        exc_info=True,
                    )
                    linked = False
            result["fork_of"] = fork_of
            result["fork_linked"] = linked
            if not linked:
                result["fork_warning"] = (
                    f"'**Fork of:** {fork_of}' was written to the context header, "
                    "but the database link does not (yet) point at that parent. "
                    "The link self-heals on the next scan once the parent "
                    "resolves unambiguously."
                )
        return result

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
    waiting_on_add: Annotated[
        list[dict[str, str]] | None,
        Field(
            description=(
                "Waiting-on rows to append, each "
                '{"what": ..., "who": ..., "since": "YYYY-MM-DD", "gates": ...}. '
                "'since' defaults to today. Creates the '## Waiting on' "
                "section before Next Steps if the file does not have one yet."
            )
        ),
    ] = None,
    waiting_on_resolve: Annotated[
        list[dict[str, str]] | None,
        Field(
            description=(
                'Waiting-on rows to resolve, each {"match": <substring of the '
                'What cell>, "outcome": <what happened>}. The first matching '
                "row is removed and the resolution is recorded in today's "
                "Recent Changes subsection. Entries matching no row are "
                "returned in 'waiting_on_unmatched' - check it, never assume "
                "a resolve landed."
            )
        ),
    ] = None,
) -> dict:
    """
    Update a context.md file atomically.

    Updates timestamp and specified sections. Much faster than multiple
    Read/Edit calls. Maintains the '## Waiting on' table via waiting_on_add /
    waiting_on_resolve, and enforces the Recent Changes cap (overflow rolls
    into the per-project journal file automatically).
    """
    try:
        _validate_path(context_file, "context_file", must_be_under=settings.root)
        result = project_files.update_context_file(
            context_file=context_file,
            next_steps=next_steps,
            recent_changes=recent_changes,
            key_decisions=key_decisions,
            gotchas=gotchas,
            key_files=key_files,
            waiting_on_add=waiting_on_add,
            waiting_on_resolve=waiting_on_resolve,
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
                    ("waiting_on", waiting_on_add or waiting_on_resolve),
                ]
                if v
            ],
            "waiting_on_unmatched": result["waiting_on_unmatched"],
            "journal_rolled_over": result["journal_rolled_over"],
        }

    except MissionCacheError as e:
        return e.to_dict()
    except Exception as e:
        logger.exception("Error updating context file")
        return {"error": True, "message": str(e)}


def _stamp_shared_seen_for_fork_reader(
    parent_name: str,
    parent_ctx_path: str,
    parent_mtime: float,
    session_id: str | None = None,
) -> bool:
    """Restamp the calling session's shared-seen marker when it is a fork of
    ``parent_name`` - reading the parent's digest IS consuming the shared
    layer, so the statusline's "parent updated" indicator clears immediately
    instead of waiting for the next /missioncache:load. When this stamp fires,
    the digest response carries ``shared_seen_stamped: true`` and the
    /missioncache:load fork flow skips its own bash stamp (which would
    otherwise overwrite this bytes-coupled value with an older mtime from an
    earlier response). Best-effort on every branch: no session id, no binding,
    non-fork caller, or an IO failure all mean "no stamp", never an error."""
    session_id = _resolve_session_id(session_id)
    if not session_id:
        return False
    bound = _get_bound_project(session_id)
    if not bound or bound == parent_name:
        return False
    try:
        bfiles = project_files.get_missioncache_files(bound)
        if not bfiles.context_file:
            return False
        # 8KB head read: the "**Fork of:**" header lives in the header region
        # (before the first "##") by the context-file convention, and the
        # statusline reads the same 8KB head (statusline.get_project_info) -
        # this size IS the de-facto contract for fork-header placement.
        with open(bfiles.context_file, "r", encoding="utf-8", errors="strict") as fh:
            head = fh.read(8192)
    except (OSError, MissionCacheError, ValueError):
        # UnicodeDecodeError is a ValueError subclass - covered.
        return False
    if context_health.parse_fork_parent(head) != parent_name:
        return False
    marker_dir = Path.home() / ".claude" / "hooks" / "state" / "shared-seen"
    try:
        marker_dir.mkdir(parents=True, exist_ok=True)
        missioncache_db.atomic_write_json(
            marker_dir / f"{session_id}.json",
            {
                "parent": parent_name,
                "parent_context_path": parent_ctx_path,
                "seen_mtime": parent_mtime,
                "seen_at": datetime.now().astimezone().isoformat(),
            },
        )
    except OSError:
        logger.warning(
            "shared-seen stamp for parent %r failed", parent_name, exc_info=True
        )
        return False
    return True


@mcp.tool()
async def get_context_digest(
    project_name: Annotated[str, Field(description="Project name")],
    seen_mtime: Annotated[
        float | None,
        Field(
            description="For forks: the parent-context mtime this session last "
            "saw (from its shared-seen marker). When passed, parent_digest."
            "changed_since_seen reports whether a parallel session updated the "
            "shared layer since."
        ),
    ] = None,
    session_id: Annotated[
        str | None,
        Field(
            description="Claude session id; lets a fork session's direct read "
            "of its parent's digest restamp the shared-seen marker. "
            + SESSION_ID_RESOLVE_HINT
        ),
    ] = None,
) -> dict:
    """
    Resume digest of a project's context file - read this INSTEAD of the
    full file on /missioncache:load.

    Parses server-side (no file-size limit; works on context files past the
    256KB Read-tool cap) and returns the resume-critical slices: Last
    Updated, Hub / Related-projects header lines, the Waiting on section
    verbatim, Next Steps verbatim, the newest 3 Recent Changes subsections,
    a section index (name + line number) for targeted follow-up reads, the
    file size, and per-project health warnings.

    For a FORK (context header carries "**Fork of:** <parent>"), also returns
    a ``parent_digest`` block: the parent's name, context file path, Last
    Updated, current mtime, and - when ``seen_mtime`` is passed -
    ``changed_since_seen``. The parent resolves from active/ or completed/,
    so a completed parent's shared layer stays reachable. Best-effort: an
    unresolvable parent yields ``parent_digest: null``, never an error.
    """
    try:
        # Fail fast on junk names and close the pre-validation existence
        # probe: get_missioncache_files joins the name onto settings.root and
        # calls .exists() before _validate_path runs.
        project_files.validate_task_name(project_name)
        files = project_files.get_missioncache_files(project_name)
        if not files.context_file:
            raise MissionCacheFileNotFoundError(
                f"context file for project '{project_name}'"
            )
        _validate_path(files.context_file, "context_file", must_be_under=settings.root)
        path = Path(files.context_file)
        # Couple the mtime to the bytes actually read (same dance as the
        # parent branch below) - the shared-seen stamp must baseline exactly
        # the snapshot the caller consumed, never a racing writer's newer one.
        own_mtime = path.stat().st_mtime
        content = path.read_text()
        if path.stat().st_mtime != own_mtime:
            own_mtime = path.stat().st_mtime
            content = path.read_text()
        digest = context_health.build_digest(content, path)

        parent_digest = None
        # is_fork lets the caller tell "not a fork" (parent_digest null, no
        # error) from "fork whose parent read failed" (parent_digest null,
        # fork_parent_error set) - the latter must still show the fork banner
        # and prompt a manual re-read, not silently downgrade to a plain resume.
        fork_name = context_health.parse_fork_parent(content)
        is_fork = bool(fork_name and fork_name != project_name)
        # A fork is never itself a parent (chains are one level deep), so only
        # a non-fork digest can be a shared-layer read. Gating here spares the
        # stamp's DB connection and bound-context read on every fork digest.
        shared_seen_stamped = False
        if not is_fork:
            shared_seen_stamped = _stamp_shared_seen_for_fork_reader(
                project_name, files.context_file, own_mtime, session_id
            )
        fork_parent_error = None
        if is_fork:
            try:
                pfiles = project_files.get_missioncache_files(fork_name)
                if pfiles.context_file:
                    _validate_path(
                        pfiles.context_file, "context_file", must_be_under=settings.root
                    )
                    ppath = Path(pfiles.context_file)
                    # Couple the reported mtime to the bytes actually read:
                    # stat, read, stat again; if an atomic writer replaced the
                    # file mid-read, re-read once so mtime and digest describe
                    # the same snapshot. (A content hash is the v2 contract.)
                    pmtime = ppath.stat().st_mtime
                    pcontent = ppath.read_text()
                    if ppath.stat().st_mtime != pmtime:
                        pmtime = ppath.stat().st_mtime
                        pcontent = ppath.read_text()
                    pdigest = context_health.build_digest(pcontent, ppath)
                    parent_digest = {
                        "name": fork_name,
                        "context_file": pfiles.context_file,
                        "last_updated": pdigest["last_updated"],
                        "context_mtime": pmtime,
                        "changed_since_seen": (
                            pmtime > seen_mtime
                            if seen_mtime is not None
                            else None
                        ),
                    }
                else:
                    fork_parent_error = f"parent project '{fork_name}' has no context file"
            # Narrow: a read/validation failure means "couldn't read the
            # parent", but a programming error (AttributeError/KeyError) or a
            # path-escape rejection should not be relabeled "didn't resolve".
            except (OSError, MissionCacheError, ValueError) as e:
                fork_parent_error = f"could not read parent '{fork_name}': {e}"
                logger.warning(
                    "Fork parent %r of %r did not resolve; parent_digest omitted",
                    fork_name,
                    project_name,
                    exc_info=True,
                )
        return {
            "success": True,
            "file": files.context_file,
            **digest,
            "is_fork": is_fork,
            "parent_digest": parent_digest,
            "fork_parent_error": fork_parent_error,
            # True when this read auto-restamped the calling fork session's
            # shared-seen marker (the caller is a fork of project_name).
            "shared_seen_stamped": shared_seen_stamped,
        }

    except MissionCacheError as e:
        return e.to_dict()
    except Exception as e:
        logger.exception("Error building context digest")
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
            # Pass full_path only for subtasks. For a top-level task the
            # column is stale after complete_task moves the dir to
            # completed/<name> without rewriting it, so trusting it would
            # search only the vanished active/<name> path. Letting
            # get_missioncache_files run its active+completed search finds
            # the tasks.md wherever the project now lives.
            full_path = task.full_path if task.parent_id is not None else None
            files = project_files.get_missioncache_files(task.name, full_path=full_path)
            file_path = files.tasks_file
            if not file_path:
                raise MissionCacheFileNotFoundError(
                    task.name, f"No tasks.md found for task '{task.name}'"
                )

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

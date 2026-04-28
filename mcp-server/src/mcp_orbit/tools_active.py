"""Active orbit-task pointer MCP tools.

Lets a caller (Claude in interactive use, or a script) declare which
orbit checklist task numbers are currently in progress. The statusline
reads the resulting per-session pointer to render its ``Task:`` field.

The pointer replaces the previous read of Claude Code's internal TodoList
(``~/.claude/tasks/<sid>/*.json``) which duplicated information Claude
already prints in chat. With these tools, the statusline shows orbit
checklist focus instead, generalizing to Codex and OpenCode for the
multi-tool story since they all consume the same MCP server.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

from pydantic import Field

from . import active_task, orbit
from .app import mcp
from .errors import ErrorCode, OrbitError, ValidationError
from .tasks_parse import find_item, parse_tasks_md

logger = logging.getLogger(__name__)


@mcp.tool()
async def set_active_orbit_tasks(
    project_name: Annotated[
        str, Field(description="Orbit project name (kebab-case)")
    ],
    task_numbers: Annotated[
        list[str],
        Field(
            description="Checklist task numbers to mark as in-progress, e.g. "
            "['54a'] or ['56', '57']. Numbers must match unchecked items in "
            "the project's tasks.md."
        ),
    ],
    session_id: Annotated[
        str,
        Field(
            description="Claude Code (or other tool) session ID. Pointer is "
            "scoped per session so concurrent sessions don't clobber each "
            "other's display."
        ),
    ],
) -> dict:
    """Set the active checklist tasks for this session.

    Replaces the prior pointer for the session (idempotent). Validates
    that each task number exists as an unchecked ``[ ]`` line in the
    project's tasks.md. Returns the validated set on success.

    Statusline display rules driven by the result:
      - 1 number: ``Task: 54a. <text>``
      - 2-3 numbers sharing a parent: parent line text + ``(N active)``
      - 2-3 numbers without shared parent: ``Tasks: 54a, 56, 57``
      - 4+ numbers: first 3 + ``(+N)``

    Call ``clear_active_orbit_tasks`` to clear focus, or pass an empty
    list (this same tool returns success with no pointer written).
    """
    try:
        if not session_id:
            raise ValidationError("session_id is required", field="session_id")
        if not project_name:
            raise ValidationError("project_name is required", field="project_name")
        # Reject path-traversal-shaped names early. The kebab-case promise in
        # the docstring is enforced by the same validator that ``create_orbit_files``
        # uses; rejecting here keeps the pointer write below safe even if a
        # downstream caller skips its own checks.
        orbit.validate_task_name(project_name)

        # Empty list is a no-op clear (don't write pointer; remove if present).
        if not task_numbers:
            removed = active_task.clear_pointer(session_id)
            return {
                "success": True,
                "project_name": project_name,
                "task_numbers": [],
                "cleared": removed,
            }

        files = orbit.get_orbit_files(project_name)
        if not files.tasks_file:
            raise OrbitError(
                ErrorCode.FILE_NOT_FOUND,
                f"No tasks.md found for project '{project_name}'",
                {"project_name": project_name},
            )
        # Permission errors and other OSError surface as the outer
        # ``except Exception`` catch-all instead of being remapped to
        # FILE_NOT_FOUND, which would mislead the caller.
        content = Path(files.tasks_file).read_text()

        items = parse_tasks_md(content)
        unknown: list[str] = []
        already_done: list[str] = []
        for n in task_numbers:
            item = find_item(items, n)
            if item is None:
                unknown.append(n)
            elif item.checked:
                already_done.append(n)

        if unknown or already_done:
            msg_parts: list[str] = []
            if unknown:
                msg_parts.append(f"unknown numbers: {', '.join(unknown)}")
            if already_done:
                msg_parts.append(f"already completed: {', '.join(already_done)}")
            err = ValidationError(
                f"Invalid task numbers for '{project_name}' ({'; '.join(msg_parts)})",
                field="task_numbers",
            )
            err.details.update(
                {
                    "project_name": project_name,
                    "unknown_numbers": unknown,
                    "already_completed_numbers": already_done,
                }
            )
            raise err

        path = active_task.write_pointer(session_id, project_name, task_numbers)
        return {
            "success": True,
            "project_name": project_name,
            "task_numbers": list(task_numbers),
            "pointer_path": str(path),
        }

    except OrbitError as e:
        return e.to_dict()
    except Exception as e:
        logger.exception("Error in set_active_orbit_tasks")
        return {"error": True, "message": str(e)}


@mcp.tool()
async def clear_active_orbit_tasks(
    session_id: Annotated[
        str,
        Field(description="Claude Code (or other tool) session ID."),
    ],
) -> dict:
    """Clear the active orbit-task pointer for this session.

    Statusline ``Task:`` field hides until ``set_active_orbit_tasks``
    is called again.
    """
    try:
        if not session_id:
            raise ValidationError("session_id is required", field="session_id")
        removed = active_task.clear_pointer(session_id)
        return {"success": True, "session_id": session_id, "cleared": removed}
    except OrbitError as e:
        return e.to_dict()
    except Exception as e:
        logger.exception("Error in clear_active_orbit_tasks")
        return {"error": True, "message": str(e)}

"""Project-management MCP tools - action items, stakeholders, tickets, due dates.

Thin wrappers over ``missioncache_db.pm_items`` (the single write path shared
with the dashboard and the CLI). Every mutation updates SQLite and re-renders
the read-only mirror sections in the project's context file under the sidecar
lock, so a change made here is visible to the dashboard immediately and to
the next /missioncache:load digest.
"""

import logging
from dataclasses import asdict
from typing import Annotated

from pydantic import Field

from missioncache_db import pm_items

from .app import mcp
from .db import get_db
from .errors import MissionCacheError, TaskNotFoundError

logger = logging.getLogger(__name__)


def _resolve_project(project_name: str):
    db = get_db()
    task = db.get_task_by_name(project_name)
    if task is None:
        raise TaskNotFoundError(project_name)
    return db, task


def _clear_sentinel(value: str | None) -> str | None:
    """Map the explicit clear sentinels (\"\" / \"none\") to None."""
    if value is not None and value.strip().lower() in ("", "none"):
        return None
    return value


@mcp.tool()
async def add_action_item(
    project_name: Annotated[str, Field(description="Project name")],
    what: Annotated[str, Field(description="The action item text")],
    requested_by: Annotated[
        str | None,
        Field(description="Who asked for it (person, or e.g. 'AIP weekly 2026-07-24')"),
    ] = None,
    assignee: Annotated[
        str, Field(description="Who owns it: 'me' or a person's name")
    ] = "me",
    due_date: Annotated[
        str | None, Field(description="Due date YYYY-MM-DD (omit if none was agreed)")
    ] = None,
    source: Annotated[
        str | None,
        Field(description="Provenance: meeting name + date, transcript path, 'conversation'"),
    ] = None,
    notes: Annotated[str | None, Field(description="Free-text notes")] = None,
) -> dict:
    """
    Record an action item on a project (commitments ledger).

    Use when a meeting transcript or conversation produces a commitment -
    yours or a colleague's. The item gets a stable id (AI-<n>), lands in
    the DB, renders into the context file's '## Action Items' section, and
    surfaces on every /missioncache:load with an overdue flag. Blocking
    dependencies belong in 'Waiting on' instead - an action item is a
    commitment, not a blocker.
    """
    try:
        db, task = _resolve_project(project_name)
        item = pm_items.add_action_item(
            db, task.id, what, requested_by=requested_by, assignee=assignee,
            due_date=due_date, source=source, notes=notes,
        )
        return {"success": True, "item": asdict(item)}
    except MissionCacheError as e:
        return e.to_dict()
    except ValueError as e:
        return {"error": True, "code": "VALIDATION_ERROR", "message": str(e)}
    except Exception as e:
        logger.exception("Error in add_action_item")
        return {"error": True, "message": str(e)}


@mcp.tool()
async def update_action_item(
    item_id: Annotated[int, Field(description="Action item id (the <n> of AI-<n>)")],
    status: Annotated[
        str | None,
        Field(description="New status: open, done, or dropped. Marking done stamps completed_at."),
    ] = None,
    what: Annotated[str | None, Field(description="Rewritten item text")] = None,
    requested_by: Annotated[str | None, Field(description="Who asked for it")] = None,
    assignee: Annotated[str | None, Field(description="New owner ('me' or a name)")] = None,
    due_date: Annotated[
        str | None,
        Field(description="New due date YYYY-MM-DD; pass 'none' to clear it"),
    ] = None,
    notes: Annotated[
        str | None,
        Field(description="Notes/outcome (e.g. how it was resolved when marking done)"),
    ] = None,
) -> dict:
    """
    Update an action item - complete it (status='done'), reopen, reassign,
    change the due date, or record the outcome.

    Only the fields passed change. Prefer recording the outcome in `notes`
    when completing, so the ledger says how it was resolved.
    """
    try:
        db = get_db()
        kwargs: dict = {}
        if status is not None:
            kwargs["status"] = status
        if what is not None:
            kwargs["what"] = what
        if requested_by is not None:
            kwargs["requested_by"] = requested_by
        if assignee is not None:
            kwargs["assignee"] = assignee
        if due_date is not None:
            kwargs["due_date"] = _clear_sentinel(due_date)
        if notes is not None:
            kwargs["notes"] = notes
        item = pm_items.update_action_item(db, item_id, **kwargs)
        return {"success": True, "item": asdict(item)}
    except ValueError as e:
        return {"error": True, "code": "VALIDATION_ERROR", "message": str(e)}
    except Exception as e:
        logger.exception("Error in update_action_item")
        return {"error": True, "message": str(e)}


@mcp.tool()
async def list_action_items(
    project_name: Annotated[
        str | None,
        Field(description="Project name; omit to list across ALL active/paused projects"),
    ] = None,
    status: Annotated[
        str | None, Field(description="Filter: open, done, or dropped")
    ] = None,
    assignee: Annotated[
        str | None, Field(description="Filter by owner ('me' or a name, case-insensitive)")
    ] = None,
    overdue_only: Annotated[
        bool, Field(description="Only open items past their due date")
    ] = False,
    due_within_days: Annotated[
        int | None, Field(description="Only open items due within N days")
    ] = None,
) -> dict:
    """
    List action items for one project or across every active project.

    The cross-project scope (no project_name) answers "what's due this
    week, anywhere" from any session; each item carries its project name
    and an overdue flag.
    """
    try:
        db = get_db()
        task_id = None
        if project_name is not None:
            _, task = _resolve_project(project_name)
            task_id = task.id
        items = pm_items.list_action_items(
            db, task_id=task_id, status=status, assignee=assignee,
            overdue_only=overdue_only, due_within_days=due_within_days,
        )
        return {
            "success": True,
            "count": len(items),
            "items": [
                {**asdict(i), "label": i.label, "overdue": i.is_overdue()}
                for i in items
            ],
        }
    except MissionCacheError as e:
        return e.to_dict()
    except ValueError as e:
        return {"error": True, "code": "VALIDATION_ERROR", "message": str(e)}
    except Exception as e:
        logger.exception("Error in list_action_items")
        return {"error": True, "message": str(e)}


@mcp.tool()
async def set_stakeholder(
    project_name: Annotated[str, Field(description="Project name")],
    name: Annotated[str, Field(description="Person's name")],
    role: Annotated[
        str | None, Field(description="Their role for THIS project (e.g. 'Manager', 'Centra QA')")
    ] = None,
    notes: Annotated[str | None, Field(description="Free-text notes")] = None,
    remove: Annotated[bool, Field(description="Remove this stakeholder instead")] = False,
) -> dict:
    """
    Add, update, or remove a project stakeholder.

    Upserts on (project, name): calling again with a new role updates in
    place. Stakeholders render into the context file's '## Stakeholders'
    section (the structured successor of the Key People convention).
    """
    try:
        db, task = _resolve_project(project_name)
        if remove:
            removed = pm_items.remove_stakeholder(db, task.id, name)
            return {"success": True, "removed": removed}
        stakeholder = pm_items.add_stakeholder(db, task.id, name, role=role, notes=notes)
        return {"success": True, "stakeholder": asdict(stakeholder)}
    except MissionCacheError as e:
        return e.to_dict()
    except ValueError as e:
        return {"error": True, "code": "VALIDATION_ERROR", "message": str(e)}
    except Exception as e:
        logger.exception("Error in set_stakeholder")
        return {"error": True, "message": str(e)}


@mcp.tool()
async def set_ticket(
    project_name: Annotated[str, Field(description="Project name")],
    label: Annotated[
        str, Field(description="Ticket label as displayed (e.g. 'GC-162794', 'MON-45')")
    ],
    url: Annotated[
        str | None,
        Field(description="Full ticket URL; omitted JIRA-style keys get a URL from the user's prefix map when one matches"),
    ] = None,
    system: Annotated[
        str | None, Field(description="Display hint only: jira, monday, github, ... (never branched on)")
    ] = None,
    status: Annotated[
        str | None, Field(description="Free-text status cache (e.g. 'In Progress'); MissionCache never fetches it")
    ] = None,
    notes: Annotated[str | None, Field(description="Free-text notes")] = None,
    remove: Annotated[bool, Field(description="Remove this ticket reference instead")] = False,
) -> dict:
    """
    Link, update, or remove an external ticket reference on a project.

    Ticket-system agnostic: label + url is the whole contract. Upserts on
    (project, label) - call again to update the cached status. Renders
    into the context file's '## Tickets' section.
    """
    try:
        db, task = _resolve_project(project_name)
        if remove:
            removed = pm_items.remove_ticket(db, task.id, label)
            return {"success": True, "removed": removed}
        if url is None:
            url = pm_items.jira_url_for(label)
        ticket = pm_items.add_ticket(
            db, task.id, label, url=url, system=system, status=status, notes=notes
        )
        return {"success": True, "ticket": asdict(ticket)}
    except MissionCacheError as e:
        return e.to_dict()
    except ValueError as e:
        return {"error": True, "code": "VALIDATION_ERROR", "message": str(e)}
    except Exception as e:
        logger.exception("Error in set_ticket")
        return {"error": True, "message": str(e)}


@mcp.tool()
async def set_project_due_date(
    project_name: Annotated[str, Field(description="Project name")],
    due_date: Annotated[
        str | None,
        Field(description="Target date YYYY-MM-DD; pass 'none' (or omit) to clear"),
    ] = None,
) -> dict:
    """
    Set or clear the project-level due date.

    Renders as a '**Due:** <date>' header line in the context file and
    surfaces on the /missioncache:load digest; health flags it when it is
    within 7 days or past. Per the estimation discipline, set one only for
    committed work - never fabricate a date onto uncommitted backlog.
    """
    try:
        db, task = _resolve_project(project_name)
        value = pm_items.set_project_due_date(db, task.id, _clear_sentinel(due_date))
        return {"success": True, "task_id": task.id, "due_date": value}
    except MissionCacheError as e:
        return e.to_dict()
    except ValueError as e:
        return {"error": True, "code": "VALIDATION_ERROR", "message": str(e)}
    except Exception as e:
        logger.exception("Error in set_project_due_date")
        return {"error": True, "message": str(e)}

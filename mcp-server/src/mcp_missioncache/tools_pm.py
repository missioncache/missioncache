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
from .helpers import attach_lead_session, live_peer_sessions_for_project

logger = logging.getLogger(__name__)


def _resolve_project(project_name: str):
    db = get_db()
    task = db.get_task_by_name(project_name)
    if task is None:
        raise TaskNotFoundError(project_name)
    return db, task


def _with_live_sessions(response: dict, project_name: str | None) -> dict:
    """Attach ``live_sessions`` to a mutation response, key only when non-empty.

    Every mutation in this module writes the DB and best-effort re-renders the
    context-file mirror sections under the sidecar lock - either way a live
    session on this project is now working from stale load-time state, which
    is what the cross-session notification contract covers. Same contract as
    update_context_file: presence of the key is the signal.
    """
    if project_name:
        peers = live_peer_sessions_for_project(project_name)
        if peers:
            response["live_sessions"] = peers
    return attach_lead_session(response)


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
        return _with_live_sessions({"success": True, "item": asdict(item)}, task.name)
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
        # By-id path: the project is not a parameter here, so resolve it from
        # the item row for the notification contract. Best-effort in full: a
        # lookup that RAISES must not turn the committed update into an error
        # response, so it degrades to "no hint" exactly like a None return.
        try:
            task = db.get_task(item.task_id)
            project = task.name if task else None
        except Exception:
            project = None
        return _with_live_sessions({"success": True, "item": asdict(item)}, project)
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
            # A no-op remove rewrote nothing - a notify hint would be noise.
            return _with_live_sessions(
                {"success": True, "removed": removed}, task.name if removed else None
            )
        stakeholder = pm_items.add_stakeholder(db, task.id, name, role=role, notes=notes)
        return _with_live_sessions(
            {"success": True, "stakeholder": asdict(stakeholder)}, task.name
        )
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
            return _with_live_sessions(
                {"success": True, "removed": removed}, task.name if removed else None
            )
        if url is None:
            url = pm_items.jira_url_for(label)
        ticket = pm_items.add_ticket(
            db, task.id, label, url=url, system=system, status=status, notes=notes
        )
        return _with_live_sessions({"success": True, "ticket": asdict(ticket)}, task.name)
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
        return _with_live_sessions(
            {"success": True, "task_id": task.id, "due_date": value}, task.name
        )
    except MissionCacheError as e:
        return e.to_dict()
    except ValueError as e:
        return {"error": True, "code": "VALIDATION_ERROR", "message": str(e)}
    except Exception as e:
        logger.exception("Error in set_project_due_date")
        return {"error": True, "message": str(e)}


# ---------------------------------------------------------------------------
# Cross-project portfolio
# ---------------------------------------------------------------------------

# Chat-turn budget. The full /api/today payload measured 92,882 bytes (about
# 23,200 tokens) on a real machine, which is unusable in a turn. These caps
# bring the same data to roughly 4K tokens. They clip DISPLAY strings only:
# the ranking, the sort keys and every count are computed before clipping, so
# the chat brief and the dashboard agree on what is urgent.
_PORTFOLIO_WHAT_CHARS = 120
_PORTFOLIO_LEFT_OFF_CHARS = 140
_PORTFOLIO_NEXT_UP_CHARS = 100
_PORTFOLIO_ON_ME_PER_BUCKET = 5


def _clip(text, limit: int):
    if not isinstance(text, str):
        return text
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _clip_portfolio(full: dict, live: list[dict], lead: dict | None,
                    max_projects: int, max_rows_per_project: int) -> dict:
    """Trim a full portfolio to what a chat turn can carry.

    Drops the ids and URLs the chat surface never uses (``row_index`` and
    ``task_id`` exist for the dashboard's resolve endpoint), caps list
    lengths, and shortens free text. ``counts`` is passed through untouched:
    it is the one thing that must read the same in both surfaces.
    """
    projects = [
        {
            "name": p["name"],
            "at_risk": p["at_risk"],
            "overdue_count": p["overdue_count"],
            "open_count": p["open_count"],
            "on_others_count": p["on_others_count"],
            "stale_on_others_count": p["stale_on_others_count"],
            "days_to_due": p["days_to_due"],
            "days_since_worked": p["days_since_worked"],
            "completion_pct": p["completion_pct"],
            "completed_count": p["completed_count"],
            "total_count": p["total_count"],
            "next_up": _clip(p["next_up"], _PORTFOLIO_NEXT_UP_CHARS),
            "left_off": _clip(p["left_off"], _PORTFOLIO_LEFT_OFF_CHARS),
            "ticket_label": p["ticket_label"],
            "category": p["category"],
        }
        for p in full["projects"][:max_projects]
    ]
    on_others = [
        {
            "project_name": g["project_name"],
            "count": g["count"],
            "mine_count": g["mine_count"],
            "stale_count": g["stale_count"],
            "newest_age_days": g["newest_age_days"],
            "rows": [
                {
                    "what": _clip(r["what"], _PORTFOLIO_WHAT_CHARS),
                    "who": r["who_primary"],
                    "mine": r["mine"],
                    "days_past_line": r["days_past_line"],
                    "age_days": r["age_days"],
                }
                for r in g["rows"][:max_rows_per_project]
            ],
        }
        for g in full["on_others"][:max_projects]
    ]
    on_me = {
        bucket: [
            {
                "what": _clip(r["what"], _PORTFOLIO_WHAT_CHARS),
                "project_name": r.get("project_name"),
                "due_date": r.get("due_date"),
                "kind": r.get("kind"),
            }
            for r in rows[:_PORTFOLIO_ON_ME_PER_BUCKET]
        ]
        for bucket, rows in full["on_me"].items()
    }
    return {
        "success": True,
        "generated_at": full["generated_at"],
        "user_name": full["user_name"],
        "counts": full["counts"],
        "on_me": on_me,
        "on_others": on_others,
        "projects": projects,
        # session_id dropped on purpose: the chat side reconciles against
        # ListAgents by title, and 10 ids cost 1.3KB for nothing.
        "live_sessions": [
            {"project_name": s["project_name"], "title": s["title"],
             "last_active": s["last_active"]}
            for s in live
        ],
        "lead_session": (
            {"title": lead["title"], "since": lead["since"]} if lead else None
        ),
    }


@mcp.tool()
async def get_portfolio(
    scope: Annotated[
        str,
        Field(description="'focus' (default): projects with a live session or worked "
                          "in the last recent_days. 'all': every project with "
                          "something outstanding."),
    ] = "focus",
    recent_days: Annotated[
        int, Field(description="Recency window for the 'focus' scope, in days")
    ] = 7,
    max_projects: Annotated[
        int, Field(description="Cap on projects and on waiting-on groups returned")
    ] = 12,
    max_rows_per_project: Annotated[
        int, Field(description="Cap on waiting-on rows per project")
    ] = 3,
) -> dict:
    """
    Cross-project state in one call: what is urgent, what is waiting on you,
    what is waiting on others, which projects have a live session, and which
    session is the designated lead.

    Same ranking and counts as the dashboard's Attention view - both come from
    missioncache_db.portfolio - clipped to a chat-sized payload. Read-only and
    session-neutral: it never binds the calling session to a project, because
    a project-manager session is not a project.
    """
    try:
        import missioncache_db
        from missioncache_db import portfolio

        db = get_db()
        # Sampled BEFORE the rollup, never after. A write that lands between
        # the two is then reflected in the payload but NOT yet in the token,
        # so the next tick still sees a change. Sampling after would fold that
        # write into the token beside a payload that predates it, and the delta
        # loop would call it "nothing changed" and lose it for good.
        mark = portfolio.portfolio_watermark(db)
        full = portfolio.build_portfolio(db)
        # Every live bound session, INCLUDING the caller's if it has one: a
        # portfolio wants the whole picture, and the notification-contract
        # exclusion in live_peer_sessions_for_project does not apply here.
        live = missioncache_db.live_sessions_all()
        lead = missioncache_db.live_lead_session()

        if scope == "focus":
            names = {s["project_name"] for s in live} | {
                p["name"]
                for p in full["projects"]
                if p["days_since_worked"] is not None
                and p["days_since_worked"] <= recent_days
            }
            full = portfolio.scope_portfolio(full, names)
        elif scope != "all":
            return {"error": True, "message": f"scope must be 'focus' or 'all', got {scope!r}"}

        result = _clip_portfolio(full, live, lead, max_projects, max_rows_per_project)
        result["scope"] = scope
        result["watermark"] = mark
        return result
    except MissionCacheError as e:
        return e.to_dict()
    except Exception as e:
        logger.exception("Error in get_portfolio")
        return {"error": True, "message": str(e)}

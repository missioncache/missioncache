"""Event log: one row per change MissionCache makes to a project.

Each write records what it changed here, so every consumer - a lead
session, the dashboard, a tracking session - reads the same history
instead of relying on peers' messages. The rows live only in the local
``tasks.db``.

Two layers write. The database writes (action items, due dates, the
dashboard's Waiting-on resolve, complete / reopen / rename) record inside
this package, so the MCP server, the dashboard and the CLI all produce
events. The markdown-only writes (Recent Changes lines, Waiting-on rows
added or resolved through ``update_context_file``, ticked tasks, a move
between projects) happen in the MCP server's file layer and record from
there.

Stdlib-only, same contract as ``pm_items``: the MCP server imports from
missioncache_db, never the reverse.
"""

import os
import re
import sqlite3
from datetime import datetime
from typing import Any, Iterable, Optional

EVENT_KINDS = (
    "recent_change",
    "waiting_added",
    "waiting_resolved",
    "task_done",
    "action_item",
    "due_date",
    "moved",
    "renamed",
    "completed",
    "reopened",
)

# Long enough for a Recent Changes line, short enough that a pasted block
# cannot turn one event into a document.
WHAT_MAX_CHARS = 500
DEFAULT_RETENTION_DAYS = 90
MAX_LIMIT = 1000

_TICKET_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
# tasks.db stores local time as "YYYY-MM-DD HH:MM:SS" and compares it as
# text, so a reader's `since` must be in that shape. An ISO "T" separator,
# fractional seconds and an offset are folded or dropped first.
_SINCE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}( \d{2}:\d{2}(:\d{2})?)?$")


def _one_line(text: Any) -> str:
    # Events are never written into markdown, so whitespace is the only
    # thing to flatten. The cap keeps a pasted block from becoming one row.
    line = " ".join(str(text or "").split())
    return line if len(line) <= WHAT_MAX_CHARS else line[: WHAT_MAX_CHARS - 1] + "…"


def normalize_since(since: Optional[str]) -> Optional[str]:
    """``since`` in tasks.db's local-time text shape, or None when empty.

    Raises ValueError on anything else, rather than letting a well-meant
    ISO timestamp compare wrong and silently drop a day's events."""
    if not since or not str(since).strip():
        return None
    value = str(since).strip()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is not None and parsed.tzinfo is not None:
        # An offset names another clock; the stored times are local.
        return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    value = re.sub(r"\.\d+$", "", value.replace("T", " "))
    if not _SINCE_RE.match(value):
        raise ValueError(
            f"since must look like 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM[:SS]', got {since!r}"
        )
    return value


def insert_events(
    conn: sqlite3.Connection,
    project: Optional[str],
    items: Iterable[tuple[str, str, Optional[str]]],
    task_id: Optional[int] = None,
    source_session: Optional[str] = None,
) -> int:
    """Insert ``(kind, what, section)`` items on an open connection,
    WITHOUT committing, so a database write records its event in its own
    transaction. Returns how many rows went in.

    The task is resolved by name when ``task_id`` is not given, and the name
    from ``task_id`` when ``project`` is None. Nothing is written when
    neither resolves to a project. The ticket is
    the project's own ticket (its first ``tickets`` row, else its legacy
    ``jira_key``), else the first ticket-shaped key in ``what``. The session
    defaults to ``CLAUDE_CODE_SESSION_ID``, which Claude Code sets on the MCP
    server it launches; a dashboard or CLI write records none.

    An unknown kind raises ValueError, so a typo at a call site fails in
    tests rather than writing rows no reader filters for.
    """
    rows = []
    for kind, what, section in items:
        if kind not in EVENT_KINDS:
            raise ValueError(f"unknown event kind {kind!r}")
        text = _one_line(what)
        if text:
            rows.append((kind, text, _one_line(section) or None))
    if not rows:
        return 0
    found = conn.execute(
        "SELECT t.id, t.name, "
        "(SELECT label FROM tickets WHERE task_id = t.id ORDER BY id LIMIT 1), "
        "t.jira_key FROM tasks t WHERE "
        + ("t.id = ?" if task_id is not None else "t.name = ? ORDER BY t.id DESC")
        + " LIMIT 1",
        (task_id if task_id is not None else project,),
    ).fetchone()
    project_ticket = None
    if found:
        task_id, project = found[0], project or found[1]
        project_ticket = found[2] or found[3]
    if not project:
        return 0
    session = source_session or os.environ.get("CLAUDE_CODE_SESSION_ID") or None
    conn.executemany(
        "INSERT INTO events (project, task_id, kind, what, ticket, section, source_session) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (project, task_id, kind, text, project_ticket or _first_ticket(text), section, session)
            for kind, text, section in rows
        ],
    )
    return len(rows)


def record_events(
    db,
    project: str,
    items: Iterable[tuple[str, str, Optional[str]]],
    source_session: Optional[str] = None,
    task_id: Optional[int] = None,
) -> int:
    """``insert_events`` in a transaction of its own, for the file-layer
    writes that have no database transaction to join."""
    with db.connection() as conn:
        count = insert_events(conn, project, items, task_id, source_session)
        if count:
            conn.commit()
        return count


def _first_ticket(text: str) -> Optional[str]:
    match = _TICKET_RE.search(text)
    return match.group(0) if match else None


def list_events(
    db,
    since: Optional[str] = None,
    project: Optional[str] = None,
    kinds: Optional[Iterable[str]] = None,
    limit: int = 200,
    after_id: Optional[int] = None,
    task_id: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Events newest first. ``list_events_page`` with the overflow flag dropped."""
    return list_events_page(
        db, since=since, project=project, kinds=kinds, limit=limit, after_id=after_id, task_id=task_id
    )[0]


def list_events_page(
    db,
    since: Optional[str] = None,
    project: Optional[str] = None,
    kinds: Optional[Iterable[str]] = None,
    limit: int = 200,
    after_id: Optional[int] = None,
    task_id: Optional[int] = None,
) -> tuple[list[dict[str, Any]], bool]:
    """``(events newest first, has_more)``.

    ``after_id`` is the exact cursor for "what is new since I last looked":
    pass the highest ``id`` already seen. With a cursor the page is the OLDEST
    rows after it, so a reader that stamps the newest id it got never skips a
    row when more than ``limit`` accumulated; ``has_more`` says the log holds
    rows past this page. Without a cursor the page is the newest rows.
    ``since`` is a local timestamp (see ``normalize_since``) for a human
    window. ``project`` matches the name the event was recorded under;
    ``task_id`` follows the project across a rename. ``limit`` is clamped to
    1..1000.
    """
    where, args = [], []
    since = normalize_since(since)
    if since:
        where.append("created_at >= ?")
        args.append(since)
    if after_id is not None:
        where.append("id > ?")
        args.append(int(after_id))
    if project:
        where.append("project = ?")
        args.append(project)
    if task_id is not None:
        where.append("task_id = ?")
        args.append(int(task_id))
    kinds = [k for k in (kinds or []) if k]
    unknown = [k for k in kinds if k not in EVENT_KINDS]
    if unknown:
        raise ValueError(f"unknown event kind(s): {', '.join(unknown)}")
    if kinds:
        where.append(f"kind IN ({', '.join('?' for _ in kinds)})")
        args.extend(kinds)
    sql = (
        "SELECT id, project, task_id, kind, what, ticket, section, source_session, created_at "
        "FROM events"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    limit = min(MAX_LIMIT, max(1, int(limit)))
    sql += f" ORDER BY id {'ASC' if after_id is not None else 'DESC'} LIMIT ?"
    args.append(limit + 1)  # one extra row tells whether the log goes on
    with db.connection() as conn:
        cursor = conn.execute(sql, args)
        columns = [c[0] for c in cursor.description]
        rows = [dict(zip(columns, r)) for r in cursor.fetchall()]
    has_more = len(rows) > limit
    rows = rows[:limit]
    if after_id is not None:
        rows.reverse()
    return rows, has_more


def latest_event_id(db) -> int:
    """The newest event id, or 0 on an empty log. The lead's baseline cursor."""
    with db.connection() as conn:
        return conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]


def prune_events(db, days: int = DEFAULT_RETENTION_DAYS) -> int:
    """Delete events older than ``days`` (at least 1) and return how many
    went. Use ``delete_project_events`` to clear one project's history."""
    days = int(days)
    if days < 1:
        raise ValueError(f"days must be at least 1, got {days}")
    with db.connection() as conn:
        cursor = conn.execute(
            "DELETE FROM events WHERE julianday('now', 'localtime') - julianday(created_at) > ?",
            (days,),
        )
        conn.commit()
        return cursor.rowcount


def delete_project_events(db, project: str) -> int:
    """Delete every event recorded under ``project`` and return the count.
    History outlives a project on purpose; this is how to clear it."""
    with db.connection() as conn:
        cursor = conn.execute("DELETE FROM events WHERE project = ?", (project,))
        conn.commit()
        return cursor.rowcount

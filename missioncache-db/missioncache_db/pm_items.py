"""Project-management layer: action items, stakeholders, tickets, due dates.

SQLite (the ``action_items`` / ``stakeholders`` / ``tickets`` tables plus
``tasks.due_date``) is the source of truth. The project's context file gets
a rendered READ-ONLY markdown mirror - ``## Action Items``, ``## Stakeholders``,
``## Tickets`` sections and a ``**Due:**`` header line - refreshed on every
mutation under the same sidecar flock every other context writer uses. The
mirror is never parsed back; hand edits inside the managed sections are
overwritten on the next sync.

Consumed by the MCP server (action-item tools), the dashboard (REST
endpoints, direct SQLite), and the missioncache-db CLI. All three converge
on the functions here, so a UI toggle and an in-session tool call take the
identical write path.

Stdlib-only, same contract as ``context_health``: the MCP server imports
from missioncache_db, never the reverse.
"""

import contextlib
import fcntl
import json
import logging
import os
import re
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from . import context_health

logger = logging.getLogger(__name__)

ACTION_ITEM_STATUSES = ("open", "done", "dropped")


class WaitingOnConflict(Exception):
    """A Waiting-on row moved or changed since the caller last read it.

    Raised instead of resolving a different row than the user meant. The
    Waiting-on table is hand-editable markdown with no per-row id, so a
    positional resolve has to verify what it is about to remove.
    """

# Health thresholds, plain module constants like context_health's (tests
# monkeypatch them directly).
DUE_SOON_DAYS = 7
STALE_OPEN_NO_DUE_DAYS = 14

# Done/dropped items stay visible in the rendered mirror this many days
# after completion, then live only in the DB (and the Recent Changes line
# written when they closed).
RECENT_DONE_DAYS = 7

# Stamped into every rendered section body and CHECKED before replacing one:
# a section carrying this marker is ours to overwrite, a section without it is
# the user's hand-written prose and is left alone. Without the check, a
# pre-existing hand-authored "## Tickets" would be destroyed by an unrelated
# stakeholder write.
MANAGED_MARKER = (
    "<!-- Managed by MissionCache (source of truth: tasks.db). Update via the "
    "MCP tools, the dashboard, or the missioncache-db CLI - hand edits here "
    "are overwritten on the next sync. -->"
)

_DUE_LINE_RE = re.compile(r"^\*\*Due:\*\*.*$", re.MULTILINE)
_LAST_UPDATED_LINE_RE = re.compile(r"^\*\*Last Updated:\*\*.*$", re.MULTILINE)

# Where each managed section slots into the canonical order when absent.
# Insertion lands before the first anchor present in the file, so Stakeholders
# and Tickets land before Gotchas and Action Items immediately before Waiting
# on - the order rules/missioncache.md documents, and the resume-critical tail
# (Gotchas / Waiting on / Next Steps / Recent Changes) stays intact.
#
# "Action Items" is deliberately NOT an anchor for the other two: it sits after
# Gotchas, so anchoring on it put Stakeholders and Tickets after Gotchas too
# whenever all three were created in one pass - which is the common case, since
# a project with a jira_key gets an auto-migrated ticket on its first mutation.
_SECTION_ANCHORS = {
    "Stakeholders": ("Tickets", "Gotchas", "Waiting on", "Next Steps", "Recent Changes"),
    "Tickets": ("Gotchas", "Waiting on", "Next Steps", "Recent Changes"),
    "Action Items": ("Waiting on", "Next Steps", "Recent Changes"),
}

# Recent Changes notes are single bullet lines. User text reaching them (item
# text, a resolve outcome, a person's name) must not carry newlines or a
# leading "#", or it forges sections and subsections in the file - which is
# also the file a session reads back as its own context.
_NOTE_NEWLINES_RE = re.compile(r"[\r\n]+")
_NOTE_LEADING_HASH_RE = re.compile(r"^#+\s*")


def _sanitize_note(text: str) -> str:
    """Flatten a change note to one non-heading line."""
    return _NOTE_LEADING_HASH_RE.sub("", _NOTE_NEWLINES_RE.sub(" ", text).strip())


# =============================================================================
# Data classes
# =============================================================================


@dataclass
class ActionItem:
    id: int
    task_id: int
    what: str
    requested_by: Optional[str]
    assignee: str
    due_date: Optional[str]
    status: str
    source: Optional[str]
    notes: Optional[str]
    created_at: str
    completed_at: Optional[str]
    project_name: Optional[str] = None  # filled by cross-project queries

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ActionItem":
        keys = row.keys()
        return cls(
            id=row["id"],
            task_id=row["task_id"],
            what=row["what"],
            requested_by=row["requested_by"],
            assignee=row["assignee"],
            due_date=row["due_date"],
            status=row["status"],
            source=row["source"],
            notes=row["notes"],
            created_at=row["created_at"],
            completed_at=row["completed_at"],
            project_name=row["project_name"] if "project_name" in keys else None,
        )

    @property
    def label(self) -> str:
        """The stable display id rendered in the mirror (``AI-<id>``)."""
        return f"AI-{self.id}"

    def is_overdue(self, today: Optional[date] = None) -> bool:
        if self.status != "open" or not self.due_date:
            return False
        due = _parse_iso_date(self.due_date)
        if due is None:
            return False
        return due < (today or date.today())


@dataclass
class Stakeholder:
    id: int
    task_id: int
    name: str
    role: Optional[str]
    notes: Optional[str]
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Stakeholder":
        return cls(
            id=row["id"],
            task_id=row["task_id"],
            name=row["name"],
            role=row["role"],
            notes=row["notes"],
            created_at=row["created_at"],
        )


@dataclass
class Ticket:
    id: int
    task_id: int
    label: str
    url: Optional[str]
    system: Optional[str]
    status: Optional[str]
    notes: Optional[str]
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Ticket":
        return cls(
            id=row["id"],
            task_id=row["task_id"],
            label=row["label"],
            url=row["url"],
            system=row["system"],
            status=row["status"],
            notes=row["notes"],
            created_at=row["created_at"],
        )


# =============================================================================
# Validation helpers
# =============================================================================


def _parse_iso_date(value: str) -> Optional[date]:
    try:
        return date.fromisoformat(value.strip())
    except (ValueError, AttributeError):
        return None


def _validate_due_date(value: Optional[str]) -> Optional[str]:
    """Normalize a due date to YYYY-MM-DD or None; raise on malformed."""
    if value is None or value == "":
        return None
    parsed = _parse_iso_date(value)
    if parsed is None:
        raise ValueError(f"Invalid due date: {value!r}. Expected YYYY-MM-DD.")
    return parsed.isoformat()


def _validate_ticket_url(value: Optional[str]) -> Optional[str]:
    """Allow only http(s) ticket URLs; raise otherwise.

    A ticket URL is rendered as a link in the dashboard, so a
    ``javascript:`` value would be a live script link in a page that can
    register an executed statusline command. Validating at the WRITE path
    covers every entry point at once - the REST endpoint, the MCP tool, the
    CLI, and bundle import, which is untrusted input by contract.
    """
    if value is None or value.strip() == "":
        return None
    url = value.strip()
    if not re.match(r"(?i)https?://", url):
        raise ValueError(
            f"Invalid ticket URL: {url!r}. Only http:// and https:// are allowed."
        )
    return url


def _validate_status(value: str) -> str:
    if value not in ACTION_ITEM_STATUSES:
        raise ValueError(
            f"Invalid status: {value!r}. Must be one of: "
            f"{', '.join(ACTION_ITEM_STATUSES)}"
        )
    return value


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# =============================================================================
# File locking (mirror writes)
# =============================================================================

# NOTE: ``_file_lock`` is duplicated in mcp-server's ``project_files.py`` and
# in ``hooks/pre_compact.py``. ``_atomic_update_context_with_journal`` is
# duplicated in ``project_files.py`` only - the hook has the simpler
# ``_atomic_update_text`` and deliberately skips journal rollover.
# All copies flock the SAME ``<context>.lock`` sidecar, so writers serialize
# across processes regardless of which copy they run. If locking semantics
# change, change every copy.


@contextlib.contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive lock on the ``<path>.lock`` sidecar (never deleted)."""
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lockfd:
        fcntl.flock(lockfd.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lockfd.fileno(), fcntl.LOCK_UN)


def _atomic_update_context_with_journal(
    context_path: Path,
    journal_path: Path,
    transform: Callable[[str], tuple[str, Optional[str]]],
) -> str:
    """Read-modify-write the context file (and journal overflow) under lock.

    Same semantics as the project_files.py original: journal written first,
    context second, both under the context file's sidecar lock, each via a
    ``.tmp`` + ``os.replace`` so a crash never leaves a torn file.
    """
    with _file_lock(context_path):
        content = context_path.read_text()
        new_content, journal_append = transform(content)
        if journal_append:
            if journal_path.exists():
                journal_content = journal_path.read_text().rstrip("\n") + "\n\n"
            else:
                journal_content = (
                    context_health.journal_header(context_path.parent.name) + "\n"
                )
            journal_content += journal_append
            journal_tmp = journal_path.with_name(journal_path.name + ".tmp")
            journal_tmp.write_text(journal_content)
            os.replace(journal_tmp, journal_path)
        tmp_path = context_path.with_name(context_path.name + ".tmp")
        tmp_path.write_text(new_content)
        os.replace(tmp_path, context_path)
        return new_content


# =============================================================================
# Action items
# =============================================================================


def add_action_item(
    db,
    task_id: int,
    what: str,
    requested_by: Optional[str] = None,
    assignee: str = "me",
    due_date: Optional[str] = None,
    source: Optional[str] = None,
    notes: Optional[str] = None,
    refresh_mirror: bool = True,
) -> ActionItem:
    """Create an action item; refresh the project's context mirror."""
    what = what.strip()
    if not what:
        raise ValueError("Action item 'what' must not be empty")
    due_date = _validate_due_date(due_date)
    with db.connection() as conn:
        cur = conn.execute(
            """INSERT INTO action_items
               (task_id, what, requested_by, assignee, due_date, source, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (task_id, what, requested_by, assignee or "me", due_date, source, notes),
        )
        conn.commit()
        item = _get_action_item(conn, cur.lastrowid)
    if refresh_mirror:
        due_part = f", due {item.due_date}" if item.due_date else ""
        refresh_context_mirror(
            db,
            task_id,
            change_note=(
                f"Action item added ({item.label}): {item.what} "
                f"(owner: {item.assignee}{due_part})"
            ),
        )
    return item


_UNSET = object()


def update_action_item(
    db,
    item_id: int,
    what: Any = _UNSET,
    requested_by: Any = _UNSET,
    assignee: Any = _UNSET,
    due_date: Any = _UNSET,
    status: Any = _UNSET,
    source: Any = _UNSET,
    notes: Any = _UNSET,
    refresh_mirror: bool = True,
) -> ActionItem:
    """Update fields of an action item; only passed fields change.

    ``completed_at`` is managed here (not by a trigger): set when status
    leaves ``open``, cleared when an item is reopened.
    """
    with db.connection() as conn:
        current = _get_action_item(conn, item_id)
        sets: list[str] = []
        params: list[Any] = []
        if what is not _UNSET:
            what = (what or "").strip()
            if not what:
                raise ValueError("Action item 'what' must not be empty")
            sets.append("what = ?")
            params.append(what)
        if requested_by is not _UNSET:
            sets.append("requested_by = ?")
            params.append(requested_by)
        if assignee is not _UNSET:
            sets.append("assignee = ?")
            params.append(assignee or "me")
        if due_date is not _UNSET:
            sets.append("due_date = ?")
            params.append(_validate_due_date(due_date))
        if source is not _UNSET:
            sets.append("source = ?")
            params.append(source)
        if notes is not _UNSET:
            sets.append("notes = ?")
            params.append(notes)
        if status is not _UNSET:
            status = _validate_status(status)
            sets.append("status = ?")
            params.append(status)
            if status == "open":
                sets.append("completed_at = NULL")
            elif current.status == "open":
                sets.append("completed_at = ?")
                params.append(_now())
        if not sets:
            return current
        params.append(item_id)
        conn.execute(
            f"UPDATE action_items SET {', '.join(sets)} WHERE id = ?", params
        )
        conn.commit()
        item = _get_action_item(conn, item_id)
    if refresh_mirror:
        if status is not _UNSET and status != current.status:
            verb = {"done": "done", "dropped": "dropped", "open": "reopened"}[status]
            note = f"Action item {verb} ({item.label}): {item.what}"
            if item.notes and notes is not _UNSET:
                note += f" - {item.notes}"
        else:
            note = f"Action item updated ({item.label}): {item.what}"
        refresh_context_mirror(db, item.task_id, change_note=note)
    return item


def complete_action_item(
    db, item_id: int, outcome: Optional[str] = None, refresh_mirror: bool = True
) -> ActionItem:
    """Mark an action item done, optionally recording the outcome in notes."""
    kwargs: dict[str, Any] = {"status": "done"}
    if outcome:
        kwargs["notes"] = outcome
    return update_action_item(db, item_id, refresh_mirror=refresh_mirror, **kwargs)


def get_action_item(db, item_id: int) -> ActionItem:
    with db.connection() as conn:
        return _get_action_item(conn, item_id)


def _get_action_item(conn: sqlite3.Connection, item_id: int) -> ActionItem:
    row = conn.execute(
        "SELECT * FROM action_items WHERE id = ?", (item_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"Action item not found: {item_id}")
    return ActionItem.from_row(row)


def list_action_items(
    db,
    task_id: Optional[int] = None,
    status: Optional[str] = None,
    assignee: Optional[str] = None,
    overdue_only: bool = False,
    due_within_days: Optional[int] = None,
    project_statuses: tuple = ("active", "paused"),
) -> list[ActionItem]:
    """Action items for one project (``task_id``) or across projects.

    Cross-project scope (``task_id=None``) joins tasks for the project name
    and keeps only projects whose status is in ``project_statuses`` - the
    Today-view shape. ``overdue_only`` / ``due_within_days`` imply open
    items only. Ordering: open before done/dropped, then due date (nulls
    last), then id.
    """
    if status is not None:
        _validate_status(status)
    where: list[str] = []
    params: list[Any] = []
    if task_id is not None:
        where.append("ai.task_id = ?")
        params.append(task_id)
    elif project_statuses:
        placeholders = ", ".join("?" for _ in project_statuses)
        where.append(f"t.status IN ({placeholders})")
        params.extend(project_statuses)
    if status is not None:
        where.append("ai.status = ?")
        params.append(status)
    if assignee is not None:
        where.append("lower(ai.assignee) = lower(?)")
        params.append(assignee)
    today = date.today().isoformat()
    if overdue_only:
        where.append("ai.status = 'open' AND ai.due_date IS NOT NULL AND ai.due_date < ?")
        params.append(today)
    if due_within_days is not None:
        horizon = date.fromordinal(date.today().toordinal() + due_within_days).isoformat()
        where.append("ai.status = 'open' AND ai.due_date IS NOT NULL AND ai.due_date <= ?")
        params.append(horizon)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT ai.*, t.name AS project_name
        FROM action_items ai JOIN tasks t ON t.id = ai.task_id
        {where_sql}
        ORDER BY CASE ai.status WHEN 'open' THEN 0 ELSE 1 END,
                 ai.due_date IS NULL, ai.due_date, ai.id
    """
    with db.connection() as conn:
        return [ActionItem.from_row(r) for r in conn.execute(sql, params)]


# =============================================================================
# Stakeholders
# =============================================================================


def add_stakeholder(
    db,
    task_id: int,
    name: str,
    role: Optional[str] = None,
    notes: Optional[str] = None,
    refresh_mirror: bool = True,
) -> Stakeholder:
    """Add or update a stakeholder (upsert on the (task_id, name) key)."""
    name = name.strip()
    if not name:
        raise ValueError("Stakeholder name must not be empty")
    with db.connection() as conn:
        conn.execute(
            """INSERT INTO stakeholders (task_id, name, role, notes)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(task_id, name)
               DO UPDATE SET role = excluded.role, notes = excluded.notes""",
            (task_id, name, role, notes),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM stakeholders WHERE task_id = ? AND name = ?",
            (task_id, name),
        ).fetchone()
    stakeholder = Stakeholder.from_row(row)
    if refresh_mirror:
        role_part = f" ({role})" if role else ""
        refresh_context_mirror(
            db, task_id, change_note=f"Stakeholder added: {name}{role_part}"
        )
    return stakeholder


def remove_stakeholder(db, task_id: int, name: str, refresh_mirror: bool = True) -> bool:
    """Remove a stakeholder by name; returns whether a row was deleted."""
    with db.connection() as conn:
        cur = conn.execute(
            "DELETE FROM stakeholders WHERE task_id = ? AND name = ?",
            (task_id, name.strip()),
        )
        conn.commit()
        removed = cur.rowcount > 0
    if removed and refresh_mirror:
        refresh_context_mirror(
            db, task_id, change_note=f"Stakeholder removed: {name.strip()}"
        )
    return removed


def list_stakeholders(db, task_id: int) -> list[Stakeholder]:
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT * FROM stakeholders WHERE task_id = ? ORDER BY name COLLATE NOCASE",
            (task_id,),
        ).fetchall()
    return [Stakeholder.from_row(r) for r in rows]


# =============================================================================
# Tickets
# =============================================================================


def add_ticket(
    db,
    task_id: int,
    label: str,
    url: Optional[str] = None,
    system: Optional[str] = None,
    status: Optional[str] = None,
    notes: Optional[str] = None,
    refresh_mirror: bool = True,
) -> Ticket:
    """Add or update a ticket reference (upsert on the (task_id, label) key).

    Agnostic by contract: ``label`` + ``url`` is the whole interface.
    ``system`` is a display hint, ``status`` a free-text cache - neither is
    ever branched on.
    """
    label = label.strip()
    if not label:
        raise ValueError("Ticket label must not be empty")
    url = _validate_ticket_url(url)
    with db.connection() as conn:
        conn.execute(
            """INSERT INTO tickets (task_id, label, url, system, status, notes)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(task_id, label)
               DO UPDATE SET url = excluded.url, system = excluded.system,
                             status = excluded.status, notes = excluded.notes""",
            (task_id, label, url, system, status, notes),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM tickets WHERE task_id = ? AND label = ?",
            (task_id, label),
        ).fetchone()
    ticket = Ticket.from_row(row)
    if refresh_mirror:
        refresh_context_mirror(
            db, task_id, change_note=f"Ticket linked: {label}"
        )
    return ticket


def remove_ticket(db, task_id: int, label: str, refresh_mirror: bool = True) -> bool:
    """Remove a ticket reference by label; returns whether a row was deleted."""
    with db.connection() as conn:
        cur = conn.execute(
            "DELETE FROM tickets WHERE task_id = ? AND label = ?",
            (task_id, label.strip()),
        )
        conn.commit()
        removed = cur.rowcount > 0
    if removed and refresh_mirror:
        refresh_context_mirror(
            db, task_id, change_note=f"Ticket unlinked: {label.strip()}"
        )
    return removed


def list_tickets(db, task_id: int) -> list[Ticket]:
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT * FROM tickets WHERE task_id = ? ORDER BY id", (task_id,)
        ).fetchall()
    return [Ticket.from_row(r) for r in rows]


def _dashboard_config_file() -> Path:
    """Resolved per call, not at import.

    A module-level ``Path.home()`` bound at import time meant tests read the
    developer's real ~/.claude config (and, once a test suite had imported
    this module under a tmp home, froze to a tmp_path pytest later removed).
    """
    return Path.home() / ".claude" / "missioncache-dashboard-config.json"


def jira_url_for(label: str) -> Optional[str]:
    """Best-effort URL for a JIRA-style key from the user's prefix map.

    Reads the documented ``jira_urls`` mapping in the dashboard config file
    with the same first-prefix-match + concatenation the dashboard's
    ``get_jira_url`` uses. None when the file, the mapping, or a matching
    prefix is absent - the mapping is optional instance config, not a
    dependency.
    """
    try:
        document = json.loads(_dashboard_config_file().read_text())
    except (OSError, ValueError):
        return None
    # isinstance on the DOCUMENT, not just the mapping: a config whose top
    # level is a JSON array would make .get() raise AttributeError, which
    # escapes this guard and breaks every PM write via the mirror.
    if not isinstance(document, dict):
        return None
    mapping = document.get("jira_urls", {})
    if not isinstance(mapping, dict):
        return None
    for prefix, base_url in mapping.items():
        if label.startswith(prefix):
            candidate = f"{base_url}{label}"
            # A hostile or fat-fingered prefix map must not inject a
            # javascript: link into the dashboard's ticket anchor.
            try:
                return _validate_ticket_url(candidate)
            except ValueError:
                return None
    return None


def ensure_jira_ticket_migrated(db, task) -> bool:
    """Copy a legacy ``tasks.jira_key`` into a tickets row (first touch).

    Idempotent and non-destructive: ``jira_key`` stays readable on the
    tasks row so existing consumers keep working, and an existing tickets
    row with the same label (e.g. hand-added with a better URL) is never
    overwritten. Returns whether a row was inserted. Called from
    ``refresh_context_mirror``, so the first PM mutation on a legacy
    project pulls its JIRA key into the ticket ledger.
    """
    jira_key = task.jira_key
    if not jira_key:
        return False
    with db.connection() as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO tickets (task_id, label, url, system)
               VALUES (?, ?, ?, 'jira')""",
            (task.id, jira_key, jira_url_for(jira_key)),
        )
        conn.commit()
        return cur.rowcount > 0


# =============================================================================
# Project due date
# =============================================================================


def set_project_due_date(
    db, task_id: int, due_date: Optional[str], refresh_mirror: bool = True
):
    """Set or clear (None) the project-level due date on the tasks row."""
    due_date = _validate_due_date(due_date)
    with db.connection() as conn:
        cur = conn.execute(
            "UPDATE tasks SET due_date = ? WHERE id = ?", (due_date, task_id)
        )
        conn.commit()
        if cur.rowcount == 0:
            raise ValueError(f"Task not found: {task_id}")
    if refresh_mirror:
        note = (
            f"Project due date set: {due_date}" if due_date
            else "Project due date cleared"
        )
        refresh_context_mirror(db, task_id, change_note=note)
    return due_date


# =============================================================================
# Waiting-on rows (file-canonical; this is the identity-keyed resolve)
# =============================================================================


def resolve_waiting_on_row(
    db, task_id: int, row_index: int, expected_row: dict[str, str],
    outcome: Optional[str] = None,
) -> dict[str, str]:
    """Remove one Waiting-on row from a project's context file, by identity.

    The dashboard resolves the row a user is looking at, so the match is
    positional AND verified: the row at ``row_index`` must still match every
    identity cell present in ``expected_row`` (what / who / since), or this
    raises ``WaitingOnConflict`` and writes nothing. (The save flow's
    ``waiting_on_resolve`` matches on a substring instead - the right shape
    for "the egress one came back" in conversation, the wrong one for a
    button press.)

    Verifying ALL THREE matters: two rows reading "access approval" from two
    different people is the normal shape of a blockers table, so checking
    only ``what`` would resolve one person's row while recording the
    outcome the user typed about the other.

    Same locked read-modify-write as the mirror: the resolution lands in
    Recent Changes, the cap rolls to the journal, Last Updated is stamped.
    Returns the removed row.
    """
    if not expected_row.get("what", "").strip():
        raise ValueError("expected_row must carry the row's 'what' text")
    task = db.get_task(task_id)
    if task is None:
        raise ValueError(f"Task not found: {task_id}")
    context_path = _context_path_for_task(task)
    if context_path is None:
        raise ValueError("project has no context file")

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    journal_path = context_health.derive_journal_path(context_path)
    removed: dict[str, str] = {}

    def transform(content: str) -> tuple[str, Optional[str]]:
        rows = context_health.parse_waiting_on(content)
        if not 0 <= row_index < len(rows):
            raise WaitingOnConflict(
                "that Waiting-on row is no longer in the table"
            )
        row = rows[row_index]
        # who owes what, since when. `gates` is descriptive, so editing what a
        # row blocks must not stop the row from being resolved.
        for cell in ("what", "who", "since"):
            expected = expected_row.get(cell)
            if expected is None:
                continue
            if row.get(cell, "").strip() != expected.strip():
                raise WaitingOnConflict(
                    "the Waiting-on table changed since this view was loaded"
                )
        removed.update(row)
        content = context_health.replace_waiting_on_table(
            content, rows[:row_index] + rows[row_index + 1:]
        )
        note = f"Resolved (was waiting on {row['who']}): {row['what']}"
        if outcome:
            note += f" - {outcome}"
        content = context_health.prepend_recent_changes(
            content, timestamp, f"- {_sanitize_note(note)}"
        )
        content, journal_append, _ = context_health.split_recent_changes_for_cap(
            content, journal_path.name
        )
        return _stamp_last_updated(content, timestamp), journal_append

    # A raise inside the transform happens BEFORE any write, so a conflict
    # leaves the file untouched and only releases the lock.
    _atomic_update_context_with_journal(context_path, journal_path, transform)
    return removed


# =============================================================================
# Health
# =============================================================================


def pm_health_warnings(db, task_id: int, now: Optional[date] = None) -> list[str]:
    """DB-side health warnings for one project; empty list means healthy.

    The PM companion to ``context_health.check_context_health`` (which
    stays file-only and DB-free): overdue open action items, a project due
    date within DUE_SOON_DAYS (or past), and items open longer than
    STALE_OPEN_NO_DUE_DAYS with no due date. Report-only strings, same
    contract as the file-side checker.
    """
    today = now or date.today()
    warnings: list[str] = []

    task = db.get_task(task_id)
    if task is not None and task.due_date:
        due = _parse_iso_date(task.due_date)
        if due is not None:
            days = (due - today).days
            if days < 0:
                warnings.append(f"project due date {task.due_date} passed {-days} days ago")
            elif days <= DUE_SOON_DAYS:
                warnings.append(f"project due date {task.due_date} is in {days} days")

    for item in list_action_items(db, task_id=task_id, status="open", project_statuses=()):
        what = item.what[:60]
        if item.due_date:
            due = _parse_iso_date(item.due_date)
            if due is not None and due < today:
                warnings.append(
                    f"action item {item.label} '{what}' ({item.assignee}) is "
                    f"{(today - due).days} days overdue"
                )
        else:
            created = _parse_iso_date(item.created_at[:10])
            if created is not None:
                age = (today - created).days
                if age > STALE_OPEN_NO_DUE_DAYS:
                    warnings.append(
                        f"action item {item.label} '{what}' open {age} days "
                        f"with no due date"
                    )
    return warnings


# =============================================================================
# Context-file mirror
# =============================================================================


def _context_path_for_task(task) -> Optional[Path]:
    """The project's context file, or None (non-coding task, moved dir).

    Same candidate order the rest of the codebase uses: canonical
    ``active/<name>`` first, then the stored ``full_path`` (which can be a
    legacy shape and goes stale on completion), then ``completed/<name>``.
    """
    import missioncache_db  # late import: MISSIONCACHE_ROOT is monkeypatched in tests

    root = missioncache_db.MISSIONCACHE_ROOT
    candidates = [
        root / "active" / task.name,
        root / task.full_path,
        root / "completed" / task.name,
    ]
    for task_dir in candidates:
        if not task_dir.is_dir():
            continue
        named = task_dir / f"{task.name}-context.md"
        if named.exists():
            return named
        for match in sorted(task_dir.glob("*-context.md")):
            return match
        legacy = task_dir / "context.md"
        if legacy.exists():
            return legacy
    return None


def _render_action_items_body(items: list[ActionItem], today: date) -> str:
    lines = [MANAGED_MARKER, ""]
    if not items:
        lines.append("None currently.")
        return "\n".join(lines)
    esc = context_health._escape_cell
    lines.append("| ID | What | From | Owner | Due | Status |")
    lines.append("|----|------|------|-------|-----|--------|")
    for item in items:
        due = item.due_date or ""
        if item.is_overdue(today):
            due += " (overdue)"
        lines.append(
            f"| {item.label} | {esc(item.what)} | {esc(item.requested_by or '')} "
            f"| {esc(item.assignee)} | {due} | {item.status} |"
        )
    return "\n".join(lines)


def _render_stakeholders_body(stakeholders: list[Stakeholder]) -> str:
    lines = [MANAGED_MARKER, ""]
    if not stakeholders:
        lines.append("None currently.")
        return "\n".join(lines)
    esc = context_health._escape_cell
    lines.append("| Name | Role | Notes |")
    lines.append("|------|------|-------|")
    for s in stakeholders:
        lines.append(f"| {esc(s.name)} | {esc(s.role or '')} | {esc(s.notes or '')} |")
    return "\n".join(lines)


def _render_tickets_body(tickets: list[Ticket]) -> str:
    lines = [MANAGED_MARKER, ""]
    if not tickets:
        lines.append("None currently.")
        return "\n".join(lines)
    esc = context_health._escape_cell
    lines.append("| Ticket | System | Status | Link |")
    lines.append("|--------|--------|--------|------|")
    for t in tickets:
        link = f"[link]({t.url})" if t.url else ""
        lines.append(
            f"| {esc(t.label)} | {esc(t.system or '')} | {esc(t.status or '')} | {link} |"
        )
    return "\n".join(lines)


def _insert_section_before(content: str, section_text: str, anchors: tuple) -> str:
    """Insert a full ``## `` section before the first anchor section found.

    Generalization of ``insert_waiting_on_before_next_steps``; appends at
    EOF when no anchor exists.
    """
    section_block = section_text.rstrip() + "\n\n"
    for anchor in anchors:
        span = context_health._section_span(content, anchor)
        if span is not None:
            pos = span[0]
            prefix = content[:pos]
            if prefix and not prefix.endswith("\n\n"):
                prefix = prefix.rstrip("\n") + "\n\n"
            return prefix + section_block + content[pos:]
    return content.rstrip("\n") + "\n\n" + section_block


def _apply_managed_section(
    content: str, name: str, body: str, create: bool
) -> tuple[str, Optional[str]]:
    """Replace the body of a managed section; create it (anchored) if allowed.

    Returns ``(new_content, skip_reason)``. A section is only replaced when its
    current body carries ``MANAGED_MARKER`` - a section of the same name that
    the user wrote by hand is left untouched and reported, because these are
    exactly the headings ("Tickets", "Stakeholders", "Action Items") a person
    keeps by hand in a PM-flavoured context file and the content is not
    recoverable once overwritten.

    ``create=False`` keeps projects with no PM data free of empty sections: an
    absent section stays absent until the first real row arrives.
    """
    span = context_health._section_span(content, name)
    if span is not None:
        if MANAGED_MARKER not in content[span[1]:span[2]]:
            return content, (
                f"'## {name}' already exists and was not written by MissionCache, "
                f"so it was left untouched - rename it to let MissionCache manage "
                f"that section"
            )
        return context_health.replace_section_body(content, name, body), None
    if not create:
        return content, None
    section_text = f"## {name}\n\n{body}\n"
    return _insert_section_before(content, section_text, _SECTION_ANCHORS[name]), None


def _apply_due_header(content: str, due_date: Optional[str]) -> str:
    """Set, update, or remove the ``**Due:**`` header line (header region only)."""
    masked = context_health.mask_fences(content)
    first_h2 = context_health._H2_LINE_RE.search(masked)
    header_end = first_h2.start() if first_h2 else len(content)
    header = content[:header_end]
    match = _DUE_LINE_RE.search(header)
    if due_date is None:
        if match is None:
            return content
        # Drop the line plus its trailing newline.
        line_end = match.end()
        if line_end < len(header) and header[line_end] == "\n":
            line_end += 1
        return header[: match.start()] + header[line_end:] + content[header_end:]
    due_line = f"**Due:** {due_date}"
    if match is not None:
        new_header = header[: match.start()] + due_line + header[match.end() :]
        return new_header + content[header_end:]
    anchor = _LAST_UPDATED_LINE_RE.search(header)
    if anchor is not None:
        insert_at = anchor.end()
        return (
            header[:insert_at] + f"\n{due_line}" + header[insert_at:]
            + content[header_end:]
        )
    # Defensive: no Last Updated header - insert after the H1 line.
    first_newline = header.find("\n")
    insert_at = first_newline + 1 if first_newline != -1 else len(header)
    return (
        header[:insert_at] + f"\n{due_line}\n" + header[insert_at:]
        + content[header_end:]
    )


def _stamp_last_updated(content: str, timestamp: str) -> str:
    match = _LAST_UPDATED_LINE_RE.search(context_health.mask_fences(content))
    if match is None:
        return content
    return (
        content[: match.start()]
        + f"**Last Updated:** {timestamp}"
        + content[match.end() :]
    )


def refresh_context_mirror(
    db, task_id: int, change_note: Optional[str] = None
) -> dict[str, Any]:
    """Re-render the managed sections of the project's context file.

    One locked read-modify-write: managed sections replaced (created only
    when they have data), ``**Due:**`` header synced, optional
    ``change_note`` prepended into Recent Changes (with cap/journal
    enforcement), ``**Last Updated:**`` stamped. Never raises on a missing
    file - the DB write already succeeded and is the source of truth; the
    mirror is best-effort by design and reports what it did.

    The DB rows are read INSIDE the lock: reading them outside let two
    concurrent writers render from different snapshots and the later write
    win with fewer rows, leaving the file silently under-reporting.

    Returns ``{"updated": bool, "reason"?: str, "warnings": [str]}``. Callers
    should surface ``warnings`` - it carries the hand-written-section skips,
    which are the one case where the file deliberately disagrees with the DB.
    """
    task = db.get_task(task_id)
    if task is None:
        return {"updated": False, "reason": f"task {task_id} not found", "warnings": []}
    context_path = _context_path_for_task(task)
    if context_path is None:
        reason = (
            "no context file (non-coding task)"
            if task.task_type == "non-coding"
            else "project directory or context file is missing"
        )
        return {"updated": False, "reason": reason, "warnings": []}

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    journal_path = context_health.derive_journal_path(context_path)
    warnings: list[str] = []

    def transform(content: str) -> tuple[str, Optional[str]]:
        # Read the DB under the lock so the render matches the file we write.
        ensure_jira_ticket_migrated(db, task)
        items = list_action_items(db, task_id=task_id, project_statuses=())
        today = date.today()
        cutoff = date.fromordinal(today.toordinal() - RECENT_DONE_DAYS).isoformat()
        visible = [
            i for i in items
            if i.status == "open" or (i.completed_at or "") >= cutoff
        ]
        stakeholders = list_stakeholders(db, task_id)
        tickets = list_tickets(db, task_id)

        for name, body, create in (
            ("Action Items", _render_action_items_body(visible, today), bool(visible)),
            ("Stakeholders", _render_stakeholders_body(stakeholders), bool(stakeholders)),
            ("Tickets", _render_tickets_body(tickets), bool(tickets)),
        ):
            content, skipped = _apply_managed_section(content, name, body, create)
            if skipped:
                warnings.append(skipped)

        content = _apply_due_header(content, task.due_date)
        journal_append = None
        if change_note:
            content = context_health.prepend_recent_changes(
                content, timestamp, f"- {_sanitize_note(change_note)}"
            )
            content, journal_append, _ = context_health.split_recent_changes_for_cap(
                content, journal_path.name
            )
        content = _stamp_last_updated(content, timestamp)
        return content, journal_append

    # Deliberately broad. The DB write that preceded this call is already
    # committed and is the source of truth, so NOTHING here may propagate -
    # a caller that believes a mirror failure and retries would create a
    # duplicate row (action_items has no unique key). OSError alone was not
    # enough: read_text() raises UnicodeDecodeError (a ValueError) on a
    # non-UTF-8 byte, and any context_health transform helper can raise too.
    # Logged with a traceback so a real bug in here is still diagnosable.
    try:
        _atomic_update_context_with_journal(context_path, journal_path, transform)
    except Exception as e:
        logger.warning(
            "Context mirror refresh failed for task %s (DB write stands)",
            task_id, exc_info=True,
        )
        return {
            "updated": False,
            "reason": f"mirror write failed: {e.__class__.__name__}: {e}",
            "warnings": warnings,
        }
    return {"updated": True, "path": str(context_path), "warnings": warnings}

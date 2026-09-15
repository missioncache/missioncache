"""Cross-project attention rollup - the one implementation behind two surfaces.

``build_portfolio`` is the body of the dashboard's ``GET /api/today``, moved
here so the MCP server (which imports only missioncache_db) and the dashboard
compute the same ranking and the same counts from one function. The chat
brief and the Attention view must never disagree on what is urgent, and two
implementations of this rollup would drift the first week.

The returned shape is the endpoint's contract, unchanged by the move. It is
pinned by the dashboard's ``test_pm_endpoints.py`` (48 tests through
``get_today``), which is the safety net for this extraction.

Split by WHO OWES THE WORK, not by which record type stored it. ``on_me`` is
the reader's own open action items, bucketed overdue / due-soon / other.
``on_others`` unifies the two records that both mean "someone else owes
something": open action items assigned to anyone but the reader
(``kind: commitment``) and Waiting-on rows (``kind: blocker``). Those two stay
separate at rest - different shapes, different lifecycles, one DB-canonical
and one file-canonical - and are joined only here, for reading.

Sorting across that seam uses one scale, "days past the line": a commitment's
line is its due date, a blocker's line is the 7-day staleness threshold.
"Late" follows the same scale on BOTH sides, so ``counts.overdue`` and
``overdue_count`` count everything of the reader's that has crossed its line.
A colleague sitting on an ask is THEIR latency and counts into ``stale``,
never into overdue: red is reserved for "you are late".

Three things cannot cross the package boundary and arrive as parameters:
``user_name`` (the dashboard caches it behind a tri-state its tests pin),
``ticket_url`` (resolved from dashboard-only settings), and ``today``
(testability).

Root resolution is a late ``import missioncache_db`` at call time, never
``from . import MISSIONCACHE_ROOT``: that would snapshot the value at import
and silently read the developer's real data inside every sandboxed test.
"""

import re
import subprocess
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Optional

# Names in a Waiting-on `who` cell that mean the reader themselves. Such a row
# is work they owe, so it belongs on their side of the split even though it
# lives in the same markdown table as everyone else's. This decides `mine`,
# which drives on_me vs on_others, open_count, overdue_count and at_risk, so a
# wrong answer here files the reader's own work under "waiting on other
# people" and undercounts what they owe.
WHO_SELF_BASE = frozenset({"me", "myself"})

# Below this a bullet orients nobody - "- done", "- wip" - and filling the
# row's one line with it is worse than leaving it empty.
LEFT_OFF_MIN_CHARS = 12

# A blocker's line: a Waiting-on row older than this has gone stale.
STALE_HORIZON_DAYS = 7

_COMPLETED_RE = re.compile(r"^\s*-\s*\[x\]", re.MULTILINE | re.IGNORECASE)
_PENDING_RE = re.compile(r"^\s*-\s*\[\s*\]", re.MULTILINE)
# Numbered checklist items only. An unnumbered item counts toward the totals
# above but has no identity, so it can never become `next_up`; the dashboard's
# parser reports that gap as `unnumbered_count` on purpose.
_NUMBERED_ITEM_RE = re.compile(
    r"^\s*-\s*\[([ xX])\]\s*"
    r"(\d+(?:\.\d+[a-zA-Z]?)?[a-zA-Z]?)\.\s*"
    r"(.+?)$",
    re.MULTILINE,
)
_MODE_MARKER_RE = re.compile(r"`\[(auto|inter)(?::depends=([^\]]+))?\]`\s*$")


# ---------------------------------------------------------------------------
# Pieces the dashboard used to keep privately
# ---------------------------------------------------------------------------


def git_display_name() -> Optional[str]:
    """The first token of git's global user.name, or None when it is unset.

    Deliberately NOT cached and deliberately lets ``OSError``,
    ``subprocess.SubprocessError`` and ``UnicodeDecodeError`` propagate. The
    dashboard wraps this in a tri-state cache its tests pin: "git says there
    is no name" caches as a settled answer, a transient failure does not.
    Collapsing that here would turn a one-off timeout into a permanently
    nameless greeting. ``subprocess.run`` is called by attribute so a test
    that patches the shared module object reaches it.
    """
    out = subprocess.run(
        ["git", "config", "--global", "user.name"],
        capture_output=True,
        text=True,
        timeout=2,
        # A non-ASCII name on a machine whose preferred encoding is not UTF-8
        # made text=True raise while decoding and 500'd the whole endpoint.
        # A mangled name beats no dashboard.
        encoding="utf-8",
        errors="replace",
    )
    if out.returncode != 0:
        return None
    first = out.stdout.strip().split()
    # First token only: "Tomer Brami" greets as "Tomer".
    return first[0] if first else None


def self_names(user_name: Optional[str]) -> frozenset[str]:
    """The names that mean "the person reading this".

    Built from the same source as the greeting so the two cannot disagree.
    ``owner_names`` lowercases and takes first names, so the first token of
    git's user.name is the right thing to compare against.
    """
    return (WHO_SELF_BASE | {user_name.lower()}) if user_name else WHO_SELF_BASE


def owner_names(raw: Optional[str]) -> list[str]:
    """Owner first-names in a hand-typed `who` cell, lowercased, in order.

    The cell is prose, so it needs normalizing before it can group at all:
    drop parentheticals ("Ilya (on vacation til Wed)" -> ilya), drop a
    trailing note after " - ", then split a multi-owner cell on / + and comma.
    Returns [] when nothing parses, so callers fall back to the raw string
    rather than inventing an owner.
    """
    text = re.sub(r"\([^)]*\)", " ", raw or "")
    text = text.split(" - ")[0]
    names = []
    for part in re.split(r"[/+,]", text):
        words = part.split()
        if words:
            names.append(words[0].lower())
    return names


def left_off(content: Optional[str]) -> Optional[str]:
    """The newest Recent Changes bullet: what was last actually done here.

    Recent Changes is the per-session journal, its newest subsection is the
    last session, and its first real bullet is that session's headline.
    Bullets are written by hand and by the save flow, so the leading noise
    comes in several shapes and all of it is stripped: a heading nested inside
    the bullet, a date with or without a time, a bare time, paired bold
    markers. The floor drops fragments like "- done" that orient nobody.
    """
    from missioncache_db import context_health

    if not content:
        return None
    subs = context_health.parse_recent_changes_subsections(content)
    if not subs:
        return None
    _heading, body = subs[0]
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith(("- ", "* ")):
            continue
        text = line[2:].strip()
        text = re.sub(r"^#{1,6}\s*", "", text)
        text = re.sub(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2})?\s*[-:]?\s*", "", text)
        text = re.sub(r"^\d{2}:\d{2}:?\s*", "", text)
        text = text.replace("**", "").replace("__", "")
        if len(text) > LEFT_OFF_MIN_CHARS:
            return text
    return None


def _checklist_progress(root: Path, repo_path: str, task_full_path: str) -> dict:
    """``{completed_count, total_count, completion_pct, next_up}`` for one project.

    Reproduces exactly the two behaviors of the dashboard's full progress
    parser that these four fields depend on, and nothing else. It is NOT
    ``TaskDB.parse_missioncache_progress``: that one counts with an unanchored
    regex, returns ``has_docs: False`` rather than zeros, and has no notion of
    numbered items, so it cannot produce ``next_up``. Zeros on any miss.
    """
    zeros = {"completed_count": 0, "total_count": 0, "completion_pct": 0, "next_up": None}
    if not task_full_path:
        return zeros

    task_name = Path(task_full_path).name
    candidates = [root / "active" / task_name, root / "completed" / task_name]
    if repo_path:
        repo = Path(repo_path)
        candidates.append(repo / task_full_path)
        if "dev/active/" in task_full_path:
            candidates.append(repo / "dev" / "completed" / task_name)
        elif "dev/completed/" in task_full_path:
            candidates.append(repo / "dev" / "active" / task_name)

    task_dir = next((c for c in candidates if c.exists()), None)
    if task_dir is None:
        return zeros

    tasks_file = next(
        (
            c
            for c in (task_dir / f"{task_dir.name}-tasks.md", task_dir / "tasks.md")
            if c.exists()
        ),
        None,
    )
    if tasks_file is None:
        return zeros
    try:
        content = tasks_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return zeros
    if not content:
        return zeros

    completed = len(_COMPLETED_RE.findall(content))
    total = completed + len(_PENDING_RE.findall(content))

    next_up = None
    for match in _NUMBERED_ITEM_RE.finditer(content):
        if match.group(1).lower() == "x":
            continue
        rest = match.group(3).strip()
        marker = _MODE_MARKER_RE.search(rest)
        title = (rest[: marker.start()] if marker else rest).strip()
        if title:
            next_up = title
            break

    return {
        "completed_count": completed,
        "total_count": total,
        "completion_pct": int((completed / total) * 100) if total else 0,
        "next_up": next_up,
    }


# ---------------------------------------------------------------------------
# The rollup
# ---------------------------------------------------------------------------


def build_portfolio(
    db,
    *,
    today: Optional[date] = None,
    user_name: Optional[str] = None,
    ticket_url: Optional[Callable[[Optional[str]], Optional[str]]] = None,
) -> dict[str, Any]:
    """Cross-project attention data in one call. The ``/api/today`` contract.

    ``user_name`` is the reader's first name for the greeting and for deciding
    which Waiting-on rows are theirs; None falls back to git, swallowing any
    failure because a greeting must never be why a brief fails to render.
    ``ticket_url`` maps a legacy ``jira_key`` to a URL; None leaves that field
    None, which the chat surface never needs.
    """
    import missioncache_db
    from missioncache_db import context_health, pm_items

    root = missioncache_db.MISSIONCACHE_ROOT
    today = today or date.today()
    horizon = STALE_HORIZON_DAYS

    if user_name is None:
        try:
            user_name = git_display_name()
        except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
            user_name = None
    who_self = self_names(user_name)

    def _age_days(iso: Optional[str]) -> Optional[int]:
        if not iso:
            return None
        try:
            return (today - date.fromisoformat(iso[:10])).days
        except ValueError:
            return None

    mine_overdue: list[dict] = []
    mine_due_soon: list[dict] = []
    mine_other: list[dict] = []
    on_others: list[dict] = []
    per_task: dict[int, dict] = {}

    # The project block mirrors the two lists: open/overdue count the on_me
    # list, on_others/stale_on_others count the on_others list.
    def _stats_for(task_id: int) -> dict:
        return per_task.setdefault(
            task_id,
            {"open_count": 0, "overdue_count": 0, "on_others_count": 0,
             "stale_on_others_count": 0},
        )

    for item in pm_items.list_action_items(db, status="open"):
        stats = _stats_for(item.task_id)
        overdue = item.is_overdue(today)
        if item.assignee.strip().lower() == "me":
            stats["open_count"] += 1
            if overdue:
                stats["overdue_count"] += 1
            # `kind` on this side too, so the merged My-work list has a uniform
            # discriminator instead of the client inferring it from `id`.
            entry = {**asdict(item), "label": item.label, "overdue": overdue,
                     "mine": True, "kind": "commitment"}
            due_in = None
            if item.due_date:
                age = _age_days(item.due_date)
                due_in = None if age is None else -age
            if overdue:
                mine_overdue.append(entry)
            elif due_in is not None and due_in <= horizon:
                mine_due_soon.append(entry)
            else:
                mine_other.append(entry)
        else:
            stats["on_others_count"] += 1
            if overdue:
                stats["stale_on_others_count"] += 1
            on_others.append({
                "kind": "commitment",
                "what": item.what,
                "who": item.assignee,
                # Both keys must be present on EVERY row in this list: the
                # group aggregation and the by-person rollup read them
                # unconditionally.
                "who_primary": (owner_names(item.assignee) or [item.assignee or ""])[0].title(),
                "mine": False,
                "why": item.source or item.requested_by or "",
                "project_name": item.project_name,
                "task_id": item.task_id,
                "days_past_line": _age_days(item.due_date) if overdue else None,
                "age_days": _age_days(item.created_at),
                "id": item.id,
                "label": item.label,
                "due_date": item.due_date,
                "since": None,
                "row_index": None,
            })

    projects = []
    repos_by_id = {repo.id: repo for repo in db.get_repos()}
    # Two task rows can point at ONE project: a fork and its parent, or a stale
    # row left by a rename that falls back to the name and reads its twin's
    # context file. Iterate freshest-first and skip a task whose context file
    # another task already consumed, so rows are not counted twice and the
    # surviving twin is the one worked most recently.
    seen_context: dict[str, str] = {}
    active_tasks = sorted(
        db.get_active_tasks(),
        key=lambda t: (t.last_worked_on or "", t.id),
        reverse=True,
    )
    for task in active_tasks:
        stats = _stats_for(task.id)
        due_date = task.due_date
        days_to_due = None
        if due_date:
            age = _age_days(due_date)
            days_to_due = None if age is None else -age

        # Waiting-on rows are file-side truth; read the context file once and
        # take both the display rows and the staleness count from it.
        content = None
        is_duplicate = False
        for candidate in (root / "active" / task.name, root / task.full_path):
            ctx = candidate / f"{task.name}-context.md"
            if ctx.exists():
                key = str(ctx.resolve())
                if key in seen_context:
                    is_duplicate = True
                    break
                seen_context[key] = task.name
                try:
                    content = ctx.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    content = None
                break
        if content:
            for row_index, row in enumerate(context_health.parse_waiting_on(content)):
                # A row with no What has no identity: it cannot be resolved and
                # it cannot be read, and it would otherwise parse as a blocker
                # aged from an empty Since and fabricate a permanent at-risk.
                if not row["what"].strip():
                    continue
                since = context_health.parse_since_date(row["since"])
                age = (today - since).days if since is not None else None
                stale = age is not None and age > horizon
                # A row whose `who` names the reader is work they owe, so it
                # counts on their side. It stays in this list because the
                # resolve path addresses it by task_id + row_index either way.
                names = owner_names(row["who"])
                mine = any(n in who_self for n in names)
                if mine:
                    stats["open_count"] += 1
                    # A row naming the reader that has crossed its line is late
                    # by this rollup's own contract, and has to count as such.
                    if stale:
                        stats["overdue_count"] += 1
                else:
                    stats["on_others_count"] += 1
                    if stale:
                        stats["stale_on_others_count"] += 1
                on_others.append({
                    "kind": "blocker",
                    "what": row["what"],
                    "who": row["who"],
                    # Falls back to the raw cell so an unparseable owner is
                    # still shown, never silently dropped or merged.
                    "who_primary": names[0].title() if names else (row["who"] or ""),
                    "mine": mine,
                    "why": row["gates"],
                    "project_name": task.name,
                    "task_id": task.id,
                    "days_past_line": (age - horizon) if stale else None,
                    "age_days": age,
                    "id": None,
                    "label": None,
                    "due_date": None,
                    "since": row["since"],
                    # Identity handle for the resolve endpoint: the table has
                    # no per-row id, so position is verified against `what`.
                    "row_index": row_index,
                })

        at_risk = bool(
            stats["overdue_count"]
            or stats["stale_on_others_count"]
            or (days_to_due is not None and days_to_due <= horizon)
        )
        if not is_duplicate and (stats["open_count"] or stats["on_others_count"] or due_date):
            repo = repos_by_id.get(task.repo_id)
            progress = _checklist_progress(root, repo.path if repo else "", task.full_path or "")
            # First ticket only. A row has space for one reference, and tickets
            # are ordered by insertion so the first is the project's primary.
            tickets = pm_items.list_tickets(db, task.id)
            ticket = tickets[0] if tickets else None
            projects.append({
                "task_id": task.id,
                "name": task.name,
                "due_date": due_date,
                "days_to_due": days_to_due,
                "open_count": stats["open_count"],
                "overdue_count": stats["overdue_count"],
                "on_others_count": stats["on_others_count"],
                "stale_on_others_count": stats["stale_on_others_count"],
                "at_risk": at_risk,
                "completed_count": progress["completed_count"],
                "total_count": progress["total_count"],
                "completion_pct": progress["completion_pct"],
                "next_up": progress["next_up"],
                "category": task.category,
                # Derived from the context file already read above, so it costs
                # no extra I/O. One line of where you left off, one of what is next.
                "left_off": left_off(content),
                # The tickets table is the current home; task.jira_key is the
                # legacy column that migrates into a row on the project's first
                # PM mutation. Prefer the row, fall back.
                "ticket_label": ticket.label if ticket else task.jira_key,
                "ticket_url": (
                    ticket.url if ticket else (ticket_url(task.jira_key) if ticket_url else None)
                ),
                # Recency, so a project whose asks rot while worked (chase) is
                # distinguishable from one whose asks rot because it stopped
                # (close). None when the project was never worked.
                "last_worked_on": task.last_worked_on,
                "days_since_worked": _age_days(task.last_worked_on),
            })

    # Grouped by project, because a flat cross-project list of everything
    # anyone owes runs to dozens of rows and stops being readable. GROUPS sort
    # by how much has gone stale, most first; WITHIN a group, most-past-the-line
    # first, so each project's most urgent row is the one you see at its top.
    groups: dict[int, dict] = {}
    for row in on_others:
        groups.setdefault(row["task_id"], {
            "task_id": row["task_id"],
            "project_name": row["project_name"],
            "rows": [],
        })["rows"].append(row)

    on_others_groups = []
    for group in groups.values():
        group["rows"].sort(key=lambda r: (
            r["days_past_line"] is None,
            -(r["days_past_line"] or 0),
            -(r["age_days"] or 0),
        ))
        # The counts describe the OTHER-PEOPLE side only, because that is what
        # the meter means. The reader's own rows stay in `rows` but must not
        # inflate a bar that reads as "someone else is slow".
        theirs = [r for r in group["rows"] if not r["mine"]]
        ages = [r["age_days"] for r in theirs if r["age_days"] is not None]
        group["count"] = len(theirs)
        group["mine_count"] = len(group["rows"]) - len(theirs)
        group["stale_count"] = sum(1 for r in theirs if r["days_past_line"] is not None)
        group["newest_age_days"] = min(ages) if ages else None
        on_others_groups.append(group)
    # Freshest reply first, projects nobody has answered at all last. Volume
    # then name break ties so the order is total rather than falling out of
    # dict insertion order.
    on_others_groups.sort(key=lambda g: (
        g["newest_age_days"] is None,
        g["newest_age_days"] or 0,
        -g["stale_count"],
        -g["count"],
        g["project_name"] or "",
    ))

    # Urgency order: the reader's own lateness first (the only thing that earns
    # red), then an imminent project deadline, then how much has gone idle with
    # someone else, then volume, then name so the order is stable. This is the
    # contract pinned by TestTodayEndpoint::test_at_risk_projects_sort_first.
    projects.sort(key=lambda p: (
        -p["overdue_count"],
        p["days_to_due"] is None,
        p["days_to_due"] if p["days_to_due"] is not None else 0,
        -p["stale_on_others_count"],
        -p["on_others_count"],
        -p["open_count"],
        p["name"],
    ))

    return {
        "generated_at": datetime.now().isoformat(),
        "user_name": user_name,
        "on_me": {
            "overdue": mine_overdue,
            "due_soon": mine_due_soon,
            "other_open": mine_other,
        },
        "on_others": on_others_groups,
        "counts": {
            # on_me counts the reader's action items PLUS the waiting-on rows
            # whose `who` names them; on_others and stale exclude those, so the
            # two sides add up and neither claims the same row.
            "on_me": (len(mine_overdue) + len(mine_due_soon) + len(mine_other)
                      + sum(1 for r in on_others if r["mine"])),
            # The reader's late work lives in TWO lists, so both are summed.
            "overdue": (len(mine_overdue)
                        + sum(1 for r in on_others
                              if r["mine"] and r["days_past_line"] is not None)),
            "on_others": sum(1 for r in on_others if not r["mine"]),
            "on_others_projects": len(on_others_groups),
            "stale": sum(1 for r in on_others
                         if not r["mine"] and r["days_past_line"] is not None),
        },
        "projects": projects,
    }


def scope_portfolio(result: dict[str, Any], project_names: set[str]) -> dict[str, Any]:
    """Drop every entry outside ``project_names`` and recompute ``counts``.

    A POST-filter on purpose. Filtering the task loop instead would leave
    out-of-scope projects populating ``on_me`` and ``counts`` while absent from
    ``projects`` - exactly the counts-vs-screen mismatch the fork/rename dedup
    inside ``build_portfolio`` exists to kill - and would change which twin
    that dedup keeps. The full result stays a pure move; scope is an additive
    layer on top of it.
    """
    keep = set(project_names)
    projects = [p for p in result["projects"] if p["name"] in keep]
    groups = [g for g in result["on_others"] if g["project_name"] in keep]
    on_me = {
        bucket: [row for row in rows if row.get("project_name") in keep]
        for bucket, rows in result["on_me"].items()
    }
    rows = [row for group in groups for row in group["rows"]]
    counts = {
        "on_me": sum(len(v) for v in on_me.values()) + sum(1 for r in rows if r["mine"]),
        "overdue": (len(on_me["overdue"])
                    + sum(1 for r in rows if r["mine"] and r["days_past_line"] is not None)),
        "on_others": sum(1 for r in rows if not r["mine"]),
        "on_others_projects": len(groups),
        "stale": sum(1 for r in rows if not r["mine"] and r["days_past_line"] is not None),
    }
    return {**result, "on_me": on_me, "on_others": groups, "counts": counts,
            "projects": projects}


# ---------------------------------------------------------------------------
# Change detection
# ---------------------------------------------------------------------------


def portfolio_watermark(db) -> str:
    """A short token that changes whenever the portfolio could have changed.

    The thing that makes every live mechanism cheap: a 15-minute brief loop
    and a 30-second dashboard stream both compare this before recomputing,
    and an unchanged token means the 200ms-plus rollup and pid scan are
    skipped entirely. It is a change DETECTOR, not a content hash: newest
    ``tasks.updated_at``, newest action-item write, newest ``project_state``
    activity, and the newest mtime among active context and tasks files.
    Any one of them moving is enough. Never raises; a missing store simply
    contributes nothing.
    """
    import hashlib
    import sqlite3

    import missioncache_db

    parts: list[str] = []

    def _scalar(conn, sql: str) -> str:
        try:
            row = conn.execute(sql).fetchone()
        except sqlite3.Error:
            return ""
        return str(row[0]) if row and row[0] is not None else ""

    try:
        with db.connection() as conn:
            parts.append(_scalar(conn, "SELECT MAX(updated_at) FROM tasks"))
            parts.append(_scalar(conn, "SELECT MAX(created_at) FROM action_items"))
            parts.append(_scalar(conn, "SELECT MAX(completed_at) FROM action_items"))
            parts.append(_scalar(conn, "SELECT COUNT(*) FROM action_items WHERE status = 'open'"))
    except Exception:  # noqa: BLE001 - detector only, must never raise
        pass

    state_db = missioncache_db.HOOKS_STATE_DB_PATH
    if state_db.exists():
        try:
            conn = sqlite3.connect(state_db, timeout=1.0)
            try:
                parts.append(_scalar(conn, "SELECT MAX(updated_at) FROM project_state"))
                parts.append(_scalar(conn, "SELECT COUNT(*) FROM project_state"))
            finally:
                conn.close()
        except sqlite3.Error:
            pass

    newest = 0.0
    active = missioncache_db.MISSIONCACHE_ROOT / "active"
    try:
        for project_dir in active.iterdir():
            if not project_dir.is_dir():
                continue
            for name in (f"{project_dir.name}-context.md", f"{project_dir.name}-tasks.md"):
                try:
                    newest = max(newest, (project_dir / name).stat().st_mtime)
                except OSError:
                    continue
    except OSError:
        pass
    parts.append(f"{newest:.0f}")

    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]

"""The lead board: where every project stands, and what each live session is doing.

One function, ``build_lead_board``, feeds the board the MissionCache mod draws
above the lead session's prompt (through the MCP tool ``get_lead_board``), and
any other surface that wants the same picture. The unit is
the PROJECT: progress, the task a session marked active, the last significant
change, the last save, what waits on others, and what is next. A live
session's own state (working, needs you, idle) rides along on its project.

Session state comes from ``agent_status`` in hooks-state.db, written by the
mod in every session through ``report_agent_status`` only when the state
changes. It is a different table from the statusline's ``session_state`` on
purpose: that one is owned by the statusline and rewritten on every render.

The free text kept here (the prompt a session was given, the tool it is
running, the start of its last answer) stays in the local hooks-state.db and
is cleaned of control characters and clipped before it is stored. Everything
the board returns is cleaned again on the way out, since the context files and
the event log it also reads can carry them too.

Root and DB paths are read through a late ``import missioncache_db`` at call
time, as in ``portfolio``, so tests that redirect them are honoured.
"""

import json
import logging
import math
import re
import sqlite3
import subprocess
import sys
import unicodedata
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

AGENT_STATES = ("working", "idle", "needs_you", "compacting")

# Server-side caps for the free text a session reports. The board shows one
# line of each, and nothing longer is worth keeping in a state table.
PROMPT_MAX_CHARS = 80
NOW_MAX_CHARS = 60
ANSWER_MAX_CHARS = 60
REASON_MAX_CHARS = 80

NEEDS_YOU_MAX = 8
# A project is active while a session is live on it or it changed this recently.
# Hours, not "today", so a day that runs past midnight stays one day.
ACTIVE_HOURS = 12
PICKS_MAX = 3
PICK_WHAT_MAX_CHARS = 120
PICK_PROJECT_MAX_CHARS = 80
PICK_WHY_MAX_CHARS = 160
EVENTS_PAGE = 200
# A relay is one instruction from the lead to another session, sent as one line
# that the receiving session's mod recognises and shows the person to confirm.
RELAY_PREFIX = "mc-relay/1"
RELAY_TEXT_MAX_CHARS = 500
RELAY_OUTCOMES = ("ran", "dropped", "failed")

# iTerm2's TERM_SESSION_ID is "w0t1p0:<UUID>", and the UUID is the session's
# AppleScript `unique id`.
_TERM_UUID_RE = re.compile(r"([0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12})$")

_FOCUS_SCRIPT = (
    "on run argv",
    "set target to item 1 of argv",
    'tell application "iTerm2"',
    "repeat with w in windows",
    "repeat with t in tabs of w",
    "repeat with s in sessions of t",
    "if unique id of s is target then",
    "select w",
    "tell t to select",
    "tell s to select",
    "activate",
    'return "focused"',
    "end if",
    "end repeat",
    "end repeat",
    "end repeat",
    "end tell",
    'return "not found"',
    "end run",
)


def _one_line(value: str) -> str:
    """``value`` as one line without control or format characters.

    Controls (Cc: C0 with ESC, DEL, C1) are what a terminal acts on. Format
    characters (Cf: the bidi overrides and isolates, zero-width marks, tag
    characters) reorder or hide text, and iTerm2 draws the isolates as a
    replacement glyph that scrambles the whole row. Neither may reach the
    lead's screen from text another session wrote.
    """
    return " ".join(
        "".join(" " if unicodedata.category(ch) in ("Cc", "Cf") else ch for ch in value).split()
    )


def clean_text(value: str | None, limit: int) -> str | None:
    """One clean line (see ``_one_line``), at most ``limit`` characters."""
    if value is None:
        return None
    line = _one_line(str(value))
    if not line:
        return None
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def _scrub(value: Any) -> Any:
    """Every string inside ``value`` as one clean line, lengths untouched."""
    if isinstance(value, str):
        return _one_line(value)
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def _state_conn():
    import missioncache_db

    path = missioncache_db.HOOKS_STATE_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=2.0)
    missioncache_db.init_hooks_state_db_schema(conn)
    return conn


# ---------------------------------------------------------------------------
# Session state (written by the mod in every session)
# ---------------------------------------------------------------------------


def report_agent_status(
    session_id: str,
    state: str,
    *,
    needs_reason: str | None = None,
    prompt: str | None = None,
    now: str | None = None,
    context_pct: int | None = None,
    cost_usd: float | None = None,
    turns_today: int | None = None,
    last_answer: str | None = None,
    last_turn_ms: int | None = None,
    compacted: bool = False,
) -> None:
    """Upsert one session's state.

    Raises ValueError on a bad session id, an unknown state or a cost that is
    not a finite number. A DB error propagates.

    ``state_since`` moves only when ``state`` changes, so the board can say how
    long a session has needed you. A field left None keeps its stored value,
    except three that follow the state: ``needs_reason`` is kept only while
    the state is ``needs_you``, ``now`` (the long-running tool) is cleared when
    the session goes idle, and going idle stamps ``last_turn_at``.
    """
    import missioncache_db

    if not missioncache_db._is_valid_session_id(session_id):
        raise ValueError(f"not a session id: {session_id!r}")
    if state not in AGENT_STATES:
        raise ValueError(f"state must be one of {', '.join(AGENT_STATES)}, got {state!r}")
    if cost_usd is not None and not math.isfinite(float(cost_usd)):
        raise ValueError(f"cost_usd must be a finite number, got {cost_usd!r}")
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = {
        "session_id": session_id,
        "state": state,
        "state_since": stamp,
        "needs_reason": clean_text(needs_reason, REASON_MAX_CHARS) if state == "needs_you" else None,
        "prompt": clean_text(prompt, PROMPT_MAX_CHARS),
        "now": clean_text(now, NOW_MAX_CHARS),
        "context_pct": None if context_pct is None else max(0, min(100, int(context_pct))),
        "cost_usd": None if cost_usd is None else float(cost_usd),
        "turns_today": None if turns_today is None else max(0, int(turns_today)),
        "last_turn_at": stamp if state == "idle" else None,
        "last_answer": clean_text(last_answer, ANSWER_MAX_CHARS),
        "last_turn_ms": None if last_turn_ms is None else max(0, int(last_turn_ms)),
        "compacted_at": stamp if compacted else None,
        "version": 1,
        "updated_at": stamp,
    }
    keep = ("prompt", "context_pct", "cost_usd", "turns_today", "last_turn_at",
            "last_answer", "last_turn_ms", "compacted_at")
    updates = [
        "state = excluded.state",
        ("state_since = CASE WHEN agent_status.state = excluded.state "
        "THEN agent_status.state_since ELSE excluded.state_since END"),
        "needs_reason = excluded.needs_reason",
        # The running tool belongs to the turn: idle clears it.
        ("now = CASE WHEN excluded.state = 'idle' THEN NULL "
        "ELSE COALESCE(excluded.now, agent_status.now) END"),
        "updated_at = excluded.updated_at",
        # Every write moves the watermark, even two in the same second.
        "version = agent_status.version + 1",
        *(f"{col} = COALESCE(excluded.{col}, agent_status.{col})" for col in keep),
    ]
    columns = ", ".join(row)
    conn = _state_conn()
    try:
        conn.execute(
            f"INSERT INTO agent_status ({columns}) VALUES ({', '.join('?' for _ in row)}) "
            f"ON CONFLICT(session_id) DO UPDATE SET {', '.join(updates)}",
            tuple(row.values()),
        )
        conn.commit()
    finally:
        conn.close()


def agent_status_rows(session_ids) -> dict[str, dict[str, Any]]:
    """``{session_id: row}`` for the given sessions; missing ones are absent.
    A DB error propagates."""
    import missioncache_db

    ids = [sid for sid in session_ids if sid]
    if not ids or not missioncache_db.HOOKS_STATE_DB_PATH.exists():
        return {}
    conn = _state_conn()
    try:
        cursor = conn.execute(
            f"SELECT * FROM agent_status WHERE session_id IN ({', '.join('?' for _ in ids)})",
            ids,
        )
        names = [c[0] for c in cursor.description]
        return {r[0]: dict(zip(names, r)) for r in cursor.fetchall()}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Jumping to a session's terminal
# ---------------------------------------------------------------------------


def terminal_for_session(session_id: str) -> str | None:
    """The iTerm2 session UUID the statusline last saw this session in, or None.

    ``term_sessions`` is keyed by the terminal, so a session that moved tabs
    has several rows: the newest one is where it is now. A DB error propagates.
    """
    import missioncache_db

    if not missioncache_db._is_valid_session_id(session_id):
        return None
    if not missioncache_db.HOOKS_STATE_DB_PATH.exists():
        return None
    conn = _state_conn()
    try:
        row = conn.execute(
            "SELECT term_session_id FROM term_sessions WHERE session_id = ? "
            "ORDER BY updated_at DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()
    match = _TERM_UUID_RE.search(row[0]) if row else None
    return match.group(1) if match else None


def focus_available() -> bool:
    return sys.platform == "darwin"


def focus_terminal(session_id: str) -> dict[str, Any]:
    """Bring the iTerm2 tab running ``session_id`` to the front (macOS only).

    The UUID goes to osascript as an argument, never into the script text.
    On failure ``reason`` carries osascript's own words, so a first-run
    Automation refusal is visible rather than swallowed. Every caller (the MCP
    tool, the dashboard) gets the session id checked here.
    """
    import missioncache_db

    if not missioncache_db._is_valid_session_id(session_id):
        return {"success": False, "reason": "not a session id"}
    if not focus_available():
        return {"success": False, "reason": "jumping to a tab works only with iTerm2 on macOS"}
    try:
        uuid = terminal_for_session(session_id)
    except sqlite3.Error as e:
        return {"success": False, "reason": f"could not read the terminal map: {e}"}
    if uuid is None:
        return {"success": False, "reason": "no terminal recorded for this session"}
    args = ["osascript"]
    for line in _FOCUS_SCRIPT:
        args += ["-e", line]
    try:
        out = subprocess.run(args + [uuid], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        return {"success": False, "reason": str(e)}
    answer = out.stdout.strip()
    if out.returncode != 0 or answer != "focused":
        return {"success": False, "reason": out.stderr.strip() or answer or "osascript failed"}
    return {"success": True}


# ---------------------------------------------------------------------------
# The board
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Today's picks (written by the lead session at each brief)
# ---------------------------------------------------------------------------


def set_lead_picks(session_id: str, items: list) -> list[dict] | None:
    """Replace today's picks with ``items``, at most three, and return them clean.

    Each item is ``{what, project?, why?}``. An empty list clears the picks.
    Raises ValueError on a malformed list, an item without ``what``, or a bad
    session id. A DB error propagates.

    Only the designated lead writes them, checked inside the same statement:
    a lead replaced while its brief was finishing cannot overwrite its
    successor's picks. Returns None when ``session_id`` is not the lead.

    One row, overwritten: the board shows the newest picks and their time, and
    a reader decides from that time whether they are stale.
    """
    import missioncache_db

    if not missioncache_db._is_valid_session_id(session_id):
        raise ValueError(f"not a session id: {session_id!r}")
    if not isinstance(items, list) or len(items) > PICKS_MAX:
        raise ValueError(f"picks must be a list of at most {PICKS_MAX} items")
    clean = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("every pick must be an object with a 'what'")  # noqa: TRY004 - one error type for every bad input
        what = clean_text(item.get("what"), PICK_WHAT_MAX_CHARS)
        if not what:
            raise ValueError("every pick needs a 'what'")
        clean.append({
            "what": what,
            "project": clean_text(item.get("project"), PICK_PROJECT_MAX_CHARS),
            "why": clean_text(item.get("why"), PICK_WHY_MAX_CHARS),
        })
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _state_conn()
    try:
        cursor = conn.execute(
            "INSERT INTO lead_picks (id, items, written_at) SELECT 1, ?, ? "
            "WHERE EXISTS (SELECT 1 FROM lead_session WHERE session_id = ?) "
            "ON CONFLICT(id) DO UPDATE SET items = excluded.items, written_at = excluded.written_at",
            (json.dumps(clean), stamp, session_id),
        )
        conn.commit()
    finally:
        conn.close()
    return clean if cursor.rowcount else None


def lead_picks() -> dict[str, Any] | None:
    """``{written_at, items}`` of the newest picks, or None before the first.

    A DB error propagates. A row whose items do not parse reads as None.
    """
    import missioncache_db

    if not missioncache_db.HOOKS_STATE_DB_PATH.exists():
        return None
    conn = _state_conn()
    try:
        row = conn.execute("SELECT items, written_at FROM lead_picks WHERE id = 1").fetchone()
    finally:
        conn.close()
    if not row:
        return None
    try:
        items = json.loads(row[0])
    except ValueError:
        logger.warning("lead_picks: stored items do not parse")
        return None
    return {"written_at": row[1], "items": items if isinstance(items, list) else []}


def lead_board_watermark(db, lead_session_id: str | None = None) -> str:
    """``portfolio_watermark`` plus the session-state writes, the active-task
    pointers and the lead, which decides whose session and events are hidden.

    Kept out of ``portfolio_watermark`` itself: the dashboard's stream compares
    that token, and folding a table written on every turn of every session into
    it would rebuild the dashboard's rollup each time anyone typed.
    """
    import hashlib

    import missioncache_db
    from missioncache_db import portfolio

    status = ""
    if missioncache_db.HOOKS_STATE_DB_PATH.exists():
        try:
            conn = _state_conn()
            try:
                # SUM(version) grows on every write, COUNT and MAX(updated_at)
                # catch a pruned row replaced by a new one.
                status = "|".join(
                    str(v) for v in conn.execute(
                        "SELECT SUM(version), COUNT(*), MAX(updated_at) FROM agent_status"
                    ).fetchone()
                )
                # The items as well as the time: two writes in one second differ.
                picked = conn.execute("SELECT written_at, items FROM lead_picks WHERE id = 1").fetchone()
                status += f"|{picked}"
            finally:
                conn.close()
        except sqlite3.Error as e:
            # A token that never repeats: the next poll rebuilds, and the
            # rebuild reports the error instead of a stale board looking fine.
            logger.warning("lead board watermark: hooks-state.db read failed: %s", e)
            status = f"error {datetime.now().timestamp()}"
    # The active-task pointers are files the MCP server rewrites, with no DB
    # trace, and the board shows them; their newest mtime has to count.
    newest = 0.0
    try:
        for pointer in missioncache_db.active_task_pointer_dir().glob("*.json"):
            newest = max(newest, pointer.stat().st_mtime)
    except OSError:
        pass
    token = f"{portfolio.portfolio_watermark(db)}|{status}|{newest:.3f}|{lead_session_id}"
    return hashlib.sha1(token.encode("utf-8")).hexdigest()[:16]


def _read_context(task) -> str | None:
    from missioncache_db import pm_items

    path = pm_items.context_path_for_task(task)
    try:
        return path.read_text(encoding="utf-8") if path else None
    except (OSError, UnicodeDecodeError):
        return None


def _active_task_number(session_id: str | None, project: str) -> str | None:
    """The first checklist item the session marked active on ``project``."""
    import json

    import missioncache_db

    if not session_id:
        return None
    path = missioncache_db.active_task_pointer_dir() / f"{session_id}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("project_name") != project:
        return None
    numbers = data.get("task_numbers")
    return str(numbers[0]) if isinstance(numbers, list) and numbers else None


_SESSION_FIELDS = (
    "state", "state_since", "needs_reason", "prompt", "now", "context_pct", "cost_usd",
    "turns_today", "last_turn_at", "last_answer", "last_turn_ms", "compacted_at", "updated_at",
)


def _session_view(live: dict, status: dict | None) -> dict[str, Any]:
    """The live session plus what it reported. A session that never reported
    (no mod, or an older Claude Code) carries None in every reported field."""
    status = status or {}
    return {
        "session_id": live["session_id"],
        "title": live["title"],
        **{field: status.get(field) for field in _SESSION_FIELDS},
    }


_STATE_RANK = {"needs_you": 0, "working": 1, "compacting": 2, "idle": 3}
_NO_STATE_RANK = len(_STATE_RANK)


def _state_rank(state: str | None) -> int:
    return _STATE_RANK.get(state, _NO_STATE_RANK)


def _project_card(task, *, root, repo, stats, waiting, sessions, statuses, last_event) -> dict:
    from missioncache_db import context_health, portfolio

    repo_path = repo.path if repo else ""
    full_path = task.full_path or ""
    content = _read_context(task)
    progress = portfolio.checklist_progress(root, repo_path, full_path)
    parsed = context_health.parse_last_updated(content) if content else None
    oldest = max(waiting, key=lambda r: r["age_days"] or 0) if waiting else None
    # The session that most needs the person speaks for the project; among
    # equals, the most recently active (the order ``sessions`` arrives in).
    session = (
        min(sessions, key=lambda s: _state_rank((statuses.get(s["session_id"]) or {}).get("state")))
        if sessions else None
    )
    number = _active_task_number(session["session_id"] if session else None, task.name)
    return {
        "name": task.name,
        "completed_count": progress["completed_count"],
        "total_count": progress["total_count"],
        "completion_pct": progress["completion_pct"],
        "next_up": progress["next_up"],
        "due_date": task.due_date,
        "days_to_due": stats.get("days_to_due"),
        "at_risk": bool(stats.get("at_risk")),
        "overdue_count": stats.get("overdue_count", 0),
        "open_count": stats.get("open_count", 0),
        "active_task": (
            {"number": number,
             "title": portfolio.checklist_item_title(root, repo_path, full_path, number)}
            if number else None
        ),
        "left_off": portfolio.left_off(content),
        "last_event": last_event,
        "last_save": parsed.strftime("%Y-%m-%d %H:%M") if parsed else None,
        "waiting": {
            "count": len(waiting),
            "oldest": (
                {"what": oldest["what"], "who": oldest["who_primary"], "age_days": oldest["age_days"]}
                if oldest else None
            ),
        },
        "session": _session_view(session, statuses.get(session["session_id"])) if session else None,
        "session_count": len(sessions),
    }


def _needs_you(live: list[dict], statuses: dict, on_me: dict) -> list[dict]:
    """Every live session waiting on the person, longest waiting first, then
    the person's overdue and due-soon items. Built from all live sessions, not
    from the cards, so a second session on one project is never hidden."""
    waiting = sorted(
        (
            (s, statuses[s["session_id"]])
            for s in live
            if (statuses.get(s["session_id"]) or {}).get("state") == "needs_you"
        ),
        key=lambda pair: pair[1]["state_since"],
    )
    items = [
        {
            "kind": "session",
            "project_name": s["project_name"],
            "what": status["needs_reason"] or "needs you",
            "since": status["state_since"],
            "session_id": s["session_id"],
            "title": s["title"],
        }
        for s, status in waiting
    ]
    for bucket in ("overdue", "due_soon"):
        items.extend(
            {
                "kind": bucket,
                "project_name": item.get("project_name"),
                "what": item["what"],
                "due_date": item.get("due_date"),
                "action_item_id": item.get("id"),
            }
            for item in on_me[bucket]
        )
    return items[:NEEDS_YOU_MAX]


def build_lead_board(
    db,
    *,
    after_id: int | None = None,
    watermark: str | None = None,
    scope: str = "focus",
    lead_session_id: str | None = None,
    caller_session_id: str | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Everything the lead board draws, in one call.

    With ``watermark`` equal to the current token nothing could have changed,
    and the answer is ``{"changed": False, "watermark": ...}`` without building
    anything. That is what makes polling every few seconds cheap.

    ``scope="focus"`` keeps projects with a live session or a change in the
    last ``ACTIVE_HOURS`` and lists the rest under ``quiet``; ``"all"`` keeps
    every active project. The lead's own session is never
    a project's session, and its own writes are left out of ``events``.
    """
    import missioncache_db
    from missioncache_db import events as events_mod
    from missioncache_db import portfolio

    if scope not in ("focus", "all"):
        raise ValueError(f"scope must be 'focus' or 'all', got {scope!r}")
    # Sampled before reading, as get_portfolio does: a write landing mid-build
    # then still moves the token for the next poll.
    mark = lead_board_watermark(db, lead_session_id)
    if watermark == mark:
        return {"changed": False, "watermark": mark}

    today = today or date.today()
    full = portfolio.build_portfolio(db, today=today)
    rollup = {p["name"]: p for p in full["projects"]}
    waiting_by_project = {
        g["project_name"]: [r for r in g["rows"] if not r["mine"]] for g in full["on_others"]
    }

    live = missioncache_db.live_sessions_all(exclude_session_id=lead_session_id)
    status_error = None
    try:
        statuses = agent_status_rows(s["session_id"] for s in live)
        picks = lead_picks()
    except sqlite3.Error as e:
        logger.warning("lead board: hooks-state.db read failed: %s", e)
        statuses, picks, status_error = {}, None, str(e)
    live_by_project: dict[str, list[dict]] = {}
    for session in live:  # already most recently active first
        live_by_project.setdefault(session["project_name"], []).append(session)

    # Sampled before the reads below, so an event written meanwhile is at worst
    # sent twice (the reader keys on id), never skipped.
    baseline = events_mod.latest_event_id(db)
    todays, _ = events_mod.list_events_page(
        db, since=today.isoformat(), limit=events_mod.MAX_LIMIT
    )
    tasks = {t.name: t for t in db.get_active_tasks()}
    latest = events_mod.latest_event_per_project(db)
    recent_since = (datetime.now() - timedelta(hours=ACTIVE_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    active = {n for n in tasks if n in live_by_project or (latest.get(n) or {}).get("created_at", "") >= recent_since}
    names = set(tasks) if scope == "all" else active
    # What the focus view leaves out, newest change first, so a reader can
    # still find it without the cost of a card per project.
    quiet = sorted(
        ({"name": n, "last_change": (latest.get(n) or {}).get("created_at")} for n in set(tasks) - active),
        key=lambda q: q["last_change"] or "", reverse=True,
    ) if scope == "focus" else []

    repos = {r.id: r for r in db.get_repos()}
    projects = [
        _project_card(
            tasks[name], root=missioncache_db.MISSIONCACHE_ROOT, repo=repos.get(tasks[name].repo_id),
            stats=rollup.get(name, {}), waiting=waiting_by_project.get(name, []),
            sessions=live_by_project.get(name, []), statuses=statuses, last_event=latest.get(name),
        )
        for name in names
    ]
    # Newest activity first, then a stable sort on what needs attention, so
    # activity breaks ties inside each attention rank.
    projects.sort(
        key=lambda p: max((p["last_event"] or {}).get("created_at") or "", p["last_save"] or ""),
        reverse=True,
    )
    projects.sort(key=lambda p: (_state_rank((p["session"] or {}).get("state")), not p["at_risk"]))

    # The cursor handed back is the newest id the reader has now been shown,
    # counted before the lead's own rows are dropped. With a cursor the page is
    # the OLDEST rows after it, so the global newest id would skip the rest.
    # Without one the reader starts from today's newest page and streams
    # forward, so ``has_more`` always means "newer rows past this page".
    if after_id is None:
        page, has_more = todays[:EVENTS_PAGE], False
        cursor = max([baseline, *(e["id"] for e in page)])
    else:
        page, has_more = events_mod.list_events_page(db, after_id=after_id, limit=EVENTS_PAGE)
        cursor = max([after_id, *(e["id"] for e in page)])
    feed = [
        {k: e[k] for k in ("id", "project", "kind", "what", "created_at")}
        for e in page
        if not lead_session_id or e["source_session"] != lead_session_id
    ]
    needs_you = _needs_you(live, statuses, full["on_me"])
    recent, _ = events_mod.list_events_page(db, since=recent_since, limit=events_mod.MAX_LIMIT)
    if picks is not None:
        picks["is_today"] = picks["written_at"].startswith(today.isoformat())

    return _scrub({
        "changed": True,
        "watermark": mark,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "projects": projects,
        "quiet": quiet,
        "needs_you": needs_you,
        # One set of numbers for every reader: the band and the Lead view show these.
        "counts": {
            "needs_you": len(needs_you),
            "working": sum(1 for s in statuses.values() if s.get("state") == "working"),
            "sessions": len(live),
            "changes": sum(1 for e in recent if not lead_session_id or e["source_session"] != lead_session_id),
        },
        "events": feed,
        "has_more": has_more,
        "last_event_id": cursor,
        "live_count": len(live),
        "lead": {
            "session_id": lead_session_id,
            "is_me": bool(lead_session_id) and lead_session_id == caller_session_id,
        },
        "focus_available": focus_available(),
        "picks": picks,
        "status_error": status_error,
    })


def prepare_relay(sender_session_id: str, project_name: str, text: str) -> dict[str, Any]:
    """The relay envelope for ``project_name``'s live session.

    The caller sends ``envelope`` to ``session_id`` (the mod with
    ``$.session.send``, a model with SendMessage to ``title``). Nothing runs on
    arrival: the receiving mod shows the text and runs it only when the person
    there picks Run. With several live sessions the most recently active wins.

    The text runs as typed, so it is refused rather than cut short (a cut can
    drop the "but not prod" that changes its meaning), and a leading ``!``,
    which would run a shell command outside the permission prompts, is refused.
    """
    import missioncache_db

    clean = _one_line(str(text))
    if not clean:
        raise ValueError("nothing to relay: the text is empty")
    if len(clean) > RELAY_TEXT_MAX_CHARS:
        raise ValueError(f"a relay is at most {RELAY_TEXT_MAX_CHARS} characters, this one is longer")
    if clean.startswith("!"):
        raise ValueError("a relay cannot start with '!', which runs a shell command")
    live = missioncache_db.live_sessions_for_project(project_name, exclude_session_id=sender_session_id)
    if not live:
        return {"success": False, "reason": f"{project_name} has no live session"}
    target = live[0]
    envelope = {
        "id": uuid.uuid4().hex[:12],
        "from": missioncache_db.LEAD_SESSION_TITLE,
        "to": target["session_id"],
        "project": project_name,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "text": clean,
    }
    return {
        "success": True,
        "session_id": target["session_id"],
        "title": target["title"],
        "envelope": f"{RELAY_PREFIX} {json.dumps(envelope, ensure_ascii=False)}",
    }


def record_relay(db, session_id: str, outcome: str, text: str) -> bool:
    """Log a relay the person in ``session_id`` ran, dropped, or could not run.

    Written by the receiving side, so the lead's feed (which leaves out the
    lead's own rows) shows it. False when the session is bound to no project.
    """
    import missioncache_db
    from missioncache_db import events

    if outcome not in RELAY_OUTCOMES:
        raise ValueError(f"outcome must be one of {', '.join(RELAY_OUTCOMES)}, got {outcome!r}")
    project = missioncache_db.bound_project_for_session(session_id)
    if not project:
        return False
    line = clean_text(text, RELAY_TEXT_MAX_CHARS) or ""
    events.record_events(db, project, [("relayed", f"relay {outcome}: {line}", None)], session_id)
    return True

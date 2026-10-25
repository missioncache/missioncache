"""Resume lists: every Claude Code session that closed, ready to come back to.

Each session gets one row in ``tasks.db`` with its name, MissionCache
project, directory and transcript. The resume command is derived on read,
never stored, and only for a session that has closed. A session bound to a
project goes to **Saved** when it closes, and so does one the person kept.
Every other session goes to **Closed**, newest ``CLOSED_CAP`` first.

Three writers, all hooks: SessionStart and UserPromptSubmit record the
session as seen (``record_seen``), SessionEnd closes it
(``close_session``). SessionEnd gets 1.5 s by default, and nothing says it
fires when a tab is closed, so it is only the fast path. ``reconcile`` runs
on every read and closes what never said goodbye: a dead or recycled pid, a
pid that now runs another session (``/clear``), or a session with no usable
pid that has not been seen for ``NO_PID_CLOSE_HOURS``.

Hooks write with a raw connection and a short timeout (``hook_connect``),
not ``TaskDB``: it waits up to 5 s on a locked database and runs the whole
schema on every connect. The dashboard and the CLI pass ``db.connection()``.
The table's DDL is ``missioncache_db.RESUME_SESSIONS_SCHEMA``.
"""

import json
import os
import re
import shlex
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

CLOSED_CAP = 500
NO_PID_CLOSE_HOURS = 24
# record_seen rewrites an unchanged open row only this often, so a prompt
# rarely takes the write lock the dashboard and other hooks also want.
SEEN_REFRESH_SECONDS = 600
NAME_MAX_CHARS = 200
# The most transcript one hook reads for titles. SessionEnd can follow a turn
# that appended megabytes, and its time budget is shared with other hooks.
TITLE_SCAN_BYTES = 4 * 1024 * 1024
# The shape MissionCache gives project names. A project name becomes a prompt
# (`/missioncache:load <project>`), so anything else never gets that far.
PROJECT_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# Claude Code session ids are UUIDs. Only such an id goes into `--resume`, so
# no stored id can ever read as a flag.
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
# C0, DEL, C1, and the bidi overrides and isolates, which can make a name
# print as something else in a terminal.
_CONTROL_RE = re.compile("[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")
_TITLE_KEYS = {"ai-title": "aiTitle", "custom-title": "customTitle"}
_STAMP = "%Y-%m-%d %H:%M:%S"
LISTS = ("saved", "closed", "all")


def _now() -> str:
    # tasks.db's local text format, compared as text (see events.py).
    return datetime.now().strftime(_STAMP)


def _project_ok(project: Optional[str]) -> bool:
    return bool(project and PROJECT_RE.match(project))


def _clean(text: object) -> Optional[str]:
    """A name fit to store and print: no control or bidi characters, at most
    ``NAME_MAX_CHARS``. None when nothing is left."""
    if not isinstance(text, str):
        return None
    text = " ".join(_CONTROL_RE.sub(" ", text).split())[:NAME_MAX_CHARS]
    return text or None


def migrate(conn: sqlite3.Connection) -> None:
    """Add the columns the DDL has and an existing table lacks."""
    from missioncache_db import RESUME_SESSIONS_SCHEMA

    have = {r[1] for r in conn.execute("PRAGMA table_info(resume_sessions)")}
    mem = sqlite3.connect(":memory:")
    try:
        mem.executescript(RESUME_SESSIONS_SCHEMA)
        wanted = mem.execute("PRAGMA table_info(resume_sessions)").fetchall()
    finally:
        mem.close()
    for _cid, name, col_type, notnull, default, _pk in wanted:
        if name not in have:
            extra = (" NOT NULL" if notnull else "") + (f" DEFAULT {default}" if default is not None else "")
            conn.execute(f"ALTER TABLE resume_sessions ADD COLUMN {name} {col_type}{extra}")


def hook_connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """A connection for a hook: short lock timeout, only this table's schema."""
    from missioncache_db import DB_PATH, RESUME_SESSIONS_SCHEMA

    path = Path(db_path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=0.3)
    conn.row_factory = sqlite3.Row
    conn.executescript(RESUME_SESSIONS_SCHEMA)
    migrate(conn)
    return conn


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def claude_session_record(pid: int) -> Optional[Dict[str, Any]]:
    """Claude Code's own ``~/.claude/sessions/<pid>.json``: the session id the
    process runs now and its name. It exists only while the process runs."""
    return _read_json(Path.home() / ".claude" / "sessions" / f"{pid}.json")


def scan_titles(path: Optional[str], offset: int, titles: Dict[str, Optional[str]]) -> int:
    """Read the ``ai-title`` and ``custom-title`` records appended to the
    transcript since ``offset`` into ``titles``, and return the new offset.

    Claude Code writes the generated title after the first prompt and the
    ``/rename`` title when it happens, and keeps neither in
    ``~/.claude/sessions``. A transcript only grows, so each call reads what
    was appended, at most ``TITLE_SCAN_BYTES``, and stops before a line still
    being written.
    """
    if not path:
        return offset
    try:
        with open(path, "rb") as f:
            if offset > os.fstat(f.fileno()).st_size:
                offset = 0  # a new file under the same name
            f.seek(offset)
            read = 0
            for line in f:
                if not line.endswith(b"\n") or (read and read + len(line) > TITLE_SCAN_BYTES):
                    break
                read += len(line)
                if b'-title"' in line:
                    _remember_title(titles, line)
            return offset + read
    except OSError:
        return offset


def _remember_title(titles: Dict[str, Optional[str]], line: bytes) -> None:
    try:
        entry = json.loads(line)
    except ValueError:
        return
    key = _TITLE_KEYS.get(entry.get("type")) if isinstance(entry, dict) else None
    if not key:
        return
    title = _clean(entry.get(key))
    if title:
        titles[entry["type"]] = title


def _snapshot(session_id: str, cwd: str, transcript_path: Optional[str],
              current: Optional[sqlite3.Row], start: bool) -> Dict[str, Any]:
    """The row's values as a hook knows them right now, keyed by column."""
    from missioncache_db import bound_project_for_session, read_session_title, session_pid_path

    def kept(column: str) -> Any:
        return current[column] if current is not None else None

    project = bound_project_for_session(session_id)
    project = project if _project_ok(project) else None
    mc_title = None
    if project:
        record = read_session_title(session_id) or {}
        if record.get("projectName") == project:
            mc_title = _clean(record.get("title"))
    pid_record = _read_json(session_pid_path(session_id)) or {}
    pid = pid_record.get("pid") if isinstance(pid_record.get("pid"), int) else None
    pid_start = pid_record.get("startTime") if isinstance(pid_record.get("startTime"), str) else None
    if not start:
        # SessionStart speaks for the process and the directory. Later hooks
        # keep what it saw, also once the pid record has been pruned.
        pid, pid_start = pid or kept("pid"), pid_start or kept("pid_start")
        cwd = kept("cwd") or cwd
    transcript_path = transcript_path or kept("transcript_path")
    titles = {"ai-title": kept("ai_title"), "custom-title": kept("custom_title")}
    offset = scan_titles(transcript_path, kept("title_offset") or 0, titles)

    cc_name, cc_named_by_user = None, False
    if pid:
        own = claude_session_record(pid) or {}
        if own.get("sessionId") == session_id:
            cc_name, cc_named_by_user = _clean(own.get("name")), own.get("nameSource") == "user"
    mc_title = mc_title or kept("mc_title")
    # A /rename wins, then the MissionCache title, then the title Claude Code
    # generated, then its derived name (`work-fe`), then the folder.
    auto_name = (
        titles["custom-title"]
        or (cc_name if cc_named_by_user else None)
        or mc_title
        or titles["ai-title"]
        or cc_name
        or _clean(os.path.basename(cwd.rstrip("/\\")))
        or cwd
    )
    return {
        "cwd": cwd,
        "transcript_path": transcript_path,
        "project_name": project,
        "mc_title": mc_title,
        "ai_title": titles["ai-title"],
        "custom_title": titles["custom-title"],
        "title_offset": offset,
        "auto_name": auto_name,
        "pid": pid,
        "pid_start": pid_start,
    }


def _unchanged_and_fresh(row: Optional[sqlite3.Row], values: Dict[str, Any]) -> bool:
    if row is None or row["closed_at"] is not None:
        return False
    # The offset moves with every turn and is not worth a write by itself.
    if any(values[k] is not None and row[k] != values[k] for k in values if k != "title_offset"):
        return False
    try:
        seen = datetime.strptime(row["last_seen_at"], _STAMP)
    except (TypeError, ValueError):
        return False
    return 0 <= (datetime.now() - seen).total_seconds() < SEEN_REFRESH_SECONDS


def record_seen(
    conn: sqlite3.Connection,
    session_id: str,
    cwd: str,
    transcript_path: Optional[str] = None,
    commit: bool = True,
    start: bool = False,
) -> bool:
    """Record the session as open and seen now. Returns whether it is
    tracked (False for the lead or a malformed id).

    Seeing a closed session again reopens it: ``claude --resume`` keeps the
    session id. An open row with nothing new is rewritten at most every
    ``SEEN_REFRESH_SECONDS``. ``start`` is SessionStart: its directory and
    pid replace the stored ones, even with no pid, so a resume in a process
    the hooks cannot resolve stops pointing at the old one.
    """
    from missioncache_db import _is_valid_session_id, lead_session_id

    if not _is_valid_session_id(session_id) or not cwd or session_id == lead_session_id():
        return False
    current = conn.execute("SELECT * FROM resume_sessions WHERE session_id = ?", (session_id,)).fetchone()
    values = _snapshot(session_id, cwd, transcript_path or None, current, start)
    if not start and _unchanged_and_fresh(current, values):
        return True
    columns = list(values)
    conn.execute(
        f"""
        INSERT INTO resume_sessions (session_id, {", ".join(columns)}, first_seen_at, last_seen_at)
        VALUES (:session_id, {", ".join(":" + c for c in columns)}, :now, :now)
        ON CONFLICT(session_id) DO UPDATE SET
            cwd = excluded.cwd,
            transcript_path = excluded.transcript_path,
            project_name = COALESCE(excluded.project_name, project_name),
            mc_title = excluded.mc_title,
            ai_title = excluded.ai_title,
            custom_title = excluded.custom_title,
            title_offset = excluded.title_offset,
            auto_name = excluded.auto_name,
            pid = excluded.pid,
            pid_start = excluded.pid_start,
            last_seen_at = excluded.last_seen_at,
            closed_at = NULL,
            close_reason = NULL
        """,
        {**values, "session_id": session_id, "now": _now()},
    )
    if commit:
        conn.commit()
    return True


def hook_record(
    session_id: str,
    cwd: Optional[str],
    transcript_path: Optional[str] = None,
    close_reason: Optional[str] = None,
    start: bool = False,
) -> None:
    """The whole hook-side write: record the session as seen and, from
    SessionEnd, close it, in one transaction. Never raises: a missed write is
    redone by the next prompt, or by ``reconcile`` on the next read.

    Sessions MissionCache Auto starts are not listed: each iteration is a
    headless run nobody comes back to.
    """
    if os.environ.get("MISSIONCACHE_AUTO_MODE"):
        return
    try:
        conn = hook_connect()
        try:
            tracked = record_seen(conn, session_id, cwd or "", transcript_path, commit=False, start=start)
            if tracked and close_reason is not None:
                close_session(conn, session_id, close_reason, commit=False)
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"<!-- missioncache: resume list write failed: {e} -->", file=sys.stderr)


def close_session(
    conn: sqlite3.Connection,
    session_id: str,
    reason: Optional[str],
    closed_at: Optional[str] = None,
    commit: bool = True,
    expect_seen: Optional[str] = None,
) -> bool:
    """Close the row and place it in Saved or Closed. Returns whether it
    closed.

    Saved takes a session with a project, or one already kept. It replaces
    the closed Saved rows of the same project and MissionCache title: their
    hand-given name and pin move to this row, so a renamed entry keeps its
    name and gets the new resume command, and they drop to Closed as plain
    history. An open row is never replaced.

    ``expect_seen`` is for ``reconcile``, which decides a session is gone
    before it writes: the row closes only if it is still open and nobody
    has seen it since.
    """
    row = conn.execute(
        "SELECT project_name, mc_title, saved FROM resume_sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        return False
    project, mc_title, saved = row[0], row[1], row[2]
    saved = 1 if saved or _project_ok(project) else 0
    now = _now()
    peers: List[sqlite3.Row] = []
    if saved and project and mc_title:
        peers = conn.execute(
            "SELECT session_id, custom_name, pinned FROM resume_sessions "
            "WHERE saved = 1 AND closed_at IS NOT NULL AND project_name = ? AND mc_title = ? "
            "AND session_id != ? ORDER BY saved_at DESC",
            (project, mc_title, session_id),
        ).fetchall()
    custom_name = next((p[1] for p in peers if p[1]), None)
    pinned = int(any(p[2] for p in peers))
    closed = conn.execute(
        """
        UPDATE resume_sessions SET closed_at = ?, close_reason = ?, saved = ?,
            saved_at = CASE WHEN ? THEN ? ELSE saved_at END,
            custom_name = COALESCE(custom_name, ?), pinned = MAX(pinned, ?)
        WHERE session_id = ? AND (? IS NULL OR (closed_at IS NULL AND last_seen_at = ?))
        """,
        (closed_at or now, reason, saved, saved, now, custom_name, pinned, session_id,
         expect_seen, expect_seen),
    ).rowcount > 0
    if closed and peers:
        conn.executemany(
            "UPDATE resume_sessions SET saved = 0, pinned = 0, custom_name = NULL WHERE session_id = ?",
            [(p[0],) for p in peers],
        )
    if commit:
        conn.commit()
    return closed


def _gone_at(row: sqlite3.Row, now: datetime) -> Optional[str]:
    """When an open row's session ended without saying so, or None while
    it may still run."""
    from missioncache_db import pid_alive

    alive = pid_alive(row["pid"], row["pid_start"])
    if alive is False:
        return now.strftime(_STAMP)
    if alive is True:
        own = claude_session_record(row["pid"])
        if own and isinstance(own.get("sessionId"), str):
            # Same process, another session: /clear or /resume.
            return now.strftime(_STAMP) if own["sessionId"] != row["session_id"] else None
    # No pid to go by, or a live one that says nothing about its session.
    try:
        seen = datetime.strptime(row["last_seen_at"], _STAMP)
    except (TypeError, ValueError):
        return now.strftime(_STAMP)
    return row["last_seen_at"] if now - seen > timedelta(hours=NO_PID_CLOSE_HOURS) else None


def _projects_readable() -> bool:
    return (Path.home() / ".claude" / "projects").is_dir()


def transcript_state(path: Optional[str], projects_readable: Optional[bool] = None) -> Optional[bool]:
    """True when the transcript exists, False when it is gone, None when
    that cannot be told (no path, or the projects folder is unreadable)."""
    if not path:
        return None
    if not (_projects_readable() if projects_readable is None else projects_readable):
        return None
    try:
        os.stat(path)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return None


def reconcile(conn: sqlite3.Connection) -> None:
    """Close what ended without SessionEnd, drop Closed rows that can no
    longer be resumed, and cap Closed. Saved rows are never dropped here.

    Every process and file check runs before the first write, so the write
    lock the hooks also need is held only for the writes.
    """
    now = datetime.now()
    gone = [
        (row["session_id"], stamp, row["last_seen_at"])
        for row in conn.execute(
            "SELECT session_id, pid, pid_start, last_seen_at FROM resume_sessions WHERE closed_at IS NULL"
        ).fetchall()
        if (stamp := _gone_at(row, now))
    ]
    readable = _projects_readable()
    lost = [
        (r[0],)
        for r in conn.execute(
            "SELECT session_id, transcript_path FROM resume_sessions "
            "WHERE saved = 0 AND closed_at IS NOT NULL AND transcript_path IS NOT NULL"
        ).fetchall()
        if transcript_state(r[1], readable) is False
    ]
    closed_count = conn.execute(
        "SELECT COUNT(*) FROM resume_sessions WHERE saved = 0 AND closed_at IS NOT NULL"
    ).fetchone()[0]
    if not gone and not lost and closed_count <= CLOSED_CAP:
        return
    try:
        for session_id, stamp, seen in gone:
            close_session(conn, session_id, "gone", closed_at=stamp, commit=False, expect_seen=seen)
        conn.executemany("DELETE FROM resume_sessions WHERE session_id = ?", lost)
        conn.execute(
            "DELETE FROM resume_sessions WHERE saved = 0 AND closed_at IS NOT NULL AND session_id NOT IN "
            "(SELECT session_id FROM resume_sessions WHERE saved = 0 AND closed_at IS NOT NULL "
            "ORDER BY closed_at DESC LIMIT ?)",
            (CLOSED_CAP,),
        )
        conn.commit()
    except Exception:
        # The dashboard keeps its connection: a write transaction left open
        # would lock out every hook write after this read.
        conn.rollback()
        raise


def resume_argv(row: Dict[str, Any], transcript: Optional[bool]) -> Optional[List[str]]:
    """The ``claude`` arguments that bring the session back, run in its
    ``cwd``, or None.

    A transcript Claude Code already deleted cannot be resumed. A project
    session then loads its project instead.
    """
    if transcript is False:
        project = row.get("project_name")
        return ["claude", f"/missioncache:load {project}"] if _project_ok(project) else None
    if not _UUID_RE.match(row["session_id"]):
        return None
    return ["claude", "--resume", row["session_id"]]


def _ps_quote(arg: str) -> str:
    return arg if re.fullmatch(r"[A-Za-z0-9._/:=-]+", arg) else "'" + arg.replace("'", "''") + "'"


def resume_command(row: Dict[str, Any], transcript: Optional[bool], windows: Optional[bool] = None) -> Optional[str]:
    """``resume_argv`` as one line to paste in a terminal, with the change of
    directory in front: POSIX shell, or PowerShell on Windows."""
    argv = resume_argv(row, transcript)
    if not argv:
        return None
    if os.name == "nt" if windows is None else windows:
        cwd = row["cwd"].replace("'", "''")
        return f"Set-Location -LiteralPath '{cwd}'; {' '.join(_ps_quote(a) for a in argv)}"
    return f"cd {shlex.quote(row['cwd'])} && {shlex.join(argv)}"


def _present(row: sqlite3.Row, projects_readable: Optional[bool] = None) -> Dict[str, Any]:
    out = dict(row)
    out["name"] = out.get("custom_name") or out["auto_name"]
    out["open"] = out.get("closed_at") is None
    out["list"] = "saved" if out["saved"] else "closed"
    transcript = transcript_state(out.get("transcript_path"), projects_readable)
    out["transcript_gone"] = transcript is False
    # A session still running is not resumed: two processes would write
    # one transcript.
    out["argv"] = None if out["open"] else resume_argv(out, transcript)
    out["command"] = None if out["open"] else resume_command(out, transcript)
    out["origin"] = "missioncache" if _project_ok(out.get("project_name")) else "claude"
    out["pinned"] = bool(out["pinned"])
    out["saved"] = bool(out["saved"])
    return out


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def list_sessions(
    conn: sqlite3.Connection, which: str = "all", query: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Saved and Closed rows, Saved first. An open Saved row shows only when
    pinned: while it runs it belongs to the live sessions, not this list.
    The lead session is never listed while it is the lead."""
    from missioncache_db import lead_session_id

    if which not in LISTS:
        raise ValueError(f"list must be one of {', '.join(LISTS)}")
    reconcile(conn)
    where = ["(saved = 1 AND (closed_at IS NOT NULL OR pinned = 1)) OR (saved = 0 AND closed_at IS NOT NULL)",
             "session_id != ?"]
    params: List[Any] = [lead_session_id() or ""]
    if which == "saved":
        where.append("saved = 1")
    elif which == "closed":
        where.append("saved = 0")
    if query and query.strip():
        like = f"%{_escape_like(query.strip())}%"
        where.append(
            "(COALESCE(custom_name, auto_name) LIKE ? ESCAPE '\\' OR mc_title LIKE ? ESCAPE '\\' "
            "OR project_name LIKE ? ESCAPE '\\' OR cwd LIKE ? ESCAPE '\\')"
        )
        params += [like] * 4
    rows = conn.execute(
        "SELECT * FROM resume_sessions WHERE " + " AND ".join(f"({w})" for w in where)
        + " ORDER BY saved DESC, COALESCE(closed_at, last_seen_at) DESC",
        params,
    ).fetchall()
    readable = _projects_readable()
    return [_present(r, readable) for r in rows]


def get_session(conn: sqlite3.Connection, session_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM resume_sessions WHERE session_id = ?", (session_id,)).fetchone()
    return _present(row) if row else None


def find(conn: sqlite3.Connection, ref: str) -> List[Dict[str, Any]]:
    """Rows a person's reference names: a full id, an id prefix of six or
    more characters, or an exact name (any case). Saved first. The lead
    session is left out, as in ``list_sessions``."""
    from missioncache_db import lead_session_id

    ref = (ref or "").strip()
    if not ref:
        return []
    reconcile(conn)
    rows = conn.execute(
        "SELECT * FROM resume_sessions WHERE session_id != ? AND (session_id = ? "
        "OR (length(?) >= 6 AND session_id LIKE ? ESCAPE '\\') "
        "OR lower(COALESCE(custom_name, auto_name)) = lower(?)) "
        "ORDER BY saved DESC, COALESCE(closed_at, last_seen_at) DESC",
        (lead_session_id() or "", ref, ref, _escape_like(ref) + "%", ref),
    ).fetchall()
    exact = [r for r in rows if r["session_id"] == ref]
    return [_present(r) for r in (exact or rows)]


def _update(conn: sqlite3.Connection, sql: str, params: tuple) -> bool:
    changed = conn.execute(sql, params).rowcount > 0
    conn.commit()
    return changed


def keep(conn: sqlite3.Connection, session_id: str) -> bool:
    """Move a row to Saved. Returns False when there is no such row."""
    return _update(
        conn,
        "UPDATE resume_sessions SET saved = 1, saved_at = CASE WHEN saved = 0 THEN ? ELSE saved_at END "
        "WHERE session_id = ?",
        (_now(), session_id),
    )


def set_pinned(conn: sqlite3.Connection, session_id: str, pinned: bool) -> bool:
    """Pin a row so it stays listed in Saved while its session runs."""
    return _update(
        conn,
        "UPDATE resume_sessions SET pinned = ?, saved = CASE WHEN ? THEN 1 ELSE saved END, "
        "saved_at = CASE WHEN ? AND saved = 0 THEN ? ELSE saved_at END WHERE session_id = ?",
        (int(pinned), int(pinned), int(pinned), _now(), session_id),
    )


def rename(conn: sqlite3.Connection, session_id: str, name: Optional[str]) -> bool:
    """Give a row a name of its own. An empty name goes back to the
    automatic one."""
    name = (name or "").strip()
    if _CONTROL_RE.search(name):
        raise ValueError("name must not contain control characters")
    if len(name) > NAME_MAX_CHARS:
        raise ValueError(f"name must be at most {NAME_MAX_CHARS} characters")
    return _update(conn, "UPDATE resume_sessions SET custom_name = ? WHERE session_id = ?",
                   (name or None, session_id))


def forget(conn: sqlite3.Connection, session_id: str) -> bool:
    """Delete a row. The session itself is untouched."""
    return _update(conn, "DELETE FROM resume_sessions WHERE session_id = ?", (session_id,))

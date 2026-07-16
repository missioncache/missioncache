#!/usr/bin/env python3
"""
SessionStart hook - Auto-detect active task for the current directory.

Outputs context to help Claude resume work on an active task.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Maximum age for a cwd-session pointer to still be trusted as a "previous
# session at this cwd" breadcrumb. Older than this and we treat the cwd as
# a fresh start to avoid resurrecting bindings from sessions abandoned long
# ago. 24h is wide enough to cover overnight resumes but tight enough that
# the binding still reflects recent intent.
_PICKUP_MAX_AGE_SECONDS = 24 * 60 * 60

# Defensive ceiling for a session_id read out of the cwd-session pointer JSON
# before it is bound to a SQL parameter. The Claude-issued session_id is a
# UUID (~36 chars). 256 is generous enough to never reject a legitimate id
# while preventing a corrupt pointer with a multi-megabyte string from
# trickling into the DB and bloating it.
_MAX_PREV_SESSION_ID_LEN = 256

# Charset for session_ids accepted at the stdin boundary. Claude Code emits
# UUIDs (alphanumeric + hyphens). Validating at get_session_context's exit
# defends downstream filename interpolation in write_session_project against
# path traversal (CWE-22) and bounds the value flowing into DB rows + JSON
# pointer values. Underscore is permitted because some test/dev contexts use
# it in synthetic ids.
_SESSION_ID_CHARSET_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Window for treating another session's transcript as "currently active" in
# the parallel-session collision check. Claude Code's session transcripts
# (``~/.claude/projects/<cwd-key>/<sid>.jsonl``) are appended on every
# user/assistant turn, so a fresh mtime is a strong "this session is alive
# right now" signal. 10 minutes is wide enough to catch an idle but open
# session (user stepped away mid-task) without misfiring on sessions that
# finished hours ago.
_PARALLEL_THRESHOLD_SECONDS = 10 * 60

# Per-session process records, keyed by session id, used to tell a sibling
# session that is still running apart from one that was just closed. Transcript
# mtime alone cannot make that distinction: a session closed seconds ago (e.g.
# /clear then relaunch, or quit-and-reopen in the same cwd) leaves a transcript
# whose mtime is just as fresh as a session that is genuinely still alive. The
# recorded process pid resolves the ambiguity - a dead pid proves the session
# is gone.
_SESSION_PID_DIR = Path.home() / ".claude" / "hooks" / "state" / "session-pids"

# How far up the process tree to climb when resolving the Claude Code session
# process. The hook runs as a short-lived ``python3`` descendant, usually under
# a transient ``sh -c`` / ``zsh -c`` wrapper, under ``claude``; a handful of
# levels covers every observed launch shape. Bounded so a pathological tree can
# never spin.
_SESSION_PID_WALK_MAX_DEPTH = 12


def _is_valid_session_id(value: object) -> bool:
    """True if ``value`` is a plausible session id (UUID-charset, bounded length).

    Used to validate session_id inputs from BOTH stdin and the
    ``CLAUDE_SESSION_ID`` env var before they flow into filenames, DB
    rows, or JSON pointer values. Rejects None, non-strings, empty
    strings, anything containing path separators, control characters,
    or non-ASCII, and anything over ``_MAX_PREV_SESSION_ID_LEN``.
    """
    return (
        isinstance(value, str)
        and 0 < len(value) <= _MAX_PREV_SESSION_ID_LEN
        and bool(_SESSION_ID_CHARSET_RE.fullmatch(value))
    )

# Bundled missioncache-db path for marketplace installs (no system pip install).
_BUNDLED_MISSIONCACHE_DB = Path(__file__).resolve().parent.parent / "missioncache-db"
if _BUNDLED_MISSIONCACHE_DB.is_dir() and str(_BUNDLED_MISSIONCACHE_DB) not in sys.path:
    sys.path.insert(0, str(_BUNDLED_MISSIONCACHE_DB))


OWNERSHIP_MARKER = "<!-- missioncache-plugin:managed"


def install_bundled_rules() -> None:
    """Install plugin rules into ~/.claude/rules/ without clobbering user edits.

    Marketplace installs have no external bootstrap step, so this hook is how
    rule files reach ~/.claude/rules/. We write-if-different so plugin updates
    propagate automatically, but only for files that are demonstrably plugin-
    owned. Ownership is signaled by an HTML-comment marker on the first line
    of the source file (`OWNERSHIP_MARKER`); the destination is updated only
    when it is missing, is a legacy symlink from setup.sh, or already starts
    with the same marker. A user who removes the marker from their installed
    copy takes ownership of that file and the hook stops touching it.
    """
    src_dir = Path(__file__).resolve().parent.parent / "rules"
    if not src_dir.is_dir():
        return
    dst_dir = Path.home() / ".claude" / "rules"
    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
        for src in src_dir.glob("*.md"):
            new_content = src.read_text()
            if not new_content.startswith(OWNERSHIP_MARKER):
                # Source file isn't marked plugin-managed; skip it entirely.
                continue
            dst = dst_dir / src.name
            if dst.is_symlink():
                # Legacy symlink from setup.sh - replace with a real file so
                # the marker-based ownership check works going forward.
                dst.unlink()
            elif dst.exists():
                existing = dst.read_text()
                if not existing.startswith(OWNERSHIP_MARKER):
                    # User has taken ownership (removed the marker). Leave alone.
                    continue
                if existing == new_content:
                    # Already up to date.
                    continue
            dst.write_text(new_content)
    except OSError:
        pass


def write_term_session_mapping(session_id: str) -> None:
    """Write terminal-to-session mapping for mid-session lookups.

    NOTE: CLAUDE_SESSION_ID differs from the session_id in Claude Code's
    statusline JSON. The statusline hook overwrites this mapping with
    the correct JSON session_id on first render. This initial write
    serves as a placeholder until that happens.
    """
    term_id = os.environ.get("TERM_SESSION_ID") or os.environ.get("WT_SESSION")
    if not term_id or not session_id:
        return

    term_dir = Path.home() / ".claude" / "hooks" / "state" / "term-sessions"
    term_dir.mkdir(parents=True, exist_ok=True)

    mapping_file = term_dir / term_id
    mapping_file.write_text(session_id)


def _read_cwd_pointer_sid(cwd: Path) -> str | None:
    """Return the validated sessionId from the cwd-session pointer file.

    The pointer at ``~/.claude/hooks/state/cwd-session/<cwd-key>.json``
    holds the session_id of whoever last wrote to this cwd. On a normal
    resume, that is the session being resumed FROM, which Claude Code
    re-issues under a NEW session_id. Used by ``main()`` to exclude that
    prior session from ``_detect_parallel_sessions``'s result - without
    this exclusion, the resumed-from session's still-fresh transcript
    would be misclassified as a "parallel" session and the collision
    warning would fire spuriously on every normal resume within the
    freshness window.

    Returns None when the pointer is missing, stale (>24h), or corrupt.
    """
    cwd_key = str(cwd).replace("/", "-")
    pointer_file = (
        Path.home() / ".claude" / "hooks" / "state" / "cwd-session" / f"{cwd_key}.json"
    )
    try:
        stat = pointer_file.stat()
    except OSError:
        return None
    if time.time() - stat.st_mtime > _PICKUP_MAX_AGE_SECONDS:
        return None
    try:
        data = json.loads(pointer_file.read_text())
    except (OSError, ValueError):
        return None
    sid = data.get("sessionId")
    if not isinstance(sid, str) or not sid or len(sid) > _MAX_PREV_SESSION_ID_LEN:
        return None
    return sid


def _ps_field(pid: int, fmt: str) -> str | None:
    """Return a single ``ps -o <fmt>=`` field for ``pid``, or None.

    Best-effort and portable across macOS and Linux (no ``/proc`` dependency,
    which macOS lacks). Returns None when the process is gone, ps is missing,
    or the call errors/times out.
    """
    try:
        out = subprocess.run(
            ["ps", "-o", f"{fmt}=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    val = out.stdout.strip()
    return val or None


def _resolve_session_pid() -> int | None:
    """Climb from this hook to the Claude Code session process and return its pid.

    The hook is spawned as ``python3 session_start.py``, itself a child of
    Claude Code - sometimes directly, sometimes under a transient ``sh -c`` /
    ``zsh -c`` wrapper - so ``os.getppid()`` is not reliably the session
    process. We walk the ancestry until we find a process whose executable
    name (argv0 basename, via ``ps -o comm=``) is ``claude``, which is the
    CLI's process name (verified empirically: a claude-spawned subprocess sees
    ``... -> claude -> login shell`` above it).

    Returns None when no ``claude`` ancestor is found within
    ``_SESSION_PID_WALK_MAX_DEPTH`` levels - e.g. the Claude Desktop app, whose
    process shape differs - in which case callers fall back to the mtime-only
    heuristic.
    """
    pid = os.getpid()
    for _ in range(_SESSION_PID_WALK_MAX_DEPTH):
        ppid_s = _ps_field(pid, "ppid")
        if not ppid_s:
            return None
        try:
            ppid = int(ppid_s)
        except ValueError:
            return None
        if ppid <= 1:
            return None
        comm = _ps_field(ppid, "comm") or ""
        if os.path.basename(comm) == "claude":
            return ppid
        pid = ppid
    return None


def write_session_pid(session_id: str) -> None:
    """Record the Claude Code session process pid for ``session_id``.

    Lets a later SessionStart tell a sibling session that is still running
    (pid alive) from one that was just closed (pid gone) - the signal
    transcript mtime alone cannot give, which is why closing a session and
    immediately starting another in the same cwd used to misfire the
    parallel-session warning.

    Best-effort: when the session process can't be resolved (e.g. Claude
    Desktop's process shape), nothing is written and ``_session_is_alive``
    later returns None for this sid, falling back to the mtime heuristic. The
    start time is recorded alongside the pid so a recycled pid (reused by an
    unrelated process after the session exits) is detected on read.

    The session id is validated before it becomes a filename component, the
    same path-traversal guard (CWE-22) ``_is_valid_session_id`` enforces for
    the other ``<sid>.json`` pointers - so a malformed or path-like id can
    never write outside ``_SESSION_PID_DIR``.
    """
    if not _is_valid_session_id(session_id):
        return
    pid = _resolve_session_pid()
    if pid is None:
        return

    from missioncache_db import atomic_write_json  # type: ignore[import-not-found]

    atomic_write_json(
        _SESSION_PID_DIR / f"{session_id}.json",
        {
            "sessionId": session_id,
            "pid": pid,
            "startTime": _ps_field(pid, "lstart"),
            "updatedAt": datetime.now().astimezone().isoformat(),
        },
    )


def _session_is_alive(session_id: str) -> bool | None:
    """Liveness of ``session_id`` from its recorded process pid.

    Returns:
      * ``True``  - pid recorded and the process is still running (and, when a
        start time was recorded, still the same process).
      * ``False`` - pid recorded but the process is gone, or the pid was reused
        by an unrelated process (recorded start time no longer matches).
      * ``None``  - no usable pid record (a session predating this feature,
        Claude Desktop, or a resolution failure), OR an invalid session id.
        Caller should fall back to the mtime heuristic and treat the session
        as possibly-alive.

    The id is validated before it becomes a filename component so a candidate
    sid sourced from a transcript stem cannot read outside ``_SESSION_PID_DIR``
    (CWE-22); an invalid id returns None, the safe possibly-alive fallback.
    """
    if not _is_valid_session_id(session_id):
        return None
    rec_file = _SESSION_PID_DIR / f"{session_id}.json"
    try:
        data = json.loads(rec_file.read_text())
    except (OSError, ValueError):
        return None
    pid = data.get("pid")
    if not isinstance(pid, int) or pid <= 1:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return None
    recorded_start = data.get("startTime")
    if recorded_start:
        current_start = _ps_field(pid, "lstart")
        if current_start is not None and current_start != recorded_start:
            return False  # pid was recycled by a different process
    return True


def _detect_parallel_sessions(cwd: Path, my_session_id: str) -> list[str]:
    """Return session_ids whose transcripts in ``cwd`` were modified recently.

    Reads ``~/.claude/projects/<cwd-key>/*.jsonl`` (Claude Code's transcript
    directory) and returns sids whose mtime is within
    ``_PARALLEL_THRESHOLD_SECONDS``, excluding ``my_session_id``.

    A fresh transcript mtime says "active recently" but NOT "alive right now":
    a session closed seconds ago (the common /clear-then-relaunch or
    quit-and-reopen handoff) leaves a transcript just as fresh as a session
    still running, which used to misfire the warning on every serial handoff.
    So each mtime-fresh candidate is confirmed against its recorded process
    pid via ``_session_is_alive``: a candidate is dropped only when proven
    dead. Sessions with no pid record (predating this feature, Claude Desktop,
    or a resolution failure) return ``None`` and are kept, preserving the
    mtime-only behavior rather than risking a false negative.

    Used for two purposes in ``main()``:
      * Decide whether to skip resume-pickup (which would last-writer-wins
        and bind the wrong project when multiple sessions ran here).
      * Drive the collision warning surfaced in the SessionStart output.

    Note: callers that are handling a resume must additionally exclude the
    resumed-from session via ``_read_cwd_pointer_sid`` - that prior session
    is NOT a "parallel" session, it is the conversation being continued.

    Returns an empty list when the project dir is missing (fresh cwd that
    Claude Code has never written transcripts in) or on filesystem I/O
    errors during glob/stat (with a stderr breadcrumb so the failure is
    visible in the session transcript JSONL under ``~/.claude/projects/``).
    """
    if not my_session_id:
        return []
    cwd_key = str(cwd).replace("/", "-")
    proj_dir = Path.home() / ".claude" / "projects" / cwd_key
    if not proj_dir.is_dir():
        return []

    threshold = time.time() - _PARALLEL_THRESHOLD_SECONDS
    others: list[str] = []
    try:
        candidates = list(proj_dir.glob("*.jsonl"))
    except OSError as e:
        print(
            f"<!-- missioncache: transcripts glob failed in {proj_dir}: {e} -->",
            file=sys.stderr,
        )
        return []
    for jsonl in candidates:
        sid = jsonl.stem
        if sid == my_session_id:
            continue
        try:
            if jsonl.stat().st_mtime >= threshold:
                # mtime is fresh, but confirm the session is actually still
                # running. Drop it only when its recorded pid proves it is
                # gone; keep it when alive (True) or unknown (None).
                if _session_is_alive(sid) is not False:
                    others.append(sid)
        except OSError:
            # Race: transcript deleted between glob and stat. Silent skip
            # is correct - directory-level FS failures surface via the
            # glob breadcrumb above.
            continue
    return others


def _projects_for_sessions(session_ids: list[str]) -> dict[str, str]:
    """Look up ``project_name`` in ``project_state`` for the given sids.

    Returns ``{sid: project_name}`` only for sids that have a non-empty
    binding. Used by the collision-warning path to map parallel sessions
    back to their bound projects so the warning can name them.

    Recoverable errors (``sqlite3.OperationalError`` - lock contention,
    schema not yet initialized on a fresh install) are silent because they
    self-heal on the next SessionStart fire. Other ``sqlite3.Error``
    subclasses (DB corruption, schema drift, programming errors) emit a
    stderr breadcrumb so the warning's silent degradation is debuggable
    from the session transcript JSONL under ``~/.claude/projects/``.
    """
    if not session_ids:
        return {}
    from missioncache_db import HOOKS_STATE_DB_PATH  # type: ignore[import-not-found]

    try:
        conn = sqlite3.connect(str(HOOKS_STATE_DB_PATH))
    except sqlite3.OperationalError:
        return {}
    except sqlite3.Error as e:
        print(
            f"<!-- missioncache: project_state connect failed: {e} -->",
            file=sys.stderr,
        )
        return {}
    try:
        placeholders = ",".join("?" * len(session_ids))
        rows = conn.execute(
            f"SELECT session_id, project_name FROM project_state "
            f"WHERE session_id IN ({placeholders})",
            session_ids,
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    except sqlite3.Error as e:
        print(
            f"<!-- missioncache: project_state batch lookup failed: {e} -->",
            file=sys.stderr,
        )
        return {}
    finally:
        conn.close()
    return {sid: name for sid, name in rows if name}


def _format_collision_warning(
    my_project: str | None, collisions: dict[str, str]
) -> str:
    """Render the parallel-session warning that goes to Claude's context.

    The output is plain Markdown so Claude relays it to the user verbatim.
    Session ids are truncated to 8 chars for readability - the full sid
    appears in the stderr breadcrumb for debugging if needed.
    """
    lines = ["", "## Parallel MissionCache Session Warning", ""]
    if my_project:
        lines.append(
            f"This session is bound to MissionCache project `{my_project}`, but "
            f"another active session in the same directory is bound to a "
            f"different project:"
        )
    else:
        lines.append(
            "Another active session in this directory is bound to a MissionCache "
            "project:"
        )
    lines.append("")
    for sid, project in sorted(collisions.items(), key=lambda kv: kv[1]):
        lines.append(f"- `{project}` (session `{sid[:8]}`)")
    lines.append("")
    lines.append("Risks of working on multiple projects in the same codebase:")
    lines.append("")
    lines.append("- Statusline can show the wrong project name on resume.")
    lines.append("- Git conflicts between sessions touching the same files.")
    lines.append("- Heartbeats/time-tracking can be routed to the wrong project.")
    lines.append("")
    lines.append(
        "Recommended: work in a separate git worktree per project, or close "
        "the other session before continuing. If the statusline shows the "
        "wrong project, run `/missioncache:load <project-name>` to rebind."
    )
    lines.append("")
    return "\n".join(lines)


def write_cwd_session_pointer(session_id: str) -> None:
    """Record the current session as the owner of this cwd.

    Writes `~/.claude/hooks/state/cwd-session/<cwd-sanitized>.json` so slash
    commands (/missioncache:save, /missioncache:load, /missioncache:new, /missioncache:done) can resolve the
    live session id from bash without relying on transcript-mtime heuristics.

    Cwd sanitization matches Claude Code's own scheme for its transcript
    directory (`~/.claude/projects/<sanitized-cwd>/`), so the key is a stable
    shared identifier rather than a local convention.

    Uses atomic write (tmp + os.replace) so a hook killed mid-write never
    leaves a truncated pointer for the next resume's pickup logic to trip on.

    Overwritten on every SessionStart fire. Concurrent sessions sharing the
    same cwd will clobber each other's pointer - the last writer wins. This is
    still strictly better than the mtime-on-transcripts heuristic because it
    eliminates stale transcripts from long-finished sessions as a failure mode.
    """
    if not session_id:
        return

    from missioncache_db import atomic_write_json  # type: ignore[import-not-found]

    cwd_key = str(Path.cwd()).replace("/", "-")
    pointer_file = (
        Path.home() / ".claude" / "hooks" / "state" / "cwd-session" / f"{cwd_key}.json"
    )
    atomic_write_json(
        pointer_file,
        {
            "sessionId": session_id,
            "cwd": str(Path.cwd()),
            "updatedAt": datetime.now().astimezone().isoformat(),
        },
    )


def write_session_project(task_name: str, session_id: str, task_id: int | None = None) -> None:
    """Write session-specific project file for statusline display.

    Delegates to missioncache_db.write_session_binding - the single owner of
    the binding path and format (atomic tmp+rename, no shared pending file).
    ``task_id`` gives resolution a durable identity immune to name reuse.
    """
    if not session_id:
        return

    from missioncache_db import write_session_binding  # type: ignore[import-not-found]

    write_session_binding(session_id, task_name, task_id=task_id)


def get_session_context() -> tuple[str | None, str | None]:
    """Get ``(session_id, source)`` from env var or stdin JSON.

    ``source``, when present, is expected to be one of ``"startup"``,
    ``"resume"``, ``"clear"``, or ``"compact"`` per Claude Code's
    SessionStart contract (https://code.claude.com/docs/en/hooks); the
    value is returned as-is without validation, and ``main()`` whitelist-
    gates it before acting. The env var path carries only the session_id,
    so ``source`` always comes from stdin when present.

    ``session_id`` IS validated before return via
    ``_is_valid_session_id``: it must be UUID-charset and at most
    ``_MAX_PREV_SESSION_ID_LEN`` chars. This defends downstream filename
    interpolation (``projects/<sid>.json``) against path traversal and
    bounds the value flowing into DB rows + pointer JSON. Invalid
    session_ids are dropped (returned as None) so the hook fails closed
    rather than propagating a hostile value.

    The ``select.select`` poll is a non-blocking peek so this hook still
    works under env-var-only invocation (manual testing, older bootstrap
    scripts) without hanging on an empty interactive stdin.
    """
    session_id = os.environ.get("CLAUDE_SESSION_ID")
    source: str | None = None
    try:
        import select

        if select.select([sys.stdin], [], [], 0)[0]:
            data = json.load(sys.stdin)
            session_id = session_id or data.get("session_id")
            source = data.get("source")
    except (json.JSONDecodeError, OSError, ValueError):
        # Malformed JSON, stdin OS error, or value error from select.
        # Hook falls back to env-var-only mode below.
        pass
    if not _is_valid_session_id(session_id):
        session_id = None
    return session_id, source


def _resume_hint_for_cwd(db, cwd: str) -> str | None:
    """Build a short nudge when a resumed/compacted session lands in a repo
    that has active project(s) but is not itself bound to one.

    This replaces the old silent auto-inherit. Rather than guessing a project
    from "who last used this folder" (which mis-attributed time across
    unrelated sessions sharing a repo), we name the repo's active project(s)
    and let the user bind explicitly with ``/missioncache:load``.

    Returns None when cwd is not under any tracked repo or the repo has no
    active tasks - the caller then stays silent. DB errors propagate to the
    single Exception handler in main() (which logs a breadcrumb); duplicating
    that catch here would only hide it.
    """
    cwd_path = Path(cwd).resolve()
    # Most-specific active repo that contains cwd (longest matching path).
    matching = []
    for repo in db.get_repos(active_only=True):
        try:
            cwd_path.relative_to(Path(repo.path).resolve())
            matching.append(repo)
        except ValueError:
            continue
    if not matching:
        return None
    matching.sort(key=lambda r: len(r.path), reverse=True)
    repo = matching[0]

    # active + paused tasks for this repo, ordered last_worked_on DESC.
    tasks = db.get_active_tasks(repo.id)
    if not tasks:
        return None

    listed = ", ".join(f"`{t.name}`" for t in tasks[:5])
    more = "" if len(tasks) <= 5 else f" (+{len(tasks) - 5} more)"
    repo_label = repo.short_name or repo.path
    return (
        "\n## MissionCache\n\n"
        f"Resumed in **{repo_label}**, which has active project(s): "
        f"{listed}{more}. This session is not bound to a project, so no time "
        "is being tracked. Run `/missioncache:load <name>` to bind it.\n"
    )


def main():
    """Check for active task and output context."""
    # Write term-session mapping BEFORE MissionCacheDB (independent of task detection)
    session_id, source = get_session_context()
    if session_id:
        write_term_session_mapping(session_id)
        # Record THIS session's process pid so a later SessionStart can tell
        # whether we are still alive or were just closed - the liveness signal
        # _detect_parallel_sessions uses to avoid warning on a serial handoff
        # (e.g. /clear then relaunch in the same cwd).
        write_session_pid(session_id)
        # Detect other sessions whose transcripts in this cwd were touched
        # in the last few minutes, to warn about parallel work in the same
        # codebase (statusline confusion, git conflicts, heartbeat
        # misrouting). On resume/compact, exclude the resumed-from session:
        # its transcript is often still fresh, but it is the conversation
        # being continued, not a competing parallel session.
        parallel_sids = _detect_parallel_sessions(Path.cwd(), session_id)
        if source in ("resume", "compact"):
            prev_sid_from_pointer = _read_cwd_pointer_sid(Path.cwd())
            if prev_sid_from_pointer:
                parallel_sids = [
                    s for s in parallel_sids if s != prev_sid_from_pointer
                ]
        # NOTE: this hook deliberately does NOT auto-bind the session to a
        # project. A session is bound ONLY by an explicit action
        # (/missioncache:load, /missioncache:new) or by sitting under
        # ~/.missioncache/active/<task>/ (resolved by find_task_for_cwd
        # below). The old "inherit whatever project last ran in this cwd"
        # path was removed: a repo root is shared across unrelated work, so
        # inheriting on cwd alone silently mis-attributed heartbeats/time to
        # a repo-mate task and self-perpetuated across every later session in
        # that repo. On resume we surface a one-line hint instead of guessing.
        # Always record this session as the owner of the current cwd so
        # slash commands can resolve the live session id authoritatively
        # instead of guessing by transcript mtime. Independent of project
        # binding - the cwd-session pointer answers "who owns this cwd
        # right now", not "what project are they working on".
        write_cwd_session_pointer(session_id)

        # Emit the parallel-session collision warning to stdout (Claude's
        # context) when another session in this cwd is bound to a different
        # project. Batched into one DB query: fetch project_name for both
        # the parallel sids AND our own session_id, then split. On the
        # resume-with-parallel branch, pickup was skipped above, so our
        # session_id has no binding yet and ``my_project`` is None; that's
        # the expected state, and ``_format_collision_warning`` handles it.
        if parallel_sids:
            all_projects = _projects_for_sessions(parallel_sids + [session_id])
            my_project = all_projects.get(session_id)
            collisions = {
                sid: project
                for sid, project in all_projects.items()
                if sid != session_id and project != my_project
            }
            if collisions:
                print(_format_collision_warning(my_project, collisions))
                sid_list = ", ".join(sorted(collisions.keys()))
                print(
                    f"<!-- missioncache: parallel-session collision: my={my_project} "
                    f"others=[{sid_list}] -->",
                    file=sys.stderr,
                )

    # Always attempt to refresh rule files, even if missioncache_db is unavailable.
    install_bundled_rules()

    try:
        from missioncache_db import (  # type: ignore[import-not-found]
            MISSIONCACHE_ROOT,
            TaskDB,
        )

        db = TaskDB()
        cwd = os.getcwd()

        # Find task for current directory
        task = db.find_task_for_cwd(cwd, session_id)

        if task:

            # Write session-specific project file for statusline display.
            # This is the per-session pointer that find_task_for_cwd reads
            # for heartbeat routing; the old shared pending-task.json file
            # was vestigial and removed in mcp-missioncache 0.2.13.
            if session_id:
                write_session_project(task.name, session_id, task_id=task.id)

            # Get time info
            time_seconds = db.get_task_time(task.id)
            time_formatted = db.format_duration(time_seconds)

            # Build context message
            output = f"""
## Active Task Detected

**Task:** {task.name} (ID: {task.id})
**Status:** {task.status}
**Time Invested:** {time_formatted}
"""
            if task.jira_key:
                output += f"**JIRA:** {task.jira_key}\n"

            if session_id:
                output += f"**Session ID:** `{session_id}`\n"

            if task.full_path:
                # MissionCache files live under MISSIONCACHE_ROOT, not the repo.
                task_dir = MISSIONCACHE_ROOT / task.full_path
                if task_dir.exists():
                    output += f"**MissionCache files:** `{task_dir}`\n"
                    output += """
**Tip:** Run `/missioncache:load` for full context. Mark items complete in the tasks file as you finish them via `mcp__plugin_missioncache_pm__update_tasks_file` - the tasks file is the source of truth for progress (see the MissionCache rules).
"""

            # Always-on task-tracking pointer: the divergence hook only nudges
            # this on divergence, so nothing states it proactively otherwise.
            output += "\n**Note:** Ignore Claude Code's built-in `TaskCreate` and \"task tools\" reminders while on a MissionCache project - they drive an in-conversation todo list, not this project's tasks. Use `mcp__plugin_missioncache_pm__update_tasks_file` instead.\n"

            # Output context (stdout goes to Claude's context)
            print(output)

        elif source in ("resume", "compact") and session_id:
            # Continuation (resume/compact) with no project bound. Normally a
            # compact keeps the same session_id, so a bound session resolves
            # above via find_task_for_cwd and never reaches here; this branch
            # fires only when the session genuinely has no binding. We no
            # longer guess a project from cwd - instead nudge the user to bind
            # explicitly so time tracking + statusline resume correctly.
            hint = _resume_hint_for_cwd(db, cwd)
            if hint:
                print(hint)

    except ImportError:
        # missioncache_db not available, skip silently
        pass
    except Exception as e:
        # Don't fail the session start
        print(f"<!-- missioncache: {e} -->", file=sys.stderr)


if __name__ == "__main__":
    main()

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
        and bool(_SESSION_ID_CHARSET_RE.match(value))
    )

# Bundled orbit-db path for marketplace installs (no system pip install).
_BUNDLED_ORBIT_DB = Path(__file__).resolve().parent.parent / "orbit-db"
if _BUNDLED_ORBIT_DB.is_dir() and str(_BUNDLED_ORBIT_DB) not in sys.path:
    sys.path.insert(0, str(_BUNDLED_ORBIT_DB))


OWNERSHIP_MARKER = "<!-- orbit-plugin:managed"


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


def _is_cwd_compatible_with_inherited_project(
    cwd: Path, project_name: str
) -> bool:
    """Validate that the inherited project's repo is reachable from ``cwd``.

    Defends against the umbrella-cwd false positive: a previous session at
    ``~/work`` was bound to ``project-x`` (whose actual repo is
    ``~/work/repo-x``). A new session resuming at the same umbrella cwd has
    no business inheriting ``project-x`` - the user is sitting in the parent
    and may be intending an entirely different project under it. Inheriting
    blindly mis-tags the new session, routes its heartbeats to the wrong
    task, and makes the statusline lie.

    The gate: inherit only when ``cwd`` is the project's repo path OR a
    descendant of it (i.e. the user is sitting *inside* the project). If
    the repo lives *under* the cwd (umbrella case) or in an unrelated
    location, skip the inherit and let the new session start clean - the
    user can run ``/orbit:go`` to bind their actual intent.

    Lookup-failure modes are treated conservatively: if orbit_db is
    unavailable, the task lookup raises, the task was renamed/deleted, or
    the repo row was deleted, this returns ``True`` (inherit proceeds) so a
    transient infrastructure issue does not silently blank the statusline.
    The gate only fires on affirmative evidence that the inherit is wrong.

    Non-coding tasks (``repo_id is None``) have no repo to validate against,
    so they always inherit on the cwd-pointer match alone.
    """
    try:
        from orbit_db import TaskDB  # type: ignore[import-not-found]
    except ImportError:
        return True

    try:
        db = TaskDB()
        task = db.get_task_by_name(project_name)
    except Exception:
        return True

    if task is None:
        return True

    if task.repo_id is None:
        # Non-coding task - no repo path to spatially validate against.
        return True

    try:
        repo = db.get_repo(task.repo_id)
    except Exception:
        return True

    if repo is None:
        return True

    # cwd is the repo or a descendant of the repo (working inside the project)
    try:
        cwd.resolve().relative_to(Path(repo.path).resolve())
        return True
    except ValueError:
        return False


def _read_cwd_pointer_sid(cwd: Path) -> str | None:
    """Return the validated sessionId from the cwd-session pointer file.

    The pointer at ``~/.claude/hooks/state/cwd-session/<cwd-key>.json``
    holds the session_id of whoever last wrote to this cwd. On a normal
    resume, that is the session being resumed FROM, which Claude Code
    re-issues under a NEW session_id. Used by ``main()`` to exclude that
    prior session from ``_detect_parallel_sessions``'s result - without
    this exclusion, the resumed-from session's still-fresh transcript
    would be misclassified as a "parallel" session, the resume-pickup
    would be falsely skipped, and the statusline would blank out on every
    normal resume within the freshness window.

    Returns None when the pointer is missing, stale (>24h), or corrupt.
    Validation mirrors ``_pickup_previous_session_binding`` so the two
    callers agree on what counts as a usable pointer.
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


def _detect_parallel_sessions(cwd: Path, my_session_id: str) -> list[str]:
    """Return session_ids whose transcripts in ``cwd`` were modified recently.

    Reads ``~/.claude/projects/<cwd-key>/*.jsonl`` (Claude Code's transcript
    directory) and returns sids whose mtime is within
    ``_PARALLEL_THRESHOLD_SECONDS``, excluding ``my_session_id``. A fresh
    transcript mtime is the most reliable "session is alive right now"
    signal we have at hook time - heartbeats lag, and project_state has no
    end-of-session cleanup.

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
    visible in ``~/.claude/logs/``).
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
            f"<!-- orbit: transcripts glob failed in {proj_dir}: {e} -->",
            file=sys.stderr,
        )
        return []
    for jsonl in candidates:
        sid = jsonl.stem
        if sid == my_session_id:
            continue
        try:
            if jsonl.stat().st_mtime >= threshold:
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
    from ``~/.claude/logs/``. Matches the pattern at
    ``_pickup_previous_session_binding`` (lines 347 / 358).
    """
    if not session_ids:
        return {}
    from orbit_db import HOOKS_STATE_DB_PATH  # type: ignore[import-not-found]

    try:
        conn = sqlite3.connect(str(HOOKS_STATE_DB_PATH))
    except sqlite3.OperationalError:
        return {}
    except sqlite3.Error as e:
        print(
            f"<!-- orbit: project_state connect failed: {e} -->",
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
            f"<!-- orbit: project_state batch lookup failed: {e} -->",
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
    lines = ["", "## Parallel Orbit Session Warning", ""]
    if my_project:
        lines.append(
            f"This session is bound to orbit project `{my_project}`, but "
            f"another active session in the same directory is bound to a "
            f"different project:"
        )
    else:
        lines.append(
            "Another active session in this directory is bound to an orbit "
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
        "wrong project, run `/orbit:go <project-name>` to rebind."
    )
    lines.append("")
    return "\n".join(lines)


def _pickup_previous_session_binding(cwd: Path, new_session_id: str) -> str | None:
    """On resume, look up the project bound to the previous session at this cwd.

    Reads ``cwd-session/<sanitized>.json`` BEFORE ``write_cwd_session_pointer``
    overwrites it, extracts the session_id that owned this cwd, and queries
    ``project_state`` in the shared hooks-state DB for that sid. The caller
    is expected to bind the returned project_name to ``new_session_id`` so the
    statusline can render the project across resume.

    Returns None on:
      * Missing pointer file (fresh start at this cwd).
      * Pointer mtime older than ``_PICKUP_MAX_AGE_SECONDS`` (stale).
      * Pointer's session_id missing, malformed, or equal to new_session_id.
      * Corrupt pointer JSON (also unlinks the corrupt file so the next resume
        does not keep tripping on it).
      * project_state has no row for that sid.
      * The inherited project's repo lives outside ``cwd`` (umbrella-cwd
        false-positive guard, see ``_is_cwd_compatible_with_inherited_project``).
      * sqlite3 lock contention is silent (recoverable, dashboard writes the
        same DB); other sqlite3 errors log to stderr for diagnosability.
    """
    from orbit_db import HOOKS_STATE_DB_PATH  # type: ignore[import-not-found]

    cwd_key = str(cwd).replace("/", "-")
    pointer_file = Path.home() / ".claude" / "hooks" / "state" / "cwd-session" / f"{cwd_key}.json"

    try:
        stat = pointer_file.stat()
    except FileNotFoundError:
        return None
    except OSError as e:
        # Permission error or symlink loop on a path we own. Surface so the
        # user can debug; don't return None silently.
        print(f"<!-- orbit: cwd-session stat failed {pointer_file.name}: {e} -->", file=sys.stderr)
        return None

    if time.time() - stat.st_mtime > _PICKUP_MAX_AGE_SECONDS:
        return None

    try:
        data = json.loads(pointer_file.read_text())
    except FileNotFoundError:
        return None
    except OSError as e:
        print(f"<!-- orbit: cwd-session read failed {pointer_file.name}: {e} -->", file=sys.stderr)
        return None
    except ValueError as e:
        # Truncated / corrupt pointer (mid-write crash, manual edit). Surface
        # the corruption AND unlink so the next resume gets a clean slate.
        print(
            f"<!-- orbit: corrupt cwd-session pointer {pointer_file.name}: {e}; removing -->",
            file=sys.stderr,
        )
        try:
            pointer_file.unlink()
        except OSError:
            pass
        return None

    prev_session_id = data.get("sessionId")
    if not isinstance(prev_session_id, str):
        return None
    if not prev_session_id or len(prev_session_id) > _MAX_PREV_SESSION_ID_LEN:
        return None
    if prev_session_id == new_session_id:
        # Defensive: SessionStart can in principle re-fire for the same sid
        # (hook re-execution); never resurrect ourselves with stale data.
        return None

    try:
        conn = sqlite3.connect(str(HOOKS_STATE_DB_PATH))
        try:
            row = conn.execute(
                "SELECT project_name FROM project_state WHERE session_id = ?",
                (prev_session_id,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        # Lock contention with the dashboard or missing table on a fresh
        # install: recoverable on the next resume. Stay silent.
        return None
    except sqlite3.Error as e:
        print(f"<!-- orbit: project_state lookup failed: {e} -->", file=sys.stderr)
        return None

    if not row:
        return None
    project_name = row[0]

    # Umbrella-cwd false-positive guard: if cwd has no spatial relationship
    # to the inherited project's repo, the cwd-pointer match alone is not
    # enough signal to inherit. Surface a stderr breadcrumb so the user can
    # see in ~/.claude/logs/ why their statusline went blank instead of
    # inheriting.
    if not _is_cwd_compatible_with_inherited_project(cwd, project_name):
        print(
            f"<!-- orbit: skipping inherit of {project_name!r}: cwd {cwd} "
            f"not under project's repo path -->",
            file=sys.stderr,
        )
        return None

    return project_name


def _bind_session_to_project(session_id: str, project_name: str) -> None:
    """Upsert ``project_state`` and write the per-session pointer for one binding.

    Direct SQL only - the dashboard may not be reachable when this hook fires
    on startup, and any HTTP dependency would silently degrade the resume
    binding. Initializes the schema first via ``init_hooks_state_db_schema``
    so a fresh install (dashboard never started) can still bind. The
    per-session pointer file is also written so ``find_task_for_cwd``
    resolves correctly without waiting for ``/orbit:go``.

    Failures log to stderr (visible in ``~/.claude/logs/``) so the user has
    a breadcrumb when the statusline Project field stays blank after resume.
    """
    from orbit_db import HOOKS_STATE_DB_PATH, init_hooks_state_db_schema  # type: ignore[import-not-found]

    try:
        # Ensure parent dir exists - on a fresh install ~/.claude/ may be
        # absent and sqlite3.connect raises OperationalError otherwise.
        HOOKS_STATE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(HOOKS_STATE_DB_PATH))
        try:
            init_hooks_state_db_schema(conn)
            conn.execute(
                "INSERT INTO project_state (session_id, project_name, updated_at) "
                "VALUES (?, ?, datetime('now', 'localtime')) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "project_name = excluded.project_name, "
                "updated_at = datetime('now', 'localtime')",
                (session_id, project_name),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as e:
        print(
            f"<!-- orbit: bind_session failed sid={session_id} project={project_name}: {e} -->",
            file=sys.stderr,
        )
        return

    # write_session_project uses atomic_write_json, which catches OSError
    # internally. So the per-session pointer write is non-transactional with
    # the DB upsert (DB row may exist, file may not on full disk) but cannot
    # raise into the caller. Recovery on the next SessionStart fire happens
    # via find_task_for_cwd's cwd matching path.
    write_session_project(project_name, session_id)


def write_cwd_session_pointer(session_id: str) -> None:
    """Record the current session as the owner of this cwd.

    Writes `~/.claude/hooks/state/cwd-session/<cwd-sanitized>.json` so slash
    commands (/orbit:save, /orbit:go, /orbit:new, /orbit:done) can resolve the
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

    from orbit_db import atomic_write_json  # type: ignore[import-not-found]

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


def write_session_project(task_name: str, session_id: str) -> None:
    """Write session-specific project file for statusline display.

    Writes directly to projects/<session_id>.json via tmp+rename, avoiding
    the shared pending-project.json file which is prone to race conditions
    when multiple sessions run concurrently. Atomic semantics also prevent
    a mid-write crash from leaving a truncated file that the next statusline
    read would treat as corrupt.
    """
    if not session_id:
        return

    from orbit_db import atomic_write_json  # type: ignore[import-not-found]

    project_file = Path.home() / ".claude" / "hooks" / "state" / "projects" / f"{session_id}.json"
    atomic_write_json(
        project_file,
        {
            "projectName": task_name,
            "updated": datetime.now().astimezone().isoformat(),
            "sessionId": session_id,
        },
    )


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


def main():
    """Check for active task and output context."""
    # Write term-session mapping BEFORE OrbitDB (independent of task detection)
    session_id, source = get_session_context()
    if session_id:
        write_term_session_mapping(session_id)
        # Detect other sessions whose transcripts in this cwd were touched
        # in the last few minutes. On resume/compact, exclude the resumed-
        # from session (its transcript is often still fresh from recent
        # activity but it is the conversation being continued, not a
        # parallel session). Used for two things below:
        #   1. Skip resume-pickup when ambiguous (cwd-pointer is last-writer-
        #      wins; with two truly-parallel sessions in the same cwd it can
        #      name the wrong "previous" session and bind the wrong project).
        #   2. Emit a warning to Claude's context so the user is aware of
        #      the parallel-work risk (statusline confusion, git conflicts,
        #      heartbeat misrouting).
        parallel_sids = _detect_parallel_sessions(Path.cwd(), session_id)
        prev_sid_from_pointer: str | None = None
        if source in ("resume", "compact"):
            prev_sid_from_pointer = _read_cwd_pointer_sid(Path.cwd())
            if prev_sid_from_pointer:
                parallel_sids = [
                    s for s in parallel_sids if s != prev_sid_from_pointer
                ]
        # Only inherit on genuine continuations. The umbrella-cwd false
        # positive: a fresh "startup"/"clear" in a parent directory that
        # contains many orbit projects (e.g. ~/work) would otherwise
        # steal whichever project the previous unrelated session bound.
        # Missing source defaults to no-inherit so we fail to "no project"
        # instead of "wrong project".
        if source in ("resume", "compact"):
            if parallel_sids:
                # Ambiguous resume: another session beyond the resumed-from
                # one is still alive in this cwd. The cwd-pointer cannot
                # distinguish which session is being resumed, so any pickup
                # could silently bind the wrong project. Better to start
                # with no project bound and force the user to /orbit:go.
                print(
                    f"<!-- orbit: skipping resume pickup ({len(parallel_sids)} "
                    f"parallel session(s) detected, ambiguous) -->",
                    file=sys.stderr,
                )
            else:
                inherited = _pickup_previous_session_binding(Path.cwd(), session_id)
                if inherited:
                    print(
                        f"<!-- orbit: inherited project={inherited} (source={source}) -->",
                        file=sys.stderr,
                    )
                    _bind_session_to_project(session_id, inherited)
                else:
                    # Resume/compact requested an inherit but no previous
                    # binding was available. This is the user-visible
                    # "statusline went blank on resume" failure mode; surface
                    # it so it's debuggable from ~/.claude/logs/.
                    print(
                        f"<!-- orbit: no previous binding to inherit (source={source}) -->",
                        file=sys.stderr,
                    )
        elif source is not None and source not in ("startup", "clear"):
            # Unknown source value - Claude Code added one we don't
            # recognize. Failing closed is correct, but silent is bad;
            # surface contract drift so it's visible without grepping
            # the hook source.
            print(
                f"<!-- orbit: unknown source={source!r}, no inherit -->",
                file=sys.stderr,
            )
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
                    f"<!-- orbit: parallel-session collision: my={my_project} "
                    f"others=[{sid_list}] -->",
                    file=sys.stderr,
                )

    # Always attempt to refresh rule files, even if orbit_db is unavailable.
    install_bundled_rules()

    try:
        from orbit_db import TaskDB  # type: ignore[import-not-found]

        db = TaskDB()
        cwd = os.getcwd()

        # Find task for current directory
        task = db.find_task_for_cwd(cwd, session_id)

        if task:
            # Get repo info for the output context message.
            repo_path = None
            if task.repo_id:
                repo = db.get_repo(task.repo_id)
                if repo:
                    repo_path = repo.path

            # Write session-specific project file for statusline display.
            # This is the per-session pointer that find_task_for_cwd reads
            # for heartbeat routing; the old shared pending-task.json file
            # was vestigial and removed in mcp-orbit 0.2.13.
            if session_id:
                write_session_project(task.name, session_id)

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

            if repo_path:
                task_dir = Path(repo_path) / task.full_path
                if task_dir.exists():
                    output += f"**Orbit files:** `{task_dir}`\n"
                    output += """
**Tip:** Use `/orbit:go` to load full context, or call `mcp__plugin_orbit_pm__get_task` for structured project data.

**\u26a0\ufe0f Task tracking discipline (important):**

Mark items complete in the tasks file IMMEDIATELY as you finish them, using:

  mcp__plugin_orbit_pm__update_tasks_file(
    tasks_file="<path>",
    completed_tasks=["task description"]
  )

Do NOT batch updates to session end. Do NOT rely solely on appending findings to the context file - the context file is for details, the tasks file is the source of truth for progress.

Note: Claude Code's built-in `TaskCreate` tool and any "task tools" system reminders refer to an in-conversation todo list - IGNORE them when working on an orbit project. Use `mcp__plugin_orbit_pm__update_tasks_file` instead.
"""

            # Output context (stdout goes to Claude's context)
            print(output)

    except ImportError:
        # orbit_db not available, skip silently
        pass
    except Exception as e:
        # Don't fail the session start
        print(f"<!-- orbit: {e} -->", file=sys.stderr)


if __name__ == "__main__":
    main()

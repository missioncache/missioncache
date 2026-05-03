#!/usr/bin/env python3
"""
SessionStart hook - Auto-detect active task for the current directory.

Outputs context to help Claude resume work on an active task.
Also creates pending-task.json for the activity-tracker hook.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
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


def write_pending_task(task_name: str, cwd: str) -> None:
    """Write pending-task.json for activity-tracker hook integration."""
    state_dir = Path.home() / ".claude" / "hooks" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    pending_file = state_dir / "pending-task.json"
    pending_data = {
        "taskName": task_name,
        "cwd": cwd,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    pending_file.write_text(json.dumps(pending_data, indent=2))


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
    return row[0]


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


def get_session_id() -> str | None:
    """Get session ID from env var or stdin JSON."""
    session_id = os.environ.get("CLAUDE_SESSION_ID")
    if session_id:
        return session_id

    # Fallback: try reading from stdin JSON (some hook types provide it there)
    try:
        import select

        if select.select([sys.stdin], [], [], 0)[0]:
            data = json.load(sys.stdin)
            return data.get("session_id") or None
    except Exception:
        pass

    return None


def main():
    """Check for active task and output context."""
    # Write term-session mapping BEFORE OrbitDB (independent of task detection)
    session_id = get_session_id()
    if session_id:
        write_term_session_mapping(session_id)
        # On resume, the cwd-session pointer still carries the previous
        # session's id. Read its project binding BEFORE overwriting the
        # pointer so the statusline can render the project for the new sid
        # without waiting for the user to re-run /orbit:go.
        inherited = _pickup_previous_session_binding(Path.cwd(), session_id)
        if inherited:
            _bind_session_to_project(session_id, inherited)
        # Also record this session as the owner of the current cwd so slash
        # commands can resolve the live session id authoritatively instead of
        # guessing by transcript mtime.
        write_cwd_session_pointer(session_id)

    # Always attempt to refresh rule files, even if orbit_db is unavailable.
    install_bundled_rules()

    try:
        from orbit_db import TaskDB  # type: ignore[import-not-found]

        db = TaskDB()
        cwd = os.getcwd()

        # Find task for current directory
        task = db.find_task_for_cwd(cwd, session_id)

        if task:
            # Get repo info (used for both pending-task and output)
            repo_name = None
            repo_path = None
            if task.repo_id:
                repo = db.get_repo(task.repo_id)
                if repo:
                    repo_name = repo.short_name
                    repo_path = repo.path

            # Write pending-task.json for activity-tracker integration
            # This ensures heartbeats are recorded even when not in dev/active/<task>/ dir
            write_pending_task(task.name, repo_path or cwd)

            # Write session-specific project file for statusline display
            # This avoids the shared pending-project.json race condition
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

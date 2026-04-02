#!/usr/bin/env python3
"""
SessionStart hook - Auto-detect active task for the current directory.

Outputs context to help Claude resume work on an active task.
Also creates pending-task.json for the activity-tracker hook.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


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


def write_session_project(task_name: str, session_id: str) -> None:
    """Write session-specific project file for statusline display.

    Writes directly to projects/<session_id>.json, avoiding the shared
    pending-project.json file which is prone to race conditions when
    multiple sessions run concurrently.
    """
    if not session_id:
        return

    projects_dir = Path.home() / ".claude" / "hooks" / "state" / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)

    project_file = projects_dir / f"{session_id}.json"
    project_data = {
        "projectName": task_name,
        "updated": datetime.now().astimezone().isoformat(),
        "sessionId": session_id,
    }

    project_file.write_text(json.dumps(project_data))


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

    try:
        from orbit_db import TaskDB

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

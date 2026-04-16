#!/usr/bin/env python3
"""
Orbit Dashboard - Task & Analytics Dashboard

A FastAPI server that provides:
1. Task APIs - Task tracking, time analytics (DuckDB)
2. Plans APIs - Parallel execution monitoring
3. Auto APIs - Orbit-auto execution tracking

Port: 8787 (override with ORBIT_DASHBOARD_PORT env var)
"""

from __future__ import annotations

import asyncio
import json
import os
import re

import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Add lib to path for imports
sys.path.insert(0, str(Path(__file__).parent))
from lib import config
from lib.analytics_db import (
    AnalyticsDB,
    get_db,
    get_claude_hourly_activity,
    get_claude_daily_activity,
    merge_hourly_activity,
    ClaudeSessionCache,
    group_untracked_by_cwd,
    parse_tasks_md,
    import_tasks_md,
)

# Import SQLite OrbitDB for auto execution queries (these tables are only in SQLite)
from orbit_db import TaskDB as OrbitTaskDB, AutoExecution, AutoExecutionLog


def get_sqlite_db() -> OrbitTaskDB:
    """Get an OrbitDB instance for auto execution queries."""
    return OrbitTaskDB()


# =============================================================================
# Configuration
# =============================================================================

ORBIT_ROOT = Path.home() / ".claude" / "orbit"


def _init_hooks_state_db() -> None:
    """Create hooks-state.db with schema if it doesn't exist."""
    db = sqlite3.connect(str(HOOKS_STATE_DB))
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS session_state (
            session_id TEXT PRIMARY KEY,
            context_percent INTEGER DEFAULT 0,
            context_tokens TEXT DEFAULT '',
            edit_count INTEGER DEFAULT 0,
            qa_review_suggested INTEGER DEFAULT 0,
            action TEXT,
            updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        );
        CREATE TABLE IF NOT EXISTS project_state (
            session_id TEXT PRIMARY KEY,
            project_name TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        );
        CREATE TABLE IF NOT EXISTS term_sessions (
            term_session_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        );
        CREATE TABLE IF NOT EXISTS guard_warned (
            key TEXT PRIMARY KEY,
            rule TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        );
        CREATE TABLE IF NOT EXISTS validation_state (
            session_id TEXT PRIMARY KEY,
            validated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        );
    """)
    # Migrate: add last_prompt_at column
    for col in ("last_prompt_at",):
        try:
            db.execute(f"ALTER TABLE session_state ADD COLUMN {col} TEXT")
        except sqlite3.OperationalError:
            pass  # Already exists
    db.close()


def _get_hooks_state_db() -> sqlite3.Connection:
    """Get a connection to the hooks-state DB."""
    db = sqlite3.connect(str(HOOKS_STATE_DB))
    db.row_factory = sqlite3.Row
    return db


def _resolve_orbit_path(full_path: str) -> Path:
    """Resolve DB full_path to centralized orbit directory, stripping legacy dev/ prefix."""
    if full_path.startswith("dev/"):
        full_path = full_path[4:]
    return ORBIT_ROOT / full_path


# Background sync task
sync_task: asyncio.Task | None = None
SYNC_INTERVAL_SECONDS = 60  # Sync from SQLite every 60 seconds


# History API cache (expensive query - runs git on many repos)
HISTORY_CACHE_TTL_SECONDS = 300  # 5 minutes
_history_cache: dict[int, dict] = {}  # Keyed by days parameter
_history_cache_timestamp: dict[int, datetime] = {}


async def background_sync():
    """Background task to sync SQLite to DuckDB periodically."""
    while True:
        try:
            await asyncio.sleep(SYNC_INTERVAL_SECONDS)
            db = get_db()
            result = db.sync_from_sqlite()
            if result.get("sessions", 0) > 0 or result.get("heartbeats", 0) > 0:
                print(f"[Sync] Synced from SQLite: {result}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[Sync] Error: {e}")


def _handle_task_exception(task: asyncio.Task) -> None:
    """Log unhandled exceptions in background tasks."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        print(f"[ERROR] Background task '{task.get_name()}' failed: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle management."""
    global sync_task

    # Startup: init hooks-state DB
    _init_hooks_state_db()
    print("[Startup] Hooks state DB ready")

    # Startup: sync from SQLite immediately
    print("[Startup] Syncing from SQLite to DuckDB...")
    db = get_db()
    result = db.sync_from_sqlite()
    print(f"[Startup] Sync result: {result}")

    # Start background sync task
    sync_task = asyncio.create_task(background_sync(), name="db_sync")
    sync_task.add_done_callback(_handle_task_exception)

    yield

    # Shutdown: cancel background tasks
    if sync_task:
        sync_task.cancel()
        try:
            await sync_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Orbit Dashboard", version="2.0.0", lifespan=lifespan)

# CORS for local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Paths
CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
ORBIT_DB_SCRIPT = CLAUDE_DIR / "scripts" / "orbit_db.py"
HOOKS_STATE_DB = CLAUDE_DIR / "hooks-state.db"

# Cache TTLs
REFRESH_INTERVAL = 30  # seconds for SSE

# =============================================================================
# Utility Functions
# =============================================================================


def format_duration_ms(ms: float) -> str:
    """Format milliseconds to human-readable duration."""
    if ms <= 0:
        return "0m"
    total_seconds = ms / 1000
    hours = int(total_seconds // 3600)
    minutes = int((total_seconds % 3600) // 60)
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def get_jira_url(jira_key: str | None) -> str | None:
    """Look up the full JIRA URL for a key via the user-configured mapping.

    Returns None when `jira_key` is empty, when no prefix matches, or when
    the settings file has not defined any JIRA URLs yet. Callers and
    frontend templates must handle the None case.
    """
    if not jira_key:
        return None
    for prefix, base_url in config.get_jira_urls().items():
        if jira_key.startswith(prefix):
            return base_url + jira_key
    return None


# =============================================================================
# Git LOC Statistics
# =============================================================================

# Grace period for correlating commits to sessions (30 minutes)
COMMIT_GRACE_PERIOD_SECONDS = 30 * 60


def get_commits_with_loc(repo_path: str, date: str) -> list[dict]:
    """Get commits for a specific date with LOC stats.

    Args:
        repo_path: Absolute path to the git repository
        date: Date in YYYY-MM-DD format

    Returns:
        List of commits with: hash, timestamp, lines_added, lines_removed
    """
    git_dir = Path(repo_path) / ".git"
    if not git_dir.exists():
        return []

    try:
        # Filter to commits authored by "me". Precedence:
        #   1. Explicit allowlist from settings (config.get_author_emails())
        #   2. Per-repo `git config user.email` fallback
        # If neither yields an email, return no commits. Running git log with
        # no --author filter would report EVERY contributor's commits as the
        # current user's LOC on a shared repo - a silent wrong-answer bug.
        allowlist = config.get_author_emails()
        if allowlist:
            user_emails = allowlist
        else:
            try:
                email_result = subprocess.run(
                    ["git", "-C", repo_path, "config", "user.email"],
                    capture_output=True, text=True, timeout=2,
                )
                repo_email = email_result.stdout.strip()
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                repo_email = ""
            user_emails = [repo_email] if repo_email else []

        if not user_emails:
            return []

        # Match each email wrapped in angle brackets for exact match against
        # the "Name <email>" author line. git's --author is a regex, so we
        # escape metachars and alternate with `|` to build a multi-email OR.
        # Escaped brackets avoid substring false positives like `me@foo.com`
        # matching `someone@foo.com.au`.
        author_pattern = "|".join(f"<{re.escape(e)}>" for e in user_emails)
        author_args = ["--author", author_pattern]

        # git log with numstat format:
        # commit_hash|timestamp
        # lines_added<tab>lines_removed<tab>filename
        # ...
        # (blank line between commits)
        result = subprocess.run(
            [
                "git",
                "-C",
                repo_path,
                "log",
                "--all",  # Include commits from all branches
                "--numstat",
                "--format=%H|%aI",
                f"--since={date} 00:00:00",
                f"--until={date} 23:59:59",
            ]
            + author_args,
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode != 0:
            return []

        commits = []
        current_commit = None
        lines = result.stdout.strip().split("\n")

        for line in lines:
            if not line.strip():
                continue

            # New commit line: hash|timestamp
            if "|" in line and len(line.split("|")[0]) == 40:
                if current_commit:
                    commits.append(current_commit)

                parts = line.split("|")
                try:
                    timestamp = datetime.fromisoformat(parts[1].replace("Z", "+00:00"))
                except Exception:
                    timestamp = datetime.now()

                current_commit = {
                    "hash": parts[0],
                    "timestamp": timestamp,
                    "lines_added": 0,
                    "lines_removed": 0,
                }

            # Numstat line: added<tab>removed<tab>filename
            elif current_commit and "\t" in line:
                parts = line.split("\t")
                if len(parts) >= 2:
                    try:
                        # Handle binary files (shown as "-")
                        added = int(parts[0]) if parts[0] != "-" else 0
                        removed = int(parts[1]) if parts[1] != "-" else 0
                        current_commit["lines_added"] += added
                        current_commit["lines_removed"] += removed
                    except ValueError:
                        pass

        # Don't forget the last commit
        if current_commit:
            commits.append(current_commit)

        return commits

    except Exception:
        return []


def correlate_commits_to_tasks(
    commits: list[dict],
    sessions: list[dict],
    repo_path: str,
) -> dict[int, dict]:
    """Correlate commits to tasks based on session time windows.

    A commit is attributed to a task if its timestamp falls within
    the task's session window plus a grace period.

    Args:
        commits: List of commits with timestamp, lines_added, lines_removed
        sessions: List of sessions with task_id, start_time, end_time
        repo_path: The repo path these commits came from

    Returns:
        Dict mapping task_id -> {lines_added, lines_removed, commit_count}
    """
    task_loc: dict[int, dict] = {}

    for commit in commits:
        commit_time = commit["timestamp"]
        # Make timezone-naive for comparison if needed
        if commit_time.tzinfo is not None:
            commit_time = commit_time.replace(tzinfo=None)

        # Find matching session
        for session in sessions:
            start_time = session.get("start_time")
            end_time = session.get("end_time")

            if not start_time:
                continue

            # Parse ISO timestamps if strings
            if isinstance(start_time, str):
                start_time = datetime.fromisoformat(
                    start_time.replace("Z", "+00:00")
                ).replace(tzinfo=None)
            if isinstance(end_time, str):
                end_time = datetime.fromisoformat(
                    end_time.replace("Z", "+00:00")
                ).replace(tzinfo=None)

            # Default end_time to start_time + 2 hours if missing
            if not end_time:
                end_time = start_time + timedelta(hours=2)

            # Check if commit falls within session window + grace period
            grace_end = end_time + timedelta(seconds=COMMIT_GRACE_PERIOD_SECONDS)
            if start_time <= commit_time <= grace_end:
                task_id = session["task_id"]
                if task_id not in task_loc:
                    task_loc[task_id] = {
                        "lines_added": 0,
                        "lines_removed": 0,
                        "commit_count": 0,
                    }
                task_loc[task_id]["lines_added"] += commit["lines_added"]
                task_loc[task_id]["lines_removed"] += commit["lines_removed"]
                task_loc[task_id]["commit_count"] += 1
                break  # Each commit attributed to one task only

    return task_loc


def get_loc_for_date(date: str | None = None) -> dict:
    """Get LOC stats for all repos for a specific date, correlated to tasks.

    Includes both git commits and non-git activity (shadow repos, non-git folders).

    Args:
        date: Date in YYYY-MM-DD format (defaults to today)

    Returns:
        Dict with:
        - total: {lines_added, lines_removed, commit_count}
        - by_task: {task_id: {lines_added, lines_removed, commit_count}}
        - by_repo: {repo_name: {lines_added, lines_removed, commit_count}}
    """
    import sqlite3

    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")

    db = get_db()
    repos = db.get_repos(active_only=False)
    sessions = db.get_sessions_for_timeline(date)

    total_added = 0
    total_removed = 0
    total_commits = 0
    by_task: dict[int, dict] = {}
    by_repo: dict[str, dict] = {}
    seen_commit_hashes: set[str] = set()  # Deduplicate commits across repo clones

    # 1. Get git commits from tracked repos
    for repo in repos:
        repo_path = repo.path
        if not Path(repo_path).exists():
            continue

        commits = get_commits_with_loc(repo_path, date)
        if not commits:
            continue

        # Filter out commits we've already seen (handles repo clones/worktrees)
        unique_commits = []
        for c in commits:
            if c["hash"] not in seen_commit_hashes:
                seen_commit_hashes.add(c["hash"])
                unique_commits.append(c)

        if not unique_commits:
            continue

        # Filter sessions for this repo
        repo_sessions = [s for s in sessions if s.get("repo_name") == repo.short_name]

        # Aggregate repo totals
        repo_added = sum(c["lines_added"] for c in unique_commits)
        repo_removed = sum(c["lines_removed"] for c in unique_commits)
        repo_commit_count = len(unique_commits)

        if repo_added > 0 or repo_removed > 0:
            by_repo[repo.short_name] = {
                "lines_added": repo_added,
                "lines_removed": repo_removed,
                "commit_count": repo_commit_count,
            }

        total_added += repo_added
        total_removed += repo_removed
        total_commits += repo_commit_count

        # Correlate to tasks
        if repo_sessions:
            task_correlations = correlate_commits_to_tasks(
                unique_commits, repo_sessions, repo_path
            )
            for task_id, loc in task_correlations.items():
                if task_id not in by_task:
                    by_task[task_id] = {
                        "lines_added": 0,
                        "lines_removed": 0,
                        "commit_count": 0,
                    }
                by_task[task_id]["lines_added"] += loc["lines_added"]
                by_task[task_id]["lines_removed"] += loc["lines_removed"]
                by_task[task_id]["commit_count"] += loc["commit_count"]

    # 2. Get shadow commits from SQLite (non-git repos with shadow tracking)
    sqlite_path = Path.home() / ".claude" / "tasks.db"
    if sqlite_path.exists():
        try:
            conn = sqlite3.connect(str(sqlite_path))
            conn.row_factory = sqlite3.Row

            # Shadow commits (tracked non-git repos)
            cursor = conn.execute(
                """
                SELECT sc.task_id, sc.lines_added, sc.lines_removed, sr.folder_path
                FROM shadow_commits sc
                JOIN shadow_repos sr ON sc.shadow_repo_id = sr.id
                WHERE DATE(sc.timestamp) = ?
            """,
                (date,),
            )

            for row in cursor.fetchall():
                folder_name = Path(row["folder_path"]).name
                added = row["lines_added"] or 0
                removed = row["lines_removed"] or 0

                total_added += added
                total_removed += removed
                total_commits += 1

                if folder_name not in by_repo:
                    by_repo[folder_name] = {
                        "lines_added": 0,
                        "lines_removed": 0,
                        "commit_count": 0,
                    }
                by_repo[folder_name]["lines_added"] += added
                by_repo[folder_name]["lines_removed"] += removed
                by_repo[folder_name]["commit_count"] += 1

                task_id = row["task_id"]
                if task_id:
                    if task_id not in by_task:
                        by_task[task_id] = {
                            "lines_added": 0,
                            "lines_removed": 0,
                            "commit_count": 0,
                        }
                    by_task[task_id]["lines_added"] += added
                    by_task[task_id]["lines_removed"] += removed
                    by_task[task_id]["commit_count"] += 1

            # Non-git activity (uncommitted file changes in any folder)
            cursor = conn.execute(
                """
                SELECT folder_path, SUM(lines_total) as total_lines, SUM(files_changed) as total_files
                FROM non_git_activity
                WHERE date = ?
                GROUP BY folder_path
            """,
                (date,),
            )

            for row in cursor.fetchall():
                folder_name = Path(row["folder_path"]).name
                # For non-git, we only have total lines changed (treat as added for display)
                lines = row["total_lines"] or 0
                files = row["total_files"] or 0

                # Include uncommitted activity even for git repos (separate entry)
                total_added += lines
                label = folder_name
                if folder_name in by_repo:
                    label = folder_name + " (uncommitted)"
                by_repo[label] = {
                    "lines_added": lines,
                    "lines_removed": 0,
                    "commit_count": 0,
                    "files_changed": files,
                }

            conn.close()

        except Exception as e:
            print(f"Error reading SQLite LOC data: {e}")

    return {
        "total": {
            "lines_added": total_added,
            "lines_removed": total_removed,
            "commit_count": total_commits,
        },
        "by_task": by_task,
        "by_repo": by_repo,
    }


# =============================================================================
# Orbit Files Parsing
# =============================================================================


def parse_task_modes_from_content(content: str) -> list[dict[str, Any]]:
    """Parse per-task mode markers from tasks.md content.

    Parses markers like `[auto]`, `[inter]`, `[auto:depends=1,3]`

    Returns:
        List of dicts with task_id, title, mode, completed, dependencies
    """
    results = []

    # Pattern for checkbox items with optional mode markers
    # Matches: - [ ] 1. Task description `[auto]` or `[auto:depends=1,3]`
    # Task IDs can be: 1, 1.2, 1.2a, 4.5b, etc.
    pattern = re.compile(
        r"^\s*-\s*\[([ xX])\]\s*"  # Checkbox: - [ ] or - [x]
        r"(\d+(?:\.\d+[a-zA-Z]?)?[a-zA-Z]?)\.\s*"  # Task number: 1. 1.2. 1.2a. 4.5b.
        r"(.+?)$",  # Rest of line (title + optional mode)
        re.MULTILINE,
    )

    for match in pattern.finditer(content):
        checkbox = match.group(1)
        task_id = match.group(2)
        rest = match.group(3).strip()

        completed = checkbox.lower() == "x"

        # Parse mode marker from the rest of the line
        mode = None
        dependencies: list[str] = []
        title = rest

        # Look for mode marker at end: `[auto]` or `[inter]` or `[auto:depends=1,3]`
        mode_pattern = re.search(r"`\[(auto|inter)(?::depends=([^\]]+))?\]`\s*$", rest)
        if mode_pattern:
            mode = mode_pattern.group(1)
            if mode_pattern.group(2):
                deps_str = mode_pattern.group(2)
                dependencies = [d.strip() for d in deps_str.split(",") if d.strip()]
            title = rest[: mode_pattern.start()].strip()

        results.append(
            {
                "task_id": task_id,
                "title": title,
                "mode": mode,
                "completed": completed,
                "dependencies": dependencies,
            }
        )

    return results


def calculate_blocking_info(task_modes: list[dict[str, Any]]) -> dict[str, Any]:
    """Calculate dependency and blocking information for tasks.

    For each task, determines:
    - dependencies: explicit dependency list
    - is_blocked: whether the task can run
    - blocked_by: which task is blocking it (if any)
    - blocker_mode: mode of the blocker (auto/inter)
    - blocks: which tasks this one blocks

    Also calculates summary counts.

    Args:
        task_modes: List of task mode dicts from parse_task_modes_from_content()

    Returns:
        Dict with enhanced task_modes and summary fields
    """
    if not task_modes:
        return {
            "task_modes": [],
            "runnable_count": 0,
            "blocked_count": 0,
            "blocked_by_inter_count": 0,
        }

    # Build lookup by task_id
    task_by_id = {t["task_id"]: t for t in task_modes}

    # Track which tasks block which
    blocks_map: dict[str, list[str]] = {t["task_id"]: [] for t in task_modes}

    # Process each task
    for tm in task_modes:
        task_id = tm["task_id"]
        mode = tm.get("mode")
        completed = tm.get("completed", False)
        explicit_deps = tm.get("dependencies", [])

        # Initialize blocking fields
        tm["is_blocked"] = False
        tm["blocked_by"] = None
        tm["blocker_mode"] = None

        # Compute the full dependency chain (explicit + sequential). We persist
        # this on every task - including completed ones - so the UI can render
        # historical edges. `blocked_by` alone is not enough because it gets
        # cleared once a task completes.
        all_deps = _get_sequential_dependencies(task_id, task_modes)
        all_deps.extend(explicit_deps)
        all_deps = list(dict.fromkeys(all_deps))
        tm["depends_on"] = all_deps

        if completed:
            # Completed tasks are never blocked
            continue

        # Check each dependency
        for dep_id in all_deps:
            dep_task = task_by_id.get(dep_id)
            if not dep_task:
                continue  # Unknown dependency, skip

            if not dep_task.get("completed", False):
                # This task is blocked by dep_id
                tm["is_blocked"] = True
                tm["blocked_by"] = dep_id
                tm["blocker_mode"] = dep_task.get("mode") or "inter"
                break

        # Record that this task's dependencies block it
        for dep_id in all_deps:
            if dep_id in blocks_map:
                blocks_map[dep_id].append(task_id)

    # Add "blocks" field to each task
    for tm in task_modes:
        tm["blocks"] = blocks_map.get(tm["task_id"], [])

    # Calculate summary counts
    runnable_count = sum(
        1
        for t in task_modes
        if t.get("mode") == "auto"
        and not t.get("completed")
        and not t.get("is_blocked")
    )
    blocked_count = sum(
        1
        for t in task_modes
        if t.get("mode") == "auto" and not t.get("completed") and t.get("is_blocked")
    )
    blocked_by_inter_count = sum(
        1
        for t in task_modes
        if t.get("mode") == "auto"
        and not t.get("completed")
        and t.get("is_blocked")
        and t.get("blocker_mode") == "inter"
    )

    return {
        "task_modes": task_modes,
        "runnable_count": runnable_count,
        "blocked_count": blocked_count,
        "blocked_by_inter_count": blocked_by_inter_count,
    }


def _get_sequential_dependencies(task_id: str, all_tasks: list[dict]) -> list[str]:
    """Get implicit sequential dependencies for a task.

    Task N depends on task N-1 unless it has explicit dependencies.
    For hierarchical tasks like 1.2, it depends on 1.1.

    Args:
        task_id: The task ID to get dependencies for
        all_tasks: All tasks in the file

    Returns:
        List of task IDs that this task implicitly depends on
    """
    # Find the task to check if it has explicit dependencies
    task = next((t for t in all_tasks if t["task_id"] == task_id), None)
    if task and task.get("dependencies"):
        # Task has explicit dependencies, no implicit ones
        return []

    # Parse task_id into components
    if "." in task_id:
        # Hierarchical: 1.2 depends on 1.1
        parts = task_id.rsplit(".", 1)
        parent = parts[0]
        sub_part = parts[1]
        # Extract numeric prefix from sub-part (e.g. "5a" -> 5, "5" -> 5)
        sub_num_match = re.match(r"(\d+)", sub_part)
        if sub_num_match:
            sub_num = int(sub_num_match.group(1))
            has_suffix = len(sub_part) > len(sub_num_match.group(1))
            if has_suffix:
                # e.g. 4.5a — has letter suffix, no implicit sequential dep
                return []
            if sub_num > 1:
                return [f"{parent}.{sub_num - 1}"]
            else:
                # 1.1 depends on task 1 (the parent)
                return [parent] if parent in {t["task_id"] for t in all_tasks} else []
        else:
            return []
    else:
        # Simple: task 2 depends on task 1
        try:
            num = int(task_id)
            if num > 1:
                return [str(num - 1)]
        except ValueError:
            pass

    return []


def parse_orbit_progress(repo_path: str, task_full_path: str) -> dict[str, Any]:
    """Parse orbit task file to extract progress information.

    Args:
        repo_path: Absolute path to the repository
        task_full_path: Relative path like 'dev/active/task-name'

    Returns:
        Dictionary with status, description, remaining_summary, completion_pct, etc.
    """
    result = {
        "status": "",
        "description": "",
        "summary": "",  # For completed tasks: **Summary:** field
        "remaining_summary": "",
        "completion_pct": 0,
        "completed_count": 0,
        "total_count": 0,
        "last_updated": None,
        "orbit_in_completed": False,  # True if orbit files found in completed/
        "target_repo": None,  # Actual working repo extracted from context/plan
        # Per-task mode fields
        "project_mode": "interactive",  # "interactive", "autonomous", or "hybrid"
        "task_modes": [],  # List of {task_id, title, mode, completed}
        "auto_count": 0,
        "inter_count": 0,
        "auto_remaining": 0,
        "inter_remaining": 0,
    }

    if not repo_path or not task_full_path:
        return result

    try:
        # Extract task name from path (last component)
        task_name = Path(task_full_path).name

        # Build list of candidate task directories to check
        candidate_dirs = []

        # Centralized orbit root (primary)
        candidate_dirs.append(ORBIT_ROOT / "active" / task_name)
        candidate_dirs.append(ORBIT_ROOT / "completed" / task_name)

        # Legacy: repo-local paths for unmigrated tasks
        repo = Path(repo_path)
        candidate_dirs.append(repo / task_full_path)
        if "dev/active/" in task_full_path:
            candidate_dirs.append(repo / "dev" / "completed" / task_name)
        elif "dev/completed/" in task_full_path:
            candidate_dirs.append(repo / "dev" / "active" / task_name)

        # Find first existing candidate
        task_dir = None
        for candidate in candidate_dirs:
            if candidate.exists():
                task_dir = candidate
                break

        if not task_dir:
            return result

        # Check if orbit files are in the completed folder
        if "/completed/" in str(task_dir) and "/active/" not in str(task_dir):
            result["orbit_in_completed"] = True

        # Extract task name from the resolved path
        task_name = task_dir.name

        # Find task files - try prefixed names first, then generic names
        tasks_file = None
        for candidate in [task_dir / f"{task_name}-tasks.md", task_dir / "tasks.md"]:
            if candidate.exists():
                tasks_file = candidate
                break

        # Find context file - try prefixed names first, then generic names, then shared-context
        context_file = None
        for candidate in [
            task_dir / f"{task_name}-context.md",
            task_dir / "context.md",
            task_dir / "shared-context.md",
        ]:
            if candidate.exists():
                context_file = candidate
                break

        content = ""
        if tasks_file:
            content = tasks_file.read_text()

        if content:
            # Parse **Status:** field
            status_match = re.search(
                r"\*\*Status:\*\*\s*(.+?)(?:\n|$)", content, re.IGNORECASE
            )
            if status_match:
                result["status"] = status_match.group(1).strip()

            # Parse **Remaining:** field
            remaining_match = re.search(
                r"\*\*Remaining:\*\*\s*(.+?)(?:\n|$)", content, re.IGNORECASE
            )
            if remaining_match:
                result["remaining_summary"] = remaining_match.group(1).strip()

            # Parse **Last Updated:** field
            updated_match = re.search(
                r"\*\*Last Updated:\*\*\s*(.+?)(?:\n|$)", content, re.IGNORECASE
            )
            if updated_match:
                result["last_updated"] = updated_match.group(1).strip()

            # Parse **Summary:** field (for completed tasks)
            summary_match = re.search(
                r"\*\*Summary:\*\*\s*(.+?)(?:\n|$)", content, re.IGNORECASE
            )
            if summary_match:
                result["summary"] = summary_match.group(1).strip()

            # Count completion from checkboxes
            completed_items = len(
                re.findall(r"^\s*-\s*\[x\]", content, re.MULTILINE | re.IGNORECASE)
            )
            pending_items = len(re.findall(r"^\s*-\s*\[\s*\]", content, re.MULTILINE))
            total_items = completed_items + pending_items

            result["completed_count"] = completed_items
            result["total_count"] = total_items
            if total_items > 0:
                result["completion_pct"] = int((completed_items / total_items) * 100)

            # Generate remaining summary if not explicitly provided
            if not result["remaining_summary"] and total_items > 0:
                if result["completion_pct"] == 100:
                    result["remaining_summary"] = f"✓ Complete ({total_items} tasks)"
                else:
                    result["remaining_summary"] = (
                        f"{pending_items} of {total_items} tasks remaining"
                    )

            # Parse per-task mode markers
            task_modes = parse_task_modes_from_content(content)
            if task_modes:
                result["task_modes"] = task_modes

                # Count by mode
                auto_count = sum(1 for t in task_modes if t.get("mode") == "auto")
                inter_count = sum(1 for t in task_modes if t.get("mode") == "inter")
                unset_count = sum(1 for t in task_modes if t.get("mode") is None)

                # Count remaining by mode
                auto_remaining = sum(
                    1
                    for t in task_modes
                    if t.get("mode") == "auto" and not t.get("completed")
                )
                inter_remaining = sum(
                    1
                    for t in task_modes
                    if t.get("mode") != "auto" and not t.get("completed")
                )

                result["auto_count"] = auto_count
                result["inter_count"] = (
                    inter_count + unset_count
                )  # Unset defaults to interactive
                result["auto_remaining"] = auto_remaining
                result["inter_remaining"] = inter_remaining

                # Determine project classification
                if auto_count == 0:
                    result["project_mode"] = "interactive"
                elif inter_count + unset_count == 0:
                    result["project_mode"] = "autonomous"
                else:
                    result["project_mode"] = "hybrid"

        # Parse description from context file
        if context_file:
            try:
                ctx_content = context_file.read_text()

                # Look for ## Description section
                desc_match = re.search(
                    r"##\s*Description\s*\n+((?:[^\n#]+\n?)+)",
                    ctx_content,
                    re.IGNORECASE,
                )
                if desc_match:
                    lines = desc_match.group(1).strip().split("\n")
                    # Filter out metadata lines (those starting with **)
                    content_lines = [
                        l.strip()
                        for l in lines
                        if l.strip() and not l.strip().startswith("**")
                    ]
                    if content_lines:
                        result["description"] = " ".join(content_lines[:2])

                # Fallback: Look for other descriptive sections
                if not result["description"]:
                    for section_name in [
                        "Overview",
                        "Summary",
                        "Goal",
                        "What",
                        "About",
                    ]:
                        section_match = re.search(
                            rf"##\s*{section_name}[^\n]*\n+((?:[^\n#]+\n?)+)",
                            ctx_content,
                            re.IGNORECASE,
                        )
                        if section_match:
                            lines = section_match.group(1).strip().split("\n")
                            content_lines = [
                                l.strip()
                                for l in lines
                                if l.strip() and not l.strip().startswith("**")
                            ]
                            if content_lines:
                                result["description"] = " ".join(content_lines[:2])
                                break

                # Clean up description
                if result["description"]:
                    result["description"] = re.sub(
                        r"\s+", " ", result["description"]
                    ).strip()
                    if len(result["description"]) > 100:
                        result["description"] = result["description"][:97] + "..."

                # Extract target repo from an explicit **Target Repo:** or **Repo:**
                # field in context. The previous fallback that scanned for any
                # `(owner/repo)` parenthetical was removed because it false-matched
                # on prose like "(8/10)" or "(Sunday/Monday)" and displayed those
                # fragments as repo names in the dashboard.
                repo_field = re.search(
                    r"\*\*(?:Target\s+)?Repo:\*\*\s*(.+?)(?:\n|$)",
                    ctx_content,
                    re.IGNORECASE,
                )
                if repo_field:
                    repo_val = repo_field.group(1).strip()
                    # Extract just the repo name (last part of owner/repo)
                    if "/" in repo_val:
                        result["target_repo"] = repo_val.split("/")[-1]
                    else:
                        result["target_repo"] = repo_val

            except Exception:
                pass

    except Exception:
        pass

    return result


# =============================================================================
# Project & Activity APIs (DuckDB)
# =============================================================================



def _get_jsonl_task_times(task_ids: list[int]) -> dict[int, int]:
    """Get JSONL-based session time per task by matching cwd to repo path.

    Scopes to sessions occurring after the task was created to avoid
    over-counting when multiple tasks share the same repo.
    """
    import sqlite3

    db_path = Path.home() / ".claude" / "tasks.db"
    if not db_path.exists() or not task_ids:
        return {}

    try:
        placeholders = ",".join(["?"] * len(task_ids))
        with sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute(
                f"""SELECT t.id, SUM(c.duration_seconds) as total
                    FROM tasks t
                    JOIN repositories r ON t.repo_id = r.id
                    JOIN claude_session_cache c ON c.cwd = r.path
                    WHERE t.id IN ({placeholders})
                      AND c.duration_seconds > 0
                      AND c.date >= DATE(t.created_at)
                    GROUP BY t.id""",
                task_ids,
            ).fetchall()
        return {row[0]: int(row[1]) for row in rows}
    except sqlite3.Error as e:
        print(f"[WARN] Failed to query JSONL task times: {e}")
        return {}


def _effective_time(task_id: int, heartbeat_times: dict, jsonl_times: dict) -> int:
    return max(heartbeat_times.get(task_id, 0), jsonl_times.get(task_id, 0))


@app.get("/api/tasks/active")
async def api_tasks_active(repo_id: int = None):
    """Get active tasks with hierarchy and orbit progress info."""
    db = get_db()
    tasks = db.get_active_tasks(repo_id)

    # Separate parents and children
    parents = []
    children_map: dict[int, list] = {}

    task_ids = [t.id for t in tasks]
    times = db.get_batch_task_times(task_ids, period="all")
    jsonl_times = _get_jsonl_task_times(task_ids)

    # Cache repos for efficiency
    repos_cache: dict[int, Any] = {}

    for task in tasks:
        task_dict = task.to_dict()
        etime = _effective_time(task.id, times, jsonl_times)
        task_dict["time_spent_seconds"] = etime
        task_dict["time_spent_formatted"] = db.format_duration(etime)
        task_dict["last_worked_ago"] = db.format_time_ago(task.last_worked_on)
        task_dict["jira_url"] = get_jira_url(task.jira_key)

        # Get repo info
        repo = None
        if task.repo_id:
            if task.repo_id not in repos_cache:
                repos_cache[task.repo_id] = db.get_repo(task.repo_id)
            repo = repos_cache[task.repo_id]
            task_dict["repo_name"] = repo.short_name if repo else None
            task_dict["repo_path"] = repo.path if repo else None

        # Parse orbit files for progress info
        orbit_in_completed = False
        if repo and task.full_path:
            progress = parse_orbit_progress(repo.path, task.full_path)
            task_dict["description"] = progress.get("description", "")
            task_dict["remaining_summary"] = progress.get("remaining_summary", "")
            task_dict["completion_pct"] = progress.get("completion_pct", 0)
            task_dict["completed_count"] = progress.get("completed_count", 0)
            task_dict["total_count"] = progress.get("total_count", 0)
            orbit_in_completed = progress.get("orbit_in_completed", False)
            # Per-task mode fields
            task_dict["project_mode"] = progress.get("project_mode", "interactive")
            task_dict["task_modes"] = progress.get("task_modes", [])
            task_dict["auto_count"] = progress.get("auto_count", 0)
            task_dict["inter_count"] = progress.get("inter_count", 0)
            task_dict["auto_remaining"] = progress.get("auto_remaining", 0)
            task_dict["inter_remaining"] = progress.get("inter_remaining", 0)
            # Override repo_name with target_repo if available
            target_repo = progress.get("target_repo")
            if target_repo:
                task_dict["repo_name"] = target_repo
        else:
            task_dict["description"] = ""
            task_dict["remaining_summary"] = ""
            task_dict["completion_pct"] = 0
            task_dict["project_mode"] = "interactive"
            task_dict["task_modes"] = []
            task_dict["auto_remaining"] = 0
            task_dict["inter_remaining"] = 0

        # Skip tasks whose orbit files are in dev/completed/ folder
        # (DB status is stale, but orbit files were moved to completed)
        if orbit_in_completed:
            continue

        if task.parent_id:
            children_map.setdefault(task.parent_id, []).append(task_dict)
        else:
            parents.append(task_dict)

    # Attach children to parents and calculate combined time
    for parent in parents:
        parent_id = parent["id"]
        children = children_map.get(parent_id, [])
        parent["subtasks"] = children
        parent["subtask_count"] = len(children)

        # Combined time
        subtask_time = sum(c["time_spent_seconds"] for c in children)
        parent["combined_time_seconds"] = parent["time_spent_seconds"] + subtask_time
        parent["combined_time_formatted"] = db.format_duration(
            parent["time_spent_seconds"] + subtask_time
        )

    return {
        "tasks": parents,
        "count": len(parents),
        "total_with_subtasks": len(tasks),
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/api/task/{task_id}/structure")
async def api_task_structure(task_id: int):
    """Get detailed task structure with mode assignments.

    Returns per-task mode information for displaying in the dashboard modal.
    """
    db = get_db()

    try:
        task = db.get_task(task_id)
        if not task:
            return {"error": True, "message": f"Task {task_id} not found"}

        if not task.repo_id:
            return {"error": True, "message": "Task has no associated repository"}

        repo = db.get_repo(task.repo_id)
        if not repo:
            return {"error": True, "message": "Repository not found"}

        # Get progress info which includes task modes
        progress = parse_orbit_progress(repo.path, task.full_path)

        # Check for prompts directory
        task_dir = Path(repo.path) / task.full_path
        prompts_dir = task_dir / "prompts"
        has_prompts_dir = prompts_dir.exists()

        # Enhance task_modes with prompt existence
        task_modes = progress.get("task_modes", [])
        for tm in task_modes:
            if tm.get("mode") == "auto" and has_prompts_dir:
                # Convert task_id to prompt filename
                tid = tm["task_id"]
                if "." not in tid:
                    prompt_id = tid.zfill(2)
                else:
                    parts = tid.split(".")
                    prompt_id = "-".join(p.zfill(2) for p in parts)
                prompt_file = prompts_dir / f"task-{prompt_id}-prompt.md"
                tm["has_prompt"] = prompt_file.exists()
            else:
                tm["has_prompt"] = False

        # Calculate blocking information
        blocking_info = calculate_blocking_info(task_modes)

        return {
            "task_id": task_id,
            "task_name": task.name,
            "project_mode": progress.get("project_mode", "interactive"),
            "task_modes": blocking_info["task_modes"],
            "auto_count": progress.get("auto_count", 0),
            "inter_count": progress.get("inter_count", 0),
            "auto_remaining": progress.get("auto_remaining", 0),
            "inter_remaining": progress.get("inter_remaining", 0),
            "completed_count": progress.get("completed_count", 0),
            "total_count": progress.get("total_count", 0),
            "has_prompts_dir": has_prompts_dir,
            # Blocking summary
            "runnable_count": blocking_info["runnable_count"],
            "blocked_count": blocking_info["blocked_count"],
            "blocked_by_inter_count": blocking_info["blocked_by_inter_count"],
        }

    except Exception as e:
        return {"error": True, "message": str(e)}


@app.get("/api/tasks/completed")
async def api_tasks_completed(days: int = 30):
    """Get completed tasks with orbit summary info."""
    db = get_db()

    # Get tasks marked as completed in DB
    tasks = list(db.get_completed_tasks(days=days))

    # Also include tasks still marked as 'active' in DB but with orbit files
    # in dev/completed/ folder (orphan completed tasks due to DB constraint issues)
    active_tasks = db.get_active_tasks()
    repos_cache: dict[int, Any] = {}

    orphan_completed = []
    for task in active_tasks:
        if task.repo_id:
            if task.repo_id not in repos_cache:
                repos_cache[task.repo_id] = db.get_repo(task.repo_id)
            repo = repos_cache[task.repo_id]
            if repo and task.full_path:
                progress = parse_orbit_progress(repo.path, task.full_path)
                if progress.get("orbit_in_completed", False):
                    orphan_completed.append(task)

    tasks.extend(orphan_completed)

    task_ids = [t.id for t in tasks]
    times = db.get_batch_task_times(task_ids, period="all")
    jsonl_times_completed = _get_jsonl_task_times(task_ids)

    result = []
    for task in tasks:
        task_dict = task.to_dict()
        etime = _effective_time(task.id, times, jsonl_times_completed)
        task_dict["time_spent_seconds"] = etime
        task_dict["time_spent_formatted"] = db.format_duration(etime)
        task_dict["completed_ago"] = db.format_time_ago(task.completed_at)

        repo = None
        if task.repo_id:
            if task.repo_id not in repos_cache:
                repos_cache[task.repo_id] = db.get_repo(task.repo_id)
            repo = repos_cache[task.repo_id]
            task_dict["repo_name"] = repo.short_name if repo else None

        # Parse orbit files for description and summary
        if repo and task.full_path:
            progress = parse_orbit_progress(repo.path, task.full_path)
            task_dict["description"] = progress.get("description", "")
            task_dict["summary"] = progress.get("summary", "")
            # Override repo_name with target_repo if available
            target_repo = progress.get("target_repo")
            if target_repo:
                task_dict["repo_name"] = target_repo
        else:
            task_dict["description"] = ""
            task_dict["summary"] = ""

        result.append(task_dict)

    return {
        "tasks": result,
        "count": len(result),
        "days": days,
        "timestamp": datetime.now().isoformat(),
    }



@app.get("/api/task/{task_id}/files")
async def api_task_files(task_id: int):
    """Get orbit markdown files for a task (plan, context, tasks)."""
    db = get_db()
    task = db.get_task(task_id)

    if not task:
        return {"error": "Task not found", "task_id": task_id}

    result = {
        "task_id": task_id,
        "task_name": task.name,
        "files": {},
    }

    if not task.repo_id or not task.full_path:
        return {"error": "Task has no repository or path", **result}

    repo = db.get_repo(task.repo_id)
    if not repo:
        return {"error": "Repository not found", **result}

    repo_path = Path(repo.path)
    task_dir = _resolve_orbit_path(task.full_path)

    # Handle subtasks - check parent directory structure
    if task.parent_id:
        parent = db.get_task(task.parent_id)
        if parent and parent.full_path:
            task_dir = _resolve_orbit_path(parent.full_path) / task.name

    if not task_dir.exists():
        # Try alternate paths
        possible_paths = [
            ORBIT_ROOT / "active" / task.name,
            ORBIT_ROOT / "completed" / task.name,
            # Legacy: repo-local paths
            repo_path / "dev" / "active" / task.name,
            repo_path / "dev" / "completed" / task.name,
        ]
        task_dir = None
        for alt_path in possible_paths:
            if alt_path.exists():
                task_dir = alt_path
                break
        if not task_dir:
            return {"error": f"Task directory not found: {task.full_path}", **result}

    task_name = task_dir.name

    # Read available files
    file_patterns = [
        (f"{task_name}-plan.md", "plan"),
        (f"{task_name}-context.md", "context"),
        (f"{task_name}-tasks.md", "tasks"),
        ("plan.md", "plan"),
        ("context.md", "context"),
        ("tasks.md", "tasks"),
        ("README.md", "readme"),
    ]

    for filename, key in file_patterns:
        filepath = task_dir / filename
        if filepath.exists() and key not in result["files"]:
            try:
                content = filepath.read_text()
                result["files"][key] = {
                    "filename": filename,
                    "content": content,
                    "size": len(content),
                }
            except Exception as e:
                result["files"][key] = {
                    "filename": filename,
                    "error": str(e),
                }

    result["directory"] = str(task_dir)
    result["file_count"] = len(result["files"])

    # Lightweight check for updates count so frontend can hide empty Updates tab
    try:
        updates = db.get_task_updates(task_id, limit=1)
        result["updates_count"] = len(updates)
    except Exception:
        result["updates_count"] = 0

    return result


@app.get("/api/task/{task_id}/updates")
async def api_task_updates(task_id: int):
    """Get updates for a task from the task_updates table."""
    db = get_db()
    updates = db.get_task_updates(task_id)

    return {
        "task_id": task_id,
        "updates": updates,
        "count": len(updates),
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/api/task/{task_id}/prompt/{subtask_id}")
async def api_task_prompt(task_id: int, subtask_id: str):
    """Get prompt content for a specific subtask."""
    db = get_db()

    try:
        task = db.get_task(task_id)
        if not task:
            return {"error": True, "message": f"Task {task_id} not found"}

        if not task.repo_id:
            return {"error": True, "message": "Task has no associated repository"}

        repo = db.get_repo(task.repo_id)
        if not repo:
            return {"error": True, "message": "Repository not found"}

        task_dir = Path(repo.path) / task.full_path
        prompts_dir = task_dir / "prompts"

        if not prompts_dir.exists():
            return {"error": True, "message": "No prompts directory found"}

        # Convert subtask_id to prompt filename (same logic as api_task_structure)
        if "." not in subtask_id:
            prompt_id = subtask_id.zfill(2)
        else:
            parts = subtask_id.split(".")
            prompt_id = "-".join(p.zfill(2) for p in parts)

        filename = f"task-{prompt_id}-prompt.md"
        prompt_file = prompts_dir / filename

        if not prompt_file.exists():
            return {"error": True, "message": f"Prompt file not found: {filename}"}

        content = prompt_file.read_text()
        return {
            "subtask_id": subtask_id,
            "filename": filename,
            "content": content,
        }

    except Exception as e:
        return {"error": True, "message": str(e)}


def _merge_untracked_sessions(
    tasks_list: list[dict], sessions_list: list[dict], date: str
) -> None:
    """Merge untracked Claude Code sessions into task and session lists (in-place)."""
    cache = ClaudeSessionCache()
    untracked_raw = cache.get_untracked_sessions(date)
    untracked_groups = group_untracked_by_cwd(untracked_raw)
    for group in untracked_groups:
        tasks_list.append(group)
        sessions_list.extend(group.get("sessions", []))
    sessions_list.sort(key=lambda s: s.get("start_time", ""))


@app.get("/api/stats/today")
async def api_stats_today():
    """Get today's activity statistics.

    Uses SQLite for real-time session data (where heartbeats are written),
    and DuckDB for historical data. Also includes Claude Code activity
    from JSONL session files.
    """
    db = get_db()
    today_date = datetime.now().strftime("%Y-%m-%d")

    # Use SQLite for fresh session data (that's where new data is written)
    sessions = db.get_sessions_from_sqlite(today_date)
    task_hourly = db.get_hourly_activity_from_sqlite(today_date)
    tasks_today_raw = db.get_tasks_today_from_sqlite(today_date)
    for t in tasks_today_raw:
        t["jira_url"] = get_jira_url(t.get("jira_key"))

    # Get Claude Code activity from JSONL files
    claude_hourly = get_claude_hourly_activity(today_date)

    # Merge task-based and Claude activity into unified hourly data
    hourly = merge_hourly_activity(task_hourly, claude_hourly)

    # Calculate totals from SQLite data (orbit task sessions)
    task_seconds = sum(s["duration_seconds"] for s in sessions)
    task_count = len(tasks_today_raw)
    session_count = len(sessions)

    # Calculate Claude activity totals
    claude_messages = sum(h.get("claude_messages", 0) for h in hourly)
    claude_tool_calls = sum(h.get("claude_tool_calls", 0) for h in hourly)
    claude_tokens = sum(h.get("claude_tokens", 0) for h in hourly)
    claude_seconds_raw = sum(h.get("claude_seconds", 0) for h in hourly)
    claude_session_count = sum(h.get("claude_session_count", 0) for h in hourly)

    # Cap claude_seconds at elapsed time today (handles overlapping sessions)
    now = datetime.now()
    if today_date == now.strftime("%Y-%m-%d"):
        elapsed_today = int(
            (
                now - now.replace(hour=0, minute=0, second=0, microsecond=0)
            ).total_seconds()
        )
        claude_seconds = min(claude_seconds_raw, elapsed_today)
    else:
        # For past days, cap at 24 hours
        claude_seconds = min(claude_seconds_raw, 86400)

    # Total seconds: use only Claude JSONL activity (not orbit task time)
    total_seconds = claude_seconds

    # Get LOC stats for today
    loc_stats = get_loc_for_date(today_date)

    # Enrich tasks with LOC data
    tasks_today = []
    for t in tasks_today_raw:
        task_loc = loc_stats["by_task"].get(t["id"], {})
        tasks_today.append(
            {
                "id": t["id"],
                "name": t["name"],
                "status": t.get("status", "active"),
                "parent_name": t.get("parent_name"),
                "jira_key": t.get("jira_key"),
                "jira_url": t.get("jira_url"),
                "tags": t.get("tags", []),
                "repo_name": t.get("repo_name"),
                "time_seconds": t["time_seconds"],
                "time_formatted": t["time_formatted"],
                "loc_added": task_loc.get("lines_added", 0),
                "loc_removed": task_loc.get("lines_removed", 0),
                "commit_count": task_loc.get("commit_count", 0),
            }
        )

    # Limit tracked tasks, then add untracked (always included)
    tasks_today = tasks_today[:10]
    _merge_untracked_sessions(tasks_today, sessions, today_date)

    # Get repo breakdown from sessions
    repo_breakdown = {}
    for s in sessions:
        repo = s.get("repo_name") or "unknown"
        if repo not in repo_breakdown:
            repo_breakdown[repo] = {"seconds": 0, "sessions": 0}
        repo_breakdown[repo]["seconds"] += s.get("duration_seconds", 0)
        repo_breakdown[repo]["sessions"] += 1

    repo_breakdown_list = [
        {"repo": k, "total_seconds": v["seconds"], "session_count": v["sessions"]}
        for k, v in sorted(
            repo_breakdown.items(), key=lambda x: x[1]["seconds"], reverse=True
        )
    ]

    return {
        "date": today_date,
        "total_seconds": total_seconds,
        "total_formatted": db.format_duration(total_seconds),
        "task_count": task_count,
        "session_count": session_count,
        "loc_added": loc_stats["total"]["lines_added"],
        "loc_removed": loc_stats["total"]["lines_removed"],
        "commit_count": loc_stats["total"]["commit_count"],
        # Claude activity totals
        "claude_messages": claude_messages,
        "claude_tool_calls": claude_tool_calls,
        "claude_tokens": claude_tokens,
        "claude_seconds": claude_seconds,
        "claude_session_count": claude_session_count,
        "hourly_activity": hourly,
        "repo_breakdown": repo_breakdown_list,
        "loc_by_repo": loc_stats["by_repo"],
        "tasks_today": tasks_today,
        "sessions": sessions,  # For timeline visualization
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/api/stats/day")
async def api_stats_day(
    date: str = Query(..., description="Date in YYYY-MM-DD format"),
):
    """Get activity statistics for a specific date.

    Includes both orbit task activity and Claude Code activity from JSONL files.
    """
    db = get_db()

    stats = db.get_date_stats(date)
    task_hourly = db.get_hourly_activity(date)
    sessions = db.get_sessions_for_timeline(date)  # Timeline data
    tasks_raw = db.get_tasks_today_from_sqlite(date)  # Get tasks for this date
    for t in tasks_raw:
        t["jira_url"] = get_jira_url(t.get("jira_key"))

    # Get Claude Code activity from JSONL files
    claude_hourly = get_claude_hourly_activity(date)

    # Merge task-based and Claude activity
    hourly = merge_hourly_activity(task_hourly, claude_hourly)

    # Calculate Claude activity totals
    claude_messages = sum(h.get("claude_messages", 0) for h in hourly)
    claude_tool_calls = sum(h.get("claude_tool_calls", 0) for h in hourly)
    claude_tokens = sum(h.get("claude_tokens", 0) for h in hourly)
    claude_seconds_raw = sum(h.get("claude_seconds", 0) for h in hourly)
    claude_session_count = sum(h.get("claude_session_count", 0) for h in hourly)

    # Cap claude_seconds at elapsed time (handles overlapping sessions)
    now = datetime.now()
    if date == now.strftime("%Y-%m-%d"):
        elapsed_today = int(
            (
                now - now.replace(hour=0, minute=0, second=0, microsecond=0)
            ).total_seconds()
        )
        claude_seconds = min(claude_seconds_raw, elapsed_today)
    else:
        # For past days, cap at 24 hours
        claude_seconds = min(claude_seconds_raw, 86400)

    # Total seconds: use only Claude JSONL activity (not orbit task time)
    total_seconds = claude_seconds

    # Get LOC stats for the date
    loc_stats = get_loc_for_date(date)

    # Enrich tasks with LOC data (same pattern as /api/stats/today)
    tasks_today = []
    for t in tasks_raw:
        task_loc = loc_stats["by_task"].get(t["id"], {})
        tasks_today.append(
            {
                "id": t["id"],
                "name": t["name"],
                "status": t.get("status", "active"),
                "parent_name": t.get("parent_name"),
                "jira_key": t.get("jira_key"),
                "jira_url": t.get("jira_url"),
                "tags": t.get("tags", []),
                "repo_name": t.get("repo_name"),
                "time_seconds": t["time_seconds"],
                "time_formatted": t["time_formatted"],
                "loc_added": task_loc.get("lines_added", 0),
                "loc_removed": task_loc.get("lines_removed", 0),
                "commit_count": task_loc.get("commit_count", 0),
            }
        )

    # Limit tracked tasks, then add untracked (always included)
    tasks_today = tasks_today[:10]
    _merge_untracked_sessions(tasks_today, sessions, date)

    return {
        "date": date,
        "total_seconds": total_seconds,
        "total_formatted": db.format_duration(total_seconds),
        "task_count": stats["task_count"],
        "session_count": stats["session_count"],
        "loc_added": loc_stats["total"]["lines_added"],
        "loc_removed": loc_stats["total"]["lines_removed"],
        "commit_count": loc_stats["total"]["commit_count"],
        # Claude activity totals
        "claude_messages": claude_messages,
        "claude_tool_calls": claude_tool_calls,
        "claude_tokens": claude_tokens,
        "claude_seconds": claude_seconds,
        "claude_session_count": claude_session_count,
        "hourly_activity": hourly,
        "loc_by_repo": loc_stats["by_repo"],
        "tasks_today": tasks_today,
        "sessions": sessions,  # For timeline visualization
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/api/stats/history")
async def api_stats_history(days: int = 7):
    """Get historical activity statistics.

    Uses 5-minute cache to avoid expensive repeated queries.
    Includes Claude Code activity from JSONL session files.
    """
    global _history_cache, _history_cache_timestamp

    # Check cache
    cache_time = _history_cache_timestamp.get(days)
    if (
        cache_time
        and (datetime.now() - cache_time).total_seconds() < HISTORY_CACHE_TTL_SECONDS
    ):
        cached = _history_cache.get(days)
        if cached:
            return {**cached, "cached": True, "timestamp": datetime.now().isoformat()}

    db = get_db()

    daily = db.get_daily_activity(days=days)
    repo_breakdown = db.get_repo_breakdown(days=days)
    hourly_heatmap = db.get_hourly_heatmap(days=days)  # 7×24 heatmap
    daily_totals = db.get_daily_work_totals(days=days)  # Totals by day of week
    daily_by_date = db.get_daily_work_by_date(days=days)  # Chronological by date
    top_tasks = db.get_top_tasks_by_effort(days=days, limit=5)  # Top 5 tasks
    trends = db.get_trend_comparison(days=days)  # Period vs previous period

    # Get Claude Code activity (refreshes cache as needed)
    claude_daily = get_claude_daily_activity(days=days)

    # Index both by date for merging
    daily_by_date_dict = {d["date"]: d for d in daily}
    claude_by_date = {d["date"]: d for d in claude_daily}

    # Get all dates from both sources
    all_dates = set(daily_by_date_dict.keys()) | set(claude_by_date.keys())

    # Build merged daily_activity including Claude-only dates
    merged_daily = []
    for date in sorted(all_dates):
        task_data = daily_by_date_dict.get(
            date,
            {
                "date": date,
                "total_seconds": 0,
                "task_count": 0,
                "session_count": 0,
            },
        )
        claude_data = claude_by_date.get(date, {})

        task_secs = task_data.get("total_seconds", 0)
        claude_secs = claude_data.get("claude_seconds", 0)
        merged_day = {
            "date": date,
            "total_seconds": max(task_secs, claude_secs)
            if task_secs > 0 or claude_secs > 0
            else 0,
            "task_seconds": task_secs,
            "task_count": task_data.get("task_count", 0),
            "session_count": task_data.get("session_count", 0),
            "claude_messages": claude_data.get("claude_messages", 0),
            "claude_tool_calls": claude_data.get("claude_tool_calls", 0),
            "claude_tokens": claude_data.get("claude_tokens", 0),
            "claude_seconds": claude_secs,
            "claude_session_count": claude_data.get("session_count", 0),
        }
        merged_daily.append(merged_day)

    # Replace daily with merged
    daily = merged_daily

    # Merge Claude data into daily_by_date, adding Claude-only dates
    daily_by_date_dates = {d["date"] for d in daily_by_date}
    for day in daily_by_date:
        date = day.get("date")
        claude_data = claude_by_date.get(date, {})
        day["claude_messages"] = claude_data.get("claude_messages", 0)
        day["claude_tool_calls"] = claude_data.get("claude_tool_calls", 0)
        day["claude_tokens"] = claude_data.get("claude_tokens", 0)
        day["claude_seconds"] = claude_data.get("claude_seconds", 0)
    for date_str, claude_data in claude_by_date.items():
        if date_str not in daily_by_date_dates:
            d = datetime.strptime(date_str, "%Y-%m-%d")
            daily_by_date.append({
                "date": date_str,
                "dow": d.weekday() + 1 if d.weekday() < 6 else 0,
                "total_minutes": 0,
                "session_count": 0,
                "claude_messages": claude_data.get("claude_messages", 0),
                "claude_tool_calls": claude_data.get("claude_tool_calls", 0),
                "claude_tokens": claude_data.get("claude_tokens", 0),
                "claude_seconds": claude_data.get("claude_seconds", 0),
            })
    daily_by_date.sort(key=lambda x: x["date"])

    # Calculate totals
    total_seconds = sum(d["total_seconds"] for d in daily)
    total_sessions = sum(d["session_count"] for d in daily)
    total_tasks = sum(d["task_count"] for d in daily)

    # Claude totals (from claude_daily for accuracy)
    total_claude_messages = sum(d.get("claude_messages", 0) for d in claude_daily)
    total_claude_tool_calls = sum(d.get("claude_tool_calls", 0) for d in claude_daily)
    total_claude_tokens = sum(d.get("claude_tokens", 0) for d in claude_daily)
    total_claude_seconds = sum(d.get("claude_seconds", 0) for d in claude_daily)
    total_claude_sessions = sum(d.get("claude_session_count", 0) for d in daily)

    # Override trends time with merged total (trends query is orbit-only)
    if total_seconds > trends.get("time", {}).get("current", 0):
        trends["time"]["current"] = total_seconds
        trends["time"]["current_formatted"] = db.format_duration(total_seconds)

    result = {
        "days": days,
        "daily_activity": daily,
        "repo_breakdown": repo_breakdown,
        "hourly_heatmap": hourly_heatmap,  # For GitHub-style grid
        "daily_totals": daily_totals,  # Totals by day of week (Sun-Sat)
        "daily_by_date": daily_by_date,  # Chronological by date
        "top_tasks": top_tasks,  # Top tasks by effort
        "trends": trends,  # This period vs previous period comparison
        "total_seconds": total_seconds,
        "total_formatted": db.format_duration(total_seconds),
        "total_sessions": total_sessions,
        "total_tasks": total_tasks,
        "avg_daily_seconds": total_seconds // days if days > 0 else 0,
        "avg_daily_formatted": db.format_duration(
            total_seconds // days if days > 0 else 0
        ),
        # Claude totals
        "total_claude_messages": total_claude_messages,
        "total_claude_tool_calls": total_claude_tool_calls,
        "total_claude_tokens": total_claude_tokens,
        "total_claude_seconds": total_claude_seconds,
        "total_claude_sessions": total_claude_sessions,
    }

    # Store in cache
    _history_cache[days] = result
    _history_cache_timestamp[days] = datetime.now()

    return {**result, "cached": False, "timestamp": datetime.now().isoformat()}


@app.get("/api/repos")
async def api_repos():
    """Get all tracked repositories."""
    db = get_db()
    repos = db.get_repos(active_only=True)

    result = []
    for repo in repos:
        result.append(
            {
                "id": repo.id,
                "path": repo.path,
                "short_name": repo.short_name,
                "active": repo.active,
                "last_scanned_at": repo.last_scanned_at.isoformat()
                if repo.last_scanned_at
                else None,
            }
        )

    return {
        "repositories": result,
        "count": len(result),
        "timestamp": datetime.now().isoformat(),
    }


# =============================================================================
# Orbit Auto API - Task Graph Visualization
# =============================================================================


def _parse_orbit_tasks(tasks_file: Path) -> list[dict]:
    """Parse tasks from an orbit tasks.md file.

    Returns list of dicts with: number, title, completed, wait
    """
    if not tasks_file.exists():
        return []

    content = tasks_file.read_text()
    tasks = []

    # Pattern matches: - [ ] or - [x] followed by optional [WAIT], then number
    pattern = r"^\s*- \[([ x])\]\s*(\[WAIT\])?\s*(\d+(?:\.\d+)?)[.:]\s*(.+)$"

    for line_num, line in enumerate(content.split("\n"), 1):
        match = re.match(pattern, line)
        if match:
            tasks.append(
                {
                    "number": match.group(3),
                    "title": match.group(4).strip(),
                    "completed": match.group(1) == "x",
                    "wait": match.group(2) is not None,
                    "line": line_num,
                }
            )

    return tasks


@app.get("/api/auto/projects")
async def api_auto_projects():
    """List active orbit projects with their task graphs.

    Returns projects from ~/.claude/orbit/active/ with:
    - Task list with status (completed/pending/wait)
    - Dependencies parsed from prompts (if available)
    - Graph data for D3.js visualization
    - Repo short_name (looked up from the task DB)
    """
    projects = []
    active_dir = ORBIT_ROOT / "active"
    db = get_sqlite_db()

    if active_dir.exists():
        for project_dir in active_dir.iterdir():
            if not project_dir.is_dir() or project_dir.name.startswith("."):
                continue

            # Find tasks file
            tasks_file = project_dir / f"{project_dir.name}-tasks.md"
            if not tasks_file.exists():
                tasks_file = project_dir / "tasks.md"
            if not tasks_file.exists():
                continue

            # Parse tasks
            tasks = _parse_orbit_tasks(tasks_file)
            if not tasks:
                continue

            # Build nodes for graph
            nodes = []
            for task in tasks:
                nodes.append(
                    {
                        "id": task["number"],
                        "title": task["title"],
                        "status": "completed"
                        if task["completed"]
                        else ("wait" if task["wait"] else "pending"),
                    }
                )

            # Parse dependencies from prompts
            links = []
            prompts_dir = project_dir / "prompts"
            if prompts_dir.exists():
                links = _parse_prompt_dependencies(prompts_dir)

            # Look up repo short_name from the task DB
            repo_name = None
            task_row = db.get_task_by_name(project_dir.name)
            if task_row and task_row.repo_id:
                repo = db.get_repo(task_row.repo_id)
                if repo:
                    repo_name = repo.short_name

            # Calculate progress
            completed = sum(1 for t in tasks if t["completed"])
            total = len(tasks)

            projects.append(
                {
                    "name": project_dir.name,
                    "path": str(project_dir),
                    "repo_name": repo_name,
                    "progress": {
                        "completed": completed,
                        "total": total,
                        "percent": int(completed * 100 / total) if total > 0 else 0,
                    },
                    "graph": {
                        "nodes": nodes,
                        "links": links,
                    },
                }
            )

    return {
        "projects": projects,
        "count": len(projects),
    }


def _parse_prompt_dependencies(prompts_dir: Path) -> list[dict]:
    """Parse dependencies from prompt YAML frontmatter.

    Returns list of {source, target} for D3.js links.
    """
    import yaml

    links = []
    for prompt_file in prompts_dir.glob("task-*-prompt.md"):
        content = prompt_file.read_text()

        # Extract YAML frontmatter
        if not content.startswith("---"):
            continue

        try:
            end_idx = content.index("---", 3)
            yaml_content = content[3:end_idx].strip()
            frontmatter = yaml.safe_load(yaml_content)

            if frontmatter and "depends_on" in frontmatter:
                # Extract task number from filename (e.g., task-03-prompt.md -> "3")
                task_id = (
                    prompt_file.stem.replace("task-", "")
                    .replace("-prompt", "")
                    .lstrip("0")
                    or "0"
                )

                deps = frontmatter["depends_on"]
                if isinstance(deps, list):
                    for dep in deps:
                        # Normalize dependency (could be "1", "01", etc.)
                        dep_id = str(dep).lstrip("0") or "0"
                        links.append({"source": dep_id, "target": task_id})
                elif deps:
                    dep_id = str(deps).lstrip("0") or "0"
                    links.append({"source": dep_id, "target": task_id})
        except (ValueError, yaml.YAMLError):
            continue

    return links



@app.get("/api/auto/executions")
async def api_auto_executions(running_only: bool = False, limit: int = 20):
    """List recent auto executions.

    Args:
        running_only: If true, only return currently running executions
        limit: Maximum number of executions to return

    Returns executions with task info.
    """
    db = get_sqlite_db()

    if running_only:
        executions = db.get_running_auto_executions()
    else:
        # Get all recent executions across all tasks
        # Use raw query since we need to join with tasks
        with db.connection() as conn:
            cursor = conn.execute(
                """SELECT e.*, t.name as task_name, t.full_path
                   FROM auto_executions e
                   JOIN tasks t ON e.task_id = t.id
                   ORDER BY e.started_at DESC
                   LIMIT ?""",
                (limit,),
            )
            rows = cursor.fetchall()

        executions = []
        for row in rows:
            executions.append(
                {
                    "id": row["id"],
                    "task_id": row["task_id"],
                    "task_name": row["task_name"],
                    "full_path": row["full_path"],
                    "started_at": row["started_at"],
                    "completed_at": row["completed_at"],
                    "status": row["status"],
                    "mode": row["mode"],
                    "worker_count": row["worker_count"],
                    "total_subtasks": row["total_subtasks"],
                    "completed_subtasks": row["completed_subtasks"],
                    "failed_subtasks": row["failed_subtasks"],
                    "error_message": row["error_message"],
                }
            )

        return {
            "executions": executions,
            "count": len(executions),
        }

    # For running_only, format response
    return {
        "executions": [
            {
                "id": e.id,
                "task_id": e.task_id,
                "started_at": e.started_at,
                "completed_at": e.completed_at,
                "status": e.status,
                "mode": e.mode,
                "worker_count": e.worker_count,
                "total_subtasks": e.total_subtasks,
                "completed_subtasks": e.completed_subtasks,
                "failed_subtasks": e.failed_subtasks,
                "error_message": e.error_message,
            }
            for e in executions
        ],
        "count": len(executions),
    }


@app.get("/api/auto/executions/{task_id}")
async def api_auto_executions_for_task(task_id: int, limit: int = 10):
    """Get executions for a specific task."""
    db = get_sqlite_db()

    executions = db.get_auto_executions_for_task(task_id, limit=limit)
    if not executions:
        # Check if task exists
        task = db.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    return {
        "task_id": task_id,
        "executions": [
            {
                "id": e.id,
                "started_at": e.started_at,
                "completed_at": e.completed_at,
                "status": e.status,
                "mode": e.mode,
                "worker_count": e.worker_count,
                "total_subtasks": e.total_subtasks,
                "completed_subtasks": e.completed_subtasks,
                "failed_subtasks": e.failed_subtasks,
                "error_message": e.error_message,
            }
            for e in executions
        ],
        "count": len(executions),
    }


@app.get("/api/auto/output/{execution_id}")
async def api_auto_output(
    execution_id: int,
    since_id: int | None = None,
    limit: int = 1000,
    level: str | None = None,
    worker_id: int | None = None,
    subtask_id: str | None = None,
):
    """Get execution output logs.

    Args:
        execution_id: The execution to get logs for
        since_id: Only return logs with ID > this value (for polling)
        limit: Maximum number of log entries
        level: Filter by log level (debug, info, warn, error, success)
        worker_id: Filter by worker
        subtask_id: Filter by subtask

    Returns log entries with execution metadata.
    """
    db = get_sqlite_db()

    execution = db.get_auto_execution(execution_id)
    if not execution:
        raise HTTPException(
            status_code=404, detail=f"Execution {execution_id} not found"
        )

    logs = db.get_auto_execution_logs(
        execution_id,
        since_id=since_id,
        limit=limit,
        level=level,
        worker_id=worker_id,
        subtask_id=subtask_id,
    )

    return {
        "execution": {
            "id": execution.id,
            "task_id": execution.task_id,
            "started_at": execution.started_at,
            "completed_at": execution.completed_at,
            "status": execution.status,
            "mode": execution.mode,
            "worker_count": execution.worker_count,
            "total_subtasks": execution.total_subtasks,
            "completed_subtasks": execution.completed_subtasks,
            "failed_subtasks": execution.failed_subtasks,
        },
        "logs": [
            {
                "id": log.id,
                "timestamp": log.timestamp,
                "worker_id": log.worker_id,
                "subtask_id": log.subtask_id,
                "level": log.level,
                "message": log.message,
            }
            for log in logs
        ],
        "count": len(logs),
        "has_more": len(logs) == limit,
    }


@app.get("/api/auto/output/{execution_id}/stream")
async def api_auto_output_stream(
    execution_id: int,
    level: str | None = None,
    worker_id: int | None = None,
):
    """Stream execution output via Server-Sent Events.

    Streams log entries as they're added. Sends heartbeat every 15s.
    Closes when execution completes or client disconnects.

    Event types:
    - log: New log entry
    - status: Execution status update
    - heartbeat: Keep-alive
    """
    from sse_starlette.sse import EventSourceResponse

    db = get_sqlite_db()

    execution = db.get_auto_execution(execution_id)
    if not execution:
        raise HTTPException(
            status_code=404, detail=f"Execution {execution_id} not found"
        )

    async def event_generator():
        last_log_id = 0
        last_status = execution.status

        # Send initial status
        yield {
            "event": "status",
            "data": json.dumps(
                {
                    "execution_id": execution_id,
                    "status": execution.status,
                    "completed_subtasks": execution.completed_subtasks,
                    "failed_subtasks": execution.failed_subtasks,
                    "total_subtasks": execution.total_subtasks,
                }
            ),
        }

        while True:
            # Check for new logs
            logs = db.get_auto_execution_logs(
                execution_id,
                since_id=last_log_id,
                limit=100,
                level=level,
                worker_id=worker_id,
            )

            for log in logs:
                last_log_id = log.id
                yield {
                    "event": "log",
                    "data": json.dumps(
                        {
                            "id": log.id,
                            "timestamp": log.timestamp,
                            "worker_id": log.worker_id,
                            "subtask_id": log.subtask_id,
                            "level": log.level,
                            "message": log.message,
                        }
                    ),
                }

            # Check execution status
            current = db.get_auto_execution(execution_id)
            if current and current.status != last_status:
                last_status = current.status
                yield {
                    "event": "status",
                    "data": json.dumps(
                        {
                            "execution_id": execution_id,
                            "status": current.status,
                            "completed_subtasks": current.completed_subtasks,
                            "failed_subtasks": current.failed_subtasks,
                            "total_subtasks": current.total_subtasks,
                            "completed_at": current.completed_at,
                            "error_message": current.error_message,
                        }
                    ),
                }

                # Stop streaming if execution is done
                if current.status in ("completed", "failed", "cancelled"):
                    break

            # Heartbeat
            yield {
                "event": "heartbeat",
                "data": json.dumps({"timestamp": datetime.now().isoformat()}),
            }

            await asyncio.sleep(1)  # Poll every second

    return EventSourceResponse(event_generator())


# =============================================================================
# Static Assets
# =============================================================================

assets_dir = Path(__file__).parent.parent / "assets"
if assets_dir.exists():
    app.mount("/static", StaticFiles(directory=str(assets_dir)), name="static")

# =============================================================================
# Settings Endpoints
# =============================================================================


class JiraUrlsPayload(BaseModel):
    jira_urls: dict[str, str]


class AuthorEmailsPayload(BaseModel):
    author_emails: list[str]


class RepoOverridesPayload(BaseModel):
    repos: dict[str, dict[str, Any]]


@app.get("/api/settings")
async def get_settings():
    """Return all Tier 1 settings in one payload.

    Used by the Settings view on initial load and by the markdown
    renderer to fetch `jira_urls` for the in-content JIRA regex pass.
    """
    return {
        "jira_urls": config.get_jira_urls(),
        "author_emails": config.get_author_emails(),
        "repos": config.get_repo_overrides(),
        "dashboard_url": config.get_dashboard_url(),
    }


@app.put("/api/settings/jira")
async def update_jira_settings(payload: JiraUrlsPayload):
    """Replace the JIRA prefix-to-URL mapping."""
    config.set_jira_urls(payload.jira_urls)
    return {"ok": True, "jira_urls": config.get_jira_urls()}


@app.put("/api/settings/author-emails")
async def update_author_emails(payload: AuthorEmailsPayload):
    """Replace the author email allowlist used for commit attribution."""
    config.set_author_emails(payload.author_emails)
    return {"ok": True, "author_emails": config.get_author_emails()}


@app.put("/api/settings/repos")
async def update_repo_overrides(payload: RepoOverridesPayload):
    """Replace the per-repo display name and visibility overrides."""
    config.set_repo_overrides(payload.repos)
    return {"ok": True, "repos": config.get_repo_overrides()}


# =============================================================================
# Dashboard & Utility Endpoints
# =============================================================================


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    """Serve the main dashboard HTML."""
    html_path = Path(__file__).parent / "index.html"
    if html_path.exists():
        return FileResponse(html_path, media_type="text/html")
    return HTMLResponse(
        "<h1>Orbit Dashboard</h1><p>index.html not found. Dashboard UI coming soon.</p>",
        status_code=200,
    )




# =============================================================================
# Hook Endpoints (HTTP hooks for Claude Code)
# =============================================================================

# Skip patterns for heartbeat - don't record on these prompts
_HEARTBEAT_SKIP_PATTERNS = [
    re.compile(r"^/\w+"),  # Slash commands
    re.compile(r"^!\w+"),  # Shell commands
    re.compile(r"^exit$", re.I),
    re.compile(r"^clear$", re.I),
    re.compile(r"^help$", re.I),
    re.compile(r"^y(es)?$", re.I),
    re.compile(r"^n(o)?$", re.I),
    re.compile(r"^\s*$"),  # Empty
]

@app.post("/api/hooks/heartbeat")
async def hook_heartbeat(body: dict):
    """HTTP hook: record activity heartbeat on UserPromptSubmit.

    Replaces activity-tracker.sh -> npx tsx -> python3 orbit_db chain.
    """
    # Skip in subagent context
    if body.get("agent_id"):
        return {}

    prompt_raw = body.get("prompt") or ""
    # When the user attaches images, Claude Code sends prompt as a list of content blocks
    prompt = prompt_raw.strip() if isinstance(prompt_raw, str) else ""

    # Skip prompts matching skip patterns
    if any(p.search(prompt) for p in _HEARTBEAT_SKIP_PATTERNS):
        return {}

    cwd = body.get("cwd", "")
    session_id = body.get("session_id", "")

    if not cwd:
        return {}

    try:
        db = get_sqlite_db()
        db.record_heartbeat_auto(cwd, session_id)
    except Exception:
        pass  # Non-blocking

    if session_id:
        try:
            hdb = _get_hooks_state_db()
            hdb.execute(
                """INSERT INTO session_state (session_id, last_prompt_at, updated_at)
                   VALUES (?, datetime('now', 'localtime'), datetime('now', 'localtime'))
                   ON CONFLICT(session_id) DO UPDATE SET
                     last_prompt_at = datetime('now', 'localtime'),
                     updated_at = datetime('now', 'localtime')""",
                (session_id,),
            )
            hdb.commit()
            hdb.close()
        except Exception:
            pass

    return {}


@app.post("/api/hooks/edit-count")
async def hook_edit_count(body: dict):
    """HTTP hook: increment edit count on PostToolUse for Edit/Write/NotebookEdit.

    Writes to both hooks-state DB and legacy file (dual-write for Phase 1).
    """
    tool_name = body.get("tool_name", "")
    session_id = body.get("session_id", "")

    if tool_name not in ("Edit", "Write", "NotebookEdit") or not session_id:
        return {}

    try:
        db = _get_hooks_state_db()
        db.execute(
            """INSERT INTO session_state (session_id, edit_count, updated_at)
               VALUES (?, 1, datetime('now', 'localtime'))
               ON CONFLICT(session_id) DO UPDATE SET
                 edit_count = edit_count + 1,
                 updated_at = datetime('now', 'localtime')""",
            (session_id,),
        )
        db.commit()
        db.close()
    except Exception:
        pass

    return {}


@app.post("/api/hooks/action")
async def hook_action(body: dict):
    """HTTP hook: record current tool action for tab title display.

    Called by tab-title.sh via PostToolUse HTTP hook.
    """
    session_id = body.get("session_id", "")
    action = body.get("action", "")
    if not session_id or not action:
        return {}

    try:
        db = _get_hooks_state_db()
        db.execute(
            """INSERT INTO session_state (session_id, action, updated_at)
               VALUES (?, ?, datetime('now', 'localtime'))
               ON CONFLICT(session_id) DO UPDATE SET
                 action = ?,
                 updated_at = datetime('now', 'localtime')""",
            (session_id, action, action),
        )
        # Keep project timestamp fresh (replaces touch $PROJECT_FILE in tab-title.sh)
        db.execute(
            """UPDATE project_state SET updated_at = datetime('now', 'localtime')
               WHERE session_id = ?""",
            (session_id,),
        )
        db.commit()
        db.close()
    except Exception:
        pass

    return {}


@app.post("/api/hooks/project")
async def hook_project(body: dict):
    """HTTP hook: set active project for a session.

    Called by orbit skills and session_start via Bash.
    """
    session_id = body.get("session_id", "")
    project_name = body.get("project_name", "")
    if not session_id or not project_name:
        return {}

    try:
        db = _get_hooks_state_db()
        db.execute(
            """INSERT INTO project_state (session_id, project_name, updated_at)
               VALUES (?, ?, datetime('now', 'localtime'))
               ON CONFLICT(session_id) DO UPDATE SET
                 project_name = ?,
                 updated_at = datetime('now', 'localtime')""",
            (session_id, project_name, project_name),
        )
        db.commit()
        db.close()
    except Exception:
        pass

    return {}


@app.get("/api/hooks/term-session/{term_session_id}")
async def hook_get_term_session(term_session_id: str):
    """Resolve TERM_SESSION_ID to Claude session_id.

    Used by orbit skills to find the current session for project registration.
    """
    try:
        db = _get_hooks_state_db()
        row = db.execute(
            "SELECT session_id FROM term_sessions WHERE term_session_id = ?",
            (term_session_id,),
        ).fetchone()
        db.close()
        if row:
            return {"session_id": row["session_id"]}
    except Exception:
        pass
    return {}


@app.get("/api/hooks/session/{session_id}")
async def hook_get_session(session_id: str):
    """Read session state from hooks-state DB.

    Used by qa-reviewer-prompt.sh and other hooks that need session data.
    """
    try:
        db = _get_hooks_state_db()
        row = db.execute(
            "SELECT * FROM session_state WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        db.close()
        if row:
            return dict(row)
    except Exception:
        pass
    return {}


@app.post("/api/hooks/qa-review")
async def hook_qa_review(body: dict):
    """HTTP hook: mark QA review as suggested for a session."""
    session_id = body.get("session_id", "")
    if not session_id:
        return {}

    try:
        db = _get_hooks_state_db()
        db.execute(
            """UPDATE session_state SET qa_review_suggested = 1,
                 updated_at = datetime('now', 'localtime')
               WHERE session_id = ?""",
            (session_id,),
        )
        db.commit()
        db.close()
    except Exception:
        pass

    return {}


@app.post("/api/hooks/task-created")
async def hook_task_created(body: dict):
    """HTTP hook: fires when TaskCreate tool is used. Triggers DB sync."""
    try:
        db = get_db()
        db.sync_from_sqlite()
    except Exception:
        pass
    return {}





@app.get("/health")
async def health_check():
    """Health check endpoint."""
    db = get_db()
    return {
        "status": "healthy",
        "duckdb_path": str(db.db_path),
        "duckdb_exists": db.db_path.exists(),
        "timestamp": datetime.now().isoformat(),
    }


@app.post("/api/sync")
async def sync_databases():
    """Manually trigger sync from SQLite to DuckDB."""
    db = get_db()
    result = db.sync_from_sqlite()
    return {
        "status": "synced",
        "result": result,
        "timestamp": datetime.now().isoformat(),
    }




# =============================================================================
# Server-Sent Events for Live Updates
# =============================================================================


async def event_generator():
    """Generate Server-Sent Events with updated data."""
    while True:
        db = get_db()
        data = {
            "productivity": db.get_today_stats(),
            "timestamp": datetime.now().isoformat(),
        }
        yield f"data: {json.dumps(data)}\n\n"
        await asyncio.sleep(REFRESH_INTERVAL)


@app.get("/api/stream")
async def stream_updates():
    """Stream updates via Server-Sent Events."""
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


# =============================================================================
# Main Entry Point
# =============================================================================

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("ORBIT_DASHBOARD_PORT", "8787"))
    uvicorn.run(app, host="127.0.0.1", port=port)

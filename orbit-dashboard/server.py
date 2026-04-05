#!/usr/bin/env python3
"""
Orbit Dashboard - Task & Analytics Dashboard

A FastAPI server that provides:
1. Task APIs - Task tracking, time analytics (DuckDB)
2. Claude Code Usage APIs - Stats, projects, usage limits
3. Plans APIs - Parallel execution monitoring
4. Auto APIs - Orbit-auto execution tracking

Port: 8787
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel

# Add lib to path for imports
sys.path.insert(0, str(Path(__file__).parent))
from lib.analytics_db import (
    AnalyticsDB,
    get_db,
    get_claude_hourly_activity,
    get_claude_daily_activity,
    merge_hourly_activity,
    ClaudeSessionCache,
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

# Plan SSE subscribers - maps plan_id to list of async queues
plan_subscribers: dict[int, list[asyncio.Queue]] = {}
PLAN_SSE_HEARTBEAT_SECONDS = 30

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
STATS_FILE = CLAUDE_DIR / "stats-cache.json"
PROJECTS_DIR = CLAUDE_DIR / "projects"
ORBIT_DB_SCRIPT = CLAUDE_DIR / "scripts" / "orbit_db.py"
USAGE_CACHE_FILE = CLAUDE_DIR / "usage-limits-cache.json"
HOOKS_STATE_DB = CLAUDE_DIR / "hooks-state.db"

# Cache TTLs
USAGE_CACHE_TTL_SECONDS = 300  # 5 minutes
REFRESH_INTERVAL = 30  # seconds for SSE

# JIRA URL mapping
JIRA_URLS = {
    "PROJ-": "https://example.com/jira/browse/",
    "GC-": "https://example.atlassian.net/browse/",
}


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


def format_reset_time(iso_timestamp: str) -> str:
    """Format ISO timestamp to human-readable format."""
    try:
        dt = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
        local_dt = dt.astimezone()
        return local_dt.strftime("%a %-I:%M %p")
    except Exception:
        return "unknown"


def get_jira_url(jira_key: str | None) -> str | None:
    """Get full JIRA URL from key."""
    if not jira_key:
        return None
    for prefix, base_url in JIRA_URLS.items():
        if jira_key.startswith(prefix):
            return base_url + jira_key
    return None


# =============================================================================
# Git LOC Statistics
# =============================================================================

# Known user email addresses for commit filtering
USER_EMAILS = ["noreply@users.noreply.github.com", "noreply@users.noreply.github.com"]

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
        # Build author filter for user's commits only
        author_args = []
        for email in USER_EMAILS:
            author_args.extend(["--author", email])

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
# Dev-docs Parsing
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

        if completed:
            # Completed tasks are never blocked
            continue

        # Get all dependencies (explicit + sequential)
        all_deps = _get_sequential_dependencies(task_id, task_modes)
        all_deps.extend(explicit_deps)
        # Deduplicate while preserving order
        all_deps = list(dict.fromkeys(all_deps))

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

            except Exception:
                pass

    except Exception:
        pass

    return result


# =============================================================================
# Productivity APIs (DuckDB)
# =============================================================================


@app.get("/api/tasks")
async def api_tasks(
    status: str = Query(
        None, description="Filter by status: active, completed, paused"
    ),
    repo_id: int = Query(None, description="Filter by repository ID"),
):
    """Get tasks with optional filters."""
    db = get_db()

    if status == "active":
        tasks = db.get_active_tasks(repo_id)
    elif status == "completed":
        tasks = db.get_completed_tasks(days=30)
    else:
        tasks = db.get_tasks_with_repo(status or "active")
        return {
            "tasks": tasks,
            "count": len(tasks),
            "timestamp": datetime.now().isoformat(),
        }

    # Get time for each task
    task_ids = [t.id for t in tasks]
    times = db.get_batch_task_times(task_ids, period="all")

    result = []
    for task in tasks:
        task_dict = task.to_dict()
        task_dict["time_spent_seconds"] = times.get(task.id, 0)
        task_dict["time_spent_formatted"] = db.format_duration(times.get(task.id, 0))
        task_dict["jira_url"] = get_jira_url(task.jira_key)

        # Get subtask time if this is a parent
        subtask_time = db.get_subtask_time_total(task.id)
        if subtask_time > 0:
            task_dict["subtask_time_seconds"] = subtask_time
            task_dict["subtask_time_formatted"] = db.format_duration(subtask_time)

        result.append(task_dict)

    return {
        "tasks": result,
        "count": len(result),
        "timestamp": datetime.now().isoformat(),
    }


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

    # Cache repos for efficiency
    repos_cache: dict[int, Any] = {}

    for task in tasks:
        task_dict = task.to_dict()
        task_dict["time_spent_seconds"] = times.get(task.id, 0)
        task_dict["time_spent_formatted"] = db.format_duration(times.get(task.id, 0))
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

    result = []
    for task in tasks:
        task_dict = task.to_dict()
        task_dict["time_spent_seconds"] = times.get(task.id, 0)
        task_dict["time_spent_formatted"] = db.format_duration(times.get(task.id, 0))
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


@app.get("/api/task/{task_id}")
async def api_task_detail(task_id: int):
    """Get detailed info for a specific task."""
    db = get_db()
    task = db.get_task(task_id)

    if not task:
        return {"error": "Task not found", "task_id": task_id}

    task_dict = task.to_dict()
    task_dict["time_spent_seconds"] = db.get_task_time(task_id)
    task_dict["time_spent_formatted"] = db.format_duration(
        task_dict["time_spent_seconds"]
    )
    task_dict["jira_url"] = get_jira_url(task.jira_key)

    # Get repo info
    if task.repo_id:
        repo = db.get_repo(task.repo_id)
        task_dict["repo_name"] = repo.short_name if repo else None
        task_dict["repo_path"] = repo.path if repo else None

    # Get subtasks if parent
    subtasks = db.get_subtasks(task_id)
    if subtasks:
        subtask_ids = [s.id for s in subtasks]
        subtask_times = db.get_batch_task_times(subtask_ids)
        task_dict["subtasks"] = [
            {
                **s.to_dict(),
                "time_spent_seconds": subtask_times.get(s.id, 0),
                "time_spent_formatted": db.format_duration(subtask_times.get(s.id, 0)),
            }
            for s in subtasks
        ]

    return task_dict


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


@app.get("/api/stats/today")
async def api_stats_today():
    """Get today's productivity statistics.

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

    # Get repo breakdown from sessions
    repo_breakdown = {}
    for s in sessions:
        repo = s.get("repo_name") or "unknown"
        if repo not in repo_breakdown:
            repo_breakdown[repo] = {"seconds": 0, "sessions": 0}
        repo_breakdown[repo]["seconds"] += s["duration_seconds"]
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
        "tasks_today": tasks_today[:10],
        "sessions": sessions,  # For timeline visualization
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/api/stats/day")
async def api_stats_day(
    date: str = Query(..., description="Date in YYYY-MM-DD format"),
):
    """Get productivity statistics for a specific date.

    Includes both orbit task activity and Claude Code activity from JSONL files.
    """
    db = get_db()

    stats = db.get_date_stats(date)
    task_hourly = db.get_hourly_activity(date)
    sessions = db.get_sessions_for_timeline(date)  # Timeline data
    tasks_raw = db.get_tasks_today_from_sqlite(date)  # Get tasks for this date

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
        "tasks_today": tasks_today[:10],  # Match today endpoint's limit
        "sessions": sessions,  # For timeline visualization
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/api/stats/history")
async def api_stats_history(days: int = 7):
    """Get historical productivity statistics.

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

    # Also merge into daily_by_date
    for day in daily_by_date:
        date = day.get("date")
        claude_data = claude_by_date.get(date, {})
        day["claude_messages"] = claude_data.get("claude_messages", 0)
        day["claude_tool_calls"] = claude_data.get("claude_tool_calls", 0)
        day["claude_tokens"] = claude_data.get("claude_tokens", 0)
        day["claude_seconds"] = claude_data.get("claude_seconds", 0)

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


# =============================================================================
# Pull Request Stats (GitHub CLI)
# =============================================================================


def find_gh_cli() -> str | None:
    """Find the gh CLI executable."""
    return shutil.which("gh")


def get_pr_stats(days: int = 7) -> dict[str, Any]:
    """Get PR statistics using the gh CLI (search API for global results)."""
    gh = find_gh_cli()
    if not gh:
        return {"error": "GitHub CLI not found", "available": False}

    result = {
        "available": True,
        "days": days,
        "summary": {
            "merged_count": 0,
            "open_count": 0,
            "today_count": 0,
            "avg_review_hours": None,
        },
        "merged": [],
        "open": [],
    }

    try:
        # Get merged PRs using gh search prs (works globally, not just in current repo)
        since_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        merged_cmd = [
            gh,
            "search",
            "prs",
            "--author",
            "@me",
            "--merged",
            f"--merged-at",
            f">={since_date}",
            "--json",
            "number,title,repository,url,createdAt,closedAt",
            "--limit",
            "50",
        ]
        merged_result = subprocess.run(
            merged_cmd, capture_output=True, text=True, timeout=30
        )

        if merged_result.returncode == 0 and merged_result.stdout.strip():
            merged_prs = json.loads(merged_result.stdout)
            result["summary"]["merged_count"] = len(merged_prs)

            # Calculate avg review time and today count
            today = datetime.now().strftime("%Y-%m-%d")
            review_times = []
            for pr in merged_prs:
                # Parse repo name from repository object
                repo_info = pr.get("repository", {})
                repo_name = repo_info.get("name", "")
                if not repo_name and pr.get("url"):
                    parts = pr["url"].split("/")
                    if len(parts) >= 5:
                        repo_name = parts[-3]

                # For search results, closedAt is the merge time for merged PRs
                merged_at = pr.get("closedAt", "")
                created_at = pr.get("createdAt", "")

                # Count PRs merged today
                if merged_at and merged_at.startswith(today):
                    result["summary"]["today_count"] += 1

                # Calculate review time (time from creation to merge)
                if merged_at and created_at:
                    try:
                        merged_dt = datetime.fromisoformat(
                            merged_at.replace("Z", "+00:00")
                        )
                        created_dt = datetime.fromisoformat(
                            created_at.replace("Z", "+00:00")
                        )
                        hours = (merged_dt - created_dt).total_seconds() / 3600
                        review_times.append(hours)
                    except Exception:
                        pass

                # Format time ago
                time_ago = ""
                if merged_at:
                    try:
                        merged_dt = datetime.fromisoformat(
                            merged_at.replace("Z", "+00:00")
                        )
                        delta = datetime.now(timezone.utc) - merged_dt
                        if delta.days > 0:
                            time_ago = f"{delta.days}d ago"
                        elif delta.seconds >= 3600:
                            time_ago = f"{delta.seconds // 3600}h ago"
                        else:
                            time_ago = f"{delta.seconds // 60}m ago"
                    except Exception:
                        pass

                result["merged"].append(
                    {
                        "number": pr.get("number"),
                        "title": pr.get("title", ""),
                        "repo": repo_name,
                        "url": pr.get("url", ""),
                        "merged_at": merged_at,
                        "time_ago": time_ago,
                    }
                )

            if review_times:
                result["summary"]["avg_review_hours"] = round(
                    sum(review_times) / len(review_times), 1
                )

        # Get open PRs using gh search prs
        open_cmd = [
            gh,
            "search",
            "prs",
            "--author",
            "@me",
            "--state",
            "open",
            "--json",
            "number,title,repository,url,createdAt",
            "--limit",
            "20",
        ]
        open_result = subprocess.run(
            open_cmd, capture_output=True, text=True, timeout=30
        )

        if open_result.returncode == 0 and open_result.stdout.strip():
            open_prs = json.loads(open_result.stdout)
            result["summary"]["open_count"] = len(open_prs)

            for pr in open_prs:
                repo_info = pr.get("repository", {})
                repo_name = repo_info.get("name", "")
                if not repo_name and pr.get("url"):
                    parts = pr["url"].split("/")
                    if len(parts) >= 5:
                        repo_name = parts[-3]

                created_at = pr.get("createdAt", "")
                time_ago = ""
                if created_at:
                    try:
                        created_dt = datetime.fromisoformat(
                            created_at.replace("Z", "+00:00")
                        )
                        delta = datetime.now(timezone.utc) - created_dt
                        if delta.days > 0:
                            time_ago = f"{delta.days}d ago"
                        elif delta.seconds >= 3600:
                            time_ago = f"{delta.seconds // 3600}h ago"
                        else:
                            time_ago = f"{delta.seconds // 60}m ago"
                    except Exception:
                        pass

                result["open"].append(
                    {
                        "number": pr.get("number"),
                        "title": pr.get("title", ""),
                        "repo": repo_name,
                        "url": pr.get("url", ""),
                        "created_at": created_at,
                        "time_ago": time_ago,
                    }
                )

    except subprocess.TimeoutExpired:
        return {"error": "GitHub CLI timed out", "available": False}
    except Exception as e:
        return {"error": str(e), "available": False}

    return result


@app.get("/api/stats/prs")
async def api_stats_prs(days: int = 7):
    """Get PR statistics from GitHub."""
    return get_pr_stats(days=days)


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
# Claude Code Usage APIs
# =============================================================================


def get_oauth_token() -> Optional[str]:
    """Read OAuth token from macOS Keychain."""
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                "Claude Code-credentials",
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        creds = json.loads(result.stdout.strip())
        return creds.get("claudeAiOauth", {}).get("accessToken")
    except Exception:
        return None


def load_usage_cache() -> Optional[dict]:
    """Load cached usage data if still valid."""
    try:
        if not USAGE_CACHE_FILE.exists():
            return None
        cache = json.loads(USAGE_CACHE_FILE.read_text())
        cached_at = datetime.fromisoformat(cache["cached_at"])
        age_seconds = (datetime.now(timezone.utc) - cached_at).total_seconds()
        if age_seconds < USAGE_CACHE_TTL_SECONDS:
            return cache["data"]
        return None
    except Exception:
        return None


def save_usage_cache(data: dict) -> None:
    """Save usage data to cache."""
    try:
        cache = {
            "cached_at": datetime.now(timezone.utc).isoformat(),
            "data": data,
        }
        USAGE_CACHE_FILE.write_text(json.dumps(cache, indent=2))
    except Exception:
        pass


def fetch_usage_from_api(token: str) -> Optional[dict]:
    """Fetch usage data from Anthropic API."""
    try:
        req = urllib.request.Request(
            "https://api.anthropic.com/api/oauth/usage",
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
                "User-Agent": "orbit-dashboard/2.0",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read().decode())
    except Exception:
        return None


def format_usage_data(data: dict, from_cache: bool = False) -> dict[str, Any]:
    """Format raw usage data into structured response."""
    result = {
        "available": True,
        "from_cache": from_cache,
        "session": None,
        "weekly": None,
        "weekly_opus": None,
    }

    if data.get("five_hour") is not None:
        result["session"] = {
            "utilization": data["five_hour"].get("utilization", 0),
            "resets_at": data["five_hour"].get("resets_at"),
            "resets_at_formatted": format_reset_time(
                data["five_hour"].get("resets_at", "")
            ),
        }

    if data.get("seven_day") is not None:
        result["weekly"] = {
            "utilization": data["seven_day"].get("utilization", 0),
            "resets_at": data["seven_day"].get("resets_at"),
            "resets_at_formatted": format_reset_time(
                data["seven_day"].get("resets_at", "")
            ),
        }

    if data.get("seven_day_opus") is not None:
        result["weekly_opus"] = {
            "utilization": data["seven_day_opus"].get("utilization", 0),
            "resets_at": data["seven_day_opus"].get("resets_at"),
            "resets_at_formatted": format_reset_time(
                data["seven_day_opus"].get("resets_at", "")
            ),
        }

    return result


@app.get("/api/usage/limits")
async def api_usage_limits():
    """Get Claude Code usage limits from Anthropic API."""
    cached_data = load_usage_cache()
    if cached_data:
        return format_usage_data(cached_data, from_cache=True)

    token = get_oauth_token()
    if not token:
        return {"error": "OAuth token not found", "available": False}

    data = fetch_usage_from_api(token)
    if not data:
        return {"error": "Failed to fetch usage data", "available": False}

    save_usage_cache(data)
    return format_usage_data(data, from_cache=False)


@app.get("/api/usage/stats")
async def api_usage_stats():
    """Get stats from Claude Code stats-cache.json."""
    if not STATS_FILE.exists():
        return {"error": "Stats file not found", "path": str(STATS_FILE)}

    try:
        with open(STATS_FILE, "r") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON: {e}"}
    except Exception as e:
        return {"error": str(e)}


# Foundry pricing per 1M tokens (input, output)
FOUNDRY_PRICING = {
    "claude-sonnet-4-5-20250929": (3.0, 15.0),
    "claude-opus-4-5-20251101": (5.0, 25.0),
    "claude-opus-4-20250514": (15.0, 75.0),
    "claude-haiku-4-5-20250514": (1.0, 5.0),
}


def calculate_foundry_usage() -> dict | None:
    """Calculate token count and cost from stats-cache.json for Foundry API."""
    try:
        if not STATS_FILE.exists():
            return None
        stats = json.loads(STATS_FILE.read_text())
    except Exception:
        return None

    # Sum today's tokens
    today = datetime.now().strftime("%Y-%m-%d")
    tokens_today = 0
    for entry in stats.get("dailyModelTokens", []):
        if entry.get("date") == today:
            tokens_today = sum(entry.get("tokensByModel", {}).values())

    # Calculate total cost from modelUsage
    cost = 0.0
    total_input = 0
    total_output = 0
    for model, usage in stats.get("modelUsage", {}).items():
        input_tokens = usage.get("inputTokens", 0)
        output_tokens = usage.get("outputTokens", 0)
        total_input += input_tokens
        total_output += output_tokens
        rate = FOUNDRY_PRICING.get(model, (5.0, 25.0))  # Default to Opus 4.5 pricing
        cost += (input_tokens / 1_000_000) * rate[0]
        cost += (output_tokens / 1_000_000) * rate[1]

    return {
        "tokens_today": tokens_today,
        "cost_total": round(cost, 2),
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_tokens": total_input + total_output,
    }


@app.get("/api/usage/foundry")
async def api_usage_foundry():
    """Get example Foundry API usage stats (tokens + cost)."""
    data = calculate_foundry_usage()
    if not data:
        return {"error": "Stats file not found", "available": False}
    return {"available": True, **data}


MODE_FILE = CLAUDE_DIR / "usage-mode.json"


@app.get("/api/usage/mode")
async def api_usage_mode():
    """Get current usage mode (private vs foundry).

    Returns the active mode from usage-mode.json which is maintained
    by the sync-usage-mode.py SessionStart hook.
    """
    if not MODE_FILE.exists():
        return {"mode": "private", "switched_at": None, "initialized": False}

    try:
        data = json.loads(MODE_FILE.read_text())
        return {
            "mode": data.get("mode", "private"),
            "switched_at": data.get("switched_at"),
            "initialized": True,
        }
    except Exception as e:
        return {"mode": "private", "switched_at": None, "error": str(e)}


@app.get("/api/usage/projects")
async def api_usage_projects():
    """Get per-project Claude Code usage breakdown."""
    if not PROJECTS_DIR.exists():
        return {"error": "Projects directory not found", "projects": {}}

    projects = {}

    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue

        project_name = project_dir.name
        entries = []

        # Read all JSONL files
        for jsonl_file in project_dir.glob("*.jsonl"):
            try:
                with open(jsonl_file, "r") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                entries.append(json.loads(line))
                            except json.JSONDecodeError:
                                continue
            except Exception:
                continue

        if not entries:
            continue

        # Calculate metrics
        messages = len([e for e in entries if e.get("type") in ("user", "assistant")])
        sessions = len(set(e.get("sessionId") for e in entries if e.get("sessionId")))

        projects[project_name] = {
            "messages": messages,
            "sessions": sessions,
            "entry_count": len(entries),
        }

    return {
        "projects": projects,
        "count": len(projects),
        "timestamp": datetime.now().isoformat(),
    }


# =============================================================================
# Orbit-Auto Loop Monitoring APIs
# =============================================================================


class AutoTaskStatus(BaseModel):
    """Status of a single orbit-auto task."""

    id: str
    status: str  # pending, in_progress, completed, failed
    worker: int | None = None
    attempts: int = 0
    title: str = ""
    agents: list[str] = []
    skills: list[str] = []
    error_message: str | None = None  # Last error message when task failed


class AutoWorker(BaseModel):
    """Worker status."""

    id: int
    task_id: str | None = None
    status: str = "idle"  # idle, running


class AutoWave(BaseModel):
    """A wave of parallel tasks."""

    wave: int
    tasks: list[str]


class AutoExecStatus(BaseModel):
    """Active orbit-auto execution."""

    task_name: str
    repo_path: str
    repo_name: str
    status: str  # running, completed, failed
    started: str
    elapsed_seconds: int
    tasks: dict[str, AutoTaskStatus]
    progress: dict[str, int]  # pending, in_progress, completed, failed counts
    waves: list[AutoWave]
    adjacency: dict[str, list[str]]  # task_id -> dependencies
    workers: list[AutoWorker]
    active_worker_count: int


class AutoActiveList(BaseModel):
    """List of active orbit-auto executions."""

    executions: list[dict]  # Simplified info for list view
    count: int


def find_auto_state_files() -> list[tuple[Path, str, str]]:
    """
    Find all active orbit-auto state files across tracked repos.

    Returns list of (state_file_path, task_name, repo_path) tuples.
    """
    results = []
    repo_paths = []

    # Try to get repos from database
    try:
        db = get_db()
        repos = db.get_repos(active_only=True)
        repo_paths = [Path(repo.path) for repo in repos]
    except Exception:
        pass

    # Fallback: scan known work directories if no repos from DB
    if not repo_paths:
        work_dir = Path.home() / "work"
        if work_dir.exists():
            # Look for directories with dev/active structure
            for project_dir in work_dir.iterdir():
                if project_dir.is_dir():
                    repo_paths.append(project_dir)

    # Check centralized orbit root for orbit-auto state files
    orbit_active = ORBIT_ROOT / "active"
    if orbit_active.exists():
        for task_dir in orbit_active.iterdir():
            if not task_dir.is_dir():
                continue
            state_file = task_dir / ".orbit-auto-state" / "state.json"
            if state_file.exists():
                results.append((state_file, task_dir.name, str(orbit_active)))

    # Legacy: check repo-local paths
    for repo_path in repo_paths:
        if not repo_path.exists():
            continue
        dev_active = repo_path / "dev" / "active"
        if not dev_active.exists():
            continue
        for task_dir in dev_active.iterdir():
            if not task_dir.is_dir():
                continue
            state_file = task_dir / ".orbit-auto-state" / "state.json"
            if state_file.exists():
                results.append((state_file, task_dir.name, str(repo_path)))

    return results


def parse_auto_state(state_file: Path) -> dict | None:
    """
    Parse an orbit-auto state.json file with shared file locking.

    Returns parsed state dict or None on error.
    """
    import fcntl

    try:
        lock_file = state_file.parent / "state.lock"

        # Use shared lock for reading
        with open(lock_file, "w") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
                data = json.loads(state_file.read_text())
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                return data
            except BlockingIOError:
                # Lock held, try without lock (best effort)
                return json.loads(state_file.read_text())
    except Exception:
        return None


def parse_adjacency_file(state_dir: Path) -> dict[str, list[str]]:
    """
    Parse adjacency.txt file for DAG dependencies.

    Format: task_id:dep1,dep2,dep3
    """
    adjacency_file = state_dir / "adjacency.txt"
    adjacency: dict[str, list[str]] = {}

    if not adjacency_file.exists():
        return adjacency

    try:
        for line in adjacency_file.read_text().strip().split("\n"):
            if not line or ":" not in line:
                continue
            task_id, deps_str = line.split(":", 1)
            deps = [d.strip() for d in deps_str.split(",") if d.strip()]
            adjacency[task_id] = deps
    except Exception:
        pass

    return adjacency


def compute_dag_waves(adjacency: dict[str, list[str]]) -> list[dict]:
    """
    Compute execution waves from adjacency list.

    Returns list of {wave: int, tasks: list[str]} dicts.
    """
    task_wave: dict[str, int] = {}

    def get_wave(task: str) -> int:
        if task in task_wave:
            return task_wave[task]

        deps = adjacency.get(task, [])
        if not deps:
            task_wave[task] = 1
            return 1

        max_dep_wave = max(get_wave(d) for d in deps if d in adjacency)
        wave = max_dep_wave + 1 if deps else 1
        task_wave[task] = wave
        return wave

    # Compute waves for all tasks
    for task in adjacency:
        get_wave(task)

    # Group by wave
    from collections import defaultdict

    wave_tasks: dict[int, list[str]] = defaultdict(list)
    for task, wave in task_wave.items():
        wave_tasks[wave].append(task)

    # Build result
    result = []
    for w in sorted(wave_tasks.keys()):
        tasks = sorted(wave_tasks[w])
        result.append({"wave": w, "tasks": tasks})

    return result


def get_task_metadata_from_prompts(state_dir: Path) -> dict[str, dict]:
    """Extract task metadata (title, agents, skills) from prompt files."""
    metadata: dict[str, dict] = {}
    prompts_dir = state_dir.parent / "prompts"

    if not prompts_dir.exists():
        return metadata

    import re
    import yaml

    for prompt_file in prompts_dir.glob("task-*-prompt.md"):
        try:
            content = prompt_file.read_text()

            # Extract YAML frontmatter
            frontmatter_match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
            if not frontmatter_match:
                continue

            try:
                fm = yaml.safe_load(frontmatter_match.group(1))
            except Exception:
                # Fallback to regex parsing
                fm = {}
                id_match = re.search(
                    r'^task_id:\s*["\']?([^"\'\n]+)["\']?', content, re.MULTILINE
                )
                title_match = re.search(
                    r'^task_title:\s*["\']?([^"\'\n]+)["\']?', content, re.MULTILINE
                )
                if id_match:
                    fm["task_id"] = id_match.group(1).strip()
                if title_match:
                    fm["task_title"] = title_match.group(1).strip()

            task_id = fm.get("task_id")
            if task_id:
                metadata[str(task_id)] = {
                    "title": fm.get("task_title", f"Task {task_id}"),
                    "agents": fm.get("agents", []) or [],
                    "skills": fm.get("skills", []) or [],
                }
        except Exception:
            continue

    return metadata


def is_auto_stale(state_file: Path, state: dict) -> bool:
    """
    Check if an orbit-auto execution is stale (stopped but not cleaned up).

    An execution is considered stale if:
    - The state file hasn't been modified in 30+ seconds AND
    - There are in_progress tasks (workers should update frequently)
    """
    STALE_THRESHOLD_SECONDS = 30

    try:
        mtime = state_file.stat().st_mtime
        age_seconds = time.time() - mtime

        # If file was recently modified, not stale
        if age_seconds < STALE_THRESHOLD_SECONDS:
            return False

        # If there are in_progress tasks but file is old, it's stale
        tasks = state.get("tasks", {})
        has_in_progress = any(t.get("status") == "in_progress" for t in tasks.values())

        return has_in_progress

    except Exception:
        return False


# =============================================================================
# Plans API
# =============================================================================


@app.get("/api/plans")
async def api_list_plans(
    status: str = Query(
        None, description="Filter by status: draft, pending, running, completed, failed"
    ),
    task_id: int = Query(None, description="Filter by associated task ID"),
    limit: int = Query(
        50, ge=1, le=500, description="Maximum number of plans to return"
    ),
):
    """List execution plans with optional filters."""
    db = get_db()
    plans = db.list_plans(status=status, task_id=task_id, limit=limit)

    return {
        "plans": plans,
        "count": len(plans),
        "timestamp": datetime.now().isoformat(),
    }


class CreatePlanRequest(BaseModel):
    """Request body for creating a new plan."""

    name: str
    task_id: int | None = None
    description: str | None = None


@app.post("/api/plans")
async def api_create_plan(request: CreatePlanRequest):
    """Create a new execution plan.

    Args:
        name: Plan name (required). Must be a valid identifier.
        task_id: Optional associated task ID.
        description: Optional description stored in metadata.

    Returns:
        The created plan with its ID and status.
    """
    db = get_db()

    # Validate plan name format (letters, numbers, hyphens, underscores, starts with letter)
    import re

    if not re.match(r"^[a-zA-Z][a-zA-Z0-9_-]*$", request.name):
        raise HTTPException(
            status_code=400,
            detail="Plan name must start with a letter and contain only letters, numbers, hyphens, and underscores",
        )

    # Build metadata
    metadata = {}
    if request.description:
        metadata["description"] = request.description

    # Create the plan
    plan_id = db.create_plan(
        name=request.name,
        task_id=request.task_id,
        metadata=metadata if metadata else None,
    )

    return {
        "plan_id": plan_id,
        "status": "draft",
        "name": request.name,
        "task_id": request.task_id,
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/api/plans/{plan_id}")
async def api_get_plan(plan_id: int):
    """Get plan details with agents and dependencies."""
    db = get_db()
    plan = db.get_plan(plan_id)

    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    agents = db.get_plan_agents(plan_id)
    dependencies = db.get_plan_dependency_graph(plan_id)

    # Build summary counts
    summary = {
        "total": len(agents),
        "completed": len([a for a in agents if a["status"] == "completed"]),
        "failed": len([a for a in agents if a["status"] == "failed"]),
        "running": len([a for a in agents if a["status"] == "running"]),
        "pending": len([a for a in agents if a["status"] == "pending"]),
        "blocked": len([a for a in agents if a["status"] == "blocked"]),
    }

    return {
        "plan": plan,
        "agents": agents,
        "dependencies": dependencies,
        "summary": summary,
        "timestamp": datetime.now().isoformat(),
    }


@app.put("/api/plans/{plan_id}")
async def api_update_plan(plan_id: int, request: dict):
    """Update plan properties.

    Allows updating: name, status, metadata.
    Status transitions are validated according to the state machine.
    Metadata is merged with existing metadata (not replaced).
    """
    db = get_db()
    plan = db.get_plan(plan_id)

    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    # Only allow updating certain fields
    allowed_fields = {"name", "status", "metadata"}
    updates = {k: v for k, v in request.items() if k in allowed_fields}

    if not updates:
        raise HTTPException(
            status_code=400,
            detail=f"No valid fields to update. Allowed: {', '.join(allowed_fields)}",
        )

    # Validate status transitions
    if "status" in updates:
        valid_transitions = {
            "draft": ["pending"],
            "pending": ["running", "draft"],
            "running": ["completed", "failed"],
            "completed": [],  # Terminal state
            "failed": ["pending"],  # Allow retry
        }
        current_status = plan["status"]
        new_status = updates["status"]

        if new_status not in valid_transitions.get(current_status, []):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status transition: {current_status} -> {new_status}. "
                f"Allowed transitions from '{current_status}': {valid_transitions.get(current_status, [])}",
            )

    # Don't allow editing terminal state plans (except retry via status change)
    if plan["status"] in ("completed", "failed"):
        # Only allow status change (retry) for failed plans
        if plan["status"] == "failed" and updates == {"status": "pending"}:
            pass  # Allow retry
        elif "status" not in updates:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot modify {plan['status']} plan. "
                f"{'Use status=pending to retry.' if plan['status'] == 'failed' else ''}",
            )

    # Apply updates (metadata is merged, not replaced)
    merge_metadata = "metadata" in updates
    db.update_plan(
        plan_id,
        name=updates.get("name"),
        status=updates.get("status"),
        metadata=updates.get("metadata"),
        merge_metadata=merge_metadata,
    )

    # Return updated plan
    updated_plan = db.get_plan(plan_id)
    return {
        "plan": updated_plan,
        "timestamp": datetime.now().isoformat(),
    }


async def plan_event_generator(plan_id: int):
    """Generate Server-Sent Events for a specific plan.

    Sends initial state on connection, then waits for updates via queue.
    Includes heartbeat every PLAN_SSE_HEARTBEAT_SECONDS to keep connection alive.
    """
    queue: asyncio.Queue = asyncio.Queue()

    # Register this subscriber
    if plan_id not in plan_subscribers:
        plan_subscribers[plan_id] = []
    plan_subscribers[plan_id].append(queue)

    try:
        # Send initial state
        db = get_db()
        plan = db.get_plan(plan_id)
        if plan:
            agents = db.get_plan_agents(plan_id)
            dependencies = db.get_plan_dependency_graph(plan_id)
            summary = {
                "total": len(agents),
                "completed": len([a for a in agents if a["status"] == "completed"]),
                "failed": len([a for a in agents if a["status"] == "failed"]),
                "running": len([a for a in agents if a["status"] == "running"]),
                "pending": len([a for a in agents if a["status"] == "pending"]),
                "blocked": len([a for a in agents if a["status"] == "blocked"]),
            }
            init_data = {
                "type": "init",
                "data": {
                    "plan": plan,
                    "agents": agents,
                    "dependencies": dependencies,
                    "summary": summary,
                    "timestamp": datetime.now().isoformat(),
                },
            }
            yield f"data: {json.dumps(init_data)}\n\n"

        # Wait for updates with periodic heartbeat
        while True:
            try:
                # Wait for an event with timeout for heartbeat
                event = await asyncio.wait_for(
                    queue.get(), timeout=PLAN_SSE_HEARTBEAT_SECONDS
                )
                yield f"data: {json.dumps(event)}\n\n"
            except asyncio.TimeoutError:
                # Send heartbeat to keep connection alive
                heartbeat = {
                    "type": "heartbeat",
                    "data": {"timestamp": datetime.now().isoformat()},
                }
                yield f"data: {json.dumps(heartbeat)}\n\n"
    finally:
        # Unregister subscriber on disconnect
        if plan_id in plan_subscribers:
            plan_subscribers[plan_id].remove(queue)
            # Clean up empty subscriber lists
            if not plan_subscribers[plan_id]:
                del plan_subscribers[plan_id]


async def notify_plan_update(plan_id: int, event_type: str, data: dict) -> int:
    """Broadcast an update to all subscribers of a plan.

    Args:
        plan_id: The plan to broadcast to
        event_type: Event type (e.g., 'agent_update', 'plan_complete')
        data: Event data payload

    Returns:
        Number of subscribers notified
    """
    if plan_id not in plan_subscribers:
        return 0

    event = {
        "type": event_type,
        "data": {**data, "timestamp": datetime.now().isoformat()},
    }
    notified = 0
    for queue in plan_subscribers[plan_id]:
        await queue.put(event)
        notified += 1
    return notified


@app.get("/api/plans/{plan_id}/stream")
async def stream_plan_updates(plan_id: int):
    """SSE endpoint for real-time plan updates.

    Event types:
    - init: Initial plan state (sent immediately on connection)
    - agent_update: Agent status changed
    - plan_complete: Plan execution finished
    - heartbeat: Keep-alive (every 30s)

    Usage:
        const es = new EventSource('/api/plans/1/stream');
        es.onmessage = (e) => console.log(JSON.parse(e.data));
    """
    # Verify plan exists before starting stream
    db = get_db()
    plan = db.get_plan(plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    return StreamingResponse(
        plan_event_generator(plan_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable nginx buffering if present
        },
    )


class PlanNotifyRequest(BaseModel):
    """Request body for plan notification endpoint."""

    event_type: str
    data: dict = {}


@app.post("/api/plans/{plan_id}/notify")
async def api_notify_plan_update(plan_id: int, request: PlanNotifyRequest):
    """Trigger a plan update notification to all subscribers.

    This endpoint allows external services (like MCP tools) to push updates.

    Request body:
        event_type: Type of event (e.g., 'agent_update', 'plan_complete')
        data: Optional event payload data
    """
    notified = await notify_plan_update(plan_id, request.event_type, request.data)
    return {
        "success": True,
        "plan_id": plan_id,
        "event_type": request.event_type,
        "subscribers_notified": notified,
    }


@app.get("/api/plans/{plan_id}/mermaid")
async def api_get_plan_mermaid(plan_id: int):
    """Generate Mermaid diagram code for plan DAG visualization."""
    db = get_db()
    plan = db.get_plan(plan_id)

    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    agents = db.get_plan_agents(plan_id)

    # Build Mermaid flowchart (top-down)
    lines = ["flowchart TD"]

    # Add nodes with status-based styling
    for agent in agents:
        status = agent["status"]
        agent_id = agent["agent_id"]
        agent_name = agent["agent_name"] or "unnamed"

        # Truncate long names to fit in nodes
        if len(agent_name) > 30:
            agent_name = agent_name[:27] + "..."

        # Escape quotes in the label
        label = f"{agent_id}: {agent_name}".replace('"', '\\"')

        # Node ID must be valid identifier (no special chars)
        node_id = f"agent_{agent_id}"

        lines.append(f'    {node_id}["{label}"]:::{status}')

    # Add edges from dependencies
    for agent in agents:
        agent_id = agent["agent_id"]
        deps = db.get_agent_dependencies(plan_id, agent_id)
        for dep in deps:
            lines.append(f"    agent_{dep} --> agent_{agent_id}")

    # Add style definitions for all status types
    lines.extend(
        [
            "",
            "    classDef pending fill:#e0e0e0,stroke:#666",
            "    classDef blocked fill:#ffcc80,stroke:#f57c00",
            "    classDef running fill:#90caf9,stroke:#1976d2",
            "    classDef completed fill:#a5d6a7,stroke:#388e3c",
            "    classDef failed fill:#ef9a9a,stroke:#d32f2f",
        ]
    )

    return {
        "plan_id": plan_id,
        "mermaid": "\n".join(lines),
    }


# =============================================================================
# Plan Agent Management Endpoints
# =============================================================================


class RegisterAgentRequest(BaseModel):
    """Request body for registering an agent in a plan."""

    agent_id: str
    agent_name: str
    prompt: str
    dependencies: list[str] = []
    max_attempts: int = 3


class UpdateAgentRequest(BaseModel):
    """Request body for updating an agent's status."""

    status: str | None = None
    result: str | None = None
    error_message: str | None = None


@app.post("/api/plans/{plan_id}/agents")
async def api_register_agent(plan_id: int, request: RegisterAgentRequest):
    """Register a new agent in a plan.

    Creates an agent execution record with optional dependencies.
    Dependencies should reference agent_ids that must complete first.
    """
    db = get_db()
    plan = db.get_plan(plan_id)

    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    if plan["status"] not in ("draft", "pending"):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot add agents to plan in '{plan['status']}' status",
        )

    # Create the agent execution record
    exec_id = db.add_agent_execution(
        plan_id=plan_id,
        agent_id=request.agent_id,
        agent_name=request.agent_name,
        prompt=request.prompt,
        max_attempts=request.max_attempts,
    )

    # Add dependencies if specified
    dependency_ids = []
    if request.dependencies:
        for dep in request.dependencies:
            dep_id = db.add_agent_dependency(plan_id, request.agent_id, dep)
            dependency_ids.append(dep_id)

    # Notify subscribers
    await notify_plan_update(
        plan_id,
        "agent_registered",
        {
            "agent_id": request.agent_id,
            "agent_name": request.agent_name,
            "dependencies": request.dependencies,
        },
    )

    return {
        "execution_id": exec_id,
        "agent_id": request.agent_id,
        "agent_name": request.agent_name,
        "dependencies": request.dependencies,
        "dependency_ids": dependency_ids,
        "status": "pending",
    }


@app.get("/api/plans/{plan_id}/agents")
async def api_get_plan_agents(plan_id: int):
    """Get all agents in a plan with their current status."""
    db = get_db()
    plan = db.get_plan(plan_id)

    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    agents = db.get_plan_agents(plan_id)

    # Enrich each agent with its dependencies
    for agent in agents:
        agent["dependencies"] = db.get_agent_dependencies(plan_id, agent["agent_id"])

    return {
        "plan_id": plan_id,
        "agents": agents,
        "count": len(agents),
    }


@app.put("/api/plans/{plan_id}/agents/{agent_id}")
async def api_update_agent(plan_id: int, agent_id: str, request: UpdateAgentRequest):
    """Update an agent's status and/or result.

    Valid status transitions:
    - pending -> running, blocked
    - blocked -> pending, running
    - running -> completed, failed
    - completed/failed: terminal states (cannot change)
    """
    db = get_db()
    plan = db.get_plan(plan_id)

    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    # Find the agent by agent_id
    agents = db.get_plan_agents(plan_id)
    agent = next((a for a in agents if a["agent_id"] == agent_id), None)

    if not agent:
        raise HTTPException(
            status_code=404, detail=f"Agent '{agent_id}' not found in plan"
        )

    # Validate status transition if status is being updated
    if request.status:
        valid_transitions = {
            "pending": ["running", "blocked"],
            "blocked": ["pending", "running"],
            "running": ["completed", "failed"],
            "completed": [],
            "failed": [],
        }
        current = agent["status"]
        if request.status not in valid_transitions.get(current, []):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid transition: {current} -> {request.status}",
            )

    # Update the agent
    updated = db.update_agent_execution(
        execution_id=agent["id"],
        status=request.status,
        result=request.result,
        error_message=request.error_message,
    )

    # Notify subscribers
    await notify_plan_update(
        plan_id,
        "agent_update",
        {
            "agent_id": agent_id,
            "status": request.status or agent["status"],
            "result": request.result,
            "error_message": request.error_message,
        },
    )

    return updated


@app.delete("/api/plans/{plan_id}/agents/{agent_id}")
async def api_delete_agent(plan_id: int, agent_id: str):
    """Delete an agent from a plan.

    Removes the agent and all its dependencies (both incoming and outgoing).
    Only allowed for plans in 'draft' or 'pending' status.
    """
    db = get_db()
    plan = db.get_plan(plan_id)

    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    if plan["status"] not in ("draft", "pending"):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete agents from plan in '{plan['status']}' status",
        )

    deleted = db.delete_agent_execution(plan_id, agent_id)

    if not deleted:
        raise HTTPException(
            status_code=404, detail=f"Agent '{agent_id}' not found in plan"
        )

    # Notify subscribers
    await notify_plan_update(
        plan_id,
        "agent_deleted",
        {
            "agent_id": agent_id,
        },
    )

    return {
        "deleted": True,
        "agent_id": agent_id,
        "plan_id": plan_id,
    }


@app.get("/api/plans/{plan_id}/ready")
async def api_get_ready_agents(plan_id: int):
    """Get agents that are ready to execute.

    An agent is ready if:
    - Its status is 'pending'
    - All its dependencies are 'completed'
    """
    db = get_db()
    plan = db.get_plan(plan_id)

    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    agents = db.get_plan_agents(plan_id)
    dependencies = db.get_plan_dependency_graph(plan_id)

    # Build status lookup
    status_map = {a["agent_id"]: a["status"] for a in agents}

    ready = []
    for agent in agents:
        if agent["status"] != "pending":
            continue

        # Check all dependencies are completed
        deps = dependencies.get(agent["agent_id"], [])
        all_deps_completed = all(status_map.get(dep) == "completed" for dep in deps)

        if all_deps_completed:
            ready.append(agent)

    return {
        "plan_id": plan_id,
        "ready_agents": ready,
        "count": len(ready),
    }


@app.get("/api/plans/{plan_id}/validate")
async def api_validate_plan_dag(plan_id: int):
    """Validate the plan's DAG structure.

    Checks:
    - No circular dependencies
    - All dependency references are valid agents
    - Plan has at least one agent
    """
    db = get_db()
    plan = db.get_plan(plan_id)

    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    agents = db.get_plan_agents(plan_id)
    dependencies = db.get_plan_dependency_graph(plan_id)

    errors = []
    warnings = []

    # Check for empty plan
    if not agents:
        warnings.append("Plan has no agents")
        return {
            "valid": True,
            "errors": errors,
            "warnings": warnings,
        }

    # Get all agent IDs
    agent_ids = {a["agent_id"] for a in agents}

    # Check for invalid dependency references
    for agent_id, deps in dependencies.items():
        for dep in deps:
            if dep not in agent_ids:
                errors.append(
                    f"Agent '{agent_id}' depends on non-existent agent '{dep}'"
                )

    # Check for cycles using DFS
    def has_cycle(start, visited, rec_stack):
        visited.add(start)
        rec_stack.add(start)

        for neighbor in dependencies.get(start, []):
            if neighbor not in visited:
                if has_cycle(neighbor, visited, rec_stack):
                    return True
            elif neighbor in rec_stack:
                return True

        rec_stack.remove(start)
        return False

    visited = set()
    rec_stack = set()
    for agent_id in agent_ids:
        if agent_id not in visited:
            if has_cycle(agent_id, visited, rec_stack):
                errors.append("Circular dependency detected in DAG")
                break

    # Check for entry points (agents with no dependencies)
    entry_points = [
        a["agent_id"]
        for a in agents
        if a["agent_id"] not in dependencies or not dependencies[a["agent_id"]]
    ]
    if not entry_points:
        errors.append("No entry point found (all agents have dependencies)")

    # Build reverse dependency map (who depends on whom)
    dependents: dict[str, list[str]] = {}
    for agent_id, deps in dependencies.items():
        for dep in deps:
            if dep not in dependents:
                dependents[dep] = []
            dependents[dep].append(agent_id)

    # Check for orphan agents (no dependencies and nothing depends on them)
    if len(agents) > 1:
        for agent in agents:
            aid = agent["agent_id"]
            has_deps = aid in dependencies and len(dependencies[aid]) > 0
            has_dependents = aid in dependents and len(dependents[aid]) > 0
            if not has_deps and not has_dependents:
                warnings.append(
                    f"Agent '{aid}' has no dependencies and nothing depends on it"
                )

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "agent_count": len(agents),
        "entry_points": entry_points,
    }


# =============================================================================
# Plans Import API
# =============================================================================


class ImportPreviewRequest(BaseModel):
    """Request body for previewing tasks.md import."""

    file_path: str | None = None
    content: str | None = None


class ImportPlanRequest(BaseModel):
    """Request body for importing tasks.md as a plan."""

    file_path: str | None = None
    content: str | None = None
    plan_name: str | None = None
    task_id: int | None = None
    use_custom_dependencies: bool = True
    import_completed: bool = False


@app.post("/api/plans/import/preview")
async def api_preview_import(request: ImportPreviewRequest):
    """Preview the agents that would be imported from a tasks.md file.

    Provide either file_path (absolute path to a tasks.md file) or
    content (raw markdown content). Returns parsed agents without
    creating anything in the database.
    """
    # Get content from file or direct input
    if request.file_path:
        path = Path(request.file_path).expanduser()
        if not path.exists():
            raise HTTPException(
                status_code=404, detail=f"File not found: {request.file_path}"
            )
        content = path.read_text()
    elif request.content:
        content = request.content
    else:
        raise HTTPException(
            status_code=400, detail="Either file_path or content must be provided"
        )

    # Parse agents
    agents = parse_tasks_md(content)

    # Count completed vs uncompleted
    completed = sum(1 for a in agents if a.completed)
    uncompleted = sum(1 for a in agents if not a.completed)

    # Infer plan name from file path if available
    suggested_name = None
    if request.file_path:
        path = Path(request.file_path)
        # Try to extract task name from path like: dev/active/task-name/task-name-tasks.md
        if path.name.endswith("-tasks.md"):
            suggested_name = path.name.replace("-tasks.md", "")
        else:
            suggested_name = path.parent.name

    return {
        "agents": [
            {
                "agent_id": a.agent_id,
                "agent_name": a.agent_name,
                "prompt": a.prompt,
                "dependencies": a.dependencies,
                "completed": a.completed,
            }
            for a in agents
        ],
        "total_agents": len(agents),
        "completed_agents": completed,
        "uncompleted_agents": uncompleted,
        "suggested_plan_name": suggested_name,
        "timestamp": datetime.now().isoformat(),
    }


@app.post("/api/plans/import")
async def api_import_plan(request: ImportPlanRequest):
    """Import an orbit tasks.md file as a new plan.

    Provide either file_path (absolute path to a tasks.md file) or
    content (raw markdown content).

    Args:
        file_path: Absolute path to a tasks.md file
        content: Raw markdown content (alternative to file_path)
        plan_name: Name for the plan (inferred from file path if not provided)
        task_id: Optional associated task ID
        use_custom_dependencies: If True, parse Task Dependencies section for DAG
        import_completed: If True, also import completed ([x]) tasks
    """
    db = get_db()

    # Get content from file or direct input
    if request.file_path:
        path = Path(request.file_path).expanduser()
        if not path.exists():
            raise HTTPException(
                status_code=404, detail=f"File not found: {request.file_path}"
            )
        content = path.read_text()
    elif request.content:
        content = request.content
    else:
        raise HTTPException(
            status_code=400, detail="Either file_path or content must be provided"
        )

    # Determine plan name
    plan_name = request.plan_name
    if not plan_name:
        if request.file_path:
            path = Path(request.file_path)
            if path.name.endswith("-tasks.md"):
                plan_name = path.name.replace("-tasks.md", "")
            else:
                plan_name = path.parent.name
        else:
            plan_name = f"imported-plan-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

    # Validate plan name format
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9_-]*$", plan_name):
        raise HTTPException(
            status_code=400,
            detail="Plan name must start with a letter and contain only letters, numbers, hyphens, and underscores",
        )

    # Import the plan
    result = import_tasks_md(
        db=db,
        content=content,
        plan_name=plan_name,
        task_id=request.task_id,
        use_custom_dependencies=request.use_custom_dependencies,
        import_completed=request.import_completed,
    )

    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])

    return {
        "plan_id": result["plan_id"],
        "plan_name": plan_name,
        "agents_imported": result["agents_imported"],
        "agents": result["agents"],
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/api/plans/import/available")
async def api_list_available_tasks_files():
    """List available tasks.md files in dev/active/ directories.

    Scans known orbit locations for importable task files.
    """
    available_files = []
    seen_names: set[str] = set()

    # Centralized orbit root (primary)
    orbit_active = ORBIT_ROOT / "active"
    if orbit_active.exists():
        for tasks_file in orbit_active.glob("*/*-tasks.md"):
            try:
                content = tasks_file.read_text()
                agents = parse_tasks_md(content)
                completed = sum(1 for a in agents if a.completed)
                uncompleted = sum(1 for a in agents if not a.completed)
                task_name = tasks_file.parent.name
                seen_names.add(task_name)
                available_files.append(
                    {
                        "file_path": str(tasks_file),
                        "task_name": task_name,
                        "total_agents": len(agents),
                        "completed_agents": completed,
                        "uncompleted_agents": uncompleted,
                        "modified": datetime.fromtimestamp(
                            tasks_file.stat().st_mtime
                        ).isoformat(),
                    }
                )
            except Exception:
                continue

    # Legacy: repo-local paths for unmigrated tasks
    search_paths = [
        Path.home() / "work",
        Path.home() / "dev",
        Path.home() / "projects",
        Path.cwd(),
    ]

    for base_path in search_paths:
        if not base_path.exists():
            continue

        for tasks_file in base_path.glob("**/dev/active/*/*-tasks.md"):
            try:
                content = tasks_file.read_text()
                agents = parse_tasks_md(content)
                completed = sum(1 for a in agents if a.completed)
                uncompleted = sum(1 for a in agents if not a.completed)

                task_name = tasks_file.parent.name
                if task_name in seen_names:
                    continue  # Already found in centralized location

                available_files.append(
                    {
                        "file_path": str(tasks_file),
                        "task_name": task_name,
                        "total_agents": len(agents),
                        "completed_agents": completed,
                        "uncompleted_agents": uncompleted,
                        "modified": datetime.fromtimestamp(
                            tasks_file.stat().st_mtime
                        ).isoformat(),
                    }
                )
            except Exception:
                # Skip files that can't be parsed
                continue

    # Sort by modification time (most recent first)
    available_files.sort(key=lambda x: x["modified"], reverse=True)

    return {
        "files": available_files,
        "count": len(available_files),
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/api/orbit-auto/active", response_model=AutoActiveList)
async def get_active_auto_executions():
    """Get list of active orbit-auto executions across all tracked repos."""
    state_files = find_auto_state_files()

    executions = []
    for state_file, task_name, repo_path in state_files:
        state = parse_auto_state(state_file)
        if not state:
            continue

        # Check if stale (stopped but not cleaned up)
        stale = is_auto_stale(state_file, state)

        # Check if still running (has pending or in_progress tasks)
        tasks = state.get("tasks", {})
        has_active = any(
            t.get("status") in ("pending", "in_progress") for t in tasks.values()
        )

        # Calculate progress
        total = len(tasks)
        completed = sum(1 for t in tasks.values() if t.get("status") == "completed")
        in_progress = sum(1 for t in tasks.values() if t.get("status") == "in_progress")
        failed = sum(1 for t in tasks.values() if t.get("status") == "failed")

        # Calculate elapsed time
        started = state.get("started", "")
        elapsed = 0
        if started:
            try:
                start_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
                elapsed = int((datetime.now(timezone.utc) - start_dt).total_seconds())
            except Exception:
                pass

        # Determine status: stale takes precedence
        if stale:
            status = "stopped"
        elif has_active:
            status = "running"
        else:
            status = state.get("status", "completed")

        executions.append(
            {
                "task_name": task_name,
                "repo_path": repo_path,
                "repo_name": Path(repo_path).name,
                "status": status,
                "started": started,
                "elapsed_seconds": elapsed,
                "total_tasks": total,
                "completed_tasks": completed,
                "in_progress_tasks": in_progress,
                "failed_tasks": failed,
                "progress_percent": round(completed / total * 100) if total > 0 else 0,
                "is_stale": stale,
            }
        )

    return AutoActiveList(executions=executions, count=len(executions))


@app.get("/api/orbit-auto/{task_name}")
async def get_auto_execution_details(task_name: str, repo_path: str | None = None):
    """
    Get detailed status for a specific orbit-auto execution.

    If repo_path is not provided, finds the first matching task_name.
    """
    state_files = find_auto_state_files()

    # Find matching execution
    state_file = None
    matched_repo_path = None

    for sf, tn, rp in state_files:
        if tn == task_name:
            if repo_path is None or rp == repo_path:
                state_file = sf
                matched_repo_path = rp
                break

    if not state_file:
        raise HTTPException(
            status_code=404, detail=f"No orbit-auto execution found for task: {task_name}"
        )

    state = parse_auto_state(state_file)
    if not state:
        raise HTTPException(status_code=500, detail="Failed to parse orbit-auto state file")

    state_dir = state_file.parent

    # Get adjacency and compute waves
    adjacency = parse_adjacency_file(state_dir)
    waves = compute_dag_waves(adjacency) if adjacency else []

    # Get task metadata (title, agents, skills)
    task_metadata = get_task_metadata_from_prompts(state_dir)

    # Build task status list
    tasks_data = state.get("tasks", {})
    tasks = {}
    for tid, tdata in tasks_data.items():
        meta = task_metadata.get(tid, {})
        tasks[tid] = AutoTaskStatus(
            id=tid,
            status=tdata.get("status", "pending"),
            worker=tdata.get("worker"),
            attempts=tdata.get("attempts", 0),
            title=meta.get("title", f"Task {tid}"),
            agents=meta.get("agents", []),
            skills=meta.get("skills", []),
            error_message=tdata.get("error_message"),
        )

    # Calculate progress counts
    progress = {
        "pending": sum(1 for t in tasks.values() if t.status == "pending"),
        "in_progress": sum(1 for t in tasks.values() if t.status == "in_progress"),
        "completed": sum(1 for t in tasks.values() if t.status == "completed"),
        "failed": sum(1 for t in tasks.values() if t.status == "failed"),
        "total": len(tasks),
    }

    # Build workers list
    workers_data = state.get("workers", {})
    workers = []
    active_count = 0

    # Get workers from task assignments
    assigned_workers = set()
    for tid, task in tasks.items():
        if task.worker is not None:
            assigned_workers.add(task.worker)

    # Create worker entries (assume up to 12 workers)
    max_worker_id = max(assigned_workers) if assigned_workers else 7
    for i in range(max(8, max_worker_id + 1)):
        # Find task assigned to this worker
        assigned_task = None
        for tid, task in tasks.items():
            if task.worker == i and task.status == "in_progress":
                assigned_task = tid
                break

        workers.append(
            AutoWorker(
                id=i,
                task_id=assigned_task,
                status="running" if assigned_task else "idle",
            )
        )
        if assigned_task:
            active_count += 1

    # Calculate elapsed time
    started = state.get("started", "")
    elapsed = 0
    if started:
        try:
            start_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
            elapsed = int((datetime.now(timezone.utc) - start_dt).total_seconds())
        except Exception:
            pass

    # Check if stale
    stale = is_auto_stale(state_file, state)

    # Determine overall status
    has_active = progress["pending"] > 0 or progress["in_progress"] > 0
    if stale:
        overall_status = "stopped"
    elif has_active:
        overall_status = "running"
    else:
        overall_status = state.get("status", "completed")

    return AutoExecStatus(
        task_name=task_name,
        repo_path=matched_repo_path or "",
        repo_name=Path(matched_repo_path).name if matched_repo_path else "",
        status=overall_status,
        started=started,
        elapsed_seconds=elapsed,
        tasks=tasks,
        progress=progress,
        waves=[AutoWave(**w) for w in waves],
        adjacency=adjacency,
        workers=workers,
        active_worker_count=active_count,
    )


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
    """
    projects = []
    active_dir = ORBIT_ROOT / "active"

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

            # Calculate progress
            completed = sum(1 for t in tasks if t["completed"])
            total = len(tasks)

            projects.append(
                {
                    "name": project_dir.name,
                    "path": str(project_dir),
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


@app.get("/api/auto/project/{project_name}")
async def api_auto_project_detail(project_name: str):
    """Get detailed task graph for a specific project."""
    db = get_db()
    repos = db.get_repos()

    for repo in repos:
        repo_path = Path(repo.path)
        project_dir = repo_path / "dev" / "active" / project_name

        if not project_dir.exists():
            continue

        # Find tasks file
        tasks_file = project_dir / f"{project_name}-tasks.md"
        if not tasks_file.exists():
            tasks_file = project_dir / "tasks.md"
        if not tasks_file.exists():
            raise HTTPException(
                status_code=404, detail=f"Tasks file not found for {project_name}"
            )

        # Parse tasks
        tasks = _parse_orbit_tasks(tasks_file)

        # Build nodes
        nodes = []
        for task in tasks:
            nodes.append(
                {
                    "id": task["number"],
                    "title": task["title"],
                    "status": "completed"
                    if task["completed"]
                    else ("wait" if task["wait"] else "pending"),
                    "line": task["line"],
                }
            )

        # Parse dependencies
        links = []
        prompts_dir = project_dir / "prompts"
        if prompts_dir.exists():
            links = _parse_prompt_dependencies(prompts_dir)

        # Check for iteration log
        log_file = project_dir / f"{project_name}-iteration-log.md"
        has_log = log_file.exists()

        return {
            "name": project_name,
            "path": str(project_dir),
            "tasks_file": str(tasks_file),
            "has_prompts": prompts_dir.exists(),
            "has_log": has_log,
            "graph": {
                "nodes": nodes,
                "links": links,
            },
        }

    raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found")


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


@app.get("/api/all")
async def api_all():
    """Get all data in one request for initial load."""
    db = get_db()

    return {
        "productivity": {
            "today": db.get_today_stats(),
            "active_task_count": len(db.get_active_tasks()),
        },
        "usage": await api_usage_limits(),
        "timestamp": datetime.now().isoformat(),
        "refresh_interval": REFRESH_INTERVAL,
    }


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
    prompt = (body.get("prompt") or "").strip()

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


@app.get("/api/sync")
async def get_sync_status():
    """Get sync status and trigger sync."""
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
            "usage": format_usage_data(load_usage_cache() or {}, from_cache=True),
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

    uvicorn.run(app, host="0.0.0.0", port=8787)

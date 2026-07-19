#!/usr/bin/env python3
"""
Task Database Manager for the MissionCache system on Claude Code.

Provides SQLite-based cross-repo task tracking with WakaTime-style time tracking.

Usage:
    python missioncache_db.py init                      # Initialize database
    python missioncache_db.py add-repo <path> [name]    # Add repository to track
    python missioncache_db.py add-repos-glob <pattern>  # Add repos from glob pattern
    python missioncache_db.py scan [repo_id]            # Scan repos for tasks
    python missioncache_db.py list-active               # List all active tasks
    python missioncache_db.py list-repos                # List tracked repositories
    python missioncache_db.py get-task <task_id>        # Get task details (JSON)
    python missioncache_db.py heartbeat [task_id]       # Record activity heartbeat
    python missioncache_db.py heartbeat-auto            # Auto-detect task from cwd
    python missioncache_db.py process-heartbeats        # Aggregate heartbeats into sessions
    python missioncache_db.py task-time <task_id> [period]  # Get time spent
    python missioncache_db.py prune [days]              # Prune old completed tasks
    python missioncache_db.py complete-task <task_id>   # Mark task as completed
    python missioncache_db.py reopen-task <task_id>     # Reopen a completed task
    python missioncache_db.py rename-task <old-name> <new-name>  # Rename a project
    python missioncache_db.py list-completed [days]     # List recently completed tasks
    python missioncache_db.py get-task-by-name <name>   # Find task by name

Keyword Management:
    python missioncache_db.py add-keyword <keyword>     # Add custom tag keyword
    python missioncache_db.py remove-keyword <keyword>  # Remove custom tag keyword
    python missioncache_db.py list-keywords             # List all tag keywords
    python missioncache_db.py backfill-tags             # Backfill tags for existing tasks

Non-Coding Task Management:
    python missioncache_db.py create-task [--type TYPE] [--jira TICKET] [--category CAT] <name>  # Create task
    python missioncache_db.py set-category <task_id> <category|none>            # Set or clear project category
    python missioncache_db.py add-update <task_id> <note>                       # Add timestamped update
    python missioncache_db.py get-updates <task_id> [limit]                     # Get task updates
    python missioncache_db.py today-updates [task_id]                           # Get today's updates

Migration:
    python missioncache_db.py migrate-orbit-docs [--dry-run]  # Move docs to ~/.missioncache/

Cleanup:
    python missioncache_db.py cleanup [--dry-run]              # Archive orphans, resolve dupes, normalize paths

Diagnostics:
    python missioncache_db.py health                    # Report context-file health for all active projects

Cross-Machine Sharing:
    python missioncache_db.py export <name> [--out <path>] [--no-time] [--json]  # Build a portable bundle (markdown + missioncache.json manifest)
    python missioncache_db.py import <bundle> [--repo <path>] [--force] [--rewrite-paths] [--dry-run] [--json]  # Import a bundle, reconcile refs, print a 3-bucket alignment report
    python missioncache_db.py config set-path <repo|vault|anchor>:<name> <localpath>  # Map a portable identifier to this machine's path
    python missioncache_db.py config list-paths [kind] [--json]  # Show the per-machine path map (--json prints raw machine.json)
    python missioncache_db.py config show                        # Print the live machine.json (or a skeleton)
    python missioncache_db.py config seed [--dry-run]            # Pre-fill the map from local repos' git remotes
"""

import json
import logging
import os
import re
import sqlite3
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from enum import Enum
from glob import glob as glob_files
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

# MISSIONCACHE_ROOT can be overridden via the MISSIONCACHE_ROOT env var so a
# fresh data dir can be targeted (cross-machine import into a throwaway root,
# tests). DB_PATH derives from it. Mirrors the SHADOW_TRACKED_FOLDER override.
# `or` (not a default arg) so a set-but-empty MISSIONCACHE_ROOT falls back to
# the real dir instead of Path("") == cwd (which would silently relocate data).
MISSIONCACHE_ROOT = Path(os.environ.get("MISSIONCACHE_ROOT") or str(Path.home() / ".missioncache"))
DB_PATH = MISSIONCACHE_ROOT / "tasks.db"

# Shared session-state database used by the dashboard, statusline, and hooks.
# Multiple writers exist (dashboard's HTTP API, hooks at SessionStart, missioncache-db's
# rename sweep). The dashboard authors the schema, but every writer should call
# init_hooks_state_db_schema() to be tolerant of fresh installs where the
# dashboard never ran.
HOOKS_STATE_DB_PATH = Path.home() / ".claude" / "hooks-state.db"

# Legacy data locations the migration guard warns about, in two tiers:
#   - the ancient pre-Phase-11 ~/.claude/ layout
#   - the ~/.orbit/ layout (pre-MissionCache rename, superseded by ~/.missioncache/ in Task 71)
_LEGACY_CLAUDE_DB = Path.home() / ".claude" / "tasks.db"
_LEGACY_CLAUDE_ORBIT_ROOT = Path.home() / ".claude" / "orbit"
_LEGACY_ORBIT_DB = Path.home() / ".orbit" / "tasks.db"
_LEGACY_ORBIT_ROOT = Path.home() / ".orbit"


def atomic_write_json(path: Path, payload: object) -> None:
    """Write JSON to ``path`` via tmp+os.replace.

    Used by hooks (cwd-session pointer, per-session project pointer) and by
    statusline cache writes - any place where multiple processes can race on
    the same file. ``write_text`` lets two writers observe a half-written
    file; ``os.replace`` makes the live path atomically valid or untouched.

    Tmp filename is suffixed with the writer's pid so concurrent writers do
    not collide on the same tmp path. Pid is not stable across reboots or
    PID-reuse, so this also sweeps any sibling ``<name>.tmp.*`` older than
    one hour - that catches leftovers from prior crashes between
    ``write_text`` and ``os.replace`` without needing an external janitor.

    Failures are silent (return on OSError) so a full disk or read-only
    mount cannot break the caller's hot path. Programming errors that pass
    a non-JSON-serializable payload are still raised loudly via TypeError -
    that surface deliberately is not swallowed; silent type errors corrupt
    cache files in ways the next reader cannot detect.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Best-effort sweep of leftover tmp files from prior crashes.
        cutoff = time.time() - 3600
        for stale in path.parent.glob(f"{path.name}.tmp.*"):
            try:
                if stale.stat().st_mtime < cutoff:
                    stale.unlink()
            except OSError:
                pass
        tmp_path = path.parent / f"{path.name}.tmp.{os.getpid()}"
        tmp_path.write_text(json.dumps(payload))
        os.replace(tmp_path, path)
    except OSError:
        return


def session_binding_path(session_id: str) -> Path:
    """Canonical path of the per-session project binding file.

    Single owner of the ``~/.claude/hooks/state/projects/<sid>.json``
    convention. Every writer (session_start hook, MCP server binding,
    /missioncache:load's bash fallback) and reader (find_task_for_cwd,
    pre_compact's bound-session check, statusline) must resolve the path
    through here or match this format exactly - a hand-rolled copy that
    drifts silently reintroduces the "bound but undetected" snapshot loss.
    """
    return Path.home() / ".claude" / "hooks" / "state" / "projects" / f"{session_id}.json"


def write_session_binding(
    session_id: str, project_name: str, task_id: Optional[int] = None
) -> None:
    """Write the per-session project binding atomically.

    ``task_id`` is the durable identity: resolution prefers it over the
    name, so bindings that carry it are immune to name reuse and renames.
    Name-only bindings (older writers, the load command's bash fallback
    before it learned taskId) stay readable as legacy.
    """
    payload: Dict[str, Any] = {
        "projectName": project_name,
        "updated": datetime.now().astimezone().isoformat(),
        "sessionId": session_id,
    }
    if task_id is not None:
        payload["taskId"] = int(task_id)
    atomic_write_json(session_binding_path(session_id), payload)


def read_session_binding(session_id: str) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """Read the per-session binding as ``(file_exists, data_or_None)``.

    The two-part return lets callers distinguish the three states that
    matter: no file (session simply not bound - benign), file present and
    parseable (bound), and file present but unreadable/corrupt (a bug
    condition - pre_compact turns it into a sticky error rather than
    treating it as unbound).
    """
    path = session_binding_path(session_id)
    if not path.exists():
        return (False, None)
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return (True, None)
    return (True, data if isinstance(data, dict) else None)


def init_hooks_state_db_schema(conn: sqlite3.Connection) -> None:
    """Idempotently create every table needed in hooks-state.db.

    Safe to call from any writer. Hooks rely on this when the dashboard has
    never started (fresh install): without it, the first INSERT into
    project_state raises ``OperationalError: no such table`` and the bind
    silently no-ops. Schema must stay in sync with the dashboard's
    ``_init_hooks_state_db`` (server.py); the dashboard delegates to this
    function so they cannot drift.
    """
    conn.executescript(
        """
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
        """
    )


class MissionCacheMigrationRequired(RuntimeError):
    """Raised when MissionCache data is at a legacy path (~/.claude/orbit/ or
    ~/.orbit/) but the new ~/.missioncache/ DB doesn't exist. Caught by CLI entry
    points to print a user-friendly migration message; subclasses RuntimeError so
    existing `except Exception` handlers in hooks catch it cleanly (unlike
    SystemExit which is BaseException and would escape `except Exception`)."""


class RenameError(ValueError):
    """Base class for rename_task failures other than basic ValidationError.

    Subclasses are catchable by name in callers (CLI, MCP tool, dashboard
    endpoint) to surface specific user-facing messages.
    """


class NameCollisionError(RenameError):
    """Another task in the same repo already has the target name."""


class FilesystemCollisionError(RenameError):
    """The target MissionCache directory already exists on disk."""


class AutoRunActiveError(RenameError):
    """A missioncache-auto run is currently in progress on this task."""


class DeleteError(ValueError):
    """Base class for delete_task failures other than a missing task.

    Subclasses are catchable by name in callers (dashboard endpoint) to
    surface specific user-facing messages.
    """


class SubtasksExistError(DeleteError):
    """The project has subtasks; delete or reassign them before deleting it."""


class ImportConflictError(RuntimeError):
    """Raised by upsert_imported_task when a cross-machine import would collide
    with a DIFFERENT existing row on UNIQUE(repo_id, full_path).

    Surfaced as a hard import failure (exit 1) rather than letting the raw
    sqlite3.IntegrityError escape. Subclasses RuntimeError so callers using
    `except Exception` catch it cleanly."""


_TASK_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def validate_task_name(name: str) -> None:
    """Validate a project / task name for filesystem and DB safety.

    Mirrors ``mcp_missioncache.project_files.validate_task_name`` exactly so the same
    inputs are accepted/rejected on every surface (CLI, MCP, dashboard).
    The check is split into three branches so each failure mode gets a
    specific user-facing message.
    """
    if not name:
        raise ValueError("Project name cannot be empty.")
    if name.startswith("-"):
        raise ValueError(
            "Project name must start with a letter or digit, not a hyphen."
        )
    if not _TASK_NAME_RE.match(name):
        raise ValueError(
            "Project name must use lowercase letters, digits, and hyphens "
            "only (e.g., 'my-project')."
        )


def check_legacy_paths() -> None:
    """Raise MissionCacheMigrationRequired if MissionCache data exists at a legacy
    path but not at the canonical ~/.missioncache/ path. Reads module-level
    DB_PATH / _LEGACY_CLAUDE_DB / _LEGACY_CLAUDE_ORBIT_ROOT / _LEGACY_ORBIT_DB /
    _LEGACY_ORBIT_ROOT at call time so tests can monkeypatch them.

    Two legacy tiers are detected: the ancient ~/.claude/ layout and the ~/.orbit/
    layout (superseded by ~/.missioncache/ in the MissionCache rename, Task 71).

    Public API: missioncache-auto calls this directly (missioncache-db>=1.0.5) to warn
    about unmigrated data without constructing a TaskDB."""
    # An explicit, non-empty MISSIONCACHE_ROOT override targets a deliberately
    # chosen root (cross-machine import into a fresh dir, tests). The
    # legacy-migration prompt is about moving old data into the DEFAULT
    # ~/.missioncache/ and does not apply. Gate on truthiness, not key-presence,
    # so a set-but-empty value does not skip the guard while data goes to cwd.
    if os.environ.get("MISSIONCACHE_ROOT"):
        return
    if DB_PATH.exists():
        return
    orbit_legacy = _LEGACY_ORBIT_DB.exists() or _LEGACY_ORBIT_ROOT.exists()
    claude_legacy = _LEGACY_CLAUDE_DB.exists() or _LEGACY_CLAUDE_ORBIT_ROOT.exists()
    if not (orbit_legacy or claude_legacy):
        return
    lines = [
        "MissionCache data found at a legacy path but not at ~/.missioncache/.",
        "Migrate before starting missioncache:",
        "  mkdir -p ~/.missioncache",
    ]
    if orbit_legacy:
        lines += [
            "  # data at ~/.orbit/ (pre-rename layout):",
            "  mv ~/.orbit/active        ~/.missioncache/active     2>/dev/null",
            "  mv ~/.orbit/completed     ~/.missioncache/completed  2>/dev/null",
            "  mv ~/.orbit/tasks.db*     ~/.missioncache/           2>/dev/null",
            "  mv ~/.orbit/tasks.duckdb* ~/.missioncache/           2>/dev/null",
            "  rmdir ~/.orbit 2>/dev/null",
        ]
    if claude_legacy:
        lines += [
            "  # data at ~/.claude/orbit/ (ancient layout):",
            "  mv ~/.claude/orbit/active     ~/.missioncache/active     2>/dev/null",
            "  mv ~/.claude/orbit/completed  ~/.missioncache/completed  2>/dev/null",
            "  mv ~/.claude/tasks.db*        ~/.missioncache/           2>/dev/null",
            "  mv ~/.claude/tasks.duckdb*    ~/.missioncache/           2>/dev/null",
            "  rmdir ~/.claude/orbit 2>/dev/null",
        ]
    raise MissionCacheMigrationRequired("\n".join(lines))

# Non-git folder to track with shadow repo (only this folder gets shadow commits)
SHADOW_TRACKED_FOLDER = Path(os.environ.get("MISSIONCACHE_SHADOW_FOLDER", str(Path.home() / "work")))

DEFAULT_CONFIG = {
    "idle_timeout_seconds": 300,  # 5 minutes
    "assumed_work_seconds": 120,  # 2 minutes
    "prune_after_days": 30,
    "auto_prune_on_startup": True,
    "scan_on_startup": True,
}

# Default keywords for smart tagging
DEFAULT_TAG_KEYWORDS = {
    # Infrastructure
    "kafka",
    "clickhouse",
    "k8s",
    "kubernetes",
    "helm",
    "docker",
    "argo",
    "s3",
    "redis",
    "postgres",
    "prometheus",
    "grafana",
    "argocd",
    "mongo",
    "mysql",
    "nginx",
    "envoy",
    "istio",
    "vault",
    # Security & Config
    "auth",
    "secrets",
    "tls",
    "ssl",
    "creds",
    "credentials",
    "token",
    "oauth",
    "jwt",
    "rbac",
    "iam",
    # DevOps & CI/CD
    "ci",
    "cd",
    "cicd",
    "deploy",
    "build",
    "test",
    "pipeline",
    "release",
    "jenkins",
    "github",
    "gitlab",
    "actions",
    "workflow",
    # Actions
    "fix",
    "refactor",
    "migrate",
    "upgrade",
    "optimize",
    "cleanup",
    "debug",
    "hotfix",
    "patch",
    "update",
    "improve",
    # Scrum Master tasks
    "sprint",
    "standup",
    "retro",
    "retrospective",
    "planning",
    "grooming",
    "backlog",
    "refinement",
    "velocity",
    "burndown",
    "scrum",
    "agile",
    "blocker",
    "impediment",
    "ceremony",
    "demo",
    "review",
    "stakeholder",
    "epic",
    "story",
    "jira",
    "kanban",
    # AI Lead tasks
    "ai",
    "ml",
    "llm",
    "prompt",
    "model",
    "training",
    "inference",
    "claude",
    "gpt",
    "embedding",
    "rag",
    "agent",
    "mcp",
    "anthropic",
    "openai",
    "finetune",
    "evaluation",
    "benchmark",
    "transformer",
    # General
    "api",
    "web",
    "frontend",
    "backend",
    "service",
    "microservice",
    "gateway",
    "proxy",
    "cache",
    "queue",
    "log",
    "monitor",
    "alert",
    "doc",
    "docs",
    "documentation",
    "readme",
}


def extract_tags(task_name: str) -> List[str]:
    """Extract tags from task name using keyword matching.

    Args:
        task_name: The name of the task (e.g., "kafka-plaintext-secrets")

    Returns:
        List of matched tags sorted alphabetically
    """
    tags = set()
    name_lower = task_name.lower()

    # Split on hyphens, underscores, and spaces
    parts = re.split(r"[-_\s]+", name_lower)

    # Get merged keywords (default + custom)
    keywords = get_tag_keywords()

    for part in parts:
        # Direct match
        if part in keywords:
            tags.add(part)
        # Check for partial matches in compound words
        for keyword in keywords:
            if len(keyword) > 2 and keyword in part:
                tags.add(keyword)

    return sorted(list(tags))


def get_tag_keywords() -> set:
    """Get merged tag keywords (default + custom from config).

    Returns:
        Set of all keywords (default + user-configured)
    """
    try:
        db = TaskDB()
        custom_json = db.get_config("custom_tag_keywords", "[]")
        custom = set(json.loads(custom_json))
    except Exception:
        custom = set()

    return DEFAULT_TAG_KEYWORDS | custom


# =============================================================================
# Schema
# =============================================================================

SCHEMA_SQL = """
-- Repositories for cross-repo tracking
CREATE TABLE IF NOT EXISTS repositories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,
    short_name TEXT NOT NULL,
    glob_pattern TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    last_scanned_at TEXT
);

-- Core task tracking
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id INTEGER REFERENCES repositories(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    full_path TEXT NOT NULL,
    parent_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'paused', 'completed', 'archived')),
    type TEXT NOT NULL DEFAULT 'coding'
        CHECK (type IN ('coding', 'non-coding')),
    tags TEXT NOT NULL DEFAULT '[]',
    priority INTEGER,
    jira_key TEXT,
    branch TEXT,
    pr_url TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    completed_at TEXT,
    archived_at TEXT,
    last_worked_on TEXT,
    origin_uuid TEXT,
    category TEXT,
    UNIQUE(repo_id, full_path)
);

-- Task updates for non-coding task progress notes
CREATE TABLE IF NOT EXISTS task_updates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- WakaTime-style heartbeats
CREATE TABLE IF NOT EXISTS heartbeats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    timestamp TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    session_id TEXT,
    context TEXT,
    processed INTEGER NOT NULL DEFAULT 0
);

-- Aggregated work sessions
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    session_id TEXT,
    start_time TEXT NOT NULL,
    end_time TEXT,
    duration_seconds INTEGER NOT NULL DEFAULT 0,
    heartbeat_count INTEGER NOT NULL DEFAULT 0
);

-- Configuration
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- User-defined categories (extend the built-in CATEGORIES taxonomy).
-- Rendered as emoji + color in the dashboard; managed from its Settings view.
CREATE TABLE IF NOT EXISTS custom_categories (
    name TEXT PRIMARY KEY,
    emoji TEXT NOT NULL,
    color TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_repos_active ON repositories(active);
CREATE INDEX IF NOT EXISTS idx_repos_path ON repositories(path);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_repo_status ON tasks(repo_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_last_worked ON tasks(last_worked_on DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id);
CREATE INDEX IF NOT EXISTS idx_tasks_type ON tasks(type);
CREATE INDEX IF NOT EXISTS idx_updates_task ON task_updates(task_id);
CREATE INDEX IF NOT EXISTS idx_updates_created ON task_updates(created_at);
CREATE INDEX IF NOT EXISTS idx_heartbeats_task_time ON heartbeats(task_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_heartbeats_unprocessed ON heartbeats(processed, timestamp);
CREATE INDEX IF NOT EXISTS idx_sessions_task_time ON sessions(task_id, start_time);

-- Triggers for automatic timestamp updates
CREATE TRIGGER IF NOT EXISTS trg_repos_updated
AFTER UPDATE ON repositories
BEGIN
    UPDATE repositories SET updated_at = datetime('now', 'localtime') WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_tasks_updated
AFTER UPDATE ON tasks
BEGIN
    UPDATE tasks SET updated_at = datetime('now', 'localtime') WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_tasks_completed
AFTER UPDATE OF status ON tasks
WHEN NEW.status = 'completed' AND OLD.status != 'completed'
BEGIN
    UPDATE tasks SET completed_at = datetime('now', 'localtime') WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_tasks_archived
AFTER UPDATE OF status ON tasks
WHEN NEW.status = 'archived' AND OLD.status != 'archived'
BEGIN
    UPDATE tasks SET archived_at = datetime('now', 'localtime') WHERE id = NEW.id;
END;

-- Auto execution runs (missioncache-auto)
CREATE TABLE IF NOT EXISTS auto_executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    completed_at TEXT,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'completed', 'failed', 'cancelled')),
    mode TEXT NOT NULL DEFAULT 'parallel'
        CHECK (mode IN ('sequential', 'parallel')),
    worker_count INTEGER,
    total_subtasks INTEGER NOT NULL DEFAULT 0,
    completed_subtasks INTEGER NOT NULL DEFAULT 0,
    failed_subtasks INTEGER NOT NULL DEFAULT 0,
    error_message TEXT
);

-- Auto execution log lines (for streaming)
CREATE TABLE IF NOT EXISTS auto_execution_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id INTEGER NOT NULL REFERENCES auto_executions(id) ON DELETE CASCADE,
    timestamp TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    worker_id INTEGER,
    subtask_id TEXT,
    level TEXT NOT NULL DEFAULT 'info'
        CHECK (level IN ('debug', 'info', 'warn', 'error', 'success')),
    message TEXT NOT NULL
);

-- Indexes for auto execution tables
CREATE INDEX IF NOT EXISTS idx_auto_executions_task ON auto_executions(task_id);
CREATE INDEX IF NOT EXISTS idx_auto_executions_status ON auto_executions(status);
CREATE INDEX IF NOT EXISTS idx_auto_execution_logs_exec ON auto_execution_logs(execution_id);
CREATE INDEX IF NOT EXISTS idx_auto_execution_logs_time ON auto_execution_logs(execution_id, timestamp);
"""


# =============================================================================
# Data Classes
# =============================================================================


class TaskStatus(Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ARCHIVED = "archived"


@dataclass
class Repository:
    id: int
    path: str
    short_name: str
    glob_pattern: Optional[str]
    active: bool
    created_at: str
    updated_at: str
    last_scanned_at: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Repository":
        return cls(
            id=row["id"],
            path=row["path"],
            short_name=row["short_name"],
            glob_pattern=row["glob_pattern"],
            active=bool(row["active"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_scanned_at=row["last_scanned_at"],
        )


# Project category taxonomy. Assigned at creation time (the LLM derives it
# from the project description in /missioncache:new); the dashboard renders
# the stored value and only falls back to a name heuristic when NULL. Must
# stay in sync with TASK_ICONS/TASK_ICON_COLORS in the dashboard index.html.
CATEGORIES = (
    "bug",
    "feature",
    "refactor",
    "test",
    "docs",
    "infra",
    "ui",
    "api",
    "database",
    "security",
    "perf",
    "coding",
    "noncoding",
)


def _validate_category(
    category: Optional[str], custom: frozenset = frozenset()
) -> None:
    """Raise ValueError unless ``category`` is None, in CATEGORIES, or in ``custom``."""
    if category is not None and category not in CATEGORIES and category not in custom:
        raise ValueError(
            f"Invalid category: {category!r}. Must be one of: "
            f"{', '.join(CATEGORIES)}, or a custom category"
        )


# Custom category field constraints. name shares the kebab-case shape of the
# built-ins; "none" is reserved (it is the clear sentinel in update_task and
# the set-category CLI). color is strict hex because it lands in a style
# attribute - anything looser is a CSS injection channel. emoji must contain
# non-ASCII and no HTML metacharacters: a bare length cap would accept plain
# text ("abcdefgh") and markup fragments ("<img/"), leaving render-time
# escaping as the only XSS defense. The cap is generous because ZWJ and
# skin-tone emoji sequences span many codepoints.
_CUSTOM_CATEGORY_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,23}")
_CUSTOM_CATEGORY_COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")
_CUSTOM_CATEGORY_EMOJI_MAX_LEN = 16


@dataclass
class Task:
    id: int
    repo_id: Optional[int]  # Nullable for non-coding tasks
    name: str
    full_path: str
    parent_id: Optional[int]
    status: str
    task_type: str  # 'coding' or 'non-coding'
    tags: List[str]  # Auto-generated tags from task name
    priority: Optional[int]
    jira_key: Optional[str]
    branch: Optional[str]
    pr_url: Optional[str]
    created_at: str
    updated_at: str
    completed_at: Optional[str]
    archived_at: Optional[str]
    last_worked_on: Optional[str]
    origin_uuid: Optional[str] = None  # stable cross-machine project identity
    category: Optional[str] = None  # one of CATEGORIES; NULL = uncategorized

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Task":
        # Parse tags from JSON string
        tags_raw = row["tags"] if "tags" in row.keys() else "[]"
        try:
            tags = json.loads(tags_raw) if tags_raw else []
        except (json.JSONDecodeError, TypeError):
            tags = []

        keys = row.keys()
        return cls(
            id=row["id"],
            repo_id=row["repo_id"],
            name=row["name"],
            full_path=row["full_path"],
            parent_id=row["parent_id"],
            status=row["status"],
            task_type=row["type"] if "type" in keys else "coding",
            tags=tags,
            priority=row["priority"],
            jira_key=row["jira_key"],
            branch=row["branch"],
            pr_url=row["pr_url"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
            archived_at=row["archived_at"],
            last_worked_on=row["last_worked_on"],
            origin_uuid=row["origin_uuid"] if "origin_uuid" in keys else None,
            category=row["category"] if "category" in keys else None,
        )


@dataclass
class Session:
    id: int
    task_id: int
    session_id: Optional[str]
    start_time: str
    end_time: Optional[str]
    duration_seconds: int
    heartbeat_count: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Session":
        return cls(
            id=row["id"],
            task_id=row["task_id"],
            session_id=row["session_id"],
            start_time=row["start_time"],
            end_time=row["end_time"],
            duration_seconds=row["duration_seconds"],
            heartbeat_count=row["heartbeat_count"],
        )


@dataclass
class AutoExecution:
    """A missioncache-auto execution run."""

    id: int
    task_id: int
    started_at: str
    completed_at: Optional[str]
    status: str  # 'running', 'completed', 'failed', 'cancelled'
    mode: str  # 'sequential', 'parallel'
    worker_count: Optional[int]
    total_subtasks: int
    completed_subtasks: int
    failed_subtasks: int
    error_message: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "AutoExecution":
        return cls(
            id=row["id"],
            task_id=row["task_id"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            status=row["status"],
            mode=row["mode"],
            worker_count=row["worker_count"],
            total_subtasks=row["total_subtasks"],
            completed_subtasks=row["completed_subtasks"],
            failed_subtasks=row["failed_subtasks"],
            error_message=row["error_message"],
        )


@dataclass
class AutoExecutionLog:
    """A log entry from a missioncache-auto execution."""

    id: int
    execution_id: int
    timestamp: str
    worker_id: Optional[int]
    subtask_id: Optional[str]
    level: str  # 'debug', 'info', 'warn', 'error', 'success'
    message: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "AutoExecutionLog":
        return cls(
            id=row["id"],
            execution_id=row["execution_id"],
            timestamp=row["timestamp"],
            worker_id=row["worker_id"],
            subtask_id=row["subtask_id"],
            level=row["level"],
            message=row["message"],
        )


# =============================================================================
# Database Manager
# =============================================================================


# Shared by upsert_imported_task (update branch) and rollback_imported_task:
# both rewrite the full authoritative field set on an existing row. Kept in one
# place so a tasks-column change updates both writers in lockstep.
_IMPORT_TASK_UPDATE_SQL = (
    "UPDATE tasks SET name=?, full_path=?, repo_id=?, status=?, type=?, tags=?, "
    "priority=?, jira_key=?, branch=?, pr_url=?, parent_id=?, created_at=?, "
    "origin_uuid=?, category=? WHERE id=?"
)


class TaskDB:
    """SQLite-based task management database."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DB_PATH
        self._connection: Optional[sqlite3.Connection] = None
        # Guard against using MissionCache while data still lives at legacy paths.
        # Raises MissionCacheMigrationRequired (RuntimeError subclass) so callers
        # using `except Exception` catch it normally; CLI entry points pretty-print.
        # Note: the guard inspects module-level DB_PATH (the canonical path),
        # not self.db_path. Callers passing a custom db_path still get the
        # check against the user's primary install location - intentional, so
        # alternate-path TaskDB usage doesn't bypass the migration prompt.
        check_legacy_paths()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Context manager for database connection."""
        if self._connection is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(
                str(self.db_path), detect_types=sqlite3.PARSE_DECLTYPES
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA busy_timeout = 5000")
            # Auto-init schema on first open. SCHEMA_SQL is fully idempotent
            # (28 CREATE ... IF NOT EXISTS clauses), so this is safe for both
            # fresh and existing DBs. Without this, the bare `missioncache-db` CLI
            # and any other first-time caller would crash on "no such table"
            # errors because __init__ only ever created an empty DB file.
            self._connection.executescript(SCHEMA_SQL)
            # Column migrations must run here too, not only in initialize():
            # writers like create_task reference the new columns, so a bare
            # TaskDB() consumer (CLI, hooks) would crash on an un-migrated DB.
            self._migrate_columns(self._connection)
            for key, value in DEFAULT_CONFIG.items():
                self._connection.execute(
                    "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)",
                    (key, json.dumps(value)),
                )
            self._connection.commit()
        try:
            yield self._connection
        except Exception:
            # An error may have left an open write transaction on the cached,
            # reused connection (e.g. process_heartbeats' BEGIN IMMEDIATE with
            # an exception before its commit). Without a rollback the WAL write
            # lock stays held, blocking other processes, and every later
            # BEGIN IMMEDIATE on this connection fails "cannot start a
            # transaction within a transaction" until the process restarts.
            # Roll back before re-raising; guard the rollback so it can never
            # mask the original error.
            try:
                self._connection.rollback()
            except Exception:
                pass
            raise
        # Connection is intentionally kept open for reuse on the success path.

    def close(self):
        """Close the database connection."""
        if self._connection:
            self._connection.close()
            self._connection = None

    @staticmethod
    def _migrate_columns(conn: sqlite3.Connection) -> None:
        """Idempotent column migrations for existing DBs.

        CREATE TABLE IF NOT EXISTS is a no-op on an existing DB, so a new
        column must be added via ALTER. Existing rows are deliberately left
        origin_uuid=NULL (NOT backfilled): a fresh per-machine UUID for an
        already-shared project would differ across machines and falsely read
        as a "different project" on re-import. Existing rows stay
        category=NULL: the dashboard falls back to its name heuristic for
        NULL, and NULLs can be filled by hand via the set-category CLI.
        """
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        if "origin_uuid" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN origin_uuid TEXT")
        if "category" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN category TEXT")

    def initialize(self) -> None:
        """Initialize the database schema and default config."""
        with self.connection() as conn:
            conn.executescript(SCHEMA_SQL)
            self._migrate_columns(conn)

            # Insert default config
            for key, value in DEFAULT_CONFIG.items():
                conn.execute(
                    """INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)""",
                    (key, json.dumps(value)),
                )
            conn.commit()

    # =========================================================================
    # Configuration
    # =========================================================================

    def get_config(self, key: str, default: Any = None) -> Any:
        """Get a configuration value."""
        with self.connection() as conn:
            row = conn.execute(
                "SELECT value FROM config WHERE key = ?", (key,)
            ).fetchone()
            if row:
                return json.loads(row["value"])
            return default

    def set_config(self, key: str, value: Any) -> None:
        """Set a configuration value."""
        with self.connection() as conn:
            conn.execute(
                """INSERT INTO config (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                   updated_at = datetime('now', 'localtime')""",
                (key, json.dumps(value)),
            )
            conn.commit()

    @property
    def idle_timeout_seconds(self) -> int:
        return self.get_config("idle_timeout_seconds", 300)

    @property
    def assumed_work_seconds(self) -> int:
        return self.get_config("assumed_work_seconds", 120)

    @property
    def prune_after_days(self) -> int:
        return self.get_config("prune_after_days", 30)

    # =========================================================================
    # Keyword Management
    # =========================================================================

    def add_keyword(self, keyword: str) -> bool:
        """Add a custom tag keyword.

        Args:
            keyword: The keyword to add (lowercase)

        Returns:
            True if added, False if already exists
        """
        keyword = keyword.lower().strip()
        if not keyword:
            return False

        custom = self.get_config("custom_tag_keywords", [])
        if keyword in custom or keyword in DEFAULT_TAG_KEYWORDS:
            return False

        custom.append(keyword)
        self.set_config("custom_tag_keywords", custom)
        return True

    def remove_keyword(self, keyword: str) -> bool:
        """Remove a custom tag keyword.

        Args:
            keyword: The keyword to remove

        Returns:
            True if removed, False if not found
        """
        keyword = keyword.lower().strip()
        custom = self.get_config("custom_tag_keywords", [])

        if keyword not in custom:
            return False

        custom.remove(keyword)
        self.set_config("custom_tag_keywords", custom)
        return True

    def list_keywords(self) -> Dict[str, List[str]]:
        """List all tag keywords (default + custom).

        Returns:
            Dict with 'default' and 'custom' keyword lists
        """
        custom = self.get_config("custom_tag_keywords", [])
        return {
            "default": sorted(list(DEFAULT_TAG_KEYWORDS)),
            "custom": sorted(custom),
        }

    # =========================================================================
    # Repository Management
    # =========================================================================

    def add_repo(
        self,
        path: Union[str, Path],
        short_name: Optional[str] = None,
        glob_pattern: Optional[str] = None,
    ) -> int:
        """Add a repository to track."""
        path_obj = Path(path).expanduser().resolve()
        path_str = str(path_obj)

        if short_name is None:
            short_name = path_obj.name

        with self.connection() as conn:
            try:
                cursor = conn.execute(
                    """INSERT INTO repositories (path, short_name, glob_pattern)
                       VALUES (?, ?, ?)""",
                    (path_str, short_name, glob_pattern),
                )
                conn.commit()
                return cursor.lastrowid
            except sqlite3.IntegrityError:
                # Already exists
                row = conn.execute(
                    "SELECT id FROM repositories WHERE path = ?", (path_str,)
                ).fetchone()
                return row["id"]

    def add_repos_from_glob(self, pattern: str) -> List[int]:
        """Add multiple repos from a glob pattern."""
        expanded = str(Path(pattern).expanduser())
        paths = glob_files(expanded)
        repo_ids = []
        for p in paths:
            path_obj = Path(p)
            if path_obj.is_dir() and not path_obj.name.startswith("."):
                repo_id = self.add_repo(p, glob_pattern=pattern)
                repo_ids.append(repo_id)
        return repo_ids

    def get_repos(self, active_only: bool = True) -> List[Repository]:
        """Get all tracked repositories."""
        with self.connection() as conn:
            query = "SELECT * FROM repositories"
            if active_only:
                query += " WHERE active = 1"
            query += " ORDER BY short_name"
            rows = conn.execute(query).fetchall()
            return [Repository.from_row(r) for r in rows]

    def get_repo(self, repo_id: int) -> Optional[Repository]:
        """Get a specific repository."""
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM repositories WHERE id = ?", (repo_id,)
            ).fetchone()
            return Repository.from_row(row) if row else None

    def get_repo_by_path(self, path: Union[str, Path]) -> Optional[Repository]:
        """Get a repository by its path."""
        path_str = str(Path(path).expanduser().resolve())
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM repositories WHERE path = ?", (path_str,)
            ).fetchone()
            return Repository.from_row(row) if row else None

    # =========================================================================
    # Task Discovery & Sync
    # =========================================================================

    def scan_repo(self, repo_id: int) -> List[Task]:
        """Scan centralized MissionCache root for tasks and sync with database."""
        repo = self.get_repo(repo_id)
        if not repo:
            return []

        discovered_tasks = []

        # Scan active tasks from centralized MissionCache root
        active_dir = MISSIONCACHE_ROOT / "active"
        if active_dir.exists():
            for task_dir in active_dir.iterdir():
                if task_dir.is_dir() and not task_dir.name.startswith("."):
                    task = self._sync_task_from_dir(repo_id, task_dir, "active")
                    if task:
                        discovered_tasks.append(task)
                        # Check for subtasks
                        for subtask_dir in task_dir.iterdir():
                            if subtask_dir.is_dir() and not subtask_dir.name.startswith(
                                "."
                            ):
                                # Check if it's a subtask (has context.md or tasks.md)
                                if (
                                    (subtask_dir / "context.md").exists()
                                    or (
                                        subtask_dir / f"{subtask_dir.name}-context.md"
                                    ).exists()
                                    or (subtask_dir / "tasks.md").exists()
                                ):
                                    subtask = self._sync_task_from_dir(
                                        repo_id,
                                        subtask_dir,
                                        "active",
                                        parent_id=task.id,
                                    )
                                    if subtask:
                                        discovered_tasks.append(subtask)

        # Reconcile fork links AFTER all dirs are synced (a parent may be
        # discovered later than its child). The "**Fork of:**" header in the
        # child's context file is the durable source of truth: it re-heals a
        # parent_id nulled by ON DELETE SET NULL or lost on import, and its
        # VALID ABSENCE clears a stale link. A file that cannot be read
        # preserves the current link (absence of evidence, not evidence).
        for task in discovered_tasks:
            self._reconcile_fork_link(task)

        # Update last scanned timestamp
        with self.connection() as conn:
            conn.execute(
                "UPDATE repositories SET last_scanned_at = datetime('now', 'localtime') WHERE id = ?",
                (repo_id,),
            )
            conn.commit()

        return discovered_tasks

    def _sync_task_from_dir(
        self, repo_id: int, task_dir: Path, status: str, parent_id: Optional[int] = None
    ) -> Optional[Task]:
        """Sync a single task directory with database."""
        repo = self.get_repo(repo_id)
        if not repo:
            return None

        relative_path = str(task_dir.relative_to(MISSIONCACHE_ROOT))
        task_name = task_dir.name

        # Parse metadata from markdown files
        metadata = self._parse_task_metadata(task_dir)

        with self.connection() as conn:
            # Check if task exists for this repo
            existing = conn.execute(
                "SELECT * FROM tasks WHERE repo_id = ? AND full_path = ?",
                (repo_id, relative_path),
            ).fetchone()

            if existing:
                # Update existing task
                conn.execute(
                    """UPDATE tasks SET
                       jira_key = COALESCE(?, jira_key),
                       branch = COALESCE(?, branch),
                       pr_url = COALESCE(?, pr_url),
                       parent_id = COALESCE(?, parent_id)
                       WHERE id = ?""",
                    (
                        metadata.get("jira_key"),
                        metadata.get("branch"),
                        metadata.get("pr_url"),
                        parent_id,
                        existing["id"],
                    ),
                )
                conn.commit()
                return self.get_task(existing["id"])

            # Check if task already exists in ANY repo (prevent cross-repo duplication)
            any_existing = conn.execute(
                "SELECT * FROM tasks WHERE full_path = ? AND status IN ('active', 'paused')",
                (relative_path,),
            ).fetchone()
            if any_existing:
                # Task belongs to another repo - skip to avoid duplication
                return self.get_task(any_existing["id"])

            # Create new task only if it doesn't exist anywhere
            cursor = conn.execute(
                """INSERT INTO tasks (repo_id, name, full_path, parent_id, status,
                   jira_key, branch, pr_url, origin_uuid)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    repo_id,
                    task_name,
                    relative_path,
                    parent_id,
                    status,
                    metadata.get("jira_key"),
                    metadata.get("branch"),
                    metadata.get("pr_url"),
                    str(uuid.uuid4()),
                ),
            )
            conn.commit()
            return self.get_task(cursor.lastrowid)

    def _parse_task_metadata(self, task_dir: Path) -> Dict[str, str]:
        """Extract metadata from task markdown files."""
        metadata = {}

        # Try various files
        for filename in ["context.md", f"{task_dir.name}-context.md", "README.md"]:
            filepath = task_dir / filename
            if filepath.exists():
                try:
                    content = filepath.read_text()

                    # Extract JIRA key (pattern: GC-XXXXX or similar)
                    jira_match = re.search(r"\[([A-Z]+-\d+)\]", content)
                    if jira_match:
                        metadata["jira_key"] = jira_match.group(1)

                    # Extract branch
                    branch_match = re.search(
                        r'Branch[:\s]+[`"]?([^\s`"]+)[`"]?', content, re.IGNORECASE
                    )
                    if branch_match:
                        metadata["branch"] = branch_match.group(1)

                    # Extract PR URL
                    pr_match = re.search(
                        r"(https://github\.com/[^/]+/[^/]+/pull/\d+)", content
                    )
                    if pr_match:
                        metadata["pr_url"] = pr_match.group(1)

                    break
                except Exception:
                    pass

        return metadata

    def scan_all_repos(self) -> List[Task]:
        """Scan all active repositories for tasks."""
        all_tasks = []
        for repo in self.get_repos(active_only=True):
            tasks = self.scan_repo(repo.id)
            all_tasks.extend(tasks)
        return all_tasks

    # =========================================================================
    # Task CRUD
    # =========================================================================

    def get_task(self, task_id: int) -> Optional[Task]:
        """Get a task by ID."""
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            return Task.from_row(row) if row else None

    def get_task_by_path(self, repo_id: int, full_path: str) -> Optional[Task]:
        """Get a task by its path within a repo."""
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE repo_id = ? AND full_path = ?",
                (repo_id, full_path),
            ).fetchone()
            return Task.from_row(row) if row else None

    def find_task_by_full_path(self, full_path: str) -> Optional[Task]:
        """Find a task by full_path across all repos."""
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE full_path = ? AND status = 'active'",
                (full_path,),
            ).fetchone()
            return Task.from_row(row) if row else None

    def find_import_target(self, name: str, full_path: str) -> Optional[Task]:
        """Find the row a cross-machine import would UPDATE, or None to INSERT.

        The single lookup shared by the import collision classifier and
        ``upsert_imported_task`` so both act on the SAME row (no active-only vs
        active+paused scope drift). Prefers a live (active/paused) row at
        ``full_path``; falls back to the same-named row, active first. A
        completed/archived row at a different ``full_path`` is intentionally not
        matched here - it can still collide on ``UNIQUE(repo_id, full_path)`` at
        write time, which the upsert surfaces as ``ImportConflictError``.
        """
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE full_path = ? "
                "AND status IN ('active','paused') ORDER BY id DESC",
                (full_path,),
            ).fetchone() or conn.execute(
                "SELECT * FROM tasks WHERE name = ? "
                "ORDER BY CASE WHEN status='active' THEN 0 ELSE 1 END, id DESC",
                (name,),
            ).fetchone()
            return Task.from_row(row) if row else None

    def upsert_imported_task(
        self,
        name: str,
        full_path: str,
        *,
        repo_id: Optional[int],
        status: str,
        task_type: str,
        tags: List[str],
        priority: Optional[int],
        jira_key: Optional[str],
        branch: Optional[str],
        pr_url: Optional[str],
        parent_id: Optional[int],
        created_at: Optional[str],
        origin_uuid: Optional[str] = None,
        category: Optional[str] = None,
    ) -> Tuple[Task, str]:
        """Name/path-keyed upsert for cross-machine import (Phase 3).

        The DB write at the heart of ``portability.import_bundle``. Import must
        NOT route through ``create_task`` (writes a ``manual/``/``global/``
        full_path, lacks status/branch/pr_url/priority/parent/tags) or
        ``_sync_task_from_dir`` (COALESCE-only update can't correct drift; its
        cross-repo dedup skips a repo rebind). This writes every authoritative
        field from the manifest directly.

        The existing row is found via ``find_import_target`` - the SAME lookup
        the import pipeline's collision classifier uses, so the row this UPDATEs
        is exactly the row the classifier validated (no active-vs-paused scope
        drift). An existing match is UPDATED in place (``.id`` preserved), so
        re-import is idempotent and a changed ``repo_id`` rebinds; the UPDATE
        also rewrites ``name``/``full_path`` so the row never desyncs from where
        the files were placed. ``created_at`` is preserved as the EARLIER of
        existing vs incoming (compared separator-insensitively so a ``T`` vs
        space form does not flip the order) so the origin date survives a
        re-import. A missing incoming ``created_at`` falls back to the column
        default. Returns ``(task, "created"|"updated")``.

        ``origin_uuid`` is the stable cross-machine project identity. On UPDATE
        the existing row's uuid is preserved, adopting the incoming one only to
        heal a pre-migration NULL. On CREATE the incoming uuid is adopted (so a
        re-export from this machine still matches the origin), or a fresh one is
        minted when the bundle predates the feature.

        ``category`` follows the same asymmetry: an incoming value is
        authoritative, but an incoming NULL is ambiguous (pre-category bundle
        vs deliberately uncategorized), so on UPDATE a NULL preserves the
        existing row's category instead of clearing it.

        A write that collides with a DIFFERENT row on ``UNIQUE(repo_id,
        full_path)`` raises ``ImportConflictError``; any other integrity error
        (a real bug) propagates unwrapped rather than being mislabeled a
        conflict.
        """
        _validate_category(category, self.custom_category_names())
        existing = self.find_import_target(name, full_path)
        with self.connection() as conn:
            try:
                if existing:
                    keep_created = min(
                        filter(None, [existing.created_at, created_at]),
                        key=lambda s: s.replace("T", " "),
                        default=created_at,
                    )
                    keep_uuid = existing.origin_uuid or origin_uuid
                    keep_category = category if category is not None else existing.category
                    conn.execute(
                        _IMPORT_TASK_UPDATE_SQL,
                        (name, full_path, repo_id, status, task_type,
                         json.dumps(tags), priority, jira_key, branch, pr_url,
                         parent_id, keep_created, keep_uuid, keep_category,
                         existing.id),
                    )
                    conn.commit()
                    return self.get_task(existing.id), "updated"
                cur = conn.execute(
                    "INSERT INTO tasks (repo_id, name, full_path, parent_id, "
                    "status, type, tags, priority, jira_key, branch, pr_url, "
                    "created_at, origin_uuid, category) VALUES (?,?,?,?,?,?,?,?,?,?,?,"
                    "COALESCE(?, datetime('now','localtime')),?,?)",
                    (repo_id, name, full_path, parent_id, status, task_type,
                     json.dumps(tags), priority, jira_key, branch, pr_url,
                     created_at, origin_uuid or str(uuid.uuid4()), category),
                )
                conn.commit()
                return self.get_task(cur.lastrowid), "created"
            except sqlite3.IntegrityError as e:
                if "UNIQUE constraint" in str(e):
                    raise ImportConflictError(
                        f"import conflict: (repo_id={repo_id}, {full_path}) is "
                        f"already held by another task row: {e}"
                    )
                raise

    def rollback_imported_task(
        self, action: str, task_id: int, pre: Optional[Task]
    ) -> None:
        """Compensate a committed ``upsert_imported_task`` after placement failure.

        Import writes the DB row before placing files so a UNIQUE conflict
        aborts with the filesystem untouched (see ``portability.import_bundle``).
        This covers the mirror direction: when file placement raises AFTER the
        upsert committed, a ``created`` row is deleted and an ``updated`` row
        gets its pre-image fields written back, so import's exit 1 keeps
        meaning nothing was committed.
        """
        with self.connection() as conn:
            if action == "created":
                conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
            elif pre is not None:
                conn.execute(
                    _IMPORT_TASK_UPDATE_SQL,
                    (pre.name, pre.full_path, pre.repo_id, pre.status,
                     pre.task_type, json.dumps(pre.tags), pre.priority,
                     pre.jira_key, pre.branch, pre.pr_url, pre.parent_id,
                     pre.created_at, pre.origin_uuid, pre.category, task_id),
                )
            conn.commit()

    # =========================================================================
    # Non-Coding Task Management
    # =========================================================================

    def create_task(
        self,
        name: str,
        task_type: str = "coding",
        repo_id: Optional[int] = None,
        jira_key: Optional[str] = None,
        category: Optional[str] = None,
    ) -> Task:
        """Create a new task (coding or non-coding).

        Args:
            name: Task name (e.g., "Sprint planning meeting")
            task_type: 'coding' or 'non-coding'
            repo_id: Repository ID (required for coding, None for non-coding)
            jira_key: Optional JIRA ticket ID
            category: Optional project category (one of CATEGORIES)

        Returns:
            The created Task object
        """
        if task_type not in ("coding", "non-coding"):
            raise ValueError(f"Invalid task type: {task_type}")
        _validate_category(category, self.custom_category_names())

        # Non-coding tasks must not have a repo_id
        if task_type == "non-coding" and repo_id is not None:
            raise ValueError("Non-coding tasks cannot be associated with a repository")

        # Coding tasks should have a repo_id (though we allow None for flexibility)
        tags = extract_tags(name)
        full_path = f"global/{name}" if task_type == "non-coding" else f"manual/{name}"

        with self.connection() as conn:
            cursor = conn.execute(
                """INSERT INTO tasks (repo_id, name, full_path, type, tags,
                   jira_key, status, origin_uuid, category)
                   VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
                (repo_id, name, full_path, task_type, json.dumps(tags), jira_key,
                 str(uuid.uuid4()), category),
            )
            conn.commit()
            return self.get_task(cursor.lastrowid)

    def set_task_parent(self, child_id: int, parent_id: Optional[int]) -> Task:
        """Set (or clear, with None) a task's fork parent.

        Idempotent; refuses self-parenting and any link that would create a
        cycle (the proposed parent's ancestor chain must not reach the child).
        """
        if parent_id == child_id:
            raise ValueError("A task cannot be its own parent")
        with self.connection() as conn:
            # The cycle walk reads the ancestor chain, then the UPDATE writes.
            # BEGIN IMMEDIATE takes the write lock up front so a concurrent
            # scan_repo on another process cannot slip a mutual link between the
            # check and the write and form a cross-process cycle (which would
            # re-hide both nodes from the hierarchy). Matches process_heartbeats.
            conn.execute("BEGIN IMMEDIATE")
            if parent_id is not None:
                parent_row = conn.execute(
                    "SELECT id FROM tasks WHERE id = ?", (parent_id,)
                ).fetchone()
                if not parent_row:
                    raise ValueError(f"Parent task {parent_id} not found")
                # Walk the proposed parent's ancestor chain inside the same
                # connection; reaching the child means a cycle.
                seen = set()
                cursor_id = parent_id
                while cursor_id is not None and cursor_id not in seen:
                    seen.add(cursor_id)
                    row = conn.execute(
                        "SELECT parent_id FROM tasks WHERE id = ?", (cursor_id,)
                    ).fetchone()
                    cursor_id = row["parent_id"] if row else None
                    if cursor_id == child_id:
                        raise ValueError(
                            f"Linking task {child_id} under {parent_id} would create a cycle"
                        )
            cursor = conn.execute(
                "UPDATE tasks SET parent_id = ? WHERE id = ?", (parent_id, child_id)
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Task {child_id} not found")
            conn.commit()
        return self.get_task(child_id)

    @staticmethod
    def _is_flat_fork_path(full_path: str) -> bool:
        """Flat fork projects live at active/<name> (or manual/global/<name>);
        legacy nested subtasks live at active/<parent>/<name>. Only flat
        tasks participate in fork-header reconciliation."""
        return full_path.count("/") <= 1

    def _reconcile_fork_link(self, task: Task) -> None:
        """Sync one task's parent_id with its "**Fork of:**" context header.

        Header present -> link (unless unresolvable/ambiguous/cyclic: preserve).
        Header validly absent -> clear a stale link (flat forks only; legacy
        nested subtasks never carry the header and are never touched).
        Context file unreadable -> preserve.
        """
        from missioncache_db import context_health

        if not self._is_flat_fork_path(task.full_path):
            return
        task_dir = MISSIONCACHE_ROOT / task.full_path
        content = None
        # Prefixed name FIRST, matching every reader (get_missioncache_files,
        # statusline _resolve_parent_context / get_project_info). The reconcile
        # is the only path that can null parent_id, so it must certify off the
        # exact file the digest and statusline render from - reading the legacy
        # context.md first would let a stale unprefixed file silently de-link a
        # live fork nobody can see is unlinked.
        for filename in (f"{task.name}-context.md", "context.md"):
            filepath = task_dir / filename
            if filepath.exists():
                try:
                    content = filepath.read_text()
                except OSError:
                    return  # unreadable: preserve the current link
                break
        if content is None:
            return  # no context file: preserve
        fork_name = context_health.parse_fork_parent(content)
        if fork_name is None:
            if task.parent_id is not None:
                logger.warning(
                    "Fork reconcile: clearing parent link on %r - its context "
                    "header no longer names a parent (moved below the first "
                    "section, or edited to an unparseable form?)",
                    task.name,
                )
                self.set_task_parent(task.id, None)
            return
        if fork_name == task.name:
            return
        parent = self._resolve_task_by_name(fork_name, prefer_repo_id=task.repo_id)
        if parent is None:
            logger.warning(
                "Fork reconcile: %r declares '**Fork of:** %s' but no "
                "unambiguous parent resolved; link preserved as-is",
                task.name,
                fork_name,
            )
            return
        if parent.id == task.id or task.parent_id == parent.id:
            return
        try:
            self.set_task_parent(task.id, parent.id)
        except ValueError:
            # cycle or race: preserve the current link rather than corrupt
            logger.warning(
                "Fork reconcile: linking %r under %r rejected (cycle or "
                "concurrent scan); link preserved",
                task.name,
                fork_name,
            )
            return

    def _resolve_task_by_name(
        self, name: str, prefer_repo_id: Optional[int] = None
    ) -> Optional[Task]:
        """Resolve a "**Fork of:**" name to its parent task.

        Ranking: the canonical project row (full_path active/<name>) first,
        then the preferred repo, then active/paused over completed. Archived
        rows are excluded. If the two best candidates tie on all ranks the
        resolution is AMBIGUOUS and returns None - never guess a parent."""
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE name = ? AND status != 'archived'",
                (name,),
            ).fetchall()
        if not rows:
            return None
        tasks = [Task.from_row(r) for r in rows]

        def rank(t: Task) -> tuple:
            return (
                0 if t.full_path == f"active/{name}" else 1,
                0 if (prefer_repo_id is not None and t.repo_id == prefer_repo_id) else 1,
                0 if t.status in ("active", "paused") else 1,
            )

        tasks.sort(key=rank)
        if len(tasks) > 1 and rank(tasks[0]) == rank(tasks[1]):
            return None
        return tasks[0]

    def set_task_category(self, task_id: int, category: Optional[str]) -> Task:
        """Set (or clear) a task's category.

        Args:
            task_id: The task ID
            category: One of CATEGORIES, or None to clear

        Returns:
            The updated Task object

        Raises:
            ValueError: If the category is not in CATEGORIES or the task
                does not exist
        """
        _validate_category(category, self.custom_category_names())
        with self.connection() as conn:
            cursor = conn.execute(
                "UPDATE tasks SET category = ?, "
                "updated_at = datetime('now', 'localtime') WHERE id = ?",
                (category, task_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Task {task_id} not found")
            conn.commit()
            return self.get_task(task_id)

    def set_task_jira(self, task_id: int, jira_key: Optional[str]) -> Task:
        """Set (or clear) a task's JIRA key.

        Args:
            task_id: The task ID
            jira_key: The JIRA ticket ID (e.g. "PROJ-12345"), or None to clear

        Returns:
            The updated Task object

        Raises:
            ValueError: If the task does not exist
        """
        with self.connection() as conn:
            cursor = conn.execute(
                "UPDATE tasks SET jira_key = ?, "
                "updated_at = datetime('now', 'localtime') WHERE id = ?",
                (jira_key, task_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Task {task_id} not found")
            conn.commit()
            return self.get_task(task_id)

    # =========================================================================
    # Custom categories
    # =========================================================================

    def list_custom_categories(self) -> List[Dict[str, str]]:
        """All custom categories as [{"name", "emoji", "color"}], name-ordered."""
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT name, emoji, color FROM custom_categories ORDER BY name"
            ).fetchall()
            return [dict(r) for r in rows]

    def custom_category_names(self) -> frozenset:
        """The set of custom category names (for validation)."""
        with self.connection() as conn:
            return frozenset(
                r["name"] for r in conn.execute("SELECT name FROM custom_categories")
            )

    def add_custom_category(self, name: str, emoji: str, color: str) -> Dict[str, str]:
        """Add a custom category. Returns the stored row.

        Raises:
            ValueError: On invalid name/emoji/color, a name that collides
                with the built-in taxonomy or the 'none' clear sentinel,
                or a duplicate custom name.
        """
        name = (name or "").strip().lower()
        emoji = (emoji or "").strip()
        color = (color or "").strip()

        if not _CUSTOM_CATEGORY_NAME_RE.fullmatch(name):
            raise ValueError(
                "Invalid category name: lowercase letters, digits, and hyphens "
                "only, starting with a letter or digit (max 24 chars)"
            )
        if name in CATEGORIES or name == "none":
            raise ValueError(f"{name!r} is reserved (built-in category or sentinel)")
        if not emoji or len(emoji) > _CUSTOM_CATEGORY_EMOJI_MAX_LEN:
            raise ValueError(
                f"emoji is required (max {_CUSTOM_CATEGORY_EMOJI_MAX_LEN} chars)"
            )
        # Content, not just length: all-ASCII means plain text, not an emoji,
        # and the metacharacter check keeps markup fragments (even mixed with
        # real emoji) out of the DB entirely.
        if emoji.isascii() or any(c in emoji for c in "<>&\"'"):
            raise ValueError(
                "emoji must be emoji characters, not plain text or markup"
            )
        if not _CUSTOM_CATEGORY_COLOR_RE.fullmatch(color):
            raise ValueError("color must be a #RRGGBB hex value")

        with self.connection() as conn:
            try:
                conn.execute(
                    "INSERT INTO custom_categories (name, emoji, color) VALUES (?, ?, ?)",
                    (name, emoji, color),
                )
            except sqlite3.IntegrityError as e:
                # Only the duplicate-PK case is a "conflict"; any other
                # integrity error is a real bug and propagates unwrapped
                # (the import_task pattern - don't mislabel it).
                if "UNIQUE constraint" in str(e):
                    raise ValueError(f"Custom category {name!r} already exists")
                raise
            conn.commit()
        return {"name": name, "emoji": emoji, "color": color}

    def remove_custom_category(self, name: str) -> bool:
        """Remove a custom category. Returns True when a row was deleted.

        Tasks still carrying the removed value keep it by design (the
        dashboard renders the bare name with default styling, and the value
        stays selectable per-task); re-adding the category restores styling.
        """
        # Normalize like add_custom_category stores, so DELETE "UI" hits "ui".
        name = (name or "").strip().lower()
        with self.connection() as conn:
            cursor = conn.execute(
                "DELETE FROM custom_categories WHERE name = ?", (name,)
            )
            conn.commit()
            return cursor.rowcount > 0

    def add_task_update(self, task_id: int, note: str) -> int:
        """Add a timestamped update to a task.

        Args:
            task_id: The task ID
            note: The update note

        Returns:
            The update ID
        """
        with self.connection() as conn:
            cursor = conn.execute(
                "INSERT INTO task_updates (task_id, note) VALUES (?, ?)",
                (task_id, note),
            )
            # Also update the task's last_worked_on timestamp
            conn.execute(
                "UPDATE tasks SET last_worked_on = datetime('now', 'localtime') WHERE id = ?",
                (task_id,),
            )
            conn.commit()
            return cursor.lastrowid

    def get_task_updates(self, task_id: int, limit: int = 50) -> List[Dict]:
        """Get updates for a task.

        Args:
            task_id: The task ID
            limit: Maximum number of updates to return

        Returns:
            List of update dicts with id, note, created_at
        """
        with self.connection() as conn:
            rows = conn.execute(
                """SELECT id, note, created_at
                   FROM task_updates
                   WHERE task_id = ?
                   ORDER BY created_at DESC
                   LIMIT ?""",
                (task_id, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_today_updates(self, task_id: Optional[int] = None) -> List[Dict]:
        """Get all updates from today, optionally filtered by task.

        Args:
            task_id: Optional task ID to filter by

        Returns:
            List of update dicts with task info
        """
        with self.connection() as conn:
            if task_id:
                rows = conn.execute(
                    """SELECT u.id, u.task_id, u.note, u.created_at, t.name as task_name
                       FROM task_updates u
                       JOIN tasks t ON u.task_id = t.id
                       WHERE u.task_id = ? AND date(u.created_at) = date('now', 'localtime')
                       ORDER BY u.created_at DESC""",
                    (task_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT u.id, u.task_id, u.note, u.created_at, t.name as task_name
                       FROM task_updates u
                       JOIN tasks t ON u.task_id = t.id
                       WHERE date(u.created_at) = date('now', 'localtime')
                       ORDER BY u.created_at DESC"""
                ).fetchall()
            return [dict(row) for row in rows]

    def get_active_tasks(self, repo_id: Optional[int] = None) -> List[Task]:
        """Get all active tasks, optionally filtered by repo."""
        with self.connection() as conn:
            if repo_id:
                rows = conn.execute(
                    """SELECT * FROM tasks
                       WHERE status IN ('active', 'paused') AND repo_id = ?
                       ORDER BY last_worked_on DESC NULLS LAST""",
                    (repo_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM tasks
                       WHERE status IN ('active', 'paused')
                       ORDER BY last_worked_on DESC NULLS LAST"""
                ).fetchall()
            return [Task.from_row(r) for r in rows]

    def get_active_tasks_hierarchical(
        self, repo_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """Get active tasks organized as hierarchy.

        Returns:
            {
                "top_level": [Task, ...],  # Tasks with no parent
                "children": {parent_id: [Task, ...]},  # Child tasks grouped by parent
            }
        """
        all_tasks = self.get_active_tasks(repo_id)
        active_ids = {task.id for task in all_tasks}
        top_level = []
        children: Dict[int, List[Task]] = {}

        for task in all_tasks:
            # A child whose parent is not in the active set (completed or
            # deleted parent) surfaces top-level instead of being orphaned
            # under a key no caller will look up.
            if task.parent_id is None or task.parent_id not in active_ids:
                top_level.append(task)
            else:
                children.setdefault(task.parent_id, []).append(task)

        return {"top_level": top_level, "children": children}

    def get_recent_completed(self, days: int = 7) -> List[Task]:
        """Get recently completed tasks."""
        with self.connection() as conn:
            rows = conn.execute(
                """SELECT * FROM tasks
                   WHERE status = 'completed'
                   AND completed_at >= datetime('now', 'localtime', ?)
                   ORDER BY completed_at DESC""",
                (f"-{days} days",),
            ).fetchall()
            return [Task.from_row(r) for r in rows]

    def get_all_completed(self, limit: int = 50) -> List[Task]:
        """Get all completed tasks (not archived)."""
        with self.connection() as conn:
            rows = conn.execute(
                """SELECT * FROM tasks
                   WHERE status = 'completed'
                   ORDER BY completed_at DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
            return [Task.from_row(r) for r in rows]

    def get_task_by_name(
        self, name: str, status: Optional[str] = None
    ) -> Optional[Task]:
        """Get a task by its name, optionally filtered by status."""
        with self.connection() as conn:
            if status:
                row = conn.execute(
                    "SELECT * FROM tasks WHERE name = ? AND status = ?", (name, status)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM tasks WHERE name = ? "
                    "ORDER BY CASE WHEN status='active' THEN 0 ELSE 1 END, id DESC",
                    (name,),
                ).fetchone()
            return Task.from_row(row) if row else None

    def reopen_task(self, task_id: int) -> Optional[Task]:
        """Reopen a completed task by setting it back to active.

        Args:
            task_id: The task ID to reopen

        Returns:
            The updated Task object or None if not found
        """
        with self.connection() as conn:
            # Verify task exists and is completed
            task = self.get_task(task_id)
            if not task:
                return None
            if task.status != "completed":
                return task  # Already active, return as-is

            # Update status to active and clear completed_at
            conn.execute(
                """UPDATE tasks SET
                   status = 'active',
                   completed_at = NULL,
                   last_worked_on = datetime('now', 'localtime')
                   WHERE id = ?""",
                (task_id,),
            )
            conn.commit()
        return self.get_task(task_id)

    def update_task_status(self, task_id: int, status: str) -> Optional[Task]:
        """Update task status."""
        with self.connection() as conn:
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
            conn.commit()
        return self.get_task(task_id)

    def complete_project(self, task_id: int, move_files: bool = True) -> Dict[str, Any]:
        """Complete a project: flip status, move its MissionCache directory to
        completed/, report totals and any still-active forks.

        Single source of truth for the composition - the MCP complete_task
        tool and the dashboard's complete endpoint both delegate here so the
        two surfaces cannot drift. Errors come back as dicts with a ``code``
        (NOT_FOUND / INVALID_STATE); callers map them to their own error
        surface (MCP error classes, HTTP status codes).

        ``full_path`` deliberately stays as-is after the move (matching the
        historical MCP behavior): every consumer resolves it by searching
        active/ then completed/.
        """
        import shutil

        task = self.get_task(task_id)
        if not task:
            return {
                "error": True,
                "code": "NOT_FOUND",
                "message": f"Task {task_id} not found",
            }
        if task.status == "completed":
            return {
                "error": True,
                "code": "INVALID_STATE",
                "message": "Task is already completed",
                "current_state": "completed",
            }

        previous_status = task.status
        updated = self.update_task_status(task_id, "completed")

        files_moved = False
        if move_files and task.task_type == "coding":
            # Canonical layout first (active/<name> - what create_missioncache_files
            # writes and every resolver searches), then the stored full_path
            # (which can carry legacy shapes like manual/<name>).
            candidates = [
                MISSIONCACHE_ROOT / "active" / task.name,
                MISSIONCACHE_ROOT / task.full_path,
            ]
            source = next((c for c in candidates if c.exists()), None)
            if source is not None:
                dest = MISSIONCACHE_ROOT / "completed" / task.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(dest))
                files_moved = True

        time_total = self.get_task_time(task_id)
        result: Dict[str, Any] = {
            "task_id": task.id,
            "task_name": task.name,
            "previous_status": previous_status,
            "new_status": "completed",
            "completed_at": (updated.completed_at or "") if updated else "",
            "time_total_seconds": time_total,
            "time_total_formatted": self.format_duration(time_total),
            "files_moved": files_moved,
        }

        # Fork awareness: completing a parent with active children is allowed
        # (the shared context stays readable from completed/), but callers
        # should surface it. Advisory only - the completion is committed
        # above, so a failure here must NOT flip the result to an error.
        try:
            active_children = [
                t for t in self.get_active_tasks() if t.parent_id == task.id
            ]
            result["active_children_count"] = len(active_children)
            if active_children:
                names = ", ".join(t.name for t in active_children)
                result["warning"] = (
                    f"This project has {len(active_children)} active fork(s) "
                    f"({names}). Its context file stays readable and shared for "
                    "them from completed/."
                )
        except Exception:
            logger.warning(
                "Active-children lookup failed after completing %s; "
                "completion succeeded, fork warning unavailable",
                task.name,
                exc_info=True,
            )
            result["active_children_count"] = None
        return result

    def reopen_project(self, task_id: int, move_files: bool = True) -> Dict[str, Any]:
        """Reopen a completed project: move its directory back to active/,
        flip status back. Symmetric counterpart of complete_project and the
        shared primitive under the MCP reopen_task tool and the dashboard's
        reopen endpoint."""
        import shutil

        task = self.get_task(task_id)
        if not task:
            return {
                "error": True,
                "code": "NOT_FOUND",
                "message": f"Task {task_id} not found",
            }
        if task.status != "completed":
            return {
                "error": True,
                "code": "INVALID_STATE",
                "message": "Task is not completed",
                "current_state": task.status,
            }

        if move_files and task.task_type == "coding":
            source = MISSIONCACHE_ROOT / "completed" / task.name
            if source.exists():
                dest = MISSIONCACHE_ROOT / "active" / task.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(dest))

        self.reopen_task(task_id)
        return {
            "task_id": task.id,
            "task_name": task.name,
            "previous_status": "completed",
            "new_status": "active",
        }

    def rename_task(self, task_id: int, new_name: str) -> Dict[str, Any]:
        """Rename a project: update DB row, move directory, rename files, rewrite H1s.

        Inputs are normalized (trim + lowercase) before validation. The
        response always reports the canonical stored name in ``name``;
        callers should display that, not the user-typed input. The
        ``normalized`` flag tells callers whether normalization changed
        the input so they can prefix their confirmation message.

        Atomicity: every filesystem mutation (outer dir rename, inner
        file renames, H1 rewrites) is recorded as it happens. If the DB
        UPDATE fails after FS work succeeded, every recorded mutation is
        reversed in LIFO order before the original exception propagates.
        Reversal failures are logged via the module logger and surfaced
        in the response ``warnings`` list. The pre-flight auto-run guard
        is re-checked inside the write transaction to tighten the TOCTOU
        window between the user-facing check and the UPDATE.

        Subtasks (``parent_id is not None``) are out of scope - rename
        the parent instead.

        Returns:
            Dict with keys: success, changed, name, old_name, normalized,
            full_path, files_renamed, h1_rewritten, h1_skipped,
            sessions_updated, warnings.

        Raises:
            ValueError: invalid name (after normalization), missing task,
                subtask, or unexpected full_path shape.
            NameCollisionError: another task in the same repo has that name.
            FilesystemCollisionError: target MissionCache directory already exists.
            AutoRunActiveError: a missioncache-auto run is currently running.
        """
        raw_input = new_name
        new_name = new_name.strip().lower()
        normalized = new_name != raw_input
        validate_task_name(new_name)

        task = self.get_task(task_id)
        if not task:
            raise ValueError(f"No project found with id {task_id}.")
        if task.parent_id is not None and not self._is_flat_fork_path(task.full_path):
            # Nested legacy subtasks stay rename-blocked; flat FORK children
            # are full projects and rename like any other.
            raise ValueError(
                "Subtask rename is not supported. Rename the parent project instead."
            )

        old_name = task.name
        # No-op short-circuit. Same-name rename returns success cleanly.
        if new_name == old_name:
            return {
                "success": True,
                "changed": False,
                "name": new_name,
                "old_name": old_name,
                "normalized": normalized,
                "full_path": task.full_path,
                "files_renamed": [],
                "h1_rewritten": [],
                "h1_skipped": [],
                "sessions_updated": 0,
                "warnings": [],
            }

        # Compute new full_path - same prefix, new name. Splits on the LAST
        # "/" so any "<prefix>/<name>" shape works (active/, manual/,
        # global/, dev/active/<repo>/, etc.). Subtasks were already refused.
        prefix_parts = task.full_path.rsplit("/", 1)
        if len(prefix_parts) != 2:
            raise ValueError(
                f"Unexpected full_path shape: {task.full_path!r}. "
                f"Expected '<prefix>/<name>'."
            )
        prefix = prefix_parts[0]
        new_full_path = f"{prefix}/{new_name}"

        # Pre-flight checks (DB collision + auto-run guard). Friendly
        # fast-fail before we touch the filesystem. Re-checked inside
        # the write transaction below to tighten the TOCTOU window.
        with self.connection() as conn:
            existing = conn.execute(
                "SELECT id FROM tasks "
                "WHERE COALESCE(repo_id, -1) = COALESCE(?, -1) "
                "  AND full_path = ? AND id != ?",
                (task.repo_id, new_full_path, task_id),
            ).fetchone()
            if existing:
                raise NameCollisionError(
                    f"A project named '{new_name}' already exists. "
                    f"Pick a different name."
                )

            running = conn.execute(
                "SELECT id FROM auto_executions "
                "WHERE task_id = ? AND status = 'running' LIMIT 1",
                (task_id,),
            ).fetchone()
            if running:
                raise AutoRunActiveError(
                    "Cannot rename while missioncache-auto is running on this project. "
                    "Stop the run and try again."
                )

        # Filesystem collision pre-check.
        old_dir = MISSIONCACHE_ROOT / task.full_path
        new_dir = MISSIONCACHE_ROOT / new_full_path
        if new_dir.exists():
            raise FilesystemCollisionError(
                f"Directory '{new_dir}' already exists. Pick a different name."
            )

        files_renamed: List[str] = []
        h1_rewritten: List[str] = []
        h1_skipped: List[str] = []
        # Rollback ledgers - every successful FS mutation is appended
        # here so the DB-failure rollback can reverse it in LIFO order.
        renamed_pairs: List[Tuple[Path, Path]] = []  # (current_path, original_path)
        h1_originals: List[Tuple[Path, str]] = []  # (path, original_content)
        fs_renamed = False

        # Coding tasks have an on-disk directory; non-coding tasks
        # (full_path = "global/<name>") do not. Skip FS work in the
        # latter case - the DB update is enough.
        if old_dir.exists():
            # Pre-flight inner-file collision check BEFORE moving the outer
            # dir. POSIX rename(2) silently overwrites an existing target
            # file - if the source dir somehow already contains a file with
            # the new prefix (e.g. user manually renamed one of the four
            # without using this primitive), the inner loop would clobber
            # it. Raising here keeps the FS untouched so no rollback is
            # needed.
            for suffix in ("plan", "context", "tasks", "iteration-log"):
                proposed_target = old_dir / f"{new_name}-{suffix}.md"
                if proposed_target.exists():
                    raise FilesystemCollisionError(
                        f"File '{proposed_target.name}' already exists in "
                        f"'{old_dir}'. Resolve manually before renaming."
                    )

            old_dir.rename(new_dir)
            fs_renamed = True

            # File renames inside. Prompts subdir uses unprefixed names
            # (task-NN-prompt.md) so it stays untouched.
            for suffix in ("plan", "context", "tasks", "iteration-log"):
                old_file = new_dir / f"{old_name}-{suffix}.md"
                new_file = new_dir / f"{new_name}-{suffix}.md"
                if old_file.exists():
                    old_file.rename(new_file)
                    renamed_pairs.append((new_file, old_file))
                    files_renamed.append(new_file.name)

            # H1 rewrite - only when the H1 still matches the exact
            # template default. If the user has edited the H1 (different
            # text, different shape), leave it alone and report skipped.
            old_titlecase = old_name.replace("-", " ").title()
            new_titlecase = new_name.replace("-", " ").title()
            for suffix, label in (
                ("plan", "Plan"),
                ("context", "Context"),
                ("tasks", "Tasks"),
            ):
                f = new_dir / f"{new_name}-{suffix}.md"
                if not f.exists():
                    continue
                try:
                    content = f.read_text()
                except OSError:
                    h1_skipped.append(f.name)
                    continue
                head, _, rest = content.partition("\n")
                expected_h1 = f"# {old_titlecase} - {label}"
                if head.rstrip() == expected_h1:
                    new_h1 = f"# {new_titlecase} - {label}"
                    try:
                        f.write_text(new_h1 + "\n" + rest if rest else new_h1)
                        h1_originals.append((f, content))
                        h1_rewritten.append(f.name)
                    except OSError:
                        h1_skipped.append(f.name)
                else:
                    h1_skipped.append(f.name)

        # DB update - re-check the auto-run guard inside the same
        # connection used for the UPDATE so a concurrent missioncache-auto INSERT
        # between the pre-flight check and the UPDATE is caught. Narrow
        # except sqlite3.Error so unrelated bugs (KeyError, attribute
        # mistakes, MissionCacheMigrationRequired) don't silently trigger a
        # misdirected FS rollback.
        try:
            with self.connection() as conn:
                running = conn.execute(
                    "SELECT id FROM auto_executions "
                    "WHERE task_id = ? AND status = 'running' LIMIT 1",
                    (task_id,),
                ).fetchone()
                if running:
                    raise AutoRunActiveError(
                        "Cannot rename while missioncache-auto is running on this project. "
                        "Stop the run and try again."
                    )
                conn.execute(
                    "UPDATE tasks SET name = ?, full_path = ? WHERE id = ?",
                    (new_name, new_full_path, task_id),
                )
                # Subtask rows embed the parent's full_path as a prefix
                # (e.g. "active/parent/child"). The outer dir rename
                # already moved their on-disk dirs as subdirectories, but
                # their DB rows would otherwise keep the old prefix, so
                # the next scan_repos would re-discover them at the new
                # path and create duplicate rows. Rewrite the prefix in
                # the same transaction.
                old_prefix = task.full_path + "/"
                new_prefix = new_full_path + "/"
                children = conn.execute(
                    "SELECT id, full_path FROM tasks WHERE parent_id = ?",
                    (task_id,),
                ).fetchall()
                for child in children:
                    if child["full_path"].startswith(old_prefix):
                        new_child_path = (
                            new_prefix + child["full_path"][len(old_prefix):]
                        )
                        conn.execute(
                            "UPDATE tasks SET full_path = ? WHERE id = ?",
                            (new_child_path, child["id"]),
                        )
                conn.commit()
        except (sqlite3.Error, AutoRunActiveError):
            # Reverse every recorded FS mutation in LIFO order: H1
            # contents -> inner file renames -> outer directory rename.
            # Each step is independently logged so partial-rollback
            # state is observable.
            for f, original in reversed(h1_originals):
                try:
                    f.write_text(original)
                except OSError:
                    logger.exception(
                        "rename rollback: H1 restore failed for %s", f
                    )
            for current, original in reversed(renamed_pairs):
                try:
                    current.rename(original)
                except OSError:
                    logger.exception(
                        "rename rollback: inner file restore failed for %s -> %s",
                        current,
                        original,
                    )
            if fs_renamed:
                try:
                    new_dir.rename(old_dir)
                except OSError:
                    logger.exception(
                        "rename rollback: directory restore failed for %s -> %s",
                        new_dir,
                        old_dir,
                    )
            raise

        sweep = self._sweep_session_pointers(old_name, new_name)

        return {
            "success": True,
            "changed": True,
            "name": new_name,
            "old_name": old_name,
            "normalized": normalized,
            "full_path": new_full_path,
            "files_renamed": files_renamed,
            "h1_rewritten": h1_rewritten,
            "h1_skipped": h1_skipped,
            "sessions_updated": sweep["updated"],
            "warnings": sweep["warnings"],
        }

    def delete_task(
        self, task_id: int, *, delete_files: bool = False
    ) -> Dict[str, Any]:
        """Delete a project: remove its DB row, and optionally its files.

        By default only the database record is removed. FK cascades take
        care of heartbeats, sessions, task_updates, auto_executions and
        their logs (the schema declares ON DELETE CASCADE and
        PRAGMA foreign_keys is ON). The on-disk MissionCache directory is
        left in place unless ``delete_files=True``.

        Subtasks are refused: the ``tasks.parent_id`` FK is ON DELETE SET
        NULL, so deleting a parent would silently orphan its children.
        Delete or reassign them first.

        Args:
            task_id: the project row id.
            delete_files: also remove the on-disk directory
                (``MISSIONCACHE_ROOT / full_path``), guarded so it can
                never escape the root.

        Returns:
            Dict with keys: success, deleted, name, full_path,
            files_deleted, warnings.

        Raises:
            ValueError: no task with that id.
            SubtasksExistError: the project has subtasks.
            AutoRunActiveError: a missioncache-auto run is in progress.
        """
        task = self.get_task(task_id)
        if not task:
            raise ValueError(f"No project found with id {task_id}.")

        warnings: List[str] = []

        # Guard subtasks (the parent_id FK only NULLs on delete, which
        # would orphan them) and an active auto-run, then delete the row;
        # child rows cascade via FK. The guards run in the same connection
        # as the DELETE, which narrows but does not close the TOCTOU window:
        # sqlite3 opens a DEFERRED transaction, so the guard SELECTs take no
        # write lock and a concurrent process could commit a new subtask or
        # a running auto-run between the SELECT and the DELETE. Acceptable on
        # a single-user tool; a BEGIN IMMEDIATE would be needed to fully close it.
        with self.connection() as conn:
            children = conn.execute(
                "SELECT id, name, full_path FROM tasks WHERE parent_id = ?",
                (task_id,),
            ).fetchall()
            if children:
                # A flat fork child is a full project, not a nested subtask.
                # Give it an accurate message and a real remedy (the child's
                # own **Fork of:** header, removed then re-scanned, unlinks it)
                # rather than the legacy "delete or reassign subtasks" text
                # that names no existing reassign tool.
                forks = [c for c in children if self._is_flat_fork_path(c["full_path"])]
                if forks and len(forks) == len(children):
                    names = ", ".join(c["name"] for c in forks)
                    raise SubtasksExistError(
                        f"'{task.name}' still has {len(forks)} fork(s) that share "
                        f"its context: {names}. Complete or delete them first, or "
                        f"remove the '**Fork of:** {task.name}' line from each "
                        f"fork's context and re-scan to unlink."
                    )
                raise SubtasksExistError(
                    f"'{task.name}' has {len(children)} subtask(s). "
                    f"Delete or reassign them before deleting the project."
                )
            running = conn.execute(
                "SELECT id FROM auto_executions "
                "WHERE task_id = ? AND status = 'running' LIMIT 1",
                (task_id,),
            ).fetchone()
            if running:
                raise AutoRunActiveError(
                    "Cannot delete while missioncache-auto is running on this project. "
                    "Stop the run and try again."
                )
            conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            conn.commit()

        # Optional on-disk removal - best-effort, AFTER the row is gone, so
        # a failure here leaves an orphaned directory (warned) rather than a
        # DB row pointing at missing files. Resolve + containment check so a
        # corrupted full_path can never escape the MissionCache root.
        files_deleted = False
        if delete_files:
            import shutil

            target = MISSIONCACHE_ROOT / task.full_path
            try:
                resolved = target.resolve()
                root = MISSIONCACHE_ROOT.resolve()
                if resolved == root:
                    warnings.append("Refused to delete the MissionCache root itself.")
                elif not resolved.is_relative_to(root):
                    warnings.append(
                        f"Refused to delete files outside the MissionCache root: {target}"
                    )
                elif target.is_dir():
                    shutil.rmtree(target)
                    files_deleted = True
                # No directory (non-coding task, or already gone): nothing to do.
            except OSError as e:
                logger.exception("delete: file removal failed for %s", target)
                warnings.append(f"Could not delete files at {target}: {e}")

        return {
            "success": True,
            "deleted": True,
            "name": task.name,
            "full_path": task.full_path,
            "files_deleted": files_deleted,
            "warnings": warnings,
        }

    def _sweep_session_pointers(
        self, old_name: str, new_name: str
    ) -> Dict[str, Any]:
        """Best-effort rewrite of per-session state files that reference
        the renamed project by name.

        Updates (does not delete) so session ownership is preserved. Each
        sweep target is independently wrapped so a partial failure on one
        pointer doesn't block the others - this is post-DB-commit cleanup,
        not a transactional step. Failures are logged via the module
        logger AND surfaced in the returned ``warnings`` list so callers
        can show the user that some pointers may be stale.

        Returns a dict with keys ``updated`` (int count of pointers
        successfully rewritten) and ``warnings`` (list of human-readable
        strings describing each failure).
        """
        state_dir = Path.home() / ".claude" / "hooks" / "state"
        updated = 0
        warnings: List[str] = []

        # ``pending-task.json`` used to be swept here; removed when the file
        # itself stopped being written. The legacy file (if it still exists
        # from a pre-0.2.13 install) is no longer maintained or read by any
        # current code path. See docs/hooks.md.

        projects_dir = state_dir / "projects"
        if projects_dir.is_dir():
            for f in projects_dir.glob("*.json"):
                try:
                    data = json.loads(f.read_text())
                    if data.get("projectName") == old_name:
                        data["projectName"] = new_name
                        f.write_text(json.dumps(data))
                        updated += 1
                except (OSError, json.JSONDecodeError) as e:
                    logger.warning(
                        "session sweep: %s update failed: %s", f.name, e
                    )
                    warnings.append(
                        f"projects/{f.name} update failed ({type(e).__name__}); "
                        "that session may show stale name"
                    )

        if HOOKS_STATE_DB_PATH.exists():
            try:
                conn = sqlite3.connect(str(HOOKS_STATE_DB_PATH))
                cursor = conn.execute(
                    "UPDATE project_state SET project_name = ?, "
                    "updated_at = datetime('now', 'localtime') "
                    "WHERE project_name = ?",
                    (new_name, old_name),
                )
                if cursor.rowcount and cursor.rowcount > 0:
                    updated += cursor.rowcount
                conn.commit()
                conn.close()
            except sqlite3.Error as e:
                logger.warning(
                    "session sweep: hooks-state.db update failed: %s", e
                )
                warnings.append(
                    f"hooks-state.db update failed ({type(e).__name__}); "
                    "active sessions may show stale name"
                )

        return {"updated": updated, "warnings": warnings}

    def update_task_repo(self, task_id: int, repo_id: int) -> None:
        """Reassign a task to a different repository."""
        with self.connection() as conn:
            conn.execute(
                "UPDATE tasks SET repo_id = ? WHERE id = ?", (repo_id, task_id)
            )
            conn.commit()

    def find_task_for_cwd(
        self, cwd: Union[str, Path], session_id: Optional[str] = None
    ) -> Optional[Task]:
        """Find the active task that matches the current working directory.

        Only returns a task when explicitly working on one:
        1. Check pending-project.json for explicitly registered project (from /missioncache:load)
        2. Check per-session project file (written by statusline after consuming pending-project.json)
        3. Check if cwd is in dev/active/<task>/<subtask> directory

        Does NOT fall back to "most recent task in repo" - this prevents spurious
        updates to tasks when working in a repo on unrelated things.
        """
        cwd_path = Path(cwd).resolve()
        state_dir = Path.home() / ".claude" / "hooks" / "state"

        # Priority 1: Check pending-project.json for explicitly registered project
        pending_project_file = state_dir / "pending-project.json"
        if pending_project_file.exists():
            try:
                with open(pending_project_file) as f:
                    pending = json.load(f)
                pending_cwd = Path(pending.get("cwd", "")).resolve()
                pending_name = pending.get("projectName", "")

                # Check if pending task's cwd matches or is parent of current cwd
                if pending_name and (
                    cwd_path == pending_cwd
                    or str(cwd_path).startswith(str(pending_cwd) + os.sep)
                ):
                    # Find the task by name
                    # pending_name could be "task-name" or "parent/subtask"
                    task = self._find_task_by_registered_name(pending_name, cwd_path)
                    if task:
                        return task
            except (json.JSONDecodeError, IOError):
                pass  # Fall through to other methods

        # Priority 2: the per-session binding file (written on /missioncache:load
        # by the MCP server / session_start / the load command's bash fallback).
        # The binding is explicit - the user named the project - so cwd must
        # not veto it: a project is routinely worked from outside its
        # registered repo, and forks inherit the parent's repo_id.
        if session_id:
            _exists, binding = read_session_binding(session_id)
            if binding:
                task_id = binding.get("taskId")
                if isinstance(task_id, int):
                    # Durable identity: immune to name reuse and renames.
                    # A dead id (completed/deleted task) deliberately does
                    # NOT fall back to the name - a same-named unrelated
                    # project must not inherit this session's snapshots.
                    task = self.get_task(task_id)
                    if task and task.status in ("active", "paused"):
                        return task
                else:
                    # Legacy name-only binding (older writers).
                    task_name = binding.get("projectName", "")
                    if task_name:
                        task = self._find_task_by_registered_name(task_name, cwd_path)
                        if task:
                            return task
                        task = self._find_active_task_by_name(task_name)
                        if task:
                            return task

        # Priority 3: Check if cwd is under centralized MissionCache root.
        # Resolve the root the same way as cwd so a symlinked MISSIONCACHE_ROOT
        # (e.g. /tmp -> /private/tmp on macOS) still matches the resolved cwd.
        orbit_active = (MISSIONCACHE_ROOT / "active").resolve()
        try:
            relative = cwd_path.relative_to(orbit_active)
            parts = relative.parts
            if parts:
                task_name = parts[0]
                # Check for subtask
                if len(parts) >= 2:
                    full_path = f"active/{parts[0]}/{parts[1]}"
                    task = self.find_task_by_full_path(full_path)
                    if task:
                        return task
                # Try parent task
                full_path = f"active/{task_name}"
                task = self.find_task_by_full_path(full_path)
                if task:
                    return task
        except ValueError:
            pass  # cwd is not under MissionCache root

        # Legacy: check repo-local dev/active/ paths
        for repo in self.get_repos(active_only=True):
            repo_path = Path(repo.path)
            try:
                relative = cwd_path.relative_to(repo_path)
                parts = relative.parts

                if len(parts) >= 3 and parts[0] == "dev" and parts[1] == "active":
                    task_name = parts[2]

                    if len(parts) >= 4:
                        full_path = f"dev/active/{parts[2]}/{parts[3]}"
                        task = self.get_task_by_path(repo.id, full_path)
                        if task:
                            return task

                    full_path = f"dev/active/{task_name}"
                    task = self.get_task_by_path(repo.id, full_path)
                    if task:
                        return task

            except ValueError:
                continue

        return None

    def _find_task_by_registered_name(
        self, task_name: str, cwd_path: Path
    ) -> Optional[Task]:
        """Find a task by its registered name (from pending-project.json).

        Handles both standalone tasks ("task-name") and subtasks ("parent/subtask").
        Uses the most specific matching repo (longest path that contains cwd).
        """
        # Find the most specific repo (longest path that matches cwd)
        matching_repos = []
        for repo in self.get_repos(active_only=True):
            repo_path = Path(repo.path)
            try:
                cwd_path.relative_to(repo_path)
                matching_repos.append(repo)
            except ValueError:
                continue

        if not matching_repos:
            return None

        # Sort by path length descending (most specific first)
        matching_repos.sort(key=lambda r: len(r.path), reverse=True)

        for repo in matching_repos:
            # Handle parent/subtask format
            if "/" in task_name:
                parent_name, subtask_name = task_name.split("/", 1)
                # Find subtask by name under this parent
                with self.connection() as conn:
                    row = conn.execute(
                        """SELECT t.* FROM tasks t
                           JOIN tasks p ON t.parent_id = p.id
                           WHERE t.name = ? AND p.name = ? AND t.repo_id = ?
                           AND t.status IN ('active', 'paused')""",
                        (subtask_name, parent_name, repo.id),
                    ).fetchone()
                    if row:
                        return Task.from_row(row)

            # Try as standalone task name
            with self.connection() as conn:
                row = conn.execute(
                    """SELECT * FROM tasks
                       WHERE name = ? AND repo_id = ?
                       AND status IN ('active', 'paused')""",
                    (task_name, repo.id),
                ).fetchone()
                if row:
                    return Task.from_row(row)

        return None

    def _find_active_task_by_name(self, task_name: str) -> Optional[Task]:
        """Resolve an active task by name alone, ignoring repo and cwd.

        Fallback for LEGACY name-only session bindings (files written before
        write_session_binding stamped taskId): the binding is explicit - the
        user named the project via /missioncache:load - so cwd carries no
        signal and must not veto it. A project is routinely worked from
        outside its registered repo, and forks inherit the parent's repo_id.
        Without this fallback, every such session silently lost pre-compact
        snapshots AND heartbeats (verified 2026-07-16 on
        centra-e2e-segmentation-ai worked from ~/work).

        Returns None on an ambiguous name rather than guessing between repos.
        Bare names only: subtask rows store a bare name too, so this also
        resolves subtasks; a "parent/subtask" string simply misses, same as
        before the fallback existed.

        CAUTION - uniqueness is not identity: a single active match can
        still be a same-named STRANGER (the bound project completed, an
        unrelated project reused the name). That hole is closed properly by
        the taskId in current bindings; this name path only serves bindings
        from before taskId existed and inherits the residual risk.
        """
        with self.connection() as conn:
            rows = conn.execute(
                """SELECT * FROM tasks WHERE name = ?
                   AND status IN ('active', 'paused')""",
                (task_name,),
            ).fetchall()
        if len(rows) == 1:
            return Task.from_row(rows[0])
        if len(rows) > 1:
            # The one place that KNOWS the binding is ambiguous. Leave a
            # breadcrumb every consumer inherits (heartbeats, stop,
            # session_start) - stderr is invisible to the MCP stdio
            # protocol and hooks tolerate it, and the alternative is time
            # tracking silently vanishing with no trace anywhere.
            print(
                f"missioncache: session binding '{task_name}' is ambiguous "
                f"({len(rows)} active/paused tasks share the name) - "
                "refusing to guess, resolution skipped",
                file=sys.stderr,
            )
        return None

    # =========================================================================
    # Activity Tracking (Heartbeat System)
    # =========================================================================

    def record_heartbeat(
        self,
        task_id: int,
        session_id: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Record a heartbeat for activity tracking."""
        with self.connection() as conn:
            cursor = conn.execute(
                "INSERT INTO heartbeats (task_id, session_id, context) VALUES (?, ?, ?)",
                (task_id, session_id, json.dumps(context) if context else None),
            )

            # Update task's last_worked_on
            conn.execute(
                "UPDATE tasks SET last_worked_on = datetime('now', 'localtime') WHERE id = ?",
                (task_id,),
            )
            conn.commit()
            return cursor.lastrowid

    def record_heartbeat_auto(
        self,
        cwd: Union[str, Path],
        session_id: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Optional[int]:
        """Record a heartbeat, auto-detecting the task from cwd and session."""
        task = self.find_task_for_cwd(cwd, session_id)
        if task:
            hb_id = self.record_heartbeat(task.id, session_id, context)

            # Trigger shadow commit for non-git folders
            self._maybe_shadow_commit(cwd, task.id, session_id)

            return hb_id
        return None

    def _maybe_shadow_commit(
        self, cwd: Union[str, Path], task_id: int, session_id: Optional[str] = None
    ) -> None:
        """Trigger shadow commit if cwd is under the tracked non-git folder."""
        try:
            cwd_path = Path(cwd).resolve()
            tracked = SHADOW_TRACKED_FOLDER.resolve()

            # Only trigger for paths under the tracked folder
            if not (cwd_path == tracked or tracked in cwd_path.parents):
                return

            # Don't commit if it's actually a git repo
            if self._is_git_repo(cwd_path):
                return

            # Import here to avoid circular imports
            from shadow_repo import ShadowRepoManager

            mgr = ShadowRepoManager()
            result = mgr.sync_and_commit(
                str(tracked),  # Always commit from the root tracked folder
                task_id=task_id,
                session_id=session_id,
            )

            if result:
                # Log silently - don't spam output
                pass

        except Exception:
            # Shadow commits are best-effort, don't break heartbeat on failure
            pass

    def _is_git_repo(self, path: Union[str, Path]) -> bool:
        """Check if a path is inside a git repository."""
        path = Path(path)
        while path != path.parent:
            if (path / ".git").exists():
                return True
            path = path.parent
        return False

    def process_heartbeats(self) -> int:
        """Process unprocessed heartbeats into sessions."""
        idle_timeout = self.idle_timeout_seconds
        assumed_work = self.assumed_work_seconds
        processed_count = 0

        with self.connection() as conn:
            # Claim the write lock up-front so the read below happens inside
            # the write transaction. A concurrent process_heartbeats run then
            # blocks here on the WAL lock and, once it proceeds, its own read
            # sees these rows already marked processed - preventing the same
            # heartbeats being aggregated twice into duplicate sessions.
            conn.execute("BEGIN IMMEDIATE")

            # Get unprocessed heartbeats (skip orphaned task_ids)
            heartbeats = conn.execute(
                """SELECT h.* FROM heartbeats h
                   JOIN tasks t ON h.task_id = t.id
                   WHERE h.processed = 0
                   ORDER BY h.task_id, h.timestamp"""
            ).fetchall()

            # Mark orphaned heartbeats as processed
            conn.execute(
                """UPDATE heartbeats SET processed = 1
                   WHERE processed = 0
                   AND task_id NOT IN (SELECT id FROM tasks)"""
            )

            if not heartbeats:
                conn.commit()
                return 0

            current_task_id = None
            current_session_id = None
            last_heartbeat_time = None
            session_start_time = None

            for hb in heartbeats:
                hb_time = datetime.fromisoformat(hb["timestamp"])

                # Task changed - close any open session
                if hb["task_id"] != current_task_id:
                    if current_session_id and last_heartbeat_time:
                        self._close_session(
                            conn, current_session_id, last_heartbeat_time, assumed_work
                        )

                    current_task_id = hb["task_id"]
                    current_session_id = None
                    last_heartbeat_time = None
                    session_start_time = None

                # Check if we need a new session
                if current_session_id is None:
                    # Start new session
                    current_session_id = self._start_session(
                        conn, current_task_id, hb_time, hb["session_id"]
                    )
                    session_start_time = hb_time
                    last_heartbeat_time = hb_time
                else:
                    # Check gap since last heartbeat. Timestamps are naive
                    # local-time strings (intentionally kept - see the storage
                    # note in get_task_time); across a DST transition this
                    # wall-clock delta can misstate true elapsed seconds. A
                    # precise fix would require UTC-epoch storage; the twice-a-
                    # year edge is accepted rather than migrate the schema.
                    gap = (hb_time - last_heartbeat_time).total_seconds()

                    if gap > idle_timeout:
                        # Gap too large - close old session, start new one
                        self._close_session(
                            conn, current_session_id, last_heartbeat_time, assumed_work
                        )
                        current_session_id = self._start_session(
                            conn, current_task_id, hb_time, hb["session_id"]
                        )
                        session_start_time = hb_time
                    else:
                        # Continue session - add time
                        self._add_to_session(conn, current_session_id, gap)

                    last_heartbeat_time = hb_time

                # Mark heartbeat as processed
                conn.execute(
                    "UPDATE heartbeats SET processed = 1 WHERE id = ?", (hb["id"],)
                )
                processed_count += 1

            # Close the trailing open session. Only credit the assumed_work
            # tail when it is genuinely idle-terminated (last heartbeat older
            # than idle_timeout). If the last heartbeat is recent, work is
            # likely still ongoing and a later call aggregates its own batch;
            # adding a full tail on every call would inflate time (each
            # save/complete/pre-compact call re-padding the tail).
            # Tradeoff: a session processed while still warm that then truly
            # ends never gets its final assumed_work tail credited - an
            # accepted bounded under-count (the alternative was per-call
            # inflation).
            if current_session_id and last_heartbeat_time:
                idle_seconds = (datetime.now() - last_heartbeat_time).total_seconds()
                tail = assumed_work if idle_seconds > idle_timeout else 0
                self._close_session(
                    conn, current_session_id, last_heartbeat_time, tail
                )

            conn.commit()

        return processed_count

    def _start_session(
        self,
        conn: sqlite3.Connection,
        task_id: int,
        start_time: datetime,
        claude_session_id: Optional[str],
    ) -> int:
        """Start a new session."""
        cursor = conn.execute(
            """INSERT INTO sessions (task_id, session_id, start_time, heartbeat_count)
               VALUES (?, ?, ?, 1)""",
            (task_id, claude_session_id, start_time.isoformat()),
        )
        return cursor.lastrowid

    def _add_to_session(
        self, conn: sqlite3.Connection, session_id: int, duration: float
    ) -> None:
        """Add duration to an existing session."""
        conn.execute(
            """UPDATE sessions SET
               duration_seconds = duration_seconds + ?,
               heartbeat_count = heartbeat_count + 1
               WHERE id = ?""",
            (int(duration), session_id),
        )

    def _close_session(
        self,
        conn: sqlite3.Connection,
        session_id: int,
        last_time: datetime,
        assumed_work: int,
    ) -> None:
        """Close a session with end time."""
        end_time = last_time + timedelta(seconds=assumed_work)
        conn.execute(
            """UPDATE sessions SET
               end_time = ?,
               duration_seconds = duration_seconds + ?
               WHERE id = ?""",
            (end_time.isoformat(), assumed_work, session_id),
        )

    # =========================================================================
    # Time Queries
    # =========================================================================

    def get_task_time(self, task_id: int, period: str = "all") -> int:
        """Get total time spent on a task in seconds."""
        with self.connection() as conn:
            if period == "all":
                row = conn.execute(
                    "SELECT COALESCE(SUM(duration_seconds), 0) as total FROM sessions WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
            elif period == "week":
                row = conn.execute(
                    """SELECT COALESCE(SUM(duration_seconds), 0) as total FROM sessions
                       WHERE task_id = ? AND datetime(start_time) >= datetime('now', 'localtime', '-7 days')""",
                    (task_id,),
                ).fetchone()
            elif period == "today":
                # start_time is stored as a naive local-time ISO string (Python datetime.now()),
                # so date(start_time) already extracts the local calendar date. Applying 'localtime'
                # here would re-interpret the input as UTC and add the TZ offset, crossing midnight
                # for late-evening sessions in east-of-UTC zones (known timezone-window bug).
                # The 'localtime' modifier is still needed on 'now' because SQLite's now is UTC.
                row = conn.execute(
                    """SELECT COALESCE(SUM(duration_seconds), 0) as total FROM sessions
                       WHERE task_id = ? AND date(start_time) = date('now', 'localtime')""",
                    (task_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COALESCE(SUM(duration_seconds), 0) as total FROM sessions WHERE task_id = ?",
                    (task_id,),
                ).fetchone()

            return row["total"] if row else 0

    def get_subtask_time_total(self, parent_task_id: int) -> int:
        """Get total time spent on all subtasks of a parent task."""
        with self.connection() as conn:
            row = conn.execute(
                """SELECT COALESCE(SUM(s.duration_seconds), 0) as total
                   FROM sessions s
                   JOIN tasks t ON s.task_id = t.id
                   WHERE t.parent_id = ?""",
                (parent_task_id,),
            ).fetchone()
            return row["total"] if row else 0

    def get_task_session_count(self, task_id: int) -> int:
        """Get number of sessions for a task."""
        with self.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as count FROM sessions WHERE task_id = ?", (task_id,)
            ).fetchone()
            return row["count"] if row else 0

    def get_batch_task_times(
        self, task_ids: List[int], period: str = "all"
    ) -> Dict[int, int]:
        """Get time for multiple tasks in ONE query instead of N queries.

        Args:
            task_ids: List of task IDs to query
            period: "all", "today", or "week"

        Returns:
            Dict mapping task_id to seconds spent
        """
        if not task_ids:
            return {}

        with self.connection() as conn:
            placeholders = ",".join("?" * len(task_ids))

            if period == "today":
                # See get_task_time() above for why 'localtime' is dropped from date(start_time).
                query = f"""
                    SELECT task_id, COALESCE(SUM(duration_seconds), 0) as total
                    FROM sessions
                    WHERE task_id IN ({placeholders}) AND date(start_time) = date('now', 'localtime')
                    GROUP BY task_id
                """
            elif period == "week":
                query = f"""
                    SELECT task_id, COALESCE(SUM(duration_seconds), 0) as total
                    FROM sessions
                    WHERE task_id IN ({placeholders}) AND datetime(start_time) >= datetime('now', 'localtime', '-7 days')
                    GROUP BY task_id
                """
            else:  # all
                query = f"""
                    SELECT task_id, COALESCE(SUM(duration_seconds), 0) as total
                    FROM sessions
                    WHERE task_id IN ({placeholders})
                    GROUP BY task_id
                """

            rows = conn.execute(query, task_ids).fetchall()
            result = {row["task_id"]: row["total"] for row in rows}

            # Fill in zeros for tasks with no sessions
            for task_id in task_ids:
                if task_id not in result:
                    result[task_id] = 0

            return result

    def get_tasks_by_ids(self, task_ids: List[int]) -> List["Task"]:
        """Get multiple tasks in ONE query instead of N queries.

        Args:
            task_ids: List of task IDs to fetch

        Returns:
            List of Task objects (order not guaranteed)
        """
        if not task_ids:
            return []

        with self.connection() as conn:
            placeholders = ",".join("?" * len(task_ids))
            rows = conn.execute(
                f"SELECT * FROM tasks WHERE id IN ({placeholders})", task_ids
            ).fetchall()
            return [Task.from_row(dict(r)) for r in rows]

    def get_current_session_time(self, task_id: Optional[int] = None) -> int:
        """Get working time for current uninterrupted session (WakaTime-style).

        Calculates time from recent heartbeats, accounting for idle gaps.
        Returns seconds of active working time in the current session.
        """
        idle_timeout = self.idle_timeout_seconds

        with self.connection() as conn:
            # Query recent heartbeats (last 8 hours) regardless of processed flag
            # The gap detection algorithm will find the current active session
            cutoff = (datetime.now() - timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")

            if task_id:
                heartbeats = conn.execute(
                    """SELECT timestamp FROM heartbeats
                       WHERE task_id = ? AND timestamp > ?
                       ORDER BY timestamp ASC""",
                    (task_id, cutoff),
                ).fetchall()
            else:
                heartbeats = conn.execute(
                    """SELECT timestamp FROM heartbeats
                       WHERE timestamp > ?
                       ORDER BY timestamp ASC""",
                    (cutoff,),
                ).fetchall()

            if not heartbeats:
                return 0

            # Calculate working time with WakaTime algorithm
            # Gaps > idle_timeout reset the session
            session_seconds = 0
            last_time = None

            for hb in heartbeats:
                hb_time = datetime.fromisoformat(hb["timestamp"])
                if last_time:
                    # Naive local-time delta (see process_heartbeats): accepted
                    # to misstate elapsed time only across a DST transition.
                    gap = (hb_time - last_time).total_seconds()
                    if gap > idle_timeout:
                        # Idle gap - reset current session, keep only recent
                        session_seconds = 0
                    else:
                        session_seconds += gap
                last_time = hb_time

            # Add assumed work time (2 min) for the last heartbeat to now
            # Only if we're within idle timeout
            if last_time:
                now = datetime.now()
                gap_to_now = (now - last_time).total_seconds()
                if gap_to_now <= idle_timeout:
                    # Still active - add assumed work (capped at actual gap)
                    assumed_work = min(120, gap_to_now)
                    session_seconds += assumed_work

            return int(session_seconds)

    @staticmethod
    def format_duration(seconds: int) -> str:
        """Format seconds as human-readable duration."""
        if seconds < 60:
            return f"{seconds}s"
        elif seconds < 3600:
            return f"{seconds // 60}m"
        else:
            hours = seconds // 3600
            minutes = (seconds % 3600) // 60
            return f"{hours}h {minutes}m" if minutes else f"{hours}h"

    @staticmethod
    def format_time_ago(timestamp: Optional[str]) -> str:
        """Format timestamp as relative time ago.

        Assumes timestamps are in local time (matching SQLite's
        datetime('now', 'localtime')).
        """
        if not timestamp:
            return "never"

        try:
            dt = datetime.fromisoformat(timestamp)
            now = datetime.now()
            diff = now - dt

            if diff.days > 7:
                return dt.strftime("%b %d")
            elif diff.days > 0:
                return f"{diff.days}d ago"
            elif diff.seconds > 3600:
                return f"{diff.seconds // 3600}h ago"
            elif diff.seconds > 60:
                return f"{diff.seconds // 60}m ago"
            else:
                return "just now"
        except Exception:
            return "unknown"

    def get_effective_last_updated(self, task: "Task") -> Optional[str]:
        """Get effective last updated timestamp for a task.

        Uses the MORE RECENT of:
        1. Database last_worked_on (from heartbeats)
        2. File modification time of task files (context.md, tasks.md, etc.)

        This ensures that if files were edited (accurate mtime) but heartbeats
        were incorrectly assigned to another task, the file mtime is still used.

        Args:
            task: The Task object to get last updated time for

        Returns:
            ISO format timestamp string or None if no data available
        """
        db_timestamp = None
        file_timestamp = None

        # Get database heartbeat timestamp
        if task.last_worked_on:
            try:
                db_timestamp = datetime.fromisoformat(task.last_worked_on)
            except (ValueError, TypeError):
                pass

        # Get file modification time from centralized MissionCache root
        if task.full_path:
            task_dir = MISSIONCACHE_ROOT / task.full_path
            if task_dir.exists():
                task_name = task_dir.name
                candidate_files = [
                    task_dir / "context.md",
                    task_dir / "tasks.md",
                    task_dir / f"{task_name}-context.md",
                    task_dir / f"{task_name}-tasks.md",
                    task_dir / "README.md",
                    task_dir / "shared-context.md",
                ]

                latest_mtime = None
                for filepath in candidate_files:
                    if filepath.exists():
                        try:
                            mtime = filepath.stat().st_mtime
                            if latest_mtime is None or mtime > latest_mtime:
                                latest_mtime = mtime
                        except Exception:
                            continue

                if latest_mtime:
                    file_timestamp = datetime.fromtimestamp(latest_mtime)

        # Return the MORE RECENT of the two timestamps
        if db_timestamp and file_timestamp:
            effective = max(db_timestamp, file_timestamp)
        elif db_timestamp:
            effective = db_timestamp
        elif file_timestamp:
            effective = file_timestamp
        else:
            return None

        return effective.strftime("%Y-%m-%d %H:%M:%S")

    # =========================================================================
    # Claude Transcript Time Tracking
    # =========================================================================

    @staticmethod
    def encode_path_for_claude(path: str) -> str:
        """Encode a path to match Claude's project directory naming.

        Claude encodes paths by replacing '/' with '-' and '_' with '-'.
        Example: /home/user/project -> -home-user-project
        """
        return path.replace("/", "-").replace("_", "-")

    def get_session_time_from_transcripts(
        self, task_name: str, repo_path: str
    ) -> Dict[str, Any]:
        """Get session time by parsing Claude JSONL transcripts.

        Scans Claude's project directory for sessions that mention the task name.

        Args:
            task_name: Name of the task to search for
            repo_path: Absolute path to the repository

        Returns:
            Dict with time_total_seconds, session_count, last_session_timestamp
        """
        projects_dir = Path.home() / ".claude" / "projects"
        encoded_path = self.encode_path_for_claude(repo_path)
        project_dir = projects_dir / encoded_path

        if not project_dir.exists():
            return {
                "time_total_seconds": 0,
                "session_count": 0,
                "last_session_timestamp": None,
            }

        total_seconds = 0
        session_count = 0
        last_session_timestamp = None

        # Scan all JSONL files
        for jsonl_file in project_dir.glob("*.jsonl"):
            try:
                session_info = self._parse_session_for_task(jsonl_file, task_name)
                if session_info:
                    total_seconds += session_info["duration_seconds"]
                    session_count += 1
                    if session_info["end_timestamp"]:
                        if (
                            last_session_timestamp is None
                            or session_info["end_timestamp"] > last_session_timestamp
                        ):
                            last_session_timestamp = session_info["end_timestamp"]
            except Exception:
                continue  # Skip corrupted files

        return {
            "time_total_seconds": total_seconds,
            "session_count": session_count,
            "last_session_timestamp": last_session_timestamp,
        }

    def _parse_session_for_task(
        self, jsonl_path: Path, task_name: str
    ) -> Optional[Dict[str, Any]]:
        """Parse a JSONL session file to check if it mentions the task.

        Args:
            jsonl_path: Path to the JSONL file
            task_name: Task name to search for

        Returns:
            Dict with duration_seconds and end_timestamp if task is mentioned, None otherwise
        """
        first_timestamp = None
        last_timestamp = None
        task_mentioned = False

        # Read file and check for task mentions
        # Search for task name in the raw line (faster and catches paths like dev/active/task-name)
        with open(jsonl_path, "r") as f:
            for line in f:
                line_stripped = line.strip()
                if not line_stripped:
                    continue

                # Check for task mention in raw line (catches paths and all references)
                if not task_mentioned and task_name in line:
                    task_mentioned = True

                try:
                    entry = json.loads(line_stripped)
                except json.JSONDecodeError:
                    continue

                # Check for timestamp
                timestamp_str = entry.get("timestamp")
                if timestamp_str:
                    try:
                        timestamp = datetime.fromisoformat(
                            timestamp_str.replace("Z", "+00:00")
                        )
                        if first_timestamp is None:
                            first_timestamp = timestamp
                        last_timestamp = timestamp
                    except Exception:
                        pass

        if not task_mentioned or not first_timestamp or not last_timestamp:
            return None

        duration = (last_timestamp - first_timestamp).total_seconds()
        return {
            "duration_seconds": int(duration),
            "end_timestamp": last_timestamp.isoformat(),
        }

    # =========================================================================
    # Orbit Progress Parsing
    # =========================================================================

    def _parse_summary_field(self, content: str, field_name: str) -> str:
        """Parse a summary field like **Remaining:** or **Summary:** from task content.

        Args:
            content: Markdown content
            field_name: Field name to look for (e.g., "Remaining", "Summary")

        Returns:
            The field value or empty string if not found
        """
        # Match **Remaining:** value or **Summary:** value (single line)
        pattern = rf"\*\*{field_name}:\*\*\s*(.+?)(?:\n|$)"
        match = re.search(pattern, content, re.IGNORECASE)
        if match:
            return match.group(1).strip()
        return ""

    def parse_missioncache_progress(
        self, repo_path: str, task_full_path: str, parent_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """Parse MissionCache task file to extract progress information.

        Args:
            repo_path: Absolute path to the repository (legacy, used as fallback)
            task_full_path: Relative path like 'active/task-name' or legacy 'dev/active/task-name'
            parent_id: If set, this is a subtask

        Returns:
            Dict with progress info or {"has_docs": False} if not found
        """
        # Extract task name from path (last component)
        task_name = Path(task_full_path).name
        # Resolve via centralized MissionCache root (strip legacy dev/ prefix)
        normalized = (
            task_full_path[4:] if task_full_path.startswith("dev/") else task_full_path
        )
        task_dir = MISSIONCACHE_ROOT / normalized

        # Try multiple file patterns in order of priority
        tasks_file = None
        context_file = None
        is_parent_task = False

        # Pattern 1: Standalone task format ({task_name}-tasks.md)
        standalone_tasks = task_dir / f"{task_name}-tasks.md"
        standalone_context = task_dir / f"{task_name}-context.md"

        # Pattern 2: Subtask format (tasks.md, context.md)
        subtask_tasks = task_dir / "tasks.md"
        subtask_context = task_dir / "context.md"

        # Pattern 3: Parent task format (README.md or shared-context.md)
        parent_readme = task_dir / "README.md"
        parent_context = task_dir / "shared-context.md"

        # Pattern 4: Completed folder (flat format)
        completed_tasks = MISSIONCACHE_ROOT / "completed" / f"{task_name}-tasks.md"
        completed_context = MISSIONCACHE_ROOT / "completed" / f"{task_name}-context.md"

        # Pattern 5: Completed folder (subdirectory format)
        completed_subdir_tasks = (
            MISSIONCACHE_ROOT / "completed" / task_name / f"{task_name}-tasks.md"
        )
        completed_subdir_context = (
            MISSIONCACHE_ROOT / "completed" / task_name / f"{task_name}-context.md"
        )

        if standalone_tasks.exists():
            tasks_file = standalone_tasks
            context_file = standalone_context if standalone_context.exists() else None
        elif subtask_tasks.exists():
            tasks_file = subtask_tasks
            context_file = subtask_context if subtask_context.exists() else None
        elif parent_readme.exists() or parent_context.exists():
            # Parent task - use README.md or shared-context.md
            is_parent_task = True
            tasks_file = parent_readme if parent_readme.exists() else parent_context
            context_file = parent_context if parent_context.exists() else parent_readme
        elif completed_tasks.exists():
            tasks_file = completed_tasks
            context_file = completed_context if completed_context.exists() else None
        elif completed_subdir_tasks.exists():
            tasks_file = completed_subdir_tasks
            context_file = (
                completed_subdir_context if completed_subdir_context.exists() else None
            )
        elif completed_subdir_context.exists():
            # Completed task with only context file (no tasks file)
            tasks_file = completed_subdir_context
            context_file = completed_subdir_context

        if not tasks_file:
            return {"has_docs": False}

        try:
            content = tasks_file.read_text()
        except Exception:
            return {"has_docs": False}

        # Parse status
        status_match = re.search(r"\*\*Status:\*\*\s*(.+)", content)
        status = status_match.group(1).strip() if status_match else "Unknown"

        # Parse started date
        started_match = re.search(r"\*\*Started:\*\*\s*(\d{4}-\d{2}-\d{2})", content)
        started = started_match.group(1) if started_match else None

        # Parse last updated
        updated_match = re.search(
            r"\*\*Last Updated:\*\*\s*(\d{4}-\d{2}-\d{2})", content
        )
        last_updated = updated_match.group(1) if updated_match else None

        # Count checkboxes
        completed_items = len(re.findall(r"- \[x\]", content, re.IGNORECASE))
        pending_items = len(re.findall(r"- \[ \]", content))
        total_items = completed_items + pending_items

        # Calculate completion percentage
        completion_pct = (
            int((completed_items / total_items * 100)) if total_items > 0 else 0
        )

        # Find phases and current phase
        phase_pattern = r"## (Phase \d+[:\s]+[^\n]+)"
        phases = re.findall(phase_pattern, content)

        # Find current phase (first phase with unchecked items after it)
        current_phase = None
        phases_remaining = 0

        # Split content by phases
        phase_sections = re.split(r"## Phase \d+", content)
        for i, section in enumerate(
            phase_sections[1:], 1
        ):  # Skip content before first phase
            if "- [ ]" in section:
                if current_phase is None and i <= len(phases):
                    current_phase = phases[i - 1]
                phases_remaining += 1

        # Parse **Remaining:** field from task file (written by Claude via /missioncache:save)
        remaining_summary = self._parse_summary_field(content, "Remaining")
        if not remaining_summary:
            # Fallback to simple progress indicator
            if completion_pct == 100:
                remaining_summary = f"✓ Complete ({total_items} tasks)"
            elif completion_pct == 0:
                remaining_summary = f"Not started ({total_items} tasks)"
            else:
                remaining_summary = f"{pending_items} of {total_items} tasks remaining"

        # Extract task description from context file
        description = ""
        if context_file and context_file.exists():
            try:
                ctx_content = context_file.read_text()

                # Helper to check if a line is metadata or navigation
                def is_metadata(line: str) -> bool:
                    line = line.strip()
                    if not line:
                        return True
                    if (
                        line.startswith("**") and ":" in line[:20]
                    ):  # Bold metadata like **Status:**
                        return True
                    if line.startswith("|"):  # Table row
                        return True
                    if line.startswith(">"):  # Blockquote (often navigation)
                        return True
                    if re.match(r"^\[.*\]\(.*\)$", line):  # Standalone link
                        return True
                    if "shared-context" in line.lower():  # Navigation to shared context
                        return True
                    return False

                # Pattern 1: Look for dedicated "## Description" section first (highest priority)
                desc_match = re.search(
                    r"##\s*Description\s*\n+((?:[^\n#]+\n?)+)",
                    ctx_content,
                    re.IGNORECASE,
                )
                if desc_match:
                    lines = desc_match.group(1).strip().split("\n")
                    # Filter out metadata, bullets, and numbered lists - we want prose
                    prose_lines = []
                    for line in lines:
                        line = line.strip()
                        if not line:
                            continue
                        if is_metadata(line):
                            continue
                        # Skip bullets and numbered lists
                        if re.match(r"^[-*•]\s", line) or re.match(r"^\d+\.\s", line):
                            continue
                        # Skip lines that look like code or technical (all caps, backticks)
                        if "`" in line or line.isupper():
                            continue
                        prose_lines.append(line)
                    if prose_lines:
                        description = " ".join(prose_lines[:2])

                # Pattern 2: Look for other descriptive sections
                if not description:
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
                            content_lines = [l for l in lines if not is_metadata(l)]
                            if content_lines:
                                description = " ".join(content_lines[:2])
                                break

                # Pattern 2: First non-metadata paragraph after any heading
                if not description:
                    paragraphs = re.split(r"\n\n+", ctx_content)
                    for para in paragraphs:
                        para = para.strip()
                        # Skip headings and metadata
                        if para.startswith("#") or is_metadata(para):
                            continue
                        # Skip if it's a section of metadata lines
                        lines = para.split("\n")
                        content_lines = [l for l in lines if not is_metadata(l)]
                        if content_lines:
                            description = " ".join(content_lines[:2])
                            break

                # Clean up description (let dashboard handle display truncation)
                description = re.sub(r"\s+", " ", description).strip()
                # Keep full single sentence (up to ~100 chars for flexibility)
                if len(description) > 100:
                    description = description[:97] + "..."
            except Exception:
                pass

        # For parent tasks without parsed phases, try to count subtasks
        if is_parent_task and total_items == 0:
            # Count subdirectories as subtasks
            subtask_dirs = [
                d
                for d in task_dir.iterdir()
                if d.is_dir() and not d.name.startswith(".")
            ]
            if subtask_dirs:
                total_items = len(subtask_dirs)
                remaining_summary = f"Parent task with {total_items} subtasks"

        # Parse **Summary:** field from task file (written by Claude via /missioncache:save)
        completed_summary = self._parse_summary_field(content, "Summary")
        if not completed_summary:
            # Fallback to simple completion indicator
            if total_items > 0:
                completed_summary = f"Completed {total_items} tasks"
            else:
                completed_summary = "Task completed"

        return {
            "has_docs": True,
            "status": status,
            "started": started,
            "last_updated": last_updated,
            "completion_pct": completion_pct,
            "completed_count": completed_items,
            "total_count": total_items,
            "current_phase": current_phase,
            "remaining_summary": remaining_summary,
            "completed_summary": completed_summary,
            "phases_remaining": phases_remaining,
            "phases_total": len(phases),
            "description": description,
            "is_parent_task": is_parent_task,
        }

    # =========================================================================
    # Pruning
    # =========================================================================

    def prune_completed_tasks(self, retention_days: Optional[int] = None) -> int:
        """Archive completed tasks older than retention period."""
        days = retention_days or self.prune_after_days

        with self.connection() as conn:
            cursor = conn.execute(
                """UPDATE tasks SET status = 'archived', archived_at = datetime('now', 'localtime')
                   WHERE status = 'completed'
                   AND completed_at IS NOT NULL
                   AND julianday('now') - julianday(completed_at) > ?""",
                (days,),
            )
            conn.commit()
            return cursor.rowcount

    # =========================================================================
    # Auto Execution Management
    # =========================================================================

    def create_auto_execution(
        self,
        task_id: int,
        mode: str = "parallel",
        worker_count: Optional[int] = None,
        total_subtasks: int = 0,
    ) -> int:
        """Create a new auto execution run.

        Returns the execution ID.
        """
        with self.connection() as conn:
            cursor = conn.execute(
                """INSERT INTO auto_executions
                   (task_id, mode, worker_count, total_subtasks)
                   VALUES (?, ?, ?, ?)""",
                (task_id, mode, worker_count, total_subtasks),
            )
            conn.commit()
            return cursor.lastrowid

    def get_auto_execution(self, execution_id: int) -> Optional[AutoExecution]:
        """Get an auto execution by ID."""
        with self.connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM auto_executions WHERE id = ?",
                (execution_id,),
            )
            row = cursor.fetchone()
            return AutoExecution.from_row(row) if row else None

    def get_auto_executions_for_task(
        self, task_id: int, limit: int = 10
    ) -> List[AutoExecution]:
        """Get recent auto executions for a task."""
        with self.connection() as conn:
            cursor = conn.execute(
                """SELECT * FROM auto_executions
                   WHERE task_id = ?
                   ORDER BY started_at DESC
                   LIMIT ?""",
                (task_id, limit),
            )
            return [AutoExecution.from_row(row) for row in cursor.fetchall()]

    def get_running_auto_executions(self) -> List[AutoExecution]:
        """Get all currently running auto executions."""
        with self.connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM auto_executions WHERE status = 'running' ORDER BY started_at DESC"
            )
            return [AutoExecution.from_row(row) for row in cursor.fetchall()]

    def update_auto_execution(
        self,
        execution_id: int,
        status: Optional[str] = None,
        completed_subtasks: Optional[int] = None,
        failed_subtasks: Optional[int] = None,
        error_message: Optional[str] = None,
    ) -> bool:
        """Update an auto execution's status/progress."""
        updates = []
        values = []

        if status is not None:
            updates.append("status = ?")
            values.append(status)
            if status in ("completed", "failed", "cancelled"):
                updates.append("completed_at = datetime('now', 'localtime')")

        if completed_subtasks is not None:
            updates.append("completed_subtasks = ?")
            values.append(completed_subtasks)

        if failed_subtasks is not None:
            updates.append("failed_subtasks = ?")
            values.append(failed_subtasks)

        if error_message is not None:
            updates.append("error_message = ?")
            values.append(error_message)

        if not updates:
            return False

        values.append(execution_id)

        with self.connection() as conn:
            cursor = conn.execute(
                f"UPDATE auto_executions SET {', '.join(updates)} WHERE id = ?",
                values,
            )
            conn.commit()
            return cursor.rowcount > 0

    def add_auto_execution_log(
        self,
        execution_id: int,
        message: str,
        level: str = "info",
        worker_id: Optional[int] = None,
        subtask_id: Optional[str] = None,
    ) -> int:
        """Add a log entry to an auto execution.

        Returns the log entry ID.
        """
        with self.connection() as conn:
            cursor = conn.execute(
                """INSERT INTO auto_execution_logs
                   (execution_id, message, level, worker_id, subtask_id)
                   VALUES (?, ?, ?, ?, ?)""",
                (execution_id, message, level, worker_id, subtask_id),
            )
            conn.commit()
            return cursor.lastrowid

    def get_auto_execution_logs(
        self,
        execution_id: int,
        since_id: Optional[int] = None,
        limit: int = 1000,
        level: Optional[str] = None,
        worker_id: Optional[int] = None,
        subtask_id: Optional[str] = None,
    ) -> List[AutoExecutionLog]:
        """Get log entries for an auto execution.

        Args:
            execution_id: The execution to get logs for
            since_id: Only return logs with ID > this value (for streaming)
            limit: Maximum number of logs to return
            level: Filter by log level
            worker_id: Filter by worker
            subtask_id: Filter by subtask

        Returns list of log entries ordered by timestamp.
        """
        conditions = ["execution_id = ?"]
        values: List[Any] = [execution_id]

        if since_id is not None:
            conditions.append("id > ?")
            values.append(since_id)

        if level is not None:
            conditions.append("level = ?")
            values.append(level)

        if worker_id is not None:
            conditions.append("worker_id = ?")
            values.append(worker_id)

        if subtask_id is not None:
            conditions.append("subtask_id = ?")
            values.append(subtask_id)

        values.append(limit)

        with self.connection() as conn:
            cursor = conn.execute(
                f"""SELECT * FROM auto_execution_logs
                    WHERE {" AND ".join(conditions)}
                    ORDER BY timestamp ASC, id ASC
                    LIMIT ?""",
                values,
            )
            return [AutoExecutionLog.from_row(row) for row in cursor.fetchall()]

    def delete_auto_execution_logs(
        self, execution_id: int, older_than_days: int = 7
    ) -> int:
        """Delete old log entries for cleanup.

        Returns count of deleted entries.
        """
        with self.connection() as conn:
            cursor = conn.execute(
                """DELETE FROM auto_execution_logs
                   WHERE execution_id = ?
                   AND julianday('now') - julianday(timestamp) > ?""",
                (execution_id, older_than_days),
            )
            conn.commit()
            return cursor.rowcount

    def cleanup_old_auto_executions(
        self,
        keep_per_task: int = 10,
        older_than_days: int = 30,
    ) -> dict:
        """Clean up old auto executions and their logs.

        This method implements a retention policy:
        1. Keep at most `keep_per_task` executions per task
        2. Delete executions older than `older_than_days` days
        3. Cascade deletes logs via foreign key constraint

        Returns dict with counts of deleted executions and logs.
        """
        with self.connection() as conn:
            # First, find executions to delete based on age
            old_executions = conn.execute(
                """SELECT id FROM auto_executions
                   WHERE julianday('now') - julianday(started_at) > ?
                   AND status != 'running'""",
                (older_than_days,),
            ).fetchall()
            old_ids = {row[0] for row in old_executions}

            # Find executions to delete based on per-task limit
            # Keep the most recent N per task
            excess_executions = conn.execute(
                """SELECT id FROM auto_executions
                   WHERE id NOT IN (
                       SELECT id FROM (
                           SELECT id, task_id,
                                  ROW_NUMBER() OVER (
                                      PARTITION BY task_id
                                      ORDER BY started_at DESC
                                  ) as rn
                           FROM auto_executions
                       ) WHERE rn <= ?
                   )
                   AND status != 'running'""",
                (keep_per_task,),
            ).fetchall()
            excess_ids = {row[0] for row in excess_executions}

            # Combine IDs to delete
            ids_to_delete = old_ids | excess_ids
            if not ids_to_delete:
                return {"executions_deleted": 0, "logs_deleted": 0}

            # Count logs that will be deleted
            placeholders = ",".join("?" * len(ids_to_delete))
            logs_count = conn.execute(
                f"SELECT COUNT(*) FROM auto_execution_logs WHERE execution_id IN ({placeholders})",
                list(ids_to_delete),
            ).fetchone()[0]

            # Delete executions (logs cascade due to foreign key)
            conn.execute(
                f"DELETE FROM auto_executions WHERE id IN ({placeholders})",
                list(ids_to_delete),
            )
            conn.commit()

            return {
                "executions_deleted": len(ids_to_delete),
                "logs_deleted": logs_count,
            }


# =============================================================================
# Tree Rendering
# =============================================================================


def render_task_tree(db: TaskDB, hierarchy: Dict[str, Any]) -> List[str]:
    """Render hierarchical task list with tree connectors.

    Args:
        db: TaskDB instance for time lookups
        hierarchy: Output from get_active_tasks_hierarchical()

    Returns:
        List of formatted output lines
    """
    lines = []
    for task in hierarchy["top_level"]:
        repo = db.get_repo(task.repo_id)
        time_total = db.get_task_time(task.id, "all")
        child_tasks = hierarchy["children"].get(task.id, [])

        # Build time string with subtask aggregate
        time_str = db.format_duration(time_total)
        if child_tasks:
            subtask_time = db.get_subtask_time_total(task.id)
            if subtask_time > 0:
                time_str = f"{time_str} (subtasks: {db.format_duration(subtask_time)})"

        repo_name = repo.short_name if repo else "?"
        lines.append(
            f"[{task.id}] {task.name} [{repo_name}] - {time_str} - "
            f"{db.format_time_ago(db.get_effective_last_updated(task))}"
        )

        # Render children with tree connectors
        for i, child in enumerate(child_tasks):
            connector = "└──" if i == len(child_tasks) - 1 else "├──"
            child_time = db.get_task_time(child.id, "all")
            lines.append(
                f"    {connector} [{child.id}] {child.name} - "
                f"{db.format_duration(child_time)} - "
                f"{db.format_time_ago(db.get_effective_last_updated(child))}"
            )

    return lines


# =============================================================================
# CLI Interface
# =============================================================================


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1]
    db = TaskDB()

    try:
        if command == "init":
            db.initialize()
            print(f"Database initialized at {db.db_path}")

        elif command == "add-repo":
            if len(sys.argv) < 3:
                print("Usage: missioncache_db.py add-repo <path> [short_name]")
                sys.exit(1)
            path = sys.argv[2]
            short_name = sys.argv[3] if len(sys.argv) > 3 else None
            repo_id = db.add_repo(path, short_name)
            print(f"Added repo {path} with id {repo_id}")

        elif command == "add-repos-glob":
            if len(sys.argv) < 3:
                print("Usage: missioncache_db.py add-repos-glob <pattern>")
                sys.exit(1)
            pattern = sys.argv[2]
            repo_ids = db.add_repos_from_glob(pattern)
            print(f"Added {len(repo_ids)} repos from pattern {pattern}")

        elif command == "scan":
            repo_id = int(sys.argv[2]) if len(sys.argv) > 2 else None
            if repo_id:
                tasks = db.scan_repo(repo_id)
                print(f"Discovered {len(tasks)} tasks in repo {repo_id}")
            else:
                tasks = db.scan_all_repos()
                print(f"Discovered {len(tasks)} tasks across all repos")

        elif command == "list-repos":
            repos = db.get_repos()
            for repo in repos:
                print(f"[{repo.id}] {repo.short_name}: {repo.path}")

        elif command == "list-active":
            flat_mode = "--flat" in sys.argv

            if flat_mode:
                # Original flat output for backward compatibility
                tasks = db.get_active_tasks()
                for task in tasks:
                    repo = db.get_repo(task.repo_id)
                    time_total = db.get_task_time(task.id, "all")
                    print(
                        f"[{task.id}] {task.name} [{repo.short_name if repo else '?'}] - {db.format_duration(time_total)} - {db.format_time_ago(db.get_effective_last_updated(task))}"
                    )
            else:
                # New hierarchical output
                hierarchy = db.get_active_tasks_hierarchical()
                for line in render_task_tree(db, hierarchy):
                    print(line)

        elif command == "heartbeat":
            if len(sys.argv) < 3:
                print("Usage: missioncache_db.py heartbeat <task_id> [session_id]")
                sys.exit(1)
            task_id = int(sys.argv[2])
            session_id = sys.argv[3] if len(sys.argv) > 3 else None
            hb_id = db.record_heartbeat(task_id, session_id)
            print(f"Recorded heartbeat {hb_id}")

        elif command == "heartbeat-auto":
            cwd = os.getcwd()
            session_id = os.environ.get("CLAUDE_SESSION_ID")
            hb_id = db.record_heartbeat_auto(cwd, session_id)
            if hb_id:
                print(f"Recorded heartbeat {hb_id}")
            else:
                print("No task found for current directory")

        elif command == "process-heartbeats":
            count = db.process_heartbeats()
            print(f"Processed {count} heartbeats")

        elif command == "task-time":
            if len(sys.argv) < 3:
                print("Usage: missioncache_db.py task-time <task_id> [period]")
                sys.exit(1)
            task_id = int(sys.argv[2])
            period = sys.argv[3] if len(sys.argv) > 3 else "all"
            seconds = db.get_task_time(task_id, period)
            print(db.format_duration(seconds))

        elif command == "current-session":
            # Get current session working time (WakaTime-style)
            # Optional task_id, otherwise calculates from all unprocessed heartbeats
            task_id = int(sys.argv[2]) if len(sys.argv) > 2 else None
            seconds = db.get_current_session_time(task_id)
            print(db.format_duration(seconds))

        elif command == "prune":
            days = int(sys.argv[2]) if len(sys.argv) > 2 else None
            count = db.prune_completed_tasks(days)
            print(f"Archived {count} completed tasks")

        elif command == "get-task":
            if len(sys.argv) < 3:
                print("Usage: missioncache_db.py get-task <task_id>")
                sys.exit(1)
            task_id = int(sys.argv[2])
            task = db.get_task(task_id)
            if task:
                repo = db.get_repo(task.repo_id)
                output = {
                    "id": task.id,
                    "name": task.name,
                    "full_path": task.full_path,
                    "parent_id": task.parent_id,
                    "repo_id": task.repo_id,
                    "repo_path": repo.path if repo else None,
                    "repo_name": repo.short_name if repo else None,
                    "status": task.status,
                    "jira_key": task.jira_key,
                    "branch": task.branch,
                    "pr_url": task.pr_url,
                    "last_worked_on": task.last_worked_on,
                }
                # If this is a subtask, also get parent info
                if task.parent_id:
                    parent = db.get_task(task.parent_id)
                    if parent:
                        output["parent_name"] = parent.name
                        output["parent_full_path"] = parent.full_path
                print(json.dumps(output, indent=2))
            else:
                print(json.dumps({"error": f"Task {task_id} not found"}))
                sys.exit(1)

        elif command == "complete-task":
            if len(sys.argv) < 3:
                print("Usage: missioncache_db.py complete-task <task_id>")
                sys.exit(1)
            task_id = int(sys.argv[2])
            task = db.get_task(task_id)
            if not task:
                print(json.dumps({"error": f"Task {task_id} not found"}))
                sys.exit(1)

            # Process any pending heartbeats first
            db.process_heartbeats()

            # Get final time stats before marking complete
            total_time = db.get_task_time(task_id, "all")
            session_count = db.get_task_session_count(task_id)

            # Update status to completed
            updated_task = db.update_task_status(task_id, "completed")
            repo = db.get_repo(task.repo_id)

            output = {
                "id": task_id,
                "name": task.name,
                "full_path": task.full_path,
                "repo_path": repo.path if repo else None,
                "repo_name": repo.short_name if repo else None,
                "status": "completed",
                "total_time_seconds": total_time,
                "total_time_formatted": db.format_duration(total_time),
                "session_count": session_count,
            }
            print(json.dumps(output, indent=2))

        elif command == "create-task":
            # Parse arguments
            task_type = "coding"
            name = None
            jira_key = None

            category = None

            i = 2
            while i < len(sys.argv):
                if sys.argv[i] == "--type" and i + 1 < len(sys.argv):
                    task_type = sys.argv[i + 1]
                    i += 2
                elif sys.argv[i] == "--name" and i + 1 < len(sys.argv):
                    name = sys.argv[i + 1]
                    i += 2
                elif sys.argv[i] == "--jira" and i + 1 < len(sys.argv):
                    jira_key = sys.argv[i + 1]
                    i += 2
                elif sys.argv[i] == "--category" and i + 1 < len(sys.argv):
                    category = sys.argv[i + 1]
                    i += 2
                elif not name:
                    # First positional arg is the name
                    name = sys.argv[i]
                    i += 1
                else:
                    i += 1

            if not name:
                print(
                    "Usage: missioncache_db.py create-task [--type coding|non-coding] [--jira TICKET] [--category CAT] <name>"
                )
                print(
                    "       missioncache_db.py create-task --type non-coding --name 'Sprint planning'"
                )
                sys.exit(1)

            try:
                task = db.create_task(
                    name, task_type=task_type, jira_key=jira_key, category=category
                )
            except ValueError as e:
                print(str(e))
                sys.exit(1)
            output = {
                "id": task.id,
                "name": task.name,
                "type": task.task_type,
                "tags": task.tags,
                "jira_key": task.jira_key,
                "category": task.category,
                "status": task.status,
            }
            print(json.dumps(output, indent=2))

        elif command == "add-update":
            if len(sys.argv) < 4:
                print("Usage: missioncache_db.py add-update <task_id> <note>")
                sys.exit(1)
            task_id = int(sys.argv[2])
            note = " ".join(sys.argv[3:])  # Join remaining args as note
            update_id = db.add_task_update(task_id, note)
            task = db.get_task(task_id)
            print(
                json.dumps(
                    {
                        "update_id": update_id,
                        "task_id": task_id,
                        "task_name": task.name if task else None,
                        "note": note,
                    },
                    indent=2,
                )
            )

        elif command == "get-updates":
            if len(sys.argv) < 3:
                print("Usage: missioncache_db.py get-updates <task_id> [limit]")
                sys.exit(1)
            task_id = int(sys.argv[2])
            limit = int(sys.argv[3]) if len(sys.argv) > 3 else 10
            updates = db.get_task_updates(task_id, limit)
            task = db.get_task(task_id)
            print(
                json.dumps(
                    {
                        "task_id": task_id,
                        "task_name": task.name if task else None,
                        "updates": updates,
                    },
                    indent=2,
                )
            )

        elif command == "today-updates":
            task_id = int(sys.argv[2]) if len(sys.argv) > 2 else None
            updates = db.get_today_updates(task_id)
            print(json.dumps({"updates": updates}, indent=2))

        elif command == "set-jira":
            if len(sys.argv) < 4:
                print("Usage: missioncache_db.py set-jira <task_id> <jira_key>")
                sys.exit(1)
            task_id = int(sys.argv[2])
            jira_key = sys.argv[3]
            with db.connection() as conn:
                conn.execute(
                    "UPDATE tasks SET jira_key = ? WHERE id = ?", (jira_key, task_id)
                )
                conn.commit()
            task = db.get_task(task_id)
            print(
                json.dumps(
                    {
                        "id": task_id,
                        "name": task.name if task else None,
                        "jira_key": jira_key,
                        "message": f"Set JIRA key to {jira_key}",
                    },
                    indent=2,
                )
            )

        elif command == "set-category":
            if len(sys.argv) < 4:
                print("Usage: missioncache_db.py set-category <task_id> <category|none>")
                print(f"Categories: {', '.join(CATEGORIES)}")
                customs = sorted(db.custom_category_names())
                if customs:
                    print(f"Custom categories: {', '.join(customs)}")
                sys.exit(1)
            category = None if sys.argv[3].lower() == "none" else sys.argv[3]
            try:
                task_id = int(sys.argv[2])
                task = db.set_task_category(task_id, category)
            except ValueError as e:
                print(str(e))
                sys.exit(1)
            print(
                json.dumps(
                    {
                        "id": task_id,
                        "name": task.name,
                        "category": task.category,
                        "message": (
                            f"Set category to {category}"
                            if category
                            else "Cleared category"
                        ),
                    },
                    indent=2,
                )
            )

        elif command == "add-keyword":
            if len(sys.argv) < 3:
                print("Usage: missioncache_db.py add-keyword <keyword>")
                sys.exit(1)
            keyword = sys.argv[2]
            if db.add_keyword(keyword):
                print(f"Added keyword: {keyword}")
            else:
                print(f"Keyword already exists: {keyword}")
                sys.exit(1)

        elif command == "remove-keyword":
            if len(sys.argv) < 3:
                print("Usage: missioncache_db.py remove-keyword <keyword>")
                sys.exit(1)
            keyword = sys.argv[2]
            if db.remove_keyword(keyword):
                print(f"Removed keyword: {keyword}")
            else:
                print(f"Keyword not found in custom list: {keyword}")
                sys.exit(1)

        elif command == "list-keywords":
            keywords = db.list_keywords()
            print(f"Default keywords ({len(keywords['default'])}):")
            print("  " + ", ".join(keywords["default"][:20]) + "...")
            print(f"\nCustom keywords ({len(keywords['custom'])}):")
            if keywords["custom"]:
                print("  " + ", ".join(keywords["custom"]))
            else:
                print("  (none)")

        elif command == "backfill-tags":
            # One-time operation to add tags to existing tasks
            count = 0
            with db.connection() as conn:
                tasks = conn.execute(
                    "SELECT id, name, tags FROM tasks WHERE tags = '[]'"
                ).fetchall()
                for task in tasks:
                    tags = extract_tags(task["name"])
                    if tags:
                        conn.execute(
                            "UPDATE tasks SET tags = ? WHERE id = ?",
                            (json.dumps(tags), task["id"]),
                        )
                        count += 1
                        print(f"  [{task['id']}] {task['name']} -> {tags}")
                conn.commit()
            print(f"\nBackfilled tags for {count} tasks")

        elif command == "list-completed":
            days = int(sys.argv[2]) if len(sys.argv) > 2 else 30
            tasks = db.get_recent_completed(days)
            if not tasks:
                print(f"No completed tasks in the last {days} days")
            else:
                for task in tasks:
                    repo = db.get_repo(task.repo_id)
                    completed_ago = db.format_time_ago(task.completed_at)
                    print(
                        f"[{task.id}] {task.name} [{repo.short_name if repo else '?'}] - completed {completed_ago}"
                    )

        elif command == "list-names":
            # Output task names only (for shell completion)
            status = sys.argv[2] if len(sys.argv) > 2 else "active"
            if status == "active":
                tasks = db.get_active_tasks()
            elif status == "completed":
                tasks = db.get_recent_completed(days=90)
            else:
                tasks = []
            for task in tasks:
                print(task.name)

        elif command == "reopen-task":
            if len(sys.argv) < 3:
                print("Usage: missioncache_db.py reopen-task <task_id>")
                sys.exit(1)
            task_id = int(sys.argv[2])
            task = db.get_task(task_id)
            if not task:
                print(json.dumps({"error": f"Task {task_id} not found"}))
                sys.exit(1)
            if task.status != "completed":
                print(
                    json.dumps(
                        {
                            "error": f"Task {task_id} is not completed (status: {task.status})"
                        }
                    )
                )
                sys.exit(1)

            # Get time stats before reopening
            total_time = db.get_task_time(task_id, "all")
            session_count = db.get_task_session_count(task_id)

            # Reopen the task
            updated_task = db.reopen_task(task_id)
            repo = db.get_repo(task.repo_id)

            output = {
                "id": task_id,
                "name": task.name,
                "full_path": task.full_path,
                "repo_path": repo.path if repo else None,
                "repo_name": repo.short_name if repo else None,
                "status": "active",
                "previous_time_seconds": total_time,
                "previous_time_formatted": db.format_duration(total_time),
                "session_count": session_count,
            }
            print(json.dumps(output, indent=2))

        elif command == "rename-task":
            # Usage: missioncache-db rename-task <old-name> <new-name>
            if len(sys.argv) < 4:
                print("Usage: missioncache-db rename-task <old-name> <new-name>")
                sys.exit(1)
            old_name = sys.argv[2]
            new_name_input = sys.argv[3]
            task = db.get_task_by_name(old_name)
            if not task:
                print(
                    json.dumps({"error": f"No project found with name '{old_name}'."})
                )
                sys.exit(1)
            try:
                result = db.rename_task(task.id, new_name_input)
            except (
                NameCollisionError,
                FilesystemCollisionError,
                AutoRunActiveError,
                ValueError,
            ) as e:
                print(json.dumps({"error": str(e)}))
                sys.exit(1)
            # Successful path: print canonical name and let the user know
            # if their input was normalized.
            if result["normalized"]:
                print(
                    f"Renamed: {result['old_name']} -> {result['name']} "
                    f"(normalized from '{new_name_input}')"
                )
            else:
                print(f"Renamed: {result['old_name']} -> {result['name']}")
            if result["h1_skipped"]:
                print(
                    "  Skipped H1 rewrite for (edited content): "
                    + ", ".join(result["h1_skipped"])
                )
            if result["sessions_updated"]:
                print(
                    f"  Updated {result['sessions_updated']} session pointer(s)."
                )

        elif command == "get-task-by-name":
            if len(sys.argv) < 3:
                print("Usage: missioncache_db.py get-task-by-name <name> [--status <status>]")
                sys.exit(1)
            name = sys.argv[2]
            status = None
            # Parse --status flag
            if "--status" in sys.argv:
                idx = sys.argv.index("--status")
                if idx + 1 < len(sys.argv):
                    status = sys.argv[idx + 1]

            task = db.get_task_by_name(name, status)
            if task:
                repo = db.get_repo(task.repo_id)
                output = {
                    "id": task.id,
                    "name": task.name,
                    "full_path": task.full_path,
                    "repo_id": task.repo_id,
                    "repo_path": repo.path if repo else None,
                    "repo_name": repo.short_name if repo else None,
                    "status": task.status,
                    "completed_at": task.completed_at,
                }
                print(json.dumps(output, indent=2))
            else:
                status_msg = f" with status '{status}'" if status else ""
                print(json.dumps({"error": f"Task '{name}'{status_msg} not found"}))
                sys.exit(1)

        elif command == "migrate-orbit-docs":
            import shutil

            dry_run = "--dry-run" in sys.argv
            orbit_active = MISSIONCACHE_ROOT / "active"
            orbit_completed = MISSIONCACHE_ROOT / "completed"

            if not dry_run:
                orbit_active.mkdir(parents=True, exist_ok=True)
                orbit_completed.mkdir(parents=True, exist_ok=True)

            moved = 0
            skipped = 0

            # Move files from repo-local dev/ to centralized MissionCache root
            for repo in db.get_repos():
                repo_path = Path(repo.path)
                for status, target_dir in [
                    ("active", orbit_active),
                    ("completed", orbit_completed),
                ]:
                    source_dir = repo_path / "dev" / status
                    if not source_dir.exists():
                        continue
                    for task_dir in source_dir.iterdir():
                        if not task_dir.is_dir() or task_dir.name.startswith("."):
                            continue
                        dest = target_dir / task_dir.name
                        if dest.exists():
                            print(f"  SKIP (exists): {task_dir} -> {dest}")
                            skipped += 1
                            continue
                        if dry_run:
                            print(f"  WOULD MOVE: {task_dir} -> {dest}")
                        else:
                            shutil.move(str(task_dir), str(dest))
                            print(f"  MOVED: {task_dir} -> {dest}")
                        moved += 1

            # Update DB full_path entries
            with db.connection() as conn:
                rows = conn.execute(
                    "SELECT id, full_path FROM tasks WHERE full_path LIKE 'dev/%'"
                ).fetchall()
                for row in rows:
                    old_path = row["full_path"]
                    new_path = old_path[4:]  # strip "dev/" prefix
                    if dry_run:
                        print(
                            f"  WOULD UPDATE DB: [{row['id']}] {old_path} -> {new_path}"
                        )
                    else:
                        conn.execute(
                            "UPDATE tasks SET full_path = ? WHERE id = ?",
                            (new_path, row["id"]),
                        )
                if not dry_run:
                    conn.commit()
                print(
                    f"\n{'Would update' if dry_run else 'Updated'} {len(rows)} DB entries"
                )

            prefix = "DRY RUN: Would move" if dry_run else "Moved"
            print(f"\n{prefix} {moved} task dirs ({skipped} skipped)")

        elif command == "cleanup":
            import shutil

            dry_run = "--dry-run" in sys.argv
            prefix = "DRY RUN: " if dry_run else ""

            # --- B1: Archive orphaned active tasks (no files, no/minimal work) ---
            print("=== B1: Archive orphaned active tasks ===")
            orphan_ids = []
            with db.connection() as conn:
                rows = conn.execute(
                    "SELECT id, name, full_path FROM tasks "
                    "WHERE status = 'active' AND type = 'coding'"
                ).fetchall()
                for row in rows:
                    # 'manual/*' tasks (created via create-task) never have an
                    # on-disk directory, so a missing dir is expected, not an
                    # orphan - skip them or every manual coding task is archived.
                    if row["full_path"].startswith("manual/"):
                        continue
                    task_dir = MISSIONCACHE_ROOT / row["full_path"]
                    has_files = (
                        task_dir.exists()
                        and task_dir.is_dir()
                        and any(task_dir.iterdir())
                    )
                    if not has_files:
                        # Leave duplicate-named tasks alone - don't archive a
                        # live sibling that shares this name.
                        dupes = conn.execute(
                            "SELECT COUNT(*) FROM tasks WHERE name = ?",
                            (row["name"],),
                        ).fetchone()[0]
                        if dupes > 1:
                            continue
                        orphan_ids.append(row["id"])
                        print(f"  {prefix}Archive ID={row['id']} name={row['name']}")

                if orphan_ids and not dry_run:
                    placeholders = ",".join("?" * len(orphan_ids))
                    conn.execute(
                        f"UPDATE tasks SET status = 'archived', "
                        f"archived_at = datetime('now') "
                        f"WHERE id IN ({placeholders})",
                        orphan_ids,
                    )
                    conn.commit()
            print(f"  {prefix}{len(orphan_ids)} tasks archived\n")

            # --- B2: Move orphaned repo-local files ---
            print("=== B2: Move orphaned repo-local files ===")
            dev_dir = MISSIONCACHE_ROOT
            files_moved = 0

            # statusline-layout-improvement files
            src_completed = dev_dir / "completed"
            if src_completed.exists():
                sl_files = list(src_completed.glob("statusline-layout-improvement-*"))
                if sl_files:
                    dest_dir = (
                        MISSIONCACHE_ROOT / "completed" / "statusline-layout-improvement"
                    )
                    if dry_run:
                        print(f"  {prefix}Move {len(sl_files)} files -> {dest_dir}")
                    else:
                        dest_dir.mkdir(parents=True, exist_ok=True)
                        for f in sl_files:
                            shutil.move(str(f), str(dest_dir / f.name))
                            print(f"  Moved {f.name}")
                    files_moved += len(sl_files)

            # Clean up .playwright-mcp artifacts
            pw_dir = dev_dir / "active" / ".playwright-mcp"
            if pw_dir.exists():
                if dry_run:
                    print(f"  {prefix}Remove {pw_dir}")
                else:
                    shutil.rmtree(pw_dir)
                    print(f"  Removed {pw_dir}")

            print(f"  {prefix}{files_moved} files moved\n")

            # --- B4: Normalize non-standard paths ---
            print("=== B4: Normalize non-standard paths ===")
            normalized = 0
            with db.connection() as conn:
                # manual/* completed coding tasks -> completed/*
                # Skip archived - they're dead duplicates and would
                # collide on UNIQUE(repo_id, full_path)
                rows = conn.execute(
                    "SELECT id, name, full_path, status FROM tasks "
                    "WHERE full_path LIKE 'manual/%' AND type = 'coding' "
                    "AND status = 'completed'"
                ).fetchall()
                for row in rows:
                    new_path = f"completed/{row['name']}"
                    print(f"  {prefix}ID={row['id']} {row['full_path']} -> {new_path}")
                    if not dry_run:
                        conn.execute(
                            "UPDATE tasks SET full_path = ? WHERE id = ?",
                            (new_path, row["id"]),
                        )
                    normalized += 1

                # active/* completed tasks -> completed/*
                # Skip subtasks (parent_id set) - preserve parent path
                # Skip archived - same UNIQUE constraint reason
                rows = conn.execute(
                    "SELECT id, name, full_path, status, parent_id FROM tasks "
                    "WHERE full_path LIKE 'active/%' "
                    "AND status = 'completed' "
                    "AND parent_id IS NULL"
                ).fetchall()
                for row in rows:
                    new_path = f"completed/{row['name']}"
                    print(f"  {prefix}ID={row['id']} {row['full_path']} -> {new_path}")
                    if not dry_run:
                        conn.execute(
                            "UPDATE tasks SET full_path = ? WHERE id = ?",
                            (new_path, row["id"]),
                        )
                    normalized += 1

                if not dry_run:
                    conn.commit()
            print(f"  {prefix}{normalized} paths normalized\n")

            # --- Summary ---
            print("=== Summary ===")
            print(f"  Orphans archived: {len(orphan_ids)}")
            print(f"  Paths normalized: {normalized}")
            print(f"  Files moved: {files_moved}")
            if dry_run:
                print("\n  Run without --dry-run to apply changes.")

        elif command == "health":
            from missioncache_db import context_health

            # Bare module-global lookup resolves at call time, so tests can
            # monkeypatch missioncache_db.MISSIONCACHE_ROOT directly.
            active_dir = MISSIONCACHE_ROOT / "active"
            project_dirs = (
                sorted(p for p in active_dir.iterdir() if p.is_dir())
                if active_dir.exists()
                else []
            )
            total_warnings = 0
            for project_dir in project_dirs:
                name = project_dir.name
                context_file = project_dir / f"{name}-context.md"
                if not context_file.exists():
                    context_file = project_dir / "context.md"
                if not context_file.exists():
                    print(f"{name}:")
                    print("  - no context file found")
                    total_warnings += 1
                    continue
                # Report-only contract: one unreadable/undecodable file is a
                # finding for THAT project, never a crash that voids the
                # sweep for every project after it.
                try:
                    content = context_file.read_text()
                except (OSError, UnicodeDecodeError) as e:
                    print(f"{name}:")
                    print(f"  - context file unreadable: {e.__class__.__name__}")
                    total_warnings += 1
                    continue
                warnings = context_health.check_context_health(content, context_file)
                if warnings:
                    print(f"{name}:")
                    for w in warnings:
                        print(f"  - {w}")
                    total_warnings += len(warnings)
                else:
                    print(f"{name}: ok")
            print(
                f"\n{len(project_dirs)} active projects checked, "
                f"{total_warnings} warnings"
            )

        elif command == "config":
            from missioncache_db import machine_map

            sub = sys.argv[2] if len(sys.argv) > 2 else ""

            if sub == "set-path":
                if len(sys.argv) < 5 or ":" not in sys.argv[3]:
                    print("Usage: missioncache-db config set-path <repo|vault|anchor>:<name> <localpath>")
                    sys.exit(1)
                kind, name = sys.argv[3].split(":", 1)  # first colon only (remotes contain colons)
                if kind not in machine_map.SECTION:
                    print(f"Unknown kind '{kind}' (expected repo|vault|anchor)")
                    sys.exit(1)
                localpath = str(Path(sys.argv[4]).expanduser().resolve())
                key = machine_map.record(kind, name, localpath)
                print(f"Mapped {kind}:{key} -> {localpath}")
                if not Path(localpath).exists():
                    print(f"  warning: {localpath} does not exist yet (stored anyway)", file=sys.stderr)

            elif sub == "list-paths":
                mapping = machine_map.all_mappings()
                if "--json" in sys.argv:
                    print(json.dumps(mapping, indent=2, sort_keys=True))
                else:
                    kind_filter = next((a for a in sys.argv[3:] if not a.startswith("-")), None)
                    sections = (
                        [machine_map.SECTION[kind_filter]]
                        if kind_filter in machine_map.SECTION
                        else ["repos", "vaults", "anchors"]
                    )
                    for sec in sections:
                        print(f"[{sec}]")
                        for k, v in sorted(mapping.get(sec, {}).items()):
                            print(f"  {k} -> {v}")

            elif sub == "show":
                print(json.dumps(machine_map.all_mappings(), indent=2, sort_keys=True))

            elif sub == "seed":
                dry_run = "--dry-run" in sys.argv
                result = machine_map.seed(db, dry_run=dry_run)
                for kind, key, path in result["added"]:
                    print(f"  added {kind}:{key} -> {path}")
                for path, reason in result["skipped"]:
                    print(f"  skipped {path} ({reason})")
                for kind, name, path in result["proposed"]:
                    print(f"  proposed {kind}:{name} -> {path}")
                    print(f"    run: missioncache-db config set-path {kind}:{name} {path}")
                if dry_run:
                    print("\n  Run without --dry-run to write machine.json.")

            else:
                print("Usage: missioncache-db config <set-path|list-paths|show|seed>")
                sys.exit(1)

        elif command == "export":
            from missioncache_db import portability

            args = sys.argv[2:]
            if not args or args[0].startswith("-"):
                print("Usage: missioncache-db export <name> [--out <path>] [--no-time] [--json]")
                sys.exit(1)
            name = args[0]
            out = None
            i = 1
            while i < len(args):  # value-flag index walk (create-task precedent)
                if args[i] == "--out" and i + 1 < len(args):
                    out = args[i + 1]
                    i += 2
                    continue
                i += 1
            include_time = "--no-time" not in args
            as_json = "--json" in args
            try:
                report = portability.export_project(
                    db, name, out=out, include_time=include_time
                )
            except (ValueError, OSError) as e:
                print(f"export failed: {e}", file=sys.stderr)
                sys.exit(1)
            for w in report["warnings"]:
                print(f"warning: {w}", file=sys.stderr)
            if as_json:
                print(json.dumps(report["manifest"], indent=2, sort_keys=True))
            else:
                m = report["manifest"]
                refs = m["references"]
                repo = refs["repo"]
                repo_desc = (
                    repo["remote"] if repo and repo.get("kind") == "git"
                    else repo["kind"] if repo else "none"
                )
                print(f"Exported '{name}' -> {report['bundle_path']}")
                print(f"  files: {report['file_count']}")
                print(f"  repo: {repo_desc}")
                print(
                    f"  vaults: {len(refs['vaults'])}, "
                    f"embedded paths: {len(refs['other_paths'])}"
                )
                if include_time:
                    print(
                        f"  origin time (display-only): "
                        f"{m['project']['time_total_seconds']}s"
                    )

        elif command == "import":
            from missioncache_db import portability

            args = sys.argv[2:]
            if not args or args[0].startswith("-"):
                print(
                    "Usage: missioncache-db import <bundle> [--repo <path>] "
                    "[--force] [--rewrite-paths] [--dry-run] [--json]"
                )
                sys.exit(1)
            bundle = args[0]
            repo_override = None
            i = 1
            while i < len(args):  # value-flag index walk (create-task precedent)
                if args[i] == "--repo":
                    if i + 1 >= len(args) or args[i + 1].startswith("-"):
                        print("--repo requires a path argument", file=sys.stderr)
                        sys.exit(1)
                    repo_override = args[i + 1]
                    i += 2
                    continue
                i += 1
            force = "--force" in args
            rewrite = "--rewrite-paths" in args
            dry_run = "--dry-run" in args
            as_json = "--json" in args
            try:
                report = portability.import_bundle(
                    db, bundle, repo_override=repo_override, force=force,
                    rewrite=rewrite, dry_run=dry_run,
                )
            except (ValueError, OSError) as e:
                print(f"import failed: {e}", file=sys.stderr)
                sys.exit(1)

            if as_json:
                print(json.dumps(report, indent=2, sort_keys=True))
            elif report["errors"]:
                for err in report["errors"]:
                    print(f"import failed: {err}", file=sys.stderr)
            else:
                for line in portability.format_report_lines(report):
                    print(line)
                for w in report["warnings"]:
                    print(f"  warning: {w}", file=sys.stderr)
            sys.exit(report["exit_code"])

        else:
            print(f"Unknown command: {command}")
            print(__doc__)
            sys.exit(1)

    finally:
        db.close()


if __name__ == "__main__":
    main()

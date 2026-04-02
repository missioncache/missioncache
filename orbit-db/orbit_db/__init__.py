#!/usr/bin/env python3
"""
Task Database Manager for Claude Code orbit system.

Provides SQLite-based cross-repo task tracking with WakaTime-style time tracking.

Usage:
    python orbit_db.py init                      # Initialize database
    python orbit_db.py add-repo <path> [name]    # Add repository to track
    python orbit_db.py add-repos-glob <pattern>  # Add repos from glob pattern
    python orbit_db.py scan [repo_id]            # Scan repos for tasks
    python orbit_db.py list-active               # List all active tasks
    python orbit_db.py list-repos                # List tracked repositories
    python orbit_db.py get-task <task_id>        # Get task details (JSON)
    python orbit_db.py heartbeat [task_id]       # Record activity heartbeat
    python orbit_db.py heartbeat-auto            # Auto-detect task from cwd
    python orbit_db.py process-heartbeats        # Aggregate heartbeats into sessions
    python orbit_db.py task-time <task_id> [period]  # Get time spent
    python orbit_db.py prune [days]              # Prune old completed tasks
    python orbit_db.py complete-task <task_id>   # Mark task as completed
    python orbit_db.py reopen-task <task_id>     # Reopen a completed task
    python orbit_db.py list-completed [days]     # List recently completed tasks
    python orbit_db.py get-task-by-name <name>   # Find task by name

Keyword Management:
    python orbit_db.py add-keyword <keyword>     # Add custom tag keyword
    python orbit_db.py remove-keyword <keyword>  # Remove custom tag keyword
    python orbit_db.py list-keywords             # List all tag keywords
    python orbit_db.py backfill-tags             # Backfill tags for existing tasks

Non-Coding Task Management:
    python orbit_db.py create-task [--type TYPE] [--jira TICKET] <name>  # Create task
    python orbit_db.py add-update <task_id> <note>                       # Add timestamped update
    python orbit_db.py get-updates <task_id> [limit]                     # Get task updates
    python orbit_db.py today-updates [task_id]                           # Get today's updates

Migration:
    python orbit_db.py migrate-orbit-docs [--dry-run]  # Move docs to ~/.claude/orbit/

Cleanup:
    python orbit_db.py cleanup [--dry-run]              # Archive orphans, resolve dupes, normalize paths
"""

import json
import os
import re
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from enum import Enum
from glob import glob as glob_files
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union


# =============================================================================
# Configuration
# =============================================================================

DB_PATH = Path.home() / ".claude" / "tasks.db"
ORBIT_ROOT = Path.home() / ".claude" / "orbit"

# Non-git folder to track with shadow repo (only this folder gets shadow commits)
SHADOW_TRACKED_FOLDER = Path.home() / "work" / "claude_dev"

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
    # Team-specific (example/example)
    "REDACTED",
    "REDACTED",
    "REDACTED",
    "gc",
    "example",
    "REDACTED",
    "dns",
    "feed",
    "example",
    "REDACTED",
    "REDACTED",
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

-- Auto execution runs (orbit-auto)
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

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Task":
        # Parse tags from JSON string
        tags_raw = row["tags"] if "tags" in row.keys() else "[]"
        try:
            tags = json.loads(tags_raw) if tags_raw else []
        except (json.JSONDecodeError, TypeError):
            tags = []

        return cls(
            id=row["id"],
            repo_id=row["repo_id"],
            name=row["name"],
            full_path=row["full_path"],
            parent_id=row["parent_id"],
            status=row["status"],
            task_type=row["type"] if "type" in row.keys() else "coding",
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
    """An orbit-auto execution run."""

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
    """A log entry from an orbit-auto execution."""

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


class TaskDB:
    """SQLite-based task management database."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DB_PATH
        self._connection: Optional[sqlite3.Connection] = None

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
        try:
            yield self._connection
        finally:
            pass  # Keep connection open for reuse

    def close(self):
        """Close the database connection."""
        if self._connection:
            self._connection.close()
            self._connection = None

    def initialize(self) -> None:
        """Initialize the database schema and default config."""
        with self.connection() as conn:
            conn.executescript(SCHEMA_SQL)

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
        """Scan centralized orbit root for tasks and sync with database."""
        repo = self.get_repo(repo_id)
        if not repo:
            return []

        discovered_tasks = []

        # Scan active tasks from centralized orbit root
        active_dir = ORBIT_ROOT / "active"
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

        relative_path = str(task_dir.relative_to(ORBIT_ROOT))
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
                   jira_key, branch, pr_url)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    repo_id,
                    task_name,
                    relative_path,
                    parent_id,
                    status,
                    metadata.get("jira_key"),
                    metadata.get("branch"),
                    metadata.get("pr_url"),
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

    def _find_task_by_full_path(self, full_path: str) -> Optional[Task]:
        """Find a task by full_path across all repos."""
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE full_path = ? AND status = 'active'",
                (full_path,),
            ).fetchone()
            return Task.from_row(row) if row else None

    # =========================================================================
    # Non-Coding Task Management
    # =========================================================================

    def create_task(
        self,
        name: str,
        task_type: str = "coding",
        repo_id: Optional[int] = None,
        jira_key: Optional[str] = None,
    ) -> Task:
        """Create a new task (coding or non-coding).

        Args:
            name: Task name (e.g., "Sprint planning meeting")
            task_type: 'coding' or 'non-coding'
            repo_id: Repository ID (required for coding, None for non-coding)
            jira_key: Optional JIRA ticket ID

        Returns:
            The created Task object
        """
        if task_type not in ("coding", "non-coding"):
            raise ValueError(f"Invalid task type: {task_type}")

        # Non-coding tasks must not have a repo_id
        if task_type == "non-coding" and repo_id is not None:
            raise ValueError("Non-coding tasks cannot be associated with a repository")

        # Coding tasks should have a repo_id (though we allow None for flexibility)
        tags = extract_tags(name)
        full_path = f"global/{name}" if task_type == "non-coding" else f"manual/{name}"

        with self.connection() as conn:
            cursor = conn.execute(
                """INSERT INTO tasks (repo_id, name, full_path, type, tags, jira_key, status)
                   VALUES (?, ?, ?, ?, ?, ?, 'active')""",
                (repo_id, name, full_path, task_type, json.dumps(tags), jira_key),
            )
            conn.commit()
            return self.get_task(cursor.lastrowid)

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
        top_level = []
        children: Dict[int, List[Task]] = {}

        for task in all_tasks:
            if task.parent_id is None:
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

    def find_task_for_cwd(
        self, cwd: Union[str, Path], session_id: Optional[str] = None
    ) -> Optional[Task]:
        """Find the active task that matches the current working directory.

        Only returns a task when explicitly working on one:
        1. Check pending-project.json for explicitly registered project (from /orbit:orbit-project-continue)
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

        # Priority 2: Check per-session project file (written by statusline)
        # This persists the project assignment after pending-project.json is consumed
        if session_id:
            session_project_file = state_dir / "projects" / f"{session_id}.json"
            if session_project_file.exists():
                try:
                    with open(session_project_file) as f:
                        session_data = json.load(f)
                    task_name = session_data.get("projectName", "")
                    if task_name:
                        task = self._find_task_by_registered_name(task_name, cwd_path)
                        if task:
                            return task
                except (json.JSONDecodeError, IOError):
                    pass  # Fall through to other methods

        # Priority 3: Check if cwd is under centralized orbit root
        orbit_active = ORBIT_ROOT / "active"
        try:
            relative = cwd_path.relative_to(orbit_active)
            parts = relative.parts
            if parts:
                task_name = parts[0]
                # Check for subtask
                if len(parts) >= 2:
                    full_path = f"active/{parts[0]}/{parts[1]}"
                    task = self._find_task_by_full_path(full_path)
                    if task:
                        return task
                # Try parent task
                full_path = f"active/{task_name}"
                task = self._find_task_by_full_path(full_path)
                if task:
                    return task
        except ValueError:
            pass  # cwd is not under orbit root

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
                    # Check gap since last heartbeat
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

            # Close any remaining open session
            if current_session_id and last_heartbeat_time:
                self._close_session(
                    conn, current_session_id, last_heartbeat_time, assumed_work
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
                       WHERE task_id = ? AND start_time >= datetime('now', 'localtime', '-7 days')""",
                    (task_id,),
                ).fetchone()
            elif period == "today":
                row = conn.execute(
                    """SELECT COALESCE(SUM(duration_seconds), 0) as total FROM sessions
                       WHERE task_id = ? AND date(start_time, 'localtime') = date('now', 'localtime')""",
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
                query = f"""
                    SELECT task_id, COALESCE(SUM(duration_seconds), 0) as total
                    FROM sessions
                    WHERE task_id IN ({placeholders}) AND date(start_time, 'localtime') = date('now', 'localtime')
                    GROUP BY task_id
                """
            elif period == "week":
                query = f"""
                    SELECT task_id, COALESCE(SUM(duration_seconds), 0) as total
                    FROM sessions
                    WHERE task_id IN ({placeholders}) AND start_time >= datetime('now', 'localtime', '-7 days')
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

        # Get file modification time from centralized orbit root
        if task.full_path:
            task_dir = ORBIT_ROOT / task.full_path
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
        Example: /home/user/projects/claude_dev -> -home-user-projects-claude-dev
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
    # Dev-docs Progress Parsing
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

    def parse_orbit_progress(
        self, repo_path: str, task_full_path: str, parent_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """Parse orbit task file to extract progress information.

        Args:
            repo_path: Absolute path to the repository (legacy, used as fallback)
            task_full_path: Relative path like 'active/task-name' or legacy 'dev/active/task-name'
            parent_id: If set, this is a subtask

        Returns:
            Dict with progress info or {"has_docs": False} if not found
        """
        # Extract task name from path (last component)
        task_name = Path(task_full_path).name
        # Resolve via centralized orbit root (strip legacy dev/ prefix)
        normalized = (
            task_full_path[4:] if task_full_path.startswith("dev/") else task_full_path
        )
        task_dir = ORBIT_ROOT / normalized

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
        completed_tasks = ORBIT_ROOT / "completed" / f"{task_name}-tasks.md"
        completed_context = ORBIT_ROOT / "completed" / f"{task_name}-context.md"

        # Pattern 5: Completed folder (subdirectory format)
        completed_subdir_tasks = (
            ORBIT_ROOT / "completed" / task_name / f"{task_name}-tasks.md"
        )
        completed_subdir_context = (
            ORBIT_ROOT / "completed" / task_name / f"{task_name}-context.md"
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

        # Parse **Remaining:** field from task file (written by Claude via /update-orbit)
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

        # Parse **Summary:** field from task file (written by Claude via /update-orbit)
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
                print("Usage: orbit_db.py add-repo <path> [short_name]")
                sys.exit(1)
            path = sys.argv[2]
            short_name = sys.argv[3] if len(sys.argv) > 3 else None
            repo_id = db.add_repo(path, short_name)
            print(f"Added repo {path} with id {repo_id}")

        elif command == "add-repos-glob":
            if len(sys.argv) < 3:
                print("Usage: orbit_db.py add-repos-glob <pattern>")
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
                print("Usage: orbit_db.py heartbeat <task_id> [session_id]")
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
                print("Usage: orbit_db.py task-time <task_id> [period]")
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
                print("Usage: orbit_db.py get-task <task_id>")
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
                print("Usage: orbit_db.py complete-task <task_id>")
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
                elif not name:
                    # First positional arg is the name
                    name = sys.argv[i]
                    i += 1
                else:
                    i += 1

            if not name:
                print(
                    "Usage: orbit_db.py create-task [--type coding|non-coding] [--jira TICKET] <name>"
                )
                print(
                    "       orbit_db.py create-task --type non-coding --name 'Sprint planning'"
                )
                sys.exit(1)

            task = db.create_task(name, task_type=task_type, jira_key=jira_key)
            output = {
                "id": task.id,
                "name": task.name,
                "type": task.task_type,
                "tags": task.tags,
                "jira_key": task.jira_key,
                "status": task.status,
            }
            print(json.dumps(output, indent=2))

        elif command == "add-update":
            if len(sys.argv) < 4:
                print("Usage: orbit_db.py add-update <task_id> <note>")
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
                print("Usage: orbit_db.py get-updates <task_id> [limit]")
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
                print("Usage: orbit_db.py set-jira <task_id> <jira_key>")
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

        elif command == "add-keyword":
            if len(sys.argv) < 3:
                print("Usage: orbit_db.py add-keyword <keyword>")
                sys.exit(1)
            keyword = sys.argv[2]
            if db.add_keyword(keyword):
                print(f"Added keyword: {keyword}")
            else:
                print(f"Keyword already exists: {keyword}")
                sys.exit(1)

        elif command == "remove-keyword":
            if len(sys.argv) < 3:
                print("Usage: orbit_db.py remove-keyword <keyword>")
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
                print("Usage: orbit_db.py reopen-task <task_id>")
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

        elif command == "get-task-by-name":
            if len(sys.argv) < 3:
                print("Usage: orbit_db.py get-task-by-name <name> [--status <status>]")
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
            orbit_active = ORBIT_ROOT / "active"
            orbit_completed = ORBIT_ROOT / "completed"

            if not dry_run:
                orbit_active.mkdir(parents=True, exist_ok=True)
                orbit_completed.mkdir(parents=True, exist_ok=True)

            moved = 0
            skipped = 0

            # Move files from repo-local dev/ to centralized orbit root
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
            archived_count = 0
            merged_count = 0

            # --- B1: Archive orphaned active tasks (no files, no/minimal work) ---
            print("=== B1: Archive orphaned active tasks ===")
            orphan_ids = []
            with db.connection() as conn:
                rows = conn.execute(
                    "SELECT id, name, full_path FROM tasks "
                    "WHERE status = 'active' AND type = 'coding'"
                ).fetchall()
                for row in rows:
                    task_dir = ORBIT_ROOT / row["full_path"]
                    has_files = (
                        task_dir.exists()
                        and task_dir.is_dir()
                        and any(task_dir.iterdir())
                    )
                    if not has_files:
                        # Check if it's a duplicate (handled in B3)
                        dupes = conn.execute(
                            "SELECT COUNT(*) FROM tasks WHERE name = ?",
                            (row["name"],),
                        ).fetchone()[0]
                        if dupes > 1:
                            continue  # handled in B3
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
                archived_count += len(orphan_ids)
            print(f"  {prefix}{len(orphan_ids)} tasks archived\n")

            # --- B2: Move orphaned repo-local files ---
            print("=== B2: Move orphaned repo-local files ===")
            dev_dir = Path("/home/user/projects/claude_dev/dev")
            files_moved = 0

            # statusline-layout-improvement files
            src_completed = dev_dir / "completed"
            if src_completed.exists():
                sl_files = list(src_completed.glob("statusline-layout-improvement-*"))
                if sl_files:
                    dest_dir = (
                        ORBIT_ROOT / "completed" / "statusline-layout-improvement"
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

            # Remove empty dev/ subdirs
            for subdir_name in ["active", "completed"]:
                subdir = dev_dir / subdir_name
                if subdir.exists():
                    # Remove .DS_Store files
                    ds_store = subdir / ".DS_Store"
                    if ds_store.exists() and not dry_run:
                        ds_store.unlink()
                    remaining = [f for f in subdir.iterdir() if f.name != ".DS_Store"]
                    if not remaining:
                        if dry_run:
                            print(f"  {prefix}Remove empty {subdir}")
                        else:
                            shutil.rmtree(subdir)
                            print(f"  Removed empty {subdir}")

            # Remove dev/ itself if empty
            if dev_dir.exists() and not dry_run:
                ds_store = dev_dir / ".DS_Store"
                if ds_store.exists():
                    ds_store.unlink()
                remaining = [f for f in dev_dir.iterdir() if f.name != ".DS_Store"]
                if not remaining:
                    dev_dir.rmdir()
                    print(f"  Removed empty {dev_dir}")

            print(f"  {prefix}{files_moved} files moved\n")

            # --- B3: Resolve duplicates ---
            print("=== B3: Resolve duplicate task names ===")
            # Each entry: (keep_id, archive_ids)
            # Determined by: has files > recent work > more time
            duplicates = [
                # 05-module-aware-env-defaults: 69 active+files, 68 completed manual/
                (69, [68]),
                # 06-cicd-image-builds: 71 has more time (4182s vs 972s)
                (71, [73]),
                # 07-argo-workflow-server-nightly: 74 active+files+recent, 72 manual/ 75 empty
                (74, [72, 75]),
                # claude-activity-timezone-fix: 50 active+files, 48 manual/
                (50, [48]),
                # dynamic-workflow-registration: 60 completed+time, 62 active/no files
                (60, [62]),
                # eval-graders-system: 35 has last_worked, 28 manual/
                (35, [28]),
                # fix-ddc-lldc-test-failures: 16 completed+33h, 29 active/0 time
                (16, [29]),
                # orbit-loop-test: 40 more time (2674s vs 300s), 41
                (40, [41]),
            ]

            with db.connection() as conn:
                for keep_id, archive_ids in duplicates:
                    keep = conn.execute(
                        "SELECT id, name, status FROM tasks WHERE id = ?",
                        (keep_id,),
                    ).fetchone()
                    if not keep:
                        continue

                    for aid in archive_ids:
                        victim = conn.execute(
                            "SELECT id, name, status FROM tasks WHERE id = ?",
                            (aid,),
                        ).fetchone()
                        if not victim:
                            continue

                        # Migrate time data (sessions + heartbeats)
                        session_count = conn.execute(
                            "SELECT COUNT(*) FROM sessions WHERE task_id = ?",
                            (aid,),
                        ).fetchone()[0]
                        hb_count = conn.execute(
                            "SELECT COUNT(*) FROM heartbeats WHERE task_id = ?",
                            (aid,),
                        ).fetchone()[0]

                        if session_count > 0 or hb_count > 0:
                            print(
                                f"  {prefix}Merge ID={aid} -> ID={keep_id} "
                                f"({keep['name']}): "
                                f"{session_count} sessions, {hb_count} heartbeats"
                            )
                            if not dry_run:
                                conn.execute(
                                    "UPDATE sessions SET task_id = ? WHERE task_id = ?",
                                    (keep_id, aid),
                                )
                                conn.execute(
                                    "UPDATE heartbeats SET task_id = ? "
                                    "WHERE task_id = ?",
                                    (keep_id, aid),
                                )
                            merged_count += 1

                        print(
                            f"  {prefix}Archive ID={aid} "
                            f"name={victim['name']} "
                            f"(keeping ID={keep_id})"
                        )
                        if not dry_run:
                            conn.execute(
                                "UPDATE tasks SET status = 'archived', "
                                "archived_at = datetime('now') "
                                "WHERE id = ?",
                                (aid,),
                            )
                        archived_count += 1

                if not dry_run:
                    conn.commit()
            print(
                f"  {prefix}{merged_count} time merges, "
                f"{archived_count - len(orphan_ids)} duplicates archived\n"
            )

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
            print(f"  Duplicates resolved: {len(duplicates)}")
            print(f"  Time merges: {merged_count}")
            print(f"  Paths normalized: {normalized}")
            print(f"  Files moved: {files_moved}")
            if dry_run:
                print("\n  Run without --dry-run to apply changes.")

        else:
            print(f"Unknown command: {command}")
            print(__doc__)
            sys.exit(1)

    finally:
        db.close()


if __name__ == "__main__":
    main()

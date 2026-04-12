#!/usr/bin/env python3
"""
DuckDB-based Analytics Database for Orbit Dashboard.

Provides fast analytics queries for the orbit dashboard.
Uses DuckDB for 10-100x faster aggregate queries compared to SQLite.

This module is used by the orbit-dashboard FastAPI server for read operations.
Write operations (heartbeats) are still handled by the orbit_db package.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import duckdb

# =============================================================================
# Configuration
# =============================================================================

DUCKDB_PATH = Path.home() / ".claude" / "tasks.duckdb"
SQLITE_PATH = Path.home() / ".claude" / "tasks.db"  # Fallback


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class Repository:
    id: int
    path: str
    short_name: str
    glob_pattern: str | None
    active: bool
    created_at: datetime | None
    updated_at: datetime | None
    last_scanned_at: datetime | None

    @classmethod
    def from_row(cls, row: tuple, columns: list[str]) -> Repository:
        data = dict(zip(columns, row))
        return cls(
            id=data["id"],
            path=data["path"],
            short_name=data["short_name"],
            glob_pattern=data.get("glob_pattern"),
            active=bool(data["active"]),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            last_scanned_at=data.get("last_scanned_at"),
        )


@dataclass
class Task:
    id: int
    repo_id: int | None
    name: str
    full_path: str
    parent_id: int | None
    status: str
    task_type: str
    tags: list[str]
    priority: int | None
    jira_key: str | None
    branch: str | None
    pr_url: str | None
    created_at: datetime | None
    updated_at: datetime | None
    completed_at: datetime | None
    archived_at: datetime | None
    last_worked_on: datetime | None

    @classmethod
    def from_row(cls, row: tuple, columns: list[str]) -> Task:
        data = dict(zip(columns, row))

        # Parse tags - DuckDB stores as JSON string
        tags_raw = data.get("tags", "[]")
        if isinstance(tags_raw, str):
            try:
                tags = json.loads(tags_raw)
            except (json.JSONDecodeError, TypeError):
                tags = []
        else:
            tags = tags_raw if tags_raw else []

        return cls(
            id=data["id"],
            repo_id=data.get("repo_id"),
            name=data["name"],
            full_path=data["full_path"],
            parent_id=data.get("parent_id"),
            status=data["status"],
            task_type=data.get("type", "coding"),
            tags=tags,
            priority=data.get("priority"),
            jira_key=data.get("jira_key"),
            branch=data.get("branch"),
            pr_url=data.get("pr_url"),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            completed_at=data.get("completed_at"),
            archived_at=data.get("archived_at"),
            last_worked_on=data.get("last_worked_on"),
        )

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "repo_id": self.repo_id,
            "name": self.name,
            "full_path": self.full_path,
            "parent_id": self.parent_id,
            "status": self.status,
            "type": self.task_type,
            "tags": self.tags,
            "priority": self.priority,
            "jira_key": self.jira_key,
            "branch": self.branch,
            "pr_url": self.pr_url,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "completed_at": self.completed_at.isoformat()
            if self.completed_at
            else None,
            "archived_at": self.archived_at.isoformat() if self.archived_at else None,
            "last_worked_on": self.last_worked_on.isoformat()
            if self.last_worked_on
            else None,
        }


@dataclass
class Session:
    id: int
    task_id: int
    session_id: str | None
    start_time: datetime
    end_time: datetime | None
    duration_seconds: int
    heartbeat_count: int

    @classmethod
    def from_row(cls, row: tuple, columns: list[str]) -> Session:
        data = dict(zip(columns, row))
        return cls(
            id=data["id"],
            task_id=data["task_id"],
            session_id=data.get("session_id"),
            start_time=data["start_time"],
            end_time=data.get("end_time"),
            duration_seconds=data["duration_seconds"],
            heartbeat_count=data["heartbeat_count"],
        )


@dataclass
class Plan:
    """Represents an execution plan for parallel agent orchestration."""

    id: int
    name: str
    task_id: int | None
    status: str  # draft, pending, running, completed, failed
    total_agents: int
    completed_agents: int
    failed_agents: int
    created_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    metadata: dict | None

    @classmethod
    def from_row(cls, row: tuple, columns: list[str]) -> Plan:
        data = dict(zip(columns, row))

        # Parse metadata - DuckDB stores as JSON string
        metadata_raw = data.get("metadata")
        if isinstance(metadata_raw, str):
            try:
                metadata = json.loads(metadata_raw)
            except (json.JSONDecodeError, TypeError):
                metadata = None
        else:
            metadata = metadata_raw

        return cls(
            id=data["id"],
            name=data["name"],
            task_id=data.get("task_id"),
            status=data.get("status", "draft"),
            total_agents=data.get("total_agents", 0),
            completed_agents=data.get("completed_agents", 0),
            failed_agents=data.get("failed_agents", 0),
            created_at=data.get("created_at"),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            metadata=metadata,
        )

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "name": self.name,
            "task_id": self.task_id,
            "status": self.status,
            "total_agents": self.total_agents,
            "completed_agents": self.completed_agents,
            "failed_agents": self.failed_agents,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat()
            if self.completed_at
            else None,
            "metadata": self.metadata,
        }


@dataclass
class AgentExecution:
    """Represents a single agent execution within a plan."""

    id: int
    plan_id: int
    agent_id: str  # String like "01", "02" for ordering
    agent_name: str | None
    status: str  # pending, blocked, running, completed, failed
    prompt: str | None
    result: str | None
    error_message: str | None
    attempt_count: int
    max_attempts: int
    started_at: datetime | None
    completed_at: datetime | None
    duration_ms: int | None
    metadata: dict | None

    @classmethod
    def from_row(cls, row: tuple, columns: list[str]) -> AgentExecution:
        data = dict(zip(columns, row))

        # Parse metadata - DuckDB stores as JSON string
        metadata_raw = data.get("metadata")
        if isinstance(metadata_raw, str):
            try:
                metadata = json.loads(metadata_raw)
            except (json.JSONDecodeError, TypeError):
                metadata = None
        else:
            metadata = metadata_raw

        return cls(
            id=data["id"],
            plan_id=data["plan_id"],
            agent_id=data["agent_id"],
            agent_name=data.get("agent_name"),
            status=data.get("status", "pending"),
            prompt=data.get("prompt"),
            result=data.get("result"),
            error_message=data.get("error_message"),
            attempt_count=data.get("attempt_count", 0),
            max_attempts=data.get("max_attempts", 3),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            duration_ms=data.get("duration_ms"),
            metadata=metadata,
        )

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "plan_id": self.plan_id,
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "status": self.status,
            "prompt": self.prompt,
            "result": self.result,
            "error_message": self.error_message,
            "attempt_count": self.attempt_count,
            "max_attempts": self.max_attempts,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat()
            if self.completed_at
            else None,
            "duration_ms": self.duration_ms,
            "metadata": self.metadata,
        }


# =============================================================================
# Database Manager
# =============================================================================


class AnalyticsDB:
    """DuckDB-based analytics database for fast dashboard queries."""

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or DUCKDB_PATH
        self._connection: duckdb.DuckDBPyConnection | None = None

    @contextmanager
    def connection(self) -> Iterator[duckdb.DuckDBPyConnection]:
        """Context manager for database connection."""
        if self._connection is None:
            self._connection = duckdb.connect(str(self.db_path), read_only=False)
            # Set timezone to Israel for correct date comparisons
            # Sessions are stored in UTC, CURRENT_DATE returns local date
            self._connection.execute("SET timezone = 'Asia/Jerusalem'")
            # Ensure core tables exist (required for sync)
            self._ensure_core_tables(self._connection)
            # Ensure feeds tables exist
            self._ensure_feeds_tables(self._connection)
            # Ensure plans table exists
            self._ensure_plans_table(self._connection)
            # Ensure agent dependencies table exists
            self._ensure_agent_dependencies_table(self._connection)
            # Ensure agent executions table exists
            self._ensure_agent_executions_table(self._connection)
            # Sync sequences to existing data
            self._sync_sequences(self._connection)
        try:
            yield self._connection
        finally:
            pass  # Keep connection open for reuse

    def _ensure_core_tables(self, conn: duckdb.DuckDBPyConnection) -> None:
        """Create core task database tables if they don't exist.

        These match the schema in migrate_to_duckdb.py for compatibility.
        """
        conn.execute("""
            CREATE TABLE IF NOT EXISTS repositories (
                id INTEGER PRIMARY KEY,
                path VARCHAR NOT NULL UNIQUE,
                short_name VARCHAR NOT NULL,
                glob_pattern VARCHAR,
                active BOOLEAN NOT NULL DEFAULT true,
                created_at TIMESTAMP NOT NULL DEFAULT now(),
                updated_at TIMESTAMP NOT NULL DEFAULT now(),
                last_scanned_at TIMESTAMP
            )
        """)

        # DuckDB is an analytics mirror of SQLite - no FK constraints needed.
        # DuckDB implements UPDATE as DELETE+INSERT for FK-referenced rows,
        # which breaks upsert sync for tasks referenced by sessions/heartbeats.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY,
                repo_id INTEGER,
                name VARCHAR NOT NULL,
                full_path VARCHAR NOT NULL,
                parent_id INTEGER,
                status VARCHAR NOT NULL DEFAULT 'active',
                type VARCHAR NOT NULL DEFAULT 'coding',
                tags JSON NOT NULL DEFAULT '[]',
                priority INTEGER,
                jira_key VARCHAR,
                branch VARCHAR,
                pr_url VARCHAR,
                created_at TIMESTAMP NOT NULL DEFAULT now(),
                updated_at TIMESTAMP NOT NULL DEFAULT now(),
                completed_at TIMESTAMP,
                archived_at TIMESTAMP,
                last_worked_on TIMESTAMP,
                UNIQUE(repo_id, full_path)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS task_updates (
                id INTEGER PRIMARY KEY,
                task_id INTEGER NOT NULL,
                note VARCHAR NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT now()
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS heartbeats (
                id INTEGER PRIMARY KEY,
                task_id INTEGER NOT NULL,
                timestamp TIMESTAMP NOT NULL DEFAULT now(),
                session_id VARCHAR,
                context VARCHAR,
                processed BOOLEAN NOT NULL DEFAULT false
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY,
                task_id INTEGER NOT NULL,
                session_id VARCHAR,
                start_time TIMESTAMP NOT NULL,
                end_time TIMESTAMP,
                duration_seconds INTEGER NOT NULL DEFAULT 0,
                heartbeat_count INTEGER NOT NULL DEFAULT 0
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS config (
                key VARCHAR PRIMARY KEY,
                value JSON NOT NULL,
                updated_at TIMESTAMP NOT NULL DEFAULT now()
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS shadow_repos (
                id INTEGER PRIMARY KEY,
                folder_path VARCHAR UNIQUE NOT NULL,
                shadow_path VARCHAR NOT NULL,
                folder_hash VARCHAR NOT NULL,
                active BOOLEAN NOT NULL DEFAULT true,
                created_at TIMESTAMP NOT NULL DEFAULT now(),
                last_updated TIMESTAMP NOT NULL DEFAULT now()
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS shadow_commits (
                id INTEGER PRIMARY KEY,
                shadow_repo_id INTEGER NOT NULL,
                task_id INTEGER,
                commit_hash VARCHAR NOT NULL,
                message VARCHAR NOT NULL,
                files_changed INTEGER,
                insertions INTEGER,
                deletions INTEGER,
                committed_at TIMESTAMP NOT NULL DEFAULT now()
            )
        """)

    def _ensure_feeds_tables(self, conn: duckdb.DuckDBPyConnection) -> None:
        """Create RSS feeds tables if they don't exist."""
        # Feed folders for organizing sources
        conn.execute("""
            CREATE TABLE IF NOT EXISTS feed_folders (
                id VARCHAR PRIMARY KEY,
                name VARCHAR NOT NULL,
                color VARCHAR,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Feed sources (RSS/Atom feeds or HTML pages to scrape)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS feed_sources (
                id VARCHAR PRIMARY KEY,
                url VARCHAR NOT NULL,
                title VARCHAR,
                icon_url VARCHAR,
                source_type VARCHAR DEFAULT 'rss',
                folder_id VARCHAR,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_fetched TIMESTAMP
            )
        """)

        # Feed items (articles from sources)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS feed_items (
                id VARCHAR PRIMARY KEY,
                source_id VARCHAR NOT NULL,
                guid VARCHAR,
                title VARCHAR NOT NULL,
                description VARCHAR,
                image_url VARCHAR,
                link VARCHAR NOT NULL,
                published TIMESTAMP,
                fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status VARCHAR DEFAULT 'unread',
                read_at TIMESTAMP,
                summary VARCHAR,
                summary_generated_at TIMESTAMP
            )
        """)

        # Add summary columns if they don't exist (migration for existing DBs)
        try:
            conn.execute("ALTER TABLE feed_items ADD COLUMN summary VARCHAR")
        except duckdb.CatalogException:
            pass  # Column already exists
        try:
            conn.execute(
                "ALTER TABLE feed_items ADD COLUMN summary_generated_at TIMESTAMP"
            )
        except duckdb.CatalogException:
            pass  # Column already exists

        # Indexes for common query patterns
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_feed_items_source ON feed_items(source_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_feed_items_status ON feed_items(status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_feed_items_published ON feed_items(published DESC)"
        )

    def _ensure_plans_table(self, conn: duckdb.DuckDBPyConnection) -> None:
        """Create plans table if it doesn't exist."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS plans (
                id INTEGER PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                task_id INTEGER,
                status VARCHAR(20) DEFAULT 'draft',
                total_agents INTEGER DEFAULT 0,
                completed_agents INTEGER DEFAULT 0,
                failed_agents INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                metadata TEXT
            )
        """)

        # Create sequence for auto-incrementing IDs if not exists
        try:
            conn.execute("CREATE SEQUENCE IF NOT EXISTS plans_id_seq START 1")
        except duckdb.CatalogException:
            pass  # Sequence already exists

        # Indexes for common query patterns
        conn.execute("CREATE INDEX IF NOT EXISTS idx_plans_status ON plans(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_plans_task_id ON plans(task_id)")

    def _ensure_agent_dependencies_table(self, conn: duckdb.DuckDBPyConnection) -> None:
        """Create agent_dependencies table for DAG edges between agents."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_dependencies (
                id INTEGER PRIMARY KEY,
                plan_id INTEGER NOT NULL,
                agent_id VARCHAR(50) NOT NULL,
                depends_on VARCHAR(50) NOT NULL,
                UNIQUE(plan_id, agent_id, depends_on)
            )
        """)

        # Create sequence for auto-incrementing IDs if not exists
        try:
            conn.execute(
                "CREATE SEQUENCE IF NOT EXISTS agent_dependencies_id_seq START 1"
            )
        except duckdb.CatalogException:
            pass  # Sequence already exists

        # Indexes for common query patterns
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_deps_plan ON agent_dependencies(plan_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_deps_agent ON agent_dependencies(plan_id, agent_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_deps_depends ON agent_dependencies(plan_id, depends_on)"
        )

    def _ensure_agent_executions_table(self, conn: duckdb.DuckDBPyConnection) -> None:
        """Create agent_executions table to track individual agent runs within a plan."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_executions (
                id INTEGER PRIMARY KEY,
                plan_id INTEGER NOT NULL,
                agent_id VARCHAR(50) NOT NULL,
                agent_name VARCHAR(255),
                status VARCHAR(20) DEFAULT 'pending',
                prompt TEXT,
                result TEXT,
                error_message TEXT,
                attempt_count INTEGER DEFAULT 0,
                max_attempts INTEGER DEFAULT 3,
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                duration_ms INTEGER,
                metadata TEXT
            )
        """)

        # Create sequence for auto-incrementing IDs if not exists
        try:
            conn.execute(
                "CREATE SEQUENCE IF NOT EXISTS agent_executions_id_seq START 1"
            )
        except duckdb.CatalogException:
            pass  # Sequence already exists

        # Indexes for common query patterns
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_exec_plan ON agent_executions(plan_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_exec_status ON agent_executions(status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_exec_plan_agent ON agent_executions(plan_id, agent_id)"
        )

    def _sync_sequences(self, conn: duckdb.DuckDBPyConnection) -> None:
        """Sync sequences to existing data to avoid primary key conflicts.

        When the database has existing data (from previous sessions), the sequences
        may start from 1 and conflict with existing IDs. This method updates each
        sequence to start after the max existing ID.
        """
        # Sync plans_id_seq
        result = conn.execute("SELECT COALESCE(MAX(id), 0) FROM plans").fetchone()
        max_plan_id = result[0] if result else 0
        if max_plan_id > 0:
            # DuckDB doesn't have ALTER SEQUENCE RESTART, so we drop and recreate
            try:
                conn.execute("DROP SEQUENCE IF EXISTS plans_id_seq")
                conn.execute(f"CREATE SEQUENCE plans_id_seq START {max_plan_id + 1}")
            except duckdb.CatalogException:
                pass

        # Sync agent_executions_id_seq
        result = conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM agent_executions"
        ).fetchone()
        max_exec_id = result[0] if result else 0
        if max_exec_id > 0:
            try:
                conn.execute("DROP SEQUENCE IF EXISTS agent_executions_id_seq")
                conn.execute(
                    f"CREATE SEQUENCE agent_executions_id_seq START {max_exec_id + 1}"
                )
            except duckdb.CatalogException:
                pass

        # Sync agent_dependencies_id_seq
        result = conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM agent_dependencies"
        ).fetchone()
        max_dep_id = result[0] if result else 0
        if max_dep_id > 0:
            try:
                conn.execute("DROP SEQUENCE IF EXISTS agent_dependencies_id_seq")
                conn.execute(
                    f"CREATE SEQUENCE agent_dependencies_id_seq START {max_dep_id + 1}"
                )
            except duckdb.CatalogException:
                pass

    def close(self):
        """Close the database connection."""
        if self._connection:
            self._connection.close()
            self._connection = None

    def sync_from_sqlite(self) -> dict[str, int]:
        """Sync tasks, sessions, and heartbeats from SQLite to DuckDB.

        SQLite is the source of truth where Claude Code hooks write data.
        This method syncs new/updated records to DuckDB for analytics.
        Returns counts of synced records.
        """
        import sqlite3

        if not SQLITE_PATH.exists():
            return {"error": "SQLite database not found"}

        result = {"source": "sqlite"}

        try:
            sqlite_conn = sqlite3.connect(str(SQLITE_PATH))
            sqlite_conn.row_factory = sqlite3.Row

            # Sync repositories first (tasks reference repos via foreign key)
            repos_synced = self._sync_repos_from_sqlite(sqlite_conn)
            result["repos_synced"] = repos_synced

            # Sync tasks (after repos are available)
            tasks_synced = self._sync_tasks_from_sqlite(sqlite_conn)
            result["tasks_synced"] = tasks_synced

            # Sync sessions (incremental - only new sessions)
            sessions_synced = self._sync_sessions_from_sqlite(sqlite_conn)
            result["sessions_synced"] = sessions_synced

            # Get stats
            today = datetime.now().strftime("%Y-%m-%d")
            result["sessions_today"] = sqlite_conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE DATE(start_time, 'localtime') = ?",
                (today,),
            ).fetchone()[0]
            result["heartbeats_today"] = sqlite_conn.execute(
                "SELECT COUNT(*) FROM heartbeats WHERE DATE(timestamp, 'localtime') = ?",
                (today,),
            ).fetchone()[0]
            result["total_sessions"] = sqlite_conn.execute(
                "SELECT COUNT(*) FROM sessions"
            ).fetchone()[0]

            sqlite_conn.close()

        except Exception as e:
            result["error"] = str(e)

        return result

    def _sync_tasks_from_sqlite(self, sqlite_conn) -> int:
        """Sync tasks from SQLite to DuckDB."""
        rows = sqlite_conn.execute("SELECT * FROM tasks").fetchall()
        if not rows:
            return 0

        synced = 0
        with self.connection() as conn:
            for row in rows:
                try:
                    row_dict = dict(row)
                    task_id = row_dict["id"]

                    conn.execute(
                        """
                        INSERT INTO tasks (
                            id, repo_id, name, full_path, parent_id,
                            status, type, tags, priority, jira_key, branch, pr_url,
                            created_at, updated_at, completed_at, archived_at, last_worked_on
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (id) DO UPDATE SET
                            repo_id = EXCLUDED.repo_id,
                            name = EXCLUDED.name,
                            full_path = EXCLUDED.full_path,
                            parent_id = EXCLUDED.parent_id,
                            status = EXCLUDED.status,
                            type = EXCLUDED.type,
                            tags = EXCLUDED.tags,
                            priority = EXCLUDED.priority,
                            jira_key = EXCLUDED.jira_key,
                            branch = EXCLUDED.branch,
                            pr_url = EXCLUDED.pr_url,
                            updated_at = EXCLUDED.updated_at,
                            completed_at = EXCLUDED.completed_at,
                            archived_at = EXCLUDED.archived_at,
                            last_worked_on = EXCLUDED.last_worked_on
                    """,
                        (
                            task_id,
                            row_dict.get("repo_id"),
                            row_dict.get("name"),
                            row_dict.get("full_path"),
                            row_dict.get("parent_id"),
                            row_dict.get("status"),
                            row_dict.get("type"),
                            row_dict.get("tags"),
                            row_dict.get("priority"),
                            row_dict.get("jira_key"),
                            row_dict.get("branch"),
                            row_dict.get("pr_url"),
                            row_dict.get("created_at"),
                            row_dict.get("updated_at"),
                            row_dict.get("completed_at"),
                            row_dict.get("archived_at"),
                            row_dict.get("last_worked_on"),
                        ),
                    )
                    synced += 1
                except Exception as e:
                    print(
                        f"[SYNC WARNING] Failed to sync task {task_id} ({row_dict.get('name')}): {e}"
                    )

        return synced

    def _sync_repos_from_sqlite(self, sqlite_conn) -> int:
        """Sync repositories from SQLite to DuckDB."""
        rows = sqlite_conn.execute("SELECT * FROM repositories").fetchall()
        if not rows:
            return 0

        synced = 0
        with self.connection() as conn:
            for row in rows:
                try:
                    row_dict = dict(row)
                    repo_id = row_dict["id"]

                    existing = conn.execute(
                        "SELECT id FROM repositories WHERE id = ?", (repo_id,)
                    ).fetchone()

                    if existing:
                        conn.execute(
                            """
                            UPDATE repositories SET
                                path = ?, short_name = ?
                            WHERE id = ?
                        """,
                            (
                                row_dict.get("path"),
                                row_dict.get("short_name"),
                                repo_id,
                            ),
                        )
                    else:
                        conn.execute(
                            """
                            INSERT INTO repositories (id, path, short_name, created_at)
                            VALUES (?, ?, ?, ?)
                        """,
                            (
                                repo_id,
                                row_dict.get("path"),
                                row_dict.get("short_name"),
                                row_dict.get("created_at"),
                            ),
                        )
                    synced += 1
                except Exception:
                    # Skip repos that fail due to constraints
                    pass

        return synced

    def _sync_sessions_from_sqlite(self, sqlite_conn) -> int:
        """Sync sessions from SQLite to DuckDB (incremental)."""

        def parse_timestamp(ts_str: str | None) -> datetime | None:
            """Parse SQLite timestamp string to datetime."""
            if not ts_str:
                return None
            formats = [
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M:%S.%f",
                "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M:%S.%f",
            ]
            for fmt in formats:
                try:
                    return datetime.strptime(ts_str, fmt)
                except ValueError:
                    continue
            return None

        # Get max session ID in DuckDB to do incremental sync
        with self.connection() as conn:
            max_id_row = conn.execute("SELECT MAX(id) FROM sessions").fetchone()
            max_id = max_id_row[0] if max_id_row and max_id_row[0] else 0

        # Only fetch sessions with ID > max_id (new sessions)
        rows = sqlite_conn.execute(
            "SELECT * FROM sessions WHERE id > ?", (max_id,)
        ).fetchall()

        if not rows:
            return 0

        synced = 0
        with self.connection() as conn:
            for row in rows:
                try:
                    row_dict = dict(row)

                    conn.execute(
                        """
                        INSERT INTO sessions (id, task_id, session_id, start_time, end_time,
                                             duration_seconds, heartbeat_count)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                        (
                            row_dict["id"],
                            row_dict["task_id"],
                            row_dict.get("session_id"),
                            parse_timestamp(row_dict.get("start_time")),
                            parse_timestamp(row_dict.get("end_time")),
                            row_dict.get("duration_seconds", 0),
                            row_dict.get("heartbeat_count", 0),
                        ),
                    )
                    synced += 1
                except Exception:
                    # Skip sessions that fail due to constraints
                    pass

        return synced

    def get_sqlite_stats(self) -> dict:
        """Get current session/heartbeat stats directly from SQLite."""
        import sqlite3

        if not SQLITE_PATH.exists():
            return {"error": "SQLite database not found"}

        try:
            conn = sqlite3.connect(str(SQLITE_PATH))
            conn.row_factory = sqlite3.Row

            today = datetime.now().strftime("%Y-%m-%d")

            # Count today's sessions (convert UTC to local time for comparison)
            sessions_today = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE DATE(start_time, 'localtime') = ?",
                (today,),
            ).fetchone()[0]

            # Count today's heartbeats (convert UTC to local time for comparison)
            heartbeats_today = conn.execute(
                "SELECT COUNT(*) FROM heartbeats WHERE DATE(timestamp, 'localtime') = ?",
                (today,),
            ).fetchone()[0]

            # Total sessions
            total_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

            conn.close()

            return {
                "source": "sqlite",
                "sessions_today": sessions_today,
                "heartbeats_today": heartbeats_today,
                "total_sessions": total_sessions,
            }

        except Exception as e:
            return {"error": str(e)}

    def get_sessions_from_sqlite(self, date: str | None = None) -> list[dict]:
        """Get sessions directly from SQLite for fresh data."""
        import sqlite3

        if not SQLITE_PATH.exists():
            return []

        try:
            conn = sqlite3.connect(str(SQLITE_PATH))
            conn.row_factory = sqlite3.Row

            if date is None:
                date = datetime.now().strftime("%Y-%m-%d")

            cursor = conn.execute(
                """SELECT s.id, s.task_id, t.name as task_name, t.full_path,
                          t.parent_id, p.name as parent_name,
                          datetime(s.start_time, 'localtime') as start_time,
                          datetime(s.end_time, 'localtime') as end_time,
                          s.duration_seconds,
                          r.short_name as repo_name
                   FROM sessions s
                   JOIN tasks t ON s.task_id = t.id
                   LEFT JOIN tasks p ON t.parent_id = p.id
                   LEFT JOIN repositories r ON t.repo_id = r.id
                   WHERE DATE(s.start_time, 'localtime') = ?
                   ORDER BY s.start_time""",
                (date,),
            )

            sessions = []
            for row in cursor.fetchall():
                start_time = datetime.fromisoformat(row["start_time"])
                end_time = (
                    datetime.fromisoformat(row["end_time"])
                    if row["end_time"]
                    else datetime.now()
                )

                display_name = row["task_name"]
                if row["parent_name"]:
                    display_name = f"{row['parent_name']} / {row['task_name']}"

                sessions.append(
                    {
                        "id": row["id"],
                        "task_id": row["task_id"],
                        "task_name": row["task_name"],
                        "display_name": display_name,
                        "parent_id": row["parent_id"],
                        "start_time": row["start_time"],
                        "end_time": row["end_time"],
                        "start_hour": start_time.hour + start_time.minute / 60,
                        "end_hour": end_time.hour + end_time.minute / 60,
                        "duration_seconds": row["duration_seconds"],
                        "repo_name": row["repo_name"],
                    }
                )

            conn.close()
            return sessions

        except Exception as e:
            print(f"Error reading SQLite sessions: {e}")
            return []

    def get_hourly_activity_from_sqlite(self, date: str | None = None) -> list[dict]:
        """Get hourly activity breakdown directly from SQLite."""
        import sqlite3

        if not SQLITE_PATH.exists():
            return []

        try:
            conn = sqlite3.connect(str(SQLITE_PATH))

            if date is None:
                date = datetime.now().strftime("%Y-%m-%d")

            cursor = conn.execute(
                """SELECT CAST(strftime('%H', start_time, 'localtime') AS INTEGER) as hour,
                          SUM(duration_seconds) as total_seconds,
                          COUNT(*) as session_count
                   FROM sessions
                   WHERE DATE(start_time, 'localtime') = ?
                   GROUP BY hour
                   ORDER BY hour""",
                (date,),
            )

            result = [
                {"hour": row[0], "total_seconds": row[1], "session_count": row[2]}
                for row in cursor.fetchall()
            ]

            conn.close()
            return result

        except Exception as e:
            print(f"Error reading SQLite hourly activity: {e}")
            return []

    def get_tasks_today_from_sqlite(self, date: str | None = None) -> list[dict]:
        """Get tasks worked on today directly from SQLite.

        Includes:
        - Coding tasks with sessions today (time tracked via heartbeats)
        - Non-coding tasks with updates today (activity tracked via task_updates)
        """
        import sqlite3

        if not SQLITE_PATH.exists():
            return []

        try:
            conn = sqlite3.connect(str(SQLITE_PATH))
            conn.row_factory = sqlite3.Row

            if date is None:
                date = datetime.now().strftime("%Y-%m-%d")

            # Query 1: Coding tasks with sessions today
            cursor = conn.execute(
                """SELECT t.id, t.name, t.full_path, t.status, t.parent_id,
                          p.name as parent_name, t.jira_key, t.tags, t.type,
                          r.short_name as repo_name,
                          SUM(s.duration_seconds) as time_seconds,
                          0 as update_count
                   FROM sessions s
                   JOIN tasks t ON s.task_id = t.id
                   LEFT JOIN tasks p ON t.parent_id = p.id
                   LEFT JOIN repositories r ON t.repo_id = r.id
                   WHERE DATE(s.start_time, 'localtime') = ?
                   GROUP BY t.id""",
                (date,),
            )
            session_tasks = {row["id"]: dict(row) for row in cursor.fetchall()}

            # Query 2: Non-coding tasks with updates today
            cursor = conn.execute(
                """SELECT t.id, t.name, t.full_path, t.status, t.parent_id,
                          p.name as parent_name, t.jira_key, t.tags, t.type,
                          NULL as repo_name,
                          0 as time_seconds,
                          COUNT(u.id) as update_count
                   FROM task_updates u
                   JOIN tasks t ON u.task_id = t.id
                   LEFT JOIN tasks p ON t.parent_id = p.id
                   WHERE DATE(u.created_at, 'localtime') = ?
                     AND t.type = 'non-coding'
                   GROUP BY t.id""",
                (date,),
            )

            # Merge: add non-coding tasks not already in session_tasks
            for row in cursor.fetchall():
                if row["id"] not in session_tasks:
                    session_tasks[row["id"]] = dict(row)

            # Build result list
            tasks = []
            for row in session_tasks.values():
                time_seconds = row["time_seconds"] or 0
                update_count = row["update_count"] or 0
                hours = time_seconds // 3600
                minutes = (time_seconds % 3600) // 60

                # Format time: show updates count for non-coding tasks without session time
                if time_seconds > 0:
                    if hours > 0:
                        time_formatted = f"{hours}h {minutes}m"
                    else:
                        time_formatted = f"{minutes}m"
                elif update_count > 0:
                    time_formatted = (
                        f"{update_count} update{'s' if update_count != 1 else ''}"
                    )
                else:
                    time_formatted = "0m"

                tasks.append(
                    {
                        "id": row["id"],
                        "name": row["name"],
                        "status": row["status"],
                        "type": row["type"] or "coding",  # Default to coding
                        "parent_id": row["parent_id"],
                        "parent_name": row["parent_name"],
                        "jira_key": row["jira_key"],
                        "jira_url": f"https://example.com/jira/browse/{row['jira_key']}"
                        if row["jira_key"]
                        else None,
                        "tags": json.loads(row["tags"]) if row["tags"] else [],
                        "repo_name": row["repo_name"],
                        "time_seconds": time_seconds,
                        "time_formatted": time_formatted,
                        "update_count": update_count,
                    }
                )

            # Sort: tasks with time first (descending), then by update count
            tasks.sort(
                key=lambda x: (x["time_seconds"], x["update_count"]), reverse=True
            )

            conn.close()
            return tasks

        except Exception as e:
            print(f"Error reading SQLite tasks today: {e}")
            return []

    def _fetch_all(
        self, query: str, params: tuple = ()
    ) -> tuple[list[tuple], list[str]]:
        """Execute query and return rows with column names."""
        with self.connection() as conn:
            result = conn.execute(query, params)
            columns = [desc[0] for desc in result.description]
            rows = result.fetchall()
            return rows, columns

    def _fetch_one(
        self, query: str, params: tuple = ()
    ) -> tuple[tuple | None, list[str]]:
        """Execute query and return single row with column names."""
        with self.connection() as conn:
            result = conn.execute(query, params)
            columns = [desc[0] for desc in result.description]
            row = result.fetchone()
            return row, columns

    # =========================================================================
    # Repository Queries
    # =========================================================================

    def get_repos(self, active_only: bool = True) -> list[Repository]:
        """Get all tracked repositories."""
        query = "SELECT * FROM repositories"
        if active_only:
            query += " WHERE active = true"
        query += " ORDER BY short_name"

        rows, columns = self._fetch_all(query)
        return [Repository.from_row(r, columns) for r in rows]

    def get_repo(self, repo_id: int) -> Repository | None:
        """Get a specific repository."""
        row, columns = self._fetch_one(
            "SELECT * FROM repositories WHERE id = ?", (repo_id,)
        )
        return Repository.from_row(row, columns) if row else None

    def get_repo_by_path(self, path: str) -> Repository | None:
        """Get a repository by its path."""
        path_str = str(Path(path).expanduser().resolve())
        row, columns = self._fetch_one(
            "SELECT * FROM repositories WHERE path = ?", (path_str,)
        )
        return Repository.from_row(row, columns) if row else None

    # =========================================================================
    # Task Queries
    # =========================================================================

    def get_task(self, task_id: int) -> Task | None:
        """Get a task by ID."""
        row, columns = self._fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        return Task.from_row(row, columns) if row else None

    def get_task_by_name(self, name: str) -> Task | None:
        """Get a task by name."""
        row, columns = self._fetch_one("SELECT * FROM tasks WHERE name = ?", (name,))
        return Task.from_row(row, columns) if row else None

    def get_task_updates(self, task_id: int, limit: int = 50) -> list[dict]:
        """Get updates for a task from the task_updates table."""
        rows, columns = self._fetch_all(
            """SELECT id, note, created_at
               FROM task_updates
               WHERE task_id = ?
               ORDER BY created_at DESC
               LIMIT ?""",
            (task_id, limit),
        )
        updates = []
        for row in rows:
            data = dict(zip(columns, row))
            # Format the timestamp
            created_at = data.get("created_at")
            if created_at:
                if isinstance(created_at, str):
                    from datetime import datetime as dt

                    try:
                        created_at = dt.fromisoformat(created_at.replace("Z", "+00:00"))
                    except Exception:
                        pass
                if hasattr(created_at, "strftime"):
                    data["created_at_formatted"] = created_at.strftime("%Y-%m-%d %H:%M")
                    data["created_at"] = created_at.isoformat()
                else:
                    data["created_at_formatted"] = str(created_at)
            updates.append(data)
        return updates

    def get_active_tasks(self, repo_id: int | None = None) -> list[Task]:
        """Get all active tasks, optionally filtered by repo."""
        if repo_id:
            rows, columns = self._fetch_all(
                """SELECT * FROM tasks
                   WHERE status IN ('active', 'paused') AND repo_id = ?
                   ORDER BY last_worked_on DESC NULLS LAST""",
                (repo_id,),
            )
        else:
            rows, columns = self._fetch_all(
                """SELECT * FROM tasks
                   WHERE status IN ('active', 'paused')
                   ORDER BY last_worked_on DESC NULLS LAST"""
            )
        return [Task.from_row(r, columns) for r in rows]

    def get_completed_tasks(self, days: int = 30) -> list[Task]:
        """Get completed tasks within N days."""
        rows, columns = self._fetch_all(
            f"""SELECT * FROM tasks
               WHERE status = 'completed'
               AND completed_at >= now() - INTERVAL '{days}' DAY
               ORDER BY completed_at DESC"""
        )
        return [Task.from_row(r, columns) for r in rows]

    def get_tasks_with_repo(self, status: str = "active") -> list[dict]:
        """Get tasks with repository info joined."""
        rows, columns = self._fetch_all(
            """SELECT t.*, r.short_name as repo_name, r.path as repo_path
               FROM tasks t
               LEFT JOIN repositories r ON t.repo_id = r.id
               WHERE t.status = ?
               ORDER BY t.last_worked_on DESC NULLS LAST""",
            (status,),
        )
        return [dict(zip(columns, r)) for r in rows]

    def get_subtasks(self, parent_id: int) -> list[Task]:
        """Get all subtasks of a parent task."""
        rows, columns = self._fetch_all(
            """SELECT * FROM tasks
               WHERE parent_id = ?
               ORDER BY name""",
            (parent_id,),
        )
        return [Task.from_row(r, columns) for r in rows]

    # =========================================================================
    # Time Analytics
    # =========================================================================

    def get_task_time(self, task_id: int, period: str = "all") -> int:
        """Get total time spent on a task in seconds."""
        with self.connection() as conn:
            if period == "today":
                # Convert UTC timestamp to local timezone before comparing dates
                result = conn.execute(
                    """SELECT COALESCE(SUM(duration_seconds), 0) as total
                       FROM sessions
                       WHERE task_id = ?
                         AND DATE(start_time AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Jerusalem') = CURRENT_DATE""",
                    (task_id,),
                ).fetchone()
            elif period == "week":
                result = conn.execute(
                    """SELECT COALESCE(SUM(duration_seconds), 0) as total
                       FROM sessions
                       WHERE task_id = ? AND start_time >= now() - INTERVAL 7 DAY""",
                    (task_id,),
                ).fetchone()
            else:
                result = conn.execute(
                    """SELECT COALESCE(SUM(duration_seconds), 0) as total
                       FROM sessions WHERE task_id = ?""",
                    (task_id,),
                ).fetchone()

            return int(result[0]) if result else 0

    def get_subtask_time_total(self, parent_task_id: int) -> int:
        """Get total time spent on all subtasks of a parent task."""
        with self.connection() as conn:
            result = conn.execute(
                """SELECT COALESCE(SUM(s.duration_seconds), 0) as total
                   FROM sessions s
                   JOIN tasks t ON s.task_id = t.id
                   WHERE t.parent_id = ?""",
                (parent_task_id,),
            ).fetchone()
            return int(result[0]) if result else 0

    def get_batch_task_times(
        self, task_ids: list[int], period: str = "all"
    ) -> dict[int, int]:
        """Get time for multiple tasks in ONE query."""
        if not task_ids:
            return {}

        with self.connection() as conn:
            placeholders = ",".join(["?"] * len(task_ids))

            if period == "today":
                # Convert UTC timestamp to local timezone before comparing dates
                query = f"""
                    SELECT task_id, COALESCE(SUM(duration_seconds), 0) as total
                    FROM sessions
                    WHERE task_id IN ({placeholders})
                      AND DATE(start_time AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Jerusalem') = CURRENT_DATE
                    GROUP BY task_id
                """
            elif period == "week":
                query = f"""
                    SELECT task_id, COALESCE(SUM(duration_seconds), 0) as total
                    FROM sessions
                    WHERE task_id IN ({placeholders}) AND start_time >= now() - INTERVAL 7 DAY
                    GROUP BY task_id
                """
            else:
                query = f"""
                    SELECT task_id, COALESCE(SUM(duration_seconds), 0) as total
                    FROM sessions
                    WHERE task_id IN ({placeholders})
                    GROUP BY task_id
                """

            rows = conn.execute(query, task_ids).fetchall()
            result = {row[0]: int(row[1]) for row in rows}

            # Fill in zeros for tasks with no sessions
            for task_id in task_ids:
                if task_id not in result:
                    result[task_id] = 0

            return result

    def get_daily_activity(self, days: int = 30) -> list[dict]:
        """Get daily activity summary for charting."""
        with self.connection() as conn:
            # DuckDB requires interval to be constructed differently
            rows = conn.execute(
                f"""SELECT
                       DATE(start_time) as date,
                       SUM(duration_seconds) as total_seconds,
                       COUNT(DISTINCT task_id) as task_count,
                       COUNT(*) as session_count
                   FROM sessions
                   WHERE start_time >= now() - INTERVAL '{days}' DAY
                   GROUP BY DATE(start_time)
                   ORDER BY date"""
            ).fetchall()

            return [
                {
                    "date": str(row[0]),
                    "total_seconds": int(row[1]),
                    "task_count": int(row[2]),
                    "session_count": int(row[3]),
                }
                for row in rows
            ]

    def get_hourly_activity(self, date: str | None = None) -> list[dict]:
        """Get hourly activity breakdown for a specific date."""
        with self.connection() as conn:
            # Convert UTC to local time for both date filtering and hour extraction
            local_ts = "start_time AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Jerusalem'"
            if date:
                rows = conn.execute(
                    f"""SELECT
                           EXTRACT(HOUR FROM {local_ts}) as hour,
                           SUM(duration_seconds) as total_seconds,
                           COUNT(*) as session_count
                       FROM sessions
                       WHERE DATE({local_ts}) = ?
                       GROUP BY EXTRACT(HOUR FROM {local_ts})
                       ORDER BY hour""",
                    (date,),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"""SELECT
                           EXTRACT(HOUR FROM {local_ts}) as hour,
                           SUM(duration_seconds) as total_seconds,
                           COUNT(*) as session_count
                       FROM sessions
                       WHERE DATE({local_ts}) = CURRENT_DATE
                       GROUP BY EXTRACT(HOUR FROM {local_ts})
                       ORDER BY hour"""
                ).fetchall()

            return [
                {
                    "hour": int(row[0]),
                    "total_seconds": int(row[1]),
                    "session_count": int(row[2]),
                }
                for row in rows
            ]

    def get_repo_breakdown(self, days: int = 7) -> list[dict]:
        """Get time breakdown by repository."""
        with self.connection() as conn:
            rows = conn.execute(
                f"""SELECT
                       COALESCE(r.short_name, 'Unknown') as repo_name,
                       r.id as repo_id,
                       SUM(s.duration_seconds) as total_seconds,
                       COUNT(DISTINCT t.id) as task_count
                   FROM sessions s
                   JOIN tasks t ON s.task_id = t.id
                   LEFT JOIN repositories r ON t.repo_id = r.id
                   WHERE s.start_time >= now() - INTERVAL '{days}' DAY
                   GROUP BY COALESCE(r.short_name, 'Unknown'), r.id
                   ORDER BY total_seconds DESC"""
            ).fetchall()

            return [
                {
                    "repo_name": row[0],
                    "repo_id": row[1],
                    "total_seconds": int(row[2]),
                    "task_count": int(row[3]),
                }
                for row in rows
            ]

    def get_today_stats(self) -> dict:
        """Get summary statistics for today."""
        with self.connection() as conn:
            # Convert UTC timestamp to local timezone before comparing dates
            local_ts = "start_time AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Jerusalem'"
            result = conn.execute(
                f"""SELECT
                       COALESCE(SUM(duration_seconds), 0) as total_seconds,
                       COUNT(DISTINCT task_id) as task_count,
                       COUNT(*) as session_count
                   FROM sessions
                   WHERE DATE({local_ts}) = CURRENT_DATE"""
            ).fetchone()

            return {
                "total_seconds": int(result[0]),
                "task_count": int(result[1]),
                "session_count": int(result[2]),
            }

    def get_date_stats(self, date: str) -> dict:
        """Get summary statistics for a specific date."""
        with self.connection() as conn:
            # Convert UTC timestamp to local timezone before comparing dates
            local_ts = "start_time AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Jerusalem'"
            result = conn.execute(
                f"""SELECT
                       COALESCE(SUM(duration_seconds), 0) as total_seconds,
                       COUNT(DISTINCT task_id) as task_count,
                       COUNT(*) as session_count
                   FROM sessions
                   WHERE DATE({local_ts}) = ?""",
                (date,),
            ).fetchone()

            return {
                "date": date,
                "total_seconds": int(result[0]),
                "task_count": int(result[1]),
                "session_count": int(result[2]),
            }

    # =========================================================================
    # Timeline & Heatmap Analytics (Phase 6 - Feature Parity)
    # =========================================================================

    def get_sessions_for_timeline(self, date: str | None = None) -> list[dict]:
        """Get sessions with task info for Gantt-style timeline visualization.

        Returns sessions grouped by task, suitable for rendering as horizontal
        bars on a 24-hour timeline.
        """
        with self.connection() as conn:
            # Convert UTC to local time for date filtering
            local_ts = "s.start_time AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Jerusalem'"
            if date:
                rows = conn.execute(
                    f"""SELECT
                           s.id,
                           s.task_id,
                           t.name as task_name,
                           t.parent_id,
                           s.start_time,
                           s.end_time,
                           s.duration_seconds,
                           r.short_name as repo_name
                       FROM sessions s
                       JOIN tasks t ON s.task_id = t.id
                       LEFT JOIN repositories r ON t.repo_id = r.id
                       WHERE DATE({local_ts}) = ?
                       ORDER BY s.start_time""",
                    (date,),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"""SELECT
                           s.id,
                           s.task_id,
                           t.name as task_name,
                           t.parent_id,
                           s.start_time,
                           s.end_time,
                           s.duration_seconds,
                           r.short_name as repo_name
                       FROM sessions s
                       JOIN tasks t ON s.task_id = t.id
                       LEFT JOIN repositories r ON t.repo_id = r.id
                       WHERE DATE({local_ts}) = CURRENT_DATE
                       ORDER BY s.start_time"""
                ).fetchall()

            # Get parent task names for subtasks
            parent_ids = set(row[3] for row in rows if row[3] is not None)
            parent_names = {}
            if parent_ids:
                placeholders = ",".join(["?"] * len(parent_ids))
                parent_rows = conn.execute(
                    f"SELECT id, name FROM tasks WHERE id IN ({placeholders})",
                    list(parent_ids),
                ).fetchall()
                parent_names = {r[0]: r[1] for r in parent_rows}

            result = []
            for row in rows:
                task_name = row[2]
                parent_id = row[3]

                # Format as "parent / subtask" if has parent
                if parent_id and parent_id in parent_names:
                    display_name = f"{parent_names[parent_id]} / {task_name}"
                else:
                    display_name = task_name

                result.append(
                    {
                        "id": row[0],
                        "task_id": row[1],
                        "task_name": task_name,
                        "display_name": display_name,
                        "parent_id": parent_id,
                        "start_time": row[4].isoformat() if row[4] else None,
                        "end_time": row[5].isoformat() if row[5] else None,
                        "start_hour": row[4].hour + row[4].minute / 60 if row[4] else 0,
                        "end_hour": row[5].hour + row[5].minute / 60 if row[5] else 24,
                        "duration_seconds": int(row[6]) if row[6] else 0,
                        "repo_name": row[7],
                    }
                )

            return result

    def get_hourly_heatmap(self, days: int = 7) -> list[dict]:
        """Get activity heatmap by day-of-week and hour.

        Returns 7×24 grid of activity minutes for GitHub-style contribution grid.
        Day of week: 0=Sunday (Israel week start), 6=Saturday
        """
        with self.connection() as conn:
            # Convert UTC to local time for accurate day-of-week and hour extraction
            local_ts = "start_time AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Jerusalem'"
            rows = conn.execute(
                f"""SELECT
                       EXTRACT(DOW FROM {local_ts}) as dow,
                       EXTRACT(HOUR FROM {local_ts}) as hour,
                       SUM(duration_seconds) / 60.0 as minutes,
                       COUNT(*) as session_count
                   FROM sessions
                   WHERE start_time >= now() - INTERVAL '{days}' DAY
                   GROUP BY
                       EXTRACT(DOW FROM {local_ts}),
                       EXTRACT(HOUR FROM {local_ts})
                   ORDER BY dow, hour"""
            ).fetchall()

            # Convert to list of dicts
            result = []
            for row in rows:
                result.append(
                    {
                        "dow": int(row[0]),  # 0=Sunday, 6=Saturday
                        "hour": int(row[1]),
                        "minutes": round(float(row[2]), 1),
                        "session_count": int(row[3]),
                    }
                )

            return result

    def get_daily_work_totals(self, days: int = 7) -> list[dict]:
        """Get total work aggregated by day of week.

        Returns 7 buckets (Sun-Sat) with total minutes worked.
        Day of week: 0=Sunday (Israel week start), 6=Saturday
        """
        with self.connection() as conn:
            # Convert UTC to local time for accurate day-of-week extraction
            local_ts = "start_time AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Jerusalem'"
            rows = conn.execute(
                f"""SELECT
                       EXTRACT(DOW FROM {local_ts}) as dow,
                       SUM(duration_seconds) / 60.0 as total_minutes,
                       COUNT(*) as session_count
                   FROM sessions
                   WHERE start_time >= now() - INTERVAL '{days}' DAY
                   GROUP BY EXTRACT(DOW FROM {local_ts})
                   ORDER BY dow"""
            ).fetchall()

            # Convert to list of dicts
            result = []
            for row in rows:
                result.append(
                    {
                        "dow": int(row[0]),  # 0=Sunday, 6=Saturday
                        "total_minutes": round(float(row[1]), 1),
                        "session_count": int(row[2]),
                    }
                )

            return result

    def get_daily_work_by_date(self, days: int = 7) -> list[dict]:
        """Get work totals per actual date (chronological view).

        Returns one entry per date with minutes worked.
        Useful for viewing work patterns over time without DOW aggregation.
        """
        with self.connection() as conn:
            # Convert UTC to local time for accurate date extraction
            local_ts = "start_time AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Jerusalem'"
            rows = conn.execute(
                f"""SELECT
                       DATE({local_ts}) as work_date,
                       EXTRACT(DOW FROM {local_ts}) as dow,
                       SUM(duration_seconds) / 60.0 as total_minutes,
                       COUNT(*) as session_count
                   FROM sessions
                   WHERE start_time >= now() - INTERVAL '{days}' DAY
                   GROUP BY DATE({local_ts}), EXTRACT(DOW FROM {local_ts})
                   ORDER BY work_date"""
            ).fetchall()

            result = []
            for row in rows:
                result.append(
                    {
                        "date": row[0].strftime("%Y-%m-%d"),
                        "dow": int(row[1]),  # 0=Sunday, 6=Saturday
                        "total_minutes": round(float(row[2]), 1),
                        "session_count": int(row[3]),
                    }
                )

            return result

    def get_top_tasks_by_effort(self, days: int = 7, limit: int = 5) -> list[dict]:
        """Get top N tasks by time spent in the period.

        Returns tasks sorted by total time, with parent context for subtasks.
        """
        with self.connection() as conn:
            rows = conn.execute(
                f"""SELECT
                       t.id,
                       t.name,
                       t.parent_id,
                       r.short_name as repo_name,
                       SUM(s.duration_seconds) as total_seconds,
                       COUNT(*) as session_count
                   FROM sessions s
                   JOIN tasks t ON s.task_id = t.id
                   LEFT JOIN repositories r ON t.repo_id = r.id
                   WHERE s.start_time >= now() - INTERVAL '{days}' DAY
                   GROUP BY t.id, t.name, t.parent_id, r.short_name
                   ORDER BY total_seconds DESC
                   LIMIT {limit}"""
            ).fetchall()

            # Get parent task names
            parent_ids = set(row[2] for row in rows if row[2] is not None)
            parent_names = {}
            if parent_ids:
                placeholders = ",".join(["?"] * len(parent_ids))
                parent_rows = conn.execute(
                    f"SELECT id, name FROM tasks WHERE id IN ({placeholders})",
                    list(parent_ids),
                ).fetchall()
                parent_names = {r[0]: r[1] for r in parent_rows}

            # Calculate max for percentage bars
            max_seconds = max((row[4] for row in rows), default=1)

            result = []
            for row in rows:
                task_name = row[1]
                parent_id = row[2]

                # Format as "parent / subtask" if has parent
                if parent_id and parent_id in parent_names:
                    display_name = f"{parent_names[parent_id]} / {task_name}"
                else:
                    display_name = task_name

                total_seconds = int(row[4])
                result.append(
                    {
                        "id": row[0],
                        "name": task_name,
                        "display_name": display_name,
                        "parent_id": parent_id,
                        "repo_name": row[3],
                        "total_seconds": total_seconds,
                        "total_formatted": self.format_duration(total_seconds),
                        "session_count": int(row[5]),
                        "percentage": round((total_seconds / max_seconds) * 100, 1)
                        if max_seconds
                        else 0,
                    }
                )

            return result

    def get_trend_comparison(self, days: int = 7) -> dict:
        """Compare this period vs previous period for trend analysis.

        Compares metrics from the last N days against the previous N days.
        Returns change percentages and direction indicators.

        Args:
            days: Number of days for each period (e.g., 7 = compare last 7 vs previous 7)

        Returns:
            Dictionary with time, sessions, and tasks trends:
            {
                'time': {'current': X, 'previous': Y, 'change_pct': Z, 'direction': 'up'|'down'|'neutral'},
                'sessions': {...},
                'tasks': {...}
            }
        """
        with self.connection() as conn:
            # Get current period stats (last N days)
            current = conn.execute(
                f"""SELECT
                       COALESCE(SUM(duration_seconds), 0) as total_seconds,
                       COUNT(*) as session_count,
                       COUNT(DISTINCT task_id) as task_count
                   FROM sessions
                   WHERE start_time >= now() - INTERVAL '{days}' DAY"""
            ).fetchone()

            # Get previous period stats (N days before that)
            previous = conn.execute(
                f"""SELECT
                       COALESCE(SUM(duration_seconds), 0) as total_seconds,
                       COUNT(*) as session_count,
                       COUNT(DISTINCT task_id) as task_count
                   FROM sessions
                   WHERE start_time >= now() - INTERVAL '{days * 2}' DAY
                     AND start_time < now() - INTERVAL '{days}' DAY"""
            ).fetchone()

            def calc_trend(current_val: int, previous_val: int) -> dict:
                """Calculate trend metrics for a single value."""
                current_val = int(current_val) if current_val else 0
                previous_val = int(previous_val) if previous_val else 0

                if previous_val == 0:
                    if current_val == 0:
                        change_pct = 0
                        direction = "neutral"
                    else:
                        change_pct = 100
                        direction = "up"
                else:
                    change_pct = round(
                        ((current_val - previous_val) / previous_val) * 100, 1
                    )
                    if change_pct > 5:
                        direction = "up"
                    elif change_pct < -5:
                        direction = "down"
                    else:
                        direction = "neutral"

                return {
                    "current": current_val,
                    "previous": previous_val,
                    "change_pct": change_pct,
                    "direction": direction,
                }

            return {
                "time": {
                    **calc_trend(current[0], previous[0]),
                    "current_formatted": self.format_duration(
                        int(current[0]) if current[0] else 0
                    ),
                    "previous_formatted": self.format_duration(
                        int(previous[0]) if previous[0] else 0
                    ),
                },
                "sessions": calc_trend(current[1], previous[1]),
                "tasks": calc_trend(current[2], previous[2]),
                "period_days": days,
            }

    # =========================================================================
    # Feed Source Queries
    # =========================================================================

    def get_feed_sources(self) -> list[dict]:
        """Get all feed sources with item counts and folder info."""
        rows, columns = self._fetch_all(
            """SELECT s.*,
                      COUNT(i.id) as item_count,
                      f.name as folder_name,
                      f.color as folder_color
               FROM feed_sources s
               LEFT JOIN feed_items i ON i.source_id = s.id
               LEFT JOIN feed_folders f ON f.id = s.folder_id
               GROUP BY s.id, s.url, s.title, s.icon_url, s.source_type,
                        s.folder_id, s.added_at, s.last_fetched,
                        f.name, f.color
               ORDER BY s.added_at DESC"""
        )
        return [dict(zip(columns, row)) for row in rows]

    def get_feed_source(self, source_id: str) -> dict | None:
        """Get a single feed source by ID."""
        row, columns = self._fetch_one(
            "SELECT * FROM feed_sources WHERE id = ?", (source_id,)
        )
        return dict(zip(columns, row)) if row else None

    def get_feed_source_by_url(self, url: str) -> dict | None:
        """Get a feed source by URL."""
        row, columns = self._fetch_one(
            "SELECT * FROM feed_sources WHERE url = ?", (url,)
        )
        return dict(zip(columns, row)) if row else None

    def add_feed_source(
        self,
        source_id: str,
        url: str,
        title: str | None,
        source_type: str,
        icon_url: str | None = None,
    ) -> None:
        """Add a new feed source."""
        with self.connection() as conn:
            conn.execute(
                """INSERT INTO feed_sources
                   (id, url, title, source_type, icon_url, added_at, last_fetched)
                   VALUES (?, ?, ?, ?, ?, now(), now())""",
                (source_id, url, title, source_type, icon_url),
            )

    def add_feed_items(self, items: list[dict]) -> None:
        """Add multiple feed items.

        Each item dict should have:
            id, source_id, guid, title, description, image_url, link, published
        """
        if not items:
            return

        with self.connection() as conn:
            for item in items:
                conn.execute(
                    """INSERT INTO feed_items
                       (id, source_id, guid, title, description, image_url,
                        link, published, fetched_at, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, now(), 'unread')
                       ON CONFLICT (id) DO NOTHING""",
                    (
                        item["id"],
                        item["source_id"],
                        item["guid"],
                        item["title"],
                        item["description"],
                        item["image_url"],
                        item["link"],
                        item["published"],
                    ),
                )

    def delete_feed_source(self, source_id: str) -> bool:
        """Delete a feed source and all its items.

        Returns True if source existed and was deleted.
        """
        with self.connection() as conn:
            # Check if source exists
            existing = conn.execute(
                "SELECT id FROM feed_sources WHERE id = ?", (source_id,)
            ).fetchone()

            if not existing:
                return False

            # Delete items first (referential integrity)
            conn.execute("DELETE FROM feed_items WHERE source_id = ?", (source_id,))
            # Delete source
            conn.execute("DELETE FROM feed_sources WHERE id = ?", (source_id,))
            return True

    def get_feed_item_count(self, source_id: str) -> int:
        """Get the number of items for a feed source."""
        with self.connection() as conn:
            result = conn.execute(
                "SELECT COUNT(*) FROM feed_items WHERE source_id = ?", (source_id,)
            ).fetchone()
            return result[0] if result else 0

    def get_feed_item(self, item_id: str) -> dict | None:
        """Get a single feed item by ID.

        Returns:
            dict with item data including link, or None if not found.
        """
        with self.connection() as conn:
            result = conn.execute(
                "SELECT id, source_id, title, link, published, image_url, read_at, summary, summary_generated_at "
                "FROM feed_items WHERE id = ?",
                (item_id,),
            )
            columns = [d[0] for d in result.description]
            row = result.fetchone()
            if not row:
                return None
            return dict(zip(columns, row))

    def update_feed_item_summary(self, item_id: str, summary: str) -> bool:
        """Update the summary for a feed item.

        Args:
            item_id: The feed item ID
            summary: The generated summary text

        Returns:
            True if updated successfully, False if item not found.
        """
        with self.connection() as conn:
            result = conn.execute(
                """UPDATE feed_items
                   SET summary = ?, summary_generated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (summary, item_id),
            )
            return result.rowcount > 0

    def get_feed_items(
        self,
        source_id: str | None = None,
        status: str | None = None,
        sort: str = "date",
        order: str = "desc",
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        """Get feed items with filtering and sorting.

        Args:
            source_id: Filter by source ID (optional)
            status: Filter by status - 'read' or 'unread' (optional)
            sort: Sort by 'date' (published) or 'source' (source title, then date)
            order: Sort direction - 'desc' (default) or 'asc'
            limit: Maximum items to return
            offset: Pagination offset

        Returns:
            tuple[list[dict], int]: (items with source title, total count)

        Raises:
            ValueError: If sort or order parameter is invalid
        """
        # Validate sort/order at DB layer - these go into SQL directly
        ALLOWED_SORTS = {"date", "source"}
        ALLOWED_ORDERS = {"asc", "desc"}

        if sort not in ALLOWED_SORTS:
            raise ValueError(f"sort must be one of {ALLOWED_SORTS}")
        if order not in ALLOWED_ORDERS:
            raise ValueError(f"order must be one of {ALLOWED_ORDERS}")

        # Build WHERE clause dynamically
        conditions = []
        params = []

        if source_id:
            conditions.append("i.source_id = ?")
            params.append(source_id)

        if status:
            conditions.append("i.status = ?")
            params.append(status)

        # Note: DuckDB has a quirk where parameterized queries behave differently
        # with WHERE TRUE vs actual conditions. Use empty string to skip WHERE.
        if conditions:
            where_clause = "WHERE " + " AND ".join(conditions)
        else:
            where_clause = ""

        # Determine sort order
        direction = "DESC" if order == "desc" else "ASC"
        nulls = "LAST" if order == "desc" else "FIRST"

        if sort == "source":
            order_clause = f"s.title {direction}, i.published DESC NULLS LAST"
        else:  # default: date
            order_clause = f"i.published {direction} NULLS {nulls}"

        # Convert params to tuple for immutability
        params_tuple = tuple(params)

        # Use window function to get count in single query (more efficient)
        items_query = f"""
            SELECT
                i.*,
                s.title as source_title,
                s.icon_url as source_icon,
                COUNT(*) OVER () as _total_count
            FROM feed_items i
            JOIN feed_sources s ON s.id = i.source_id
            {where_clause}
            ORDER BY {order_clause}
            LIMIT ? OFFSET ?
        """

        # Run query
        with self.connection() as conn:
            items_params = (*params_tuple, limit, offset)
            result = conn.execute(items_query, items_params)
            columns = [desc[0] for desc in result.description]
            rows = result.fetchall()

        # Extract total from first row, remove _total_count from items
        if rows:
            total_idx = columns.index("_total_count")
            total = rows[0][total_idx]
            # Build items without _total_count column
            items = []
            for row in rows:
                item = {}
                for i, col in enumerate(columns):
                    if col != "_total_count":
                        item[col] = row[i]
                items.append(item)
        else:
            total = 0
            items = []

        return items, total

    def update_last_fetched(self, source_id: str) -> None:
        """Update the last_fetched timestamp for a source."""
        with self.connection() as conn:
            conn.execute(
                "UPDATE feed_sources SET last_fetched = now() WHERE id = ?",
                (source_id,),
            )

    def update_item_status(
        self, item_id: str, status: str, read_at: datetime | None = None
    ) -> bool:
        """Update status of a single feed item.

        Args:
            item_id: The feed item ID
            status: New status ('read' or 'unread')
            read_at: Timestamp when read (uses now() for 'read' if not provided)

        Returns:
            True if item existed and was updated, False if item not found.
        """
        with self.connection() as conn:
            # Use RETURNING to verify the row existed
            # DuckDB's now() handles timezone correctly based on connection settings
            if status == "read" and read_at is None:
                result = conn.execute(
                    """UPDATE feed_items
                       SET status = ?, read_at = now()
                       WHERE id = ?
                       RETURNING id""",
                    (status, item_id),
                ).fetchone()
            else:
                result = conn.execute(
                    """UPDATE feed_items
                       SET status = ?, read_at = ?
                       WHERE id = ?
                       RETURNING id""",
                    (status, read_at, item_id),
                ).fetchone()

            return result is not None

    def bulk_update_item_status(
        self, item_ids: list[str], status: str, read_at: datetime | None = None
    ) -> list[str]:
        """Update status of multiple feed items.

        Uses a single UPDATE with RETURNING to efficiently update all items
        and return which ones actually existed.

        Args:
            item_ids: List of feed item IDs to update
            status: New status ('read' or 'unread')
            read_at: Timestamp when read (uses now() for 'read' if not provided)

        Returns:
            List of item IDs that were actually updated (existed in DB).
        """
        if not item_ids:
            return []

        with self.connection() as conn:
            # DuckDB supports parameterized IN clauses with individual values
            placeholders = ",".join(["?"] * len(item_ids))

            if status == "read" and read_at is None:
                rows = conn.execute(
                    f"""UPDATE feed_items
                        SET status = ?, read_at = now()
                        WHERE id IN ({placeholders})
                        RETURNING id""",
                    (status, *item_ids),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"""UPDATE feed_items
                        SET status = ?, read_at = ?
                        WHERE id IN ({placeholders})
                        RETURNING id""",
                    (status, read_at, *item_ids),
                ).fetchall()

            return [row[0] for row in rows]

    # =========================================================================
    # Feed Folder Methods
    # =========================================================================

    def get_feed_folders(self) -> list[dict]:
        """Get all feed folders with source counts."""
        rows, columns = self._fetch_all(
            """SELECT f.*,
                      COUNT(s.id) as source_count
               FROM feed_folders f
               LEFT JOIN feed_sources s ON s.folder_id = f.id
               GROUP BY f.id, f.name, f.color, f.created_at
               ORDER BY f.name ASC"""
        )
        return [dict(zip(columns, row)) for row in rows]

    def get_feed_folder(self, folder_id: str) -> dict | None:
        """Get a single feed folder by ID."""
        row, columns = self._fetch_one(
            "SELECT * FROM feed_folders WHERE id = ?", (folder_id,)
        )
        return dict(zip(columns, row)) if row else None

    def get_feed_folder_by_name(self, name: str) -> dict | None:
        """Get a feed folder by name (case-insensitive)."""
        row, columns = self._fetch_one(
            "SELECT * FROM feed_folders WHERE LOWER(name) = LOWER(?)", (name,)
        )
        return dict(zip(columns, row)) if row else None

    def add_feed_folder(
        self, folder_id: str, name: str, color: str | None = None
    ) -> dict:
        """Add a new feed folder.

        Returns the created folder as a dict.
        """
        with self.connection() as conn:
            conn.execute(
                """INSERT INTO feed_folders (id, name, color, created_at)
                   VALUES (?, ?, ?, now())""",
                (folder_id, name, color),
            )
            # Fetch the created row
            row = conn.execute(
                "SELECT * FROM feed_folders WHERE id = ?", (folder_id,)
            ).fetchone()
            columns = [d[0] for d in conn.description]
            return dict(zip(columns, row))

    def update_feed_folder(
        self, folder_id: str, name: str | None = None, color: str | None = None
    ) -> dict | None:
        """Update a feed folder.

        Only updates provided fields. Uses RETURNING to verify row exists.
        Returns updated folder or None if not found.
        """
        with self.connection() as conn:
            # Build dynamic update
            updates = []
            params = []
            if name is not None:
                updates.append("name = ?")
                params.append(name)
            if color is not None:
                updates.append("color = ?")
                params.append(color)

            if not updates:
                # Nothing to update, just return current state
                return self.get_feed_folder(folder_id)

            params.append(folder_id)
            row = conn.execute(
                f"""UPDATE feed_folders
                    SET {", ".join(updates)}
                    WHERE id = ?
                    RETURNING *""",
                tuple(params),
            ).fetchone()

            if not row:
                return None
            columns = [d[0] for d in conn.description]
            return dict(zip(columns, row))

    def delete_feed_folder(self, folder_id: str) -> bool:
        """Delete a feed folder.

        Sources in this folder have their folder_id set to NULL.
        Returns True if folder existed and was deleted.
        """
        with self.connection() as conn:
            # Check if folder exists
            existing = conn.execute(
                "SELECT id FROM feed_folders WHERE id = ?", (folder_id,)
            ).fetchone()

            if not existing:
                return False

            # Orphan sources (set folder_id to NULL)
            conn.execute(
                "UPDATE feed_sources SET folder_id = NULL WHERE folder_id = ?",
                (folder_id,),
            )
            # Delete folder
            conn.execute("DELETE FROM feed_folders WHERE id = ?", (folder_id,))
            return True

    def assign_source_folder(
        self, source_id: str, folder_id: str | None
    ) -> dict | None:
        """Assign a feed source to a folder.

        Args:
            source_id: The source ID
            folder_id: The folder ID, or None to remove from folder

        Returns:
            Updated source dict, or None if source not found.
        """
        with self.connection() as conn:
            row = conn.execute(
                """UPDATE feed_sources
                   SET folder_id = ?
                   WHERE id = ?
                   RETURNING *""",
                (folder_id, source_id),
            ).fetchone()

            if not row:
                return None
            columns = [d[0] for d in conn.description]
            return dict(zip(columns, row))

    # =========================================================================
    # Plan Methods (Parallel Agent Orchestration)
    # =========================================================================

    def create_plan(
        self, name: str, task_id: int | None = None, metadata: dict | None = None
    ) -> int:
        """Create a new execution plan.

        Args:
            name: Name/description of the plan
            task_id: Optional associated task ID
            metadata: Optional JSON-serializable metadata dict

        Returns:
            The created plan's ID
        """
        metadata_json = json.dumps(metadata) if metadata else None

        with self.connection() as conn:
            # Get next ID from sequence
            result = conn.execute("SELECT nextval('plans_id_seq')").fetchone()
            plan_id = result[0]

            conn.execute(
                """INSERT INTO plans (id, name, task_id, status, metadata, created_at)
                   VALUES (?, ?, ?, 'draft', ?, CURRENT_TIMESTAMP)""",
                (plan_id, name, task_id, metadata_json),
            )
            return plan_id

    def get_plan(self, plan_id: int) -> dict | None:
        """Get a plan by ID.

        Args:
            plan_id: The plan ID

        Returns:
            Plan dict or None if not found
        """
        row, columns = self._fetch_one("SELECT * FROM plans WHERE id = ?", (plan_id,))
        if not row:
            return None

        plan = Plan.from_row(row, columns)
        return plan.to_dict()

    def list_plans(
        self, status: str | None = None, task_id: int | None = None, limit: int = 50
    ) -> list[dict]:
        """List execution plans.

        Args:
            status: Filter by status (draft, pending, running, completed, failed)
            task_id: Filter by associated task ID
            limit: Maximum number of plans to return

        Returns:
            List of plan dicts ordered by created_at DESC
        """
        query = "SELECT * FROM plans WHERE 1=1"
        params: list = []

        if status:
            query += " AND status = ?"
            params.append(status)

        if task_id is not None:
            query += " AND task_id = ?"
            params.append(task_id)

        query += f" ORDER BY created_at DESC LIMIT {limit}"

        rows, columns = self._fetch_all(query, tuple(params))
        return [Plan.from_row(row, columns).to_dict() for row in rows]

    def update_plan(
        self,
        plan_id: int,
        name: str | None = None,
        status: str | None = None,
        total_agents: int | None = None,
        completed_agents: int | None = None,
        failed_agents: int | None = None,
        metadata: dict | None = None,
        merge_metadata: bool = False,
    ) -> dict | None:
        """Update an execution plan.

        Args:
            plan_id: The plan ID
            name: New plan name
            status: New status
            total_agents: Total number of agents in the plan
            completed_agents: Number of completed agents
            failed_agents: Number of failed agents
            metadata: Updated metadata dict
            merge_metadata: If True, merge with existing metadata; if False, replace

        Returns:
            Updated plan dict or None if not found
        """
        updates = []
        params: list = []

        if name is not None:
            updates.append("name = ?")
            params.append(name)

        if status is not None:
            updates.append("status = ?")
            params.append(status)
            # Set started_at when transitioning to running
            if status == "running":
                updates.append("started_at = CURRENT_TIMESTAMP")
            # Set completed_at when transitioning to completed or failed
            elif status in ("completed", "failed"):
                updates.append("completed_at = CURRENT_TIMESTAMP")

        if total_agents is not None:
            updates.append("total_agents = ?")
            params.append(total_agents)

        if completed_agents is not None:
            updates.append("completed_agents = ?")
            params.append(completed_agents)

        if failed_agents is not None:
            updates.append("failed_agents = ?")
            params.append(failed_agents)

        if metadata is not None:
            if merge_metadata:
                # Fetch existing metadata and merge
                existing = self.get_plan(plan_id)
                if existing and existing.get("metadata"):
                    merged = {**existing["metadata"], **metadata}
                else:
                    merged = metadata
                updates.append("metadata = ?")
                params.append(json.dumps(merged))
            else:
                updates.append("metadata = ?")
                params.append(json.dumps(metadata))

        if not updates:
            # Nothing to update, just return current plan
            return self.get_plan(plan_id)

        params.append(plan_id)

        with self.connection() as conn:
            conn.execute(
                f"""UPDATE plans
                    SET {", ".join(updates)}
                    WHERE id = ?""",
                tuple(params),
            )

        return self.get_plan(plan_id)

    def delete_plan(self, plan_id: int) -> bool:
        """Delete a plan by ID.

        Args:
            plan_id: The plan ID

        Returns:
            True if plan was deleted, False if not found
        """
        with self.connection() as conn:
            existing = conn.execute(
                "SELECT id FROM plans WHERE id = ?", (plan_id,)
            ).fetchone()

            if not existing:
                return False

            conn.execute("DELETE FROM plans WHERE id = ?", (plan_id,))
            return True

    # =========================================================================
    # Agent Execution Methods (Individual agent runs within a plan)
    # =========================================================================

    def add_agent_execution(
        self,
        plan_id: int,
        agent_id: str,
        agent_name: str | None = None,
        prompt: str | None = None,
        max_attempts: int = 3,
        metadata: dict | None = None,
    ) -> int:
        """Create a new agent execution record.

        Args:
            plan_id: The plan this agent belongs to
            agent_id: String identifier like "01", "02" for ordering
            agent_name: Human-readable name for the agent
            prompt: The prompt/task for this agent
            max_attempts: Maximum retry attempts (default 3)
            metadata: Optional JSON-serializable metadata dict

        Returns:
            The created execution's ID
        """
        metadata_json = json.dumps(metadata) if metadata else None

        with self.connection() as conn:
            # Get next ID from sequence
            result = conn.execute(
                "SELECT nextval('agent_executions_id_seq')"
            ).fetchone()
            execution_id = result[0]

            conn.execute(
                """INSERT INTO agent_executions
                   (id, plan_id, agent_id, agent_name, status, prompt, max_attempts, metadata)
                   VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)""",
                (
                    execution_id,
                    plan_id,
                    agent_id,
                    agent_name,
                    prompt,
                    max_attempts,
                    metadata_json,
                ),
            )
            return execution_id

    def get_agent_execution(self, execution_id: int) -> dict | None:
        """Get an agent execution by ID.

        Args:
            execution_id: The execution ID

        Returns:
            Execution dict or None if not found
        """
        row, columns = self._fetch_one(
            "SELECT * FROM agent_executions WHERE id = ?", (execution_id,)
        )
        if not row:
            return None

        execution = AgentExecution.from_row(row, columns)
        return execution.to_dict()

    def get_plan_agents(self, plan_id: int) -> list[dict]:
        """Get all agent executions for a plan.

        Args:
            plan_id: The plan ID

        Returns:
            List of execution dicts, ordered by agent_id
        """
        with self.connection() as conn:
            result = conn.execute(
                """SELECT * FROM agent_executions
                   WHERE plan_id = ?
                   ORDER BY agent_id""",
                (plan_id,),
            )
            columns = [desc[0] for desc in result.description]
            rows = result.fetchall()

        return [AgentExecution.from_row(row, columns).to_dict() for row in rows]

    def update_agent_execution(
        self,
        execution_id: int,
        status: str | None = None,
        result: str | None = None,
        error_message: str | None = None,
        attempt_count: int | None = None,
        duration_ms: int | None = None,
        metadata: dict | None = None,
    ) -> dict | None:
        """Update an agent execution.

        Args:
            execution_id: The execution ID
            status: New status (pending, blocked, running, completed, failed)
            result: Result text from agent execution
            error_message: Error message if failed
            attempt_count: Current attempt count
            duration_ms: Execution duration in milliseconds
            metadata: Updated metadata dict (replaces existing)

        Returns:
            Updated execution dict or None if not found
        """
        updates = []
        params: list = []

        if status is not None:
            updates.append("status = ?")
            params.append(status)
            # Set started_at when transitioning to running
            if status == "running":
                updates.append("started_at = CURRENT_TIMESTAMP")
            # Set completed_at when transitioning to completed or failed
            elif status in ("completed", "failed"):
                updates.append("completed_at = CURRENT_TIMESTAMP")

        if result is not None:
            updates.append("result = ?")
            params.append(result)

        if error_message is not None:
            updates.append("error_message = ?")
            params.append(error_message)

        if attempt_count is not None:
            updates.append("attempt_count = ?")
            params.append(attempt_count)

        if duration_ms is not None:
            updates.append("duration_ms = ?")
            params.append(duration_ms)

        if metadata is not None:
            updates.append("metadata = ?")
            params.append(json.dumps(metadata))

        if not updates:
            # Nothing to update, just return current execution
            return self.get_agent_execution(execution_id)

        params.append(execution_id)

        with self.connection() as conn:
            conn.execute(
                f"""UPDATE agent_executions
                    SET {", ".join(updates)}
                    WHERE id = ?""",
                tuple(params),
            )

        return self.get_agent_execution(execution_id)

    def update_agent_execution_by_agent_id(
        self,
        plan_id: int,
        agent_id: str,
        status: str | None = None,
        output: dict | None = None,
        error: str | None = None,
        increment_attempt: bool = False,
    ) -> dict | None:
        """Update an agent execution by plan_id and agent_id.

        This is a convenience wrapper around update_agent_execution that looks
        up the execution_id from the plan_id and agent_id.

        Args:
            plan_id: The plan ID
            agent_id: The agent identifier (e.g., '01', '02')
            status: New status (pending, blocked, running, completed, failed)
            output: Output dict to store (will be JSON serialized)
            error: Error message if failed
            increment_attempt: Whether to increment the attempt counter

        Returns:
            Updated execution dict or None if not found
        """
        # Find the execution by plan_id and agent_id
        agents = self.get_plan_agents(plan_id)
        execution = next((a for a in agents if a["agent_id"] == agent_id), None)

        if not execution:
            return None

        execution_id = execution["id"]

        # Handle increment_attempt
        attempt_count = None
        if increment_attempt:
            attempt_count = (execution.get("attempt_count") or 0) + 1

        # Convert output dict to result string
        result = None
        if output:
            result = json.dumps(output)

        return self.update_agent_execution(
            execution_id=execution_id,
            status=status,
            result=result,
            error_message=error,
            attempt_count=attempt_count,
        )

    # =========================================================================
    # Agent Dependency Methods (DAG for parallel orchestration)
    # =========================================================================

    def add_agent_dependency(self, plan_id: int, agent_id: str, depends_on: str) -> int:
        """Add a dependency edge between agents.

        Args:
            plan_id: The plan ID
            agent_id: The agent that has the dependency
            depends_on: The agent that must complete first

        Returns:
            The created dependency's ID

        Raises:
            ValueError: If agent depends on itself
        """
        if agent_id == depends_on:
            raise ValueError(f"Agent '{agent_id}' cannot depend on itself")

        with self.connection() as conn:
            # Get next ID from sequence
            result = conn.execute(
                "SELECT nextval('agent_dependencies_id_seq')"
            ).fetchone()
            dep_id = result[0]

            conn.execute(
                """INSERT INTO agent_dependencies (id, plan_id, agent_id, depends_on)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT (plan_id, agent_id, depends_on) DO NOTHING""",
                (dep_id, plan_id, agent_id, depends_on),
            )
            return dep_id

    def get_agent_dependencies(self, plan_id: int, agent_id: str) -> list[str]:
        """Get what agents this agent depends on.

        Args:
            plan_id: The plan ID
            agent_id: The agent to query

        Returns:
            List of agent_ids that this agent depends on
        """
        rows, _ = self._fetch_all(
            """SELECT depends_on FROM agent_dependencies
               WHERE plan_id = ? AND agent_id = ?
               ORDER BY depends_on""",
            (plan_id, agent_id),
        )
        return [row[0] for row in rows]

    def get_agent_dependents(self, plan_id: int, agent_id: str) -> list[str]:
        """Get what agents depend on this agent.

        Args:
            plan_id: The plan ID
            agent_id: The agent to query

        Returns:
            List of agent_ids that depend on this agent
        """
        rows, _ = self._fetch_all(
            """SELECT agent_id FROM agent_dependencies
               WHERE plan_id = ? AND depends_on = ?
               ORDER BY agent_id""",
            (plan_id, agent_id),
        )
        return [row[0] for row in rows]

    def get_plan_dependency_graph(self, plan_id: int) -> dict[str, list[str]]:
        """Get the full dependency DAG for a plan.

        Args:
            plan_id: The plan ID

        Returns:
            Adjacency list where keys are agent_ids and values are
            lists of agent_ids they depend on
        """
        rows, _ = self._fetch_all(
            """SELECT agent_id, depends_on FROM agent_dependencies
               WHERE plan_id = ?
               ORDER BY agent_id, depends_on""",
            (plan_id,),
        )

        # Build adjacency list
        graph: dict[str, list[str]] = {}
        for row in rows:
            agent_id, depends_on = row[0], row[1]
            if agent_id not in graph:
                graph[agent_id] = []
            graph[agent_id].append(depends_on)

        return graph

    def delete_agent_dependencies(self, plan_id: int) -> int:
        """Delete all dependencies for a plan.

        Args:
            plan_id: The plan ID

        Returns:
            Number of dependencies deleted
        """
        with self.connection() as conn:
            # DuckDB doesn't reliably return rowcount, so count first
            count = conn.execute(
                "SELECT COUNT(*) FROM agent_dependencies WHERE plan_id = ?", (plan_id,)
            ).fetchone()[0]

            conn.execute("DELETE FROM agent_dependencies WHERE plan_id = ?", (plan_id,))
            return count

    def delete_agent_execution(self, plan_id: int, agent_id: str) -> bool:
        """Delete an agent execution and its dependencies from a plan.

        Args:
            plan_id: The plan ID
            agent_id: The agent ID to delete

        Returns:
            True if agent was deleted, False if not found
        """
        with self.connection() as conn:
            # Check if agent exists
            result = conn.execute(
                """SELECT id FROM agent_executions
                   WHERE plan_id = ? AND agent_id = ?""",
                (plan_id, agent_id),
            ).fetchone()

            if not result:
                return False

            # Delete dependencies where this agent is the target
            conn.execute(
                """DELETE FROM agent_dependencies
                   WHERE plan_id = ? AND agent_id = ?""",
                (plan_id, agent_id),
            )

            # Delete dependencies where this agent is a dependency
            conn.execute(
                """DELETE FROM agent_dependencies
                   WHERE plan_id = ? AND depends_on = ?""",
                (plan_id, agent_id),
            )

            # Delete the agent execution
            conn.execute(
                """DELETE FROM agent_executions
                   WHERE plan_id = ? AND agent_id = ?""",
                (plan_id, agent_id),
            )

            return True

    # =========================================================================
    # Utility Functions
    # =========================================================================

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
    def format_time_ago(timestamp: datetime | str | None) -> str:
        """Format timestamp as relative time ago."""
        if not timestamp:
            return "never"

        try:
            if isinstance(timestamp, str):
                dt = datetime.fromisoformat(timestamp)
            else:
                dt = timestamp

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


# =============================================================================
# Claude Session Cache (SQLite)
# =============================================================================


class ClaudeSessionCache:
    """Cache for parsed JSONL session data.

    Uses SQLite for persistence since JSONL parsing is expensive.
    Cache is invalidated based on file mtime.
    """

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or SQLITE_PATH
        self._ensure_table()

    def _ensure_table(self):
        """Create cache table if it doesn't exist."""
        import sqlite3

        if not self.db_path.exists():
            return

        conn = sqlite3.connect(str(self.db_path))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS claude_session_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT UNIQUE NOT NULL,
                file_path TEXT NOT NULL,
                date TEXT NOT NULL,
                hour INTEGER NOT NULL,
                cwd TEXT,
                git_branch TEXT,
                project_path TEXT,
                message_count INTEGER DEFAULT 0,
                tool_call_count INTEGER DEFAULT 0,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                duration_seconds INTEGER DEFAULT 0,
                first_event_time TEXT,
                last_event_time TEXT,
                file_mtime REAL NOT NULL,
                cached_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_claude_session_date ON claude_session_cache(date)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_claude_session_hour ON claude_session_cache(date, hour)"
        )
        conn.commit()
        conn.close()

    def get_cached_session(self, session_id: str, file_mtime: float) -> dict | None:
        """Get cached session if still valid (mtime matches)."""
        import sqlite3

        if not self.db_path.exists():
            return None

        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM claude_session_cache WHERE session_id = ? AND file_mtime = ?",
            (session_id, file_mtime),
        ).fetchone()
        conn.close()

        return dict(row) if row else None

    def cache_session(self, session_data: dict, file_path: str, file_mtime: float):
        """Cache parsed session data."""
        import sqlite3

        if not self.db_path.exists():
            return

        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            """
            INSERT OR REPLACE INTO claude_session_cache
            (session_id, file_path, date, hour, cwd, git_branch, project_path,
             message_count, tool_call_count, input_tokens, output_tokens, duration_seconds,
             first_event_time, last_event_time, file_mtime, cached_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                session_data["session_id"],
                file_path,
                session_data.get("date"),
                session_data.get("hour", 0),
                session_data.get("cwd"),
                session_data.get("git_branch"),
                session_data.get("project_path"),
                session_data.get("message_count", 0),
                session_data.get("tool_call_count", 0),
                session_data.get("input_tokens", 0),
                session_data.get("output_tokens", 0),
                session_data.get("duration_seconds", 0),
                session_data.get("first_event_time"),
                session_data.get("last_event_time"),
                file_mtime,
                datetime.now().isoformat(),
            ),
        )
        conn.commit()
        conn.close()

    def get_hourly_activity(self, date: str) -> list[dict]:
        """Get cached hourly activity for a date."""
        import sqlite3

        if not self.db_path.exists():
            return []

        conn = sqlite3.connect(str(self.db_path))
        rows = conn.execute(
            """
            SELECT hour,
                   SUM(message_count) as claude_messages,
                   SUM(tool_call_count) as claude_tool_calls,
                   SUM(input_tokens + output_tokens) as claude_tokens,
                   SUM(duration_seconds) as claude_seconds,
                   COUNT(*) as session_count
            FROM claude_session_cache
            WHERE date = ?
            GROUP BY hour
            ORDER BY hour
        """,
            (date,),
        ).fetchall()
        conn.close()

        return [
            {
                "hour": row[0],
                "claude_messages": row[1],
                "claude_tool_calls": row[2],
                "claude_tokens": row[3],
                "claude_seconds": row[4] or 0,
                "session_count": row[5],
            }
            for row in rows
        ]

    def get_daily_activity_with_duration(self, days: int = 7) -> list[dict]:
        """Get cached daily activity with duration for the last N days."""
        import sqlite3

        if not self.db_path.exists():
            return []

        conn = sqlite3.connect(str(self.db_path))
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = conn.execute(
            """
            SELECT date,
                   SUM(message_count) as claude_messages,
                   SUM(tool_call_count) as claude_tool_calls,
                   SUM(input_tokens + output_tokens) as claude_tokens,
                   SUM(duration_seconds) as claude_seconds,
                   COUNT(*) as session_count
            FROM claude_session_cache
            WHERE date >= ?
            GROUP BY date
            ORDER BY date DESC
        """,
            (cutoff,),
        ).fetchall()
        conn.close()

        return [
            {
                "date": row[0],
                "claude_messages": row[1],
                "claude_tool_calls": row[2],
                "claude_tokens": row[3],
                "session_count": row[4],
            }
            for row in rows
        ]

    def get_daily_activity(self, days: int = 7) -> list[dict]:
        """Get cached daily activity for the last N days."""
        import sqlite3

        if not self.db_path.exists():
            return []

        conn = sqlite3.connect(str(self.db_path))
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = conn.execute(
            """
            SELECT date,
                   SUM(message_count) as claude_messages,
                   SUM(tool_call_count) as claude_tool_calls,
                   SUM(input_tokens + output_tokens) as claude_tokens,
                   SUM(duration_seconds) as claude_seconds,
                   COUNT(*) as session_count
            FROM claude_session_cache
            WHERE date >= ?
            GROUP BY date
            ORDER BY date DESC
        """,
            (cutoff,),
        ).fetchall()
        conn.close()

        return [
            {
                "date": row[0],
                "claude_messages": row[1],
                "claude_tool_calls": row[2],
                "claude_tokens": row[3],
                "claude_seconds": row[4] or 0,
                "session_count": row[5],
            }
            for row in rows
        ]

    def clear_old_entries(self, days_to_keep: int = 30):
        """Remove cache entries older than N days."""
        import sqlite3

        if not self.db_path.exists():
            return

        cutoff = (datetime.now() - timedelta(days=days_to_keep)).strftime("%Y-%m-%d")
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("DELETE FROM claude_session_cache WHERE date < ?", (cutoff,))
        conn.commit()
        conn.close()

    def get_untracked_sessions(self, date: str) -> list[dict]:
        """Get Claude Code sessions that have no corresponding orbit session.

        Returns JSONL-tracked sessions that aren't covered by orbit heartbeat
        sessions, filtering out very short sessions (< 60s).
        """
        import sqlite3

        if not self.db_path.exists():
            return []

        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT c.session_id, c.cwd, c.project_path,
                   c.duration_seconds, c.message_count, c.tool_call_count,
                   c.first_event_time, c.last_event_time
            FROM claude_session_cache c
            LEFT JOIN sessions s ON c.session_id = s.session_id
            WHERE c.date = ?
              AND s.id IS NULL
              AND c.duration_seconds > 60
            ORDER BY c.first_event_time
        """,
            (date,),
        ).fetchall()
        conn.close()

        return [dict(row) for row in rows]


def group_untracked_by_cwd(sessions: list[dict]) -> list[dict]:
    """Group untracked sessions by working directory into task-like entries."""
    from collections import defaultdict

    groups: dict[str, list[dict]] = defaultdict(list)
    for s in sessions:
        key = s.get("cwd") or s.get("project_path") or "unknown"
        groups[key].append(s)

    result = []
    for cwd, group in groups.items():
        p = Path(cwd) if cwd != "unknown" else None
        dir_name = f"{p.parent.name}/{p.name}" if p and p.parent.name else (p.name if p else "unknown")
        total_seconds = sum(s.get("duration_seconds", 0) for s in group)
        total_messages = sum(s.get("message_count", 0) for s in group)

        timeline_sessions = []
        for s in group:
            if s.get("first_event_time") and s.get("last_event_time"):
                timeline_sessions.append({
                    "task_name": f"Untracked Session - {dir_name}",
                    "start_time": s["first_event_time"],
                    "end_time": s["last_event_time"],
                    "duration_seconds": s.get("duration_seconds", 0),
                    "is_untracked": True,
                })

        result.append({
            "id": None,
            "name": dir_name,
            "status": "untracked",
            "is_untracked": True,
            "time_seconds": total_seconds,
            "time_formatted": AnalyticsDB.format_duration(total_seconds),
            "message_count": total_messages,
            "session_count": len(group),
            "cwd": cwd,
            "sessions": timeline_sessions,
        })

    result.sort(key=lambda x: x["time_seconds"], reverse=True)
    return result


def refresh_claude_session_cache(
    date: str | None = None, use_cache: bool = True
) -> dict[int, dict]:
    """Refresh Claude session cache for a date by parsing JSONL files.

    Args:
        date: Date in YYYY-MM-DD format (defaults to today)
        use_cache: If True, skip files that haven't changed

    Returns:
        Dict mapping hour to activity metrics
    """
    from lib.jsonl_parser import get_jsonl_files_for_date, parse_session_file

    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")

    target_date = datetime.strptime(date, "%Y-%m-%d").date()
    cache = ClaudeSessionCache()
    hourly: dict[int, dict] = {}

    parsed_count = 0
    cached_count = 0

    for jsonl_file in get_jsonl_files_for_date(date, max_age_days=2):
        session_id = jsonl_file.stem
        file_mtime = jsonl_file.stat().st_mtime

        # Check cache first
        if use_cache:
            cached = cache.get_cached_session(session_id, file_mtime)
            if cached and cached.get("date") == date:
                # Skip cache for cross-midnight sessions - they need fresh proration calculation
                first_time = cached.get("first_event_time")
                last_time = cached.get("last_event_time")
                spans_midnight = False
                if first_time and last_time:
                    first_date = (
                        first_time[:10]
                        if isinstance(first_time, str)
                        else str(first_time.date())
                    )
                    last_date = (
                        last_time[:10]
                        if isinstance(last_time, str)
                        else str(last_time.date())
                    )
                    spans_midnight = first_date != last_date

                if not spans_midnight:
                    hour = cached.get("hour", 0)
                    if hour not in hourly:
                        hourly[hour] = {
                            "hour": hour,
                            "claude_messages": 0,
                            "claude_tool_calls": 0,
                            "claude_tokens": 0,
                            "claude_seconds": 0,
                            "session_count": 0,
                        }
                    hourly[hour]["claude_messages"] += cached.get("message_count", 0)
                    hourly[hour]["claude_tool_calls"] += cached.get(
                        "tool_call_count", 0
                    )
                    hourly[hour]["claude_tokens"] += cached.get(
                        "input_tokens", 0
                    ) + cached.get("output_tokens", 0)
                    hourly[hour]["claude_seconds"] += cached.get("duration_seconds", 0)
                    hourly[hour]["session_count"] += 1
                    cached_count += 1
                    continue
                # Cross-midnight session falls through to re-parse

        # Parse the file
        metrics = parse_session_file(jsonl_file)
        if not metrics or not metrics.first_event_time:
            continue

        # Check if session has any events on target date
        session_date = metrics.first_event_time.date()
        use_last_event = False

        # Determine if session should be counted for this date
        has_events_on_target = any(
            ts.date() == target_date for ts in metrics.event_timestamps
        )
        if not has_events_on_target:
            continue

        # Calculate active time only for events on target date
        active_duration = metrics.active_seconds_for_date(target_date)

        # Determine which hour to assign (use last event on target date)
        events_on_date = [
            ts for ts in metrics.event_timestamps if ts.date() == target_date
        ]
        if not events_on_date:
            continue
        # Use the hour of the first event on this date for assignment
        first_event_on_date = min(events_on_date)
        use_last_event = (
            session_date != target_date
        )  # Only relevant for determining hour

        # Use the hour of the first event on target date
        hour = first_event_on_date.hour
        parsed_count += 1

        # Cache the result
        session_data = {
            "session_id": session_id,
            "date": date,
            "hour": hour,
            "cwd": metrics.cwd,
            "git_branch": metrics.git_branch,
            "project_path": metrics.project_path,
            "message_count": metrics.total_messages,
            "tool_call_count": metrics.tool_call_count,
            "input_tokens": metrics.input_tokens,
            "output_tokens": metrics.output_tokens,
            "duration_seconds": active_duration,
            "first_event_time": metrics.first_event_time.isoformat()
            if metrics.first_event_time
            else None,
            "last_event_time": metrics.last_event_time.isoformat()
            if metrics.last_event_time
            else None,
        }
        cache.cache_session(session_data, str(jsonl_file), file_mtime)

        # Aggregate
        if hour not in hourly:
            hourly[hour] = {
                "hour": hour,
                "claude_messages": 0,
                "claude_tool_calls": 0,
                "claude_tokens": 0,
                "claude_seconds": 0,
                "session_count": 0,
            }
        hourly[hour]["claude_messages"] += metrics.total_messages
        hourly[hour]["claude_tool_calls"] += metrics.tool_call_count
        hourly[hour]["claude_tokens"] += metrics.total_tokens
        hourly[hour]["claude_seconds"] += active_duration
        hourly[hour]["session_count"] += 1

    return hourly


def get_claude_hourly_activity(date: str | None = None) -> list[dict]:
    """Get Claude Code activity by hour for a date.

    Refreshes cache if needed and returns hourly metrics.

    Args:
        date: Date in YYYY-MM-DD format (defaults to today)

    Returns:
        List of dicts with hour, claude_messages, claude_tool_calls, claude_tokens
    """
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")

    # Refresh cache and get results
    hourly = refresh_claude_session_cache(date)

    # Convert to list sorted by hour
    return [hourly[h] for h in sorted(hourly.keys())]


def get_claude_daily_activity(days: int = 7) -> list[dict]:
    """Get Claude Code activity by date for the last N days.

    Refreshes cache for each day if needed.

    Args:
        days: Number of days to look back

    Returns:
        List of dicts with date, claude_messages, claude_tool_calls, claude_tokens, claude_seconds
    """
    result = []

    for i in range(days):
        date = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")

        # Refresh cache for this date
        hourly = refresh_claude_session_cache(date)

        # Aggregate hourly into daily
        daily = {
            "date": date,
            "claude_messages": sum(
                h.get("claude_messages", 0) for h in hourly.values()
            ),
            "claude_tool_calls": sum(
                h.get("claude_tool_calls", 0) for h in hourly.values()
            ),
            "claude_tokens": sum(h.get("claude_tokens", 0) for h in hourly.values()),
            "claude_seconds": sum(h.get("claude_seconds", 0) for h in hourly.values()),
            "session_count": sum(h.get("session_count", 0) for h in hourly.values()),
        }
        result.append(daily)

    return result


def merge_hourly_activity(
    task_hourly: list[dict], claude_hourly: list[dict]
) -> list[dict]:
    """Merge task-based hourly activity with Claude Code activity.

    Args:
        task_hourly: List of dicts with hour, total_seconds, session_count (from orbit)
        claude_hourly: List of dicts with hour, claude_messages, etc (from JSONL)

    Returns:
        Merged list with all fields
    """
    # Index by hour
    task_by_hour = {h["hour"]: h for h in task_hourly}
    claude_by_hour = {h["hour"]: h for h in claude_hourly}

    # Get all hours
    all_hours = set(task_by_hour.keys()) | set(claude_by_hour.keys())

    result = []
    for hour in sorted(all_hours):
        entry = {
            "hour": hour,
            "total_seconds": 0,
            "session_count": 0,
            "claude_messages": 0,
            "claude_tool_calls": 0,
            "claude_tokens": 0,
            "claude_seconds": 0,
            "claude_session_count": 0,
        }

        if hour in task_by_hour:
            entry["total_seconds"] = task_by_hour[hour].get("total_seconds", 0)
            entry["session_count"] = task_by_hour[hour].get("session_count", 0)

        if hour in claude_by_hour:
            entry["claude_messages"] = claude_by_hour[hour].get("claude_messages", 0)
            entry["claude_tool_calls"] = claude_by_hour[hour].get(
                "claude_tool_calls", 0
            )
            entry["claude_tokens"] = claude_by_hour[hour].get("claude_tokens", 0)
            entry["claude_seconds"] = claude_by_hour[hour].get("claude_seconds", 0)
            entry["claude_session_count"] = claude_by_hour[hour].get("session_count", 0)

        result.append(entry)

    return result


# =============================================================================
# Module-level convenience functions
# =============================================================================

_db: AnalyticsDB | None = None
_session_cache: ClaudeSessionCache | None = None


def get_db() -> AnalyticsDB:
    """Get or create module-level database instance."""
    global _db
    if _db is None:
        _db = AnalyticsDB()
    return _db


def get_session_cache() -> ClaudeSessionCache:
    """Get or create module-level session cache instance."""
    global _session_cache
    if _session_cache is None:
        _session_cache = ClaudeSessionCache()
    return _session_cache


# =============================================================================
# Dev-docs Tasks.md Import Functions
# =============================================================================


@dataclass
class ParsedAgent:
    """Represents a parsed agent from a tasks.md file."""

    agent_id: str
    agent_name: str
    prompt: str
    dependencies: list[str]
    completed: bool = False


def parse_tasks_md(content: str) -> list[ParsedAgent]:
    """Parse orbit tasks.md file into agent definitions.

    Parses both completed ([x]) and uncompleted ([ ]) tasks.
    Task numbers are zero-padded to 2 digits for consistent ordering.

    Args:
        content: The raw content of a tasks.md file

    Returns:
        List of ParsedAgent objects with inferred sequential dependencies
    """
    agents: list[ParsedAgent] = []
    lines = content.split("\n")

    # Pattern: - [ ] N. Task description  OR  - [x] N. Task description
    task_pattern = re.compile(r"^-\s*\[([ xX])\]\s*(\d+)\.\s*(.+)$")

    for line in lines:
        match = task_pattern.match(line.strip())
        if match:
            checkbox = match.group(1)
            task_num = match.group(2).zfill(2)
            task_desc = match.group(3).strip()
            completed = checkbox.lower() == "x"

            agents.append(
                ParsedAgent(
                    agent_id=task_num,
                    agent_name=task_desc,
                    prompt=f"Complete task: {task_desc}",
                    dependencies=[],
                    completed=completed,
                )
            )

    # Infer sequential dependencies (each task depends on the previous)
    for i in range(1, len(agents)):
        agents[i].dependencies = [agents[i - 1].agent_id]

    return agents


def parse_dependency_graph(content: str) -> dict[str, list[str]]:
    """Parse optional Task Dependencies section for custom DAG edges.

    Looks for patterns like:
        Phase 1: [1-3] → [4-6] → [7]
        Phase 2: [8] → [9-10] → [11-12]

    This allows overriding the default sequential dependencies.

    Args:
        content: The raw content of a tasks.md file

    Returns:
        Dict mapping agent_id to list of dependency agent_ids,
        or empty dict if no dependencies section found
    """
    dependencies: dict[str, list[str]] = {}

    # Find the Task Dependencies section
    dep_section_match = re.search(
        r"##\s*Task Dependencies\s*\n```\n(.*?)\n```",
        content,
        re.DOTALL | re.IGNORECASE,
    )

    if not dep_section_match:
        return dependencies

    dep_content = dep_section_match.group(1)

    # Parse each phase line
    # Pattern: Phase N: [X-Y] → [A-B] → [C]
    for line in dep_content.split("\n"):
        line = line.strip()
        if not line or not "→" in line:
            continue

        # Extract all bracket groups: [1-3] or [7]
        bracket_pattern = re.compile(r"\[(\d+(?:-\d+)?)\]")
        groups = bracket_pattern.findall(line)

        if len(groups) < 2:
            continue

        # Process each group into a list of task IDs
        def expand_group(group: str) -> list[str]:
            """Expand '1-3' to ['01', '02', '03'] or '7' to ['07']."""
            if "-" in group:
                start, end = group.split("-")
                return [str(i).zfill(2) for i in range(int(start), int(end) + 1)]
            return [group.zfill(2)]

        # Each group depends on all items in the previous group
        prev_group: list[str] = []
        for group in groups:
            current_group = expand_group(group)
            if prev_group:
                # All items in current group depend on all items in prev group
                for agent_id in current_group:
                    if agent_id not in dependencies:
                        dependencies[agent_id] = []
                    dependencies[agent_id].extend(prev_group)
            prev_group = current_group

    return dependencies


def import_tasks_md(
    db: AnalyticsDB,
    content: str,
    plan_name: str,
    task_id: int | None = None,
    use_custom_dependencies: bool = True,
    import_completed: bool = False,
) -> dict:
    """Import an orbit tasks.md file as a plan with agents.

    Args:
        db: AnalyticsDB instance
        content: The raw content of the tasks.md file
        plan_name: Name for the created plan
        task_id: Optional associated task ID
        use_custom_dependencies: If True, parse Task Dependencies section
        import_completed: If True, also import completed ([x]) tasks

    Returns:
        Dict with plan_id, agents_imported, and agents list
    """
    # Parse agents from task list
    all_agents = parse_tasks_md(content)

    # Filter based on completion status
    if not import_completed:
        agents = [a for a in all_agents if not a.completed]
    else:
        agents = all_agents

    if not agents:
        return {
            "plan_id": None,
            "agents_imported": 0,
            "agents": [],
            "error": "No tasks found to import",
        }

    # Parse custom dependencies if requested
    custom_deps: dict[str, list[str]] = {}
    if use_custom_dependencies:
        custom_deps = parse_dependency_graph(content)

    # Create the plan
    plan_id = db.create_plan(
        name=plan_name,
        task_id=task_id,
        metadata={"imported_from": "tasks_md", "total_parsed": len(all_agents)},
    )

    # Add agents and their dependencies
    imported_agents: list[dict] = []
    for agent in agents:
        # Add agent execution
        db.add_agent_execution(
            plan_id=plan_id,
            agent_id=agent.agent_id,
            agent_name=agent.agent_name,
            prompt=agent.prompt,
            max_attempts=3,
        )

        # Determine dependencies (custom overrides sequential)
        if agent.agent_id in custom_deps:
            deps = custom_deps[agent.agent_id]
        else:
            deps = agent.dependencies

        # Filter to only include dependencies that exist in our imported agents
        valid_agent_ids = {a.agent_id for a in agents}
        filtered_deps = [d for d in deps if d in valid_agent_ids]

        # Add dependencies
        for dep in filtered_deps:
            db.add_agent_dependency(plan_id, agent.agent_id, dep)

        imported_agents.append(
            {
                "agent_id": agent.agent_id,
                "agent_name": agent.agent_name,
                "prompt": agent.prompt,
                "dependencies": filtered_deps,
                "completed": agent.completed,
            }
        )

    return {
        "plan_id": plan_id,
        "agents_imported": len(imported_agents),
        "agents": imported_agents,
    }

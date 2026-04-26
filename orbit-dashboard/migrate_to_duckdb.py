#!/usr/bin/env python3
"""
Migrate task database from SQLite to DuckDB.

This script migrates all tables from the existing SQLite database to a new
DuckDB database with optimized schema:
- TEXT timestamps → TIMESTAMP
- TEXT JSON → native JSON/LIST types
- Optimized indexes for analytics queries

Usage:
    python migrate_to_duckdb.py           # Run migration
    python migrate_to_duckdb.py --verify  # Verify only (no migration)
    python migrate_to_duckdb.py --dry-run # Show what would be done
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

import duckdb

# Paths
from orbit_db import DB_PATH, ORBIT_ROOT

SQLITE_PATH = DB_PATH
DUCKDB_PATH = ORBIT_ROOT / "tasks.duckdb"
BACKUP_PATH = ORBIT_ROOT / "tasks.db.backup"


def create_duckdb_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Create the DuckDB schema with optimized types."""

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

    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY,
            repo_id INTEGER REFERENCES repositories(id),
            name VARCHAR NOT NULL,
            full_path VARCHAR NOT NULL,
            parent_id INTEGER REFERENCES tasks(id),
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
            task_id INTEGER NOT NULL REFERENCES tasks(id),
            note VARCHAR NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT now()
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS heartbeats (
            id INTEGER PRIMARY KEY,
            task_id INTEGER NOT NULL REFERENCES tasks(id),
            timestamp TIMESTAMP NOT NULL DEFAULT now(),
            session_id VARCHAR,
            context VARCHAR,
            processed BOOLEAN NOT NULL DEFAULT false
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY,
            task_id INTEGER NOT NULL REFERENCES tasks(id),
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
            last_commit_at TIMESTAMP,
            created_at TIMESTAMP NOT NULL DEFAULT now(),
            updated_at TIMESTAMP NOT NULL DEFAULT now()
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS shadow_commits (
            id INTEGER PRIMARY KEY,
            shadow_repo_id INTEGER NOT NULL REFERENCES shadow_repos(id),
            task_id INTEGER REFERENCES tasks(id),
            sha VARCHAR NOT NULL,
            timestamp TIMESTAMP NOT NULL,
            lines_added INTEGER NOT NULL DEFAULT 0,
            lines_removed INTEGER NOT NULL DEFAULT 0,
            files_changed INTEGER NOT NULL DEFAULT 0,
            message VARCHAR,
            created_at TIMESTAMP NOT NULL DEFAULT now()
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS non_git_activity (
            id INTEGER PRIMARY KEY,
            date DATE NOT NULL,
            hour INTEGER NOT NULL,
            folder_path VARCHAR NOT NULL,
            lines_total INTEGER DEFAULT 0,
            files_changed INTEGER DEFAULT 0,
            file_hashes JSON,
            created_at TIMESTAMP DEFAULT now(),
            UNIQUE(date, hour, folder_path)
        )
    """)


def create_indexes(conn: duckdb.DuckDBPyConnection) -> None:
    """Create optimized indexes for common queries."""

    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_repos_active ON repositories(active)",
        "CREATE INDEX IF NOT EXISTS idx_repos_path ON repositories(path)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_repo_status ON tasks(repo_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_last_worked ON tasks(last_worked_on DESC)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_type ON tasks(type)",
        "CREATE INDEX IF NOT EXISTS idx_updates_task ON task_updates(task_id)",
        "CREATE INDEX IF NOT EXISTS idx_updates_created ON task_updates(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_heartbeats_task_time ON heartbeats(task_id, timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_heartbeats_unprocessed ON heartbeats(processed, timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_sessions_task_time ON sessions(task_id, start_time)",
        "CREATE INDEX IF NOT EXISTS idx_shadow_repos_folder ON shadow_repos(folder_path)",
        "CREATE INDEX IF NOT EXISTS idx_shadow_repos_active ON shadow_repos(active)",
        "CREATE INDEX IF NOT EXISTS idx_shadow_commits_repo ON shadow_commits(shadow_repo_id)",
        "CREATE INDEX IF NOT EXISTS idx_shadow_commits_task ON shadow_commits(task_id)",
        "CREATE INDEX IF NOT EXISTS idx_shadow_commits_time ON shadow_commits(timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_non_git_date ON non_git_activity(date)",
        "CREATE INDEX IF NOT EXISTS idx_non_git_folder ON non_git_activity(folder_path)",
    ]

    for idx_sql in indexes:
        conn.execute(idx_sql)


def parse_timestamp(ts_str: str | None) -> datetime | None:
    """Parse SQLite timestamp string to datetime."""
    if not ts_str:
        return None

    # Try common formats
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


def migrate_repositories(
    sqlite_conn: sqlite3.Connection, duck_conn: duckdb.DuckDBPyConnection
) -> int:
    """Migrate repositories table."""
    cursor = sqlite_conn.execute("SELECT * FROM repositories")
    rows = cursor.fetchall()

    for row in rows:
        duck_conn.execute(
            """
            INSERT INTO repositories (id, path, short_name, glob_pattern, active,
                                      created_at, updated_at, last_scanned_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                row["id"],
                row["path"],
                row["short_name"],
                row["glob_pattern"],
                bool(row["active"]),
                parse_timestamp(row["created_at"]),
                parse_timestamp(row["updated_at"]),
                parse_timestamp(row["last_scanned_at"]),
            ),
        )

    return len(rows)


def migrate_tasks(
    sqlite_conn: sqlite3.Connection, duck_conn: duckdb.DuckDBPyConnection
) -> int:
    """Migrate tasks table."""
    cursor = sqlite_conn.execute("SELECT * FROM tasks")
    rows = cursor.fetchall()

    for row in rows:
        # Parse tags JSON
        tags_str = row["tags"] or "[]"
        try:
            tags = json.loads(tags_str)
        except json.JSONDecodeError:
            tags = []

        duck_conn.execute(
            """
            INSERT INTO tasks (id, repo_id, name, full_path, parent_id, status, type,
                              tags, priority, jira_key, branch, pr_url,
                              created_at, updated_at, completed_at, archived_at, last_worked_on)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                row["id"],
                row["repo_id"],
                row["name"],
                row["full_path"],
                row["parent_id"],
                row["status"],
                row["type"],
                json.dumps(tags),  # DuckDB JSON type
                row["priority"],
                row["jira_key"],
                row["branch"],
                row["pr_url"],
                parse_timestamp(row["created_at"]),
                parse_timestamp(row["updated_at"]),
                parse_timestamp(row["completed_at"]),
                parse_timestamp(row["archived_at"]),
                parse_timestamp(row["last_worked_on"]),
            ),
        )

    return len(rows)


def migrate_task_updates(
    sqlite_conn: sqlite3.Connection, duck_conn: duckdb.DuckDBPyConnection
) -> int:
    """Migrate task_updates table."""
    cursor = sqlite_conn.execute("SELECT * FROM task_updates")
    rows = cursor.fetchall()

    for row in rows:
        duck_conn.execute(
            """
            INSERT INTO task_updates (id, task_id, note, created_at)
            VALUES (?, ?, ?, ?)
        """,
            (
                row["id"],
                row["task_id"],
                row["note"],
                parse_timestamp(row["created_at"]),
            ),
        )

    return len(rows)


def migrate_heartbeats(
    sqlite_conn: sqlite3.Connection, duck_conn: duckdb.DuckDBPyConnection
) -> int:
    """Migrate heartbeats table."""
    cursor = sqlite_conn.execute("SELECT * FROM heartbeats")
    rows = cursor.fetchall()

    for row in rows:
        duck_conn.execute(
            """
            INSERT INTO heartbeats (id, task_id, timestamp, session_id, context, processed)
            VALUES (?, ?, ?, ?, ?, ?)
        """,
            (
                row["id"],
                row["task_id"],
                parse_timestamp(row["timestamp"]),
                row["session_id"],
                row["context"],
                bool(row["processed"]),
            ),
        )

    return len(rows)


def migrate_sessions(
    sqlite_conn: sqlite3.Connection, duck_conn: duckdb.DuckDBPyConnection
) -> int:
    """Migrate sessions table."""
    cursor = sqlite_conn.execute("SELECT * FROM sessions")
    rows = cursor.fetchall()

    for row in rows:
        duck_conn.execute(
            """
            INSERT INTO sessions (id, task_id, session_id, start_time, end_time,
                                 duration_seconds, heartbeat_count)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
            (
                row["id"],
                row["task_id"],
                row["session_id"],
                parse_timestamp(row["start_time"]),
                parse_timestamp(row["end_time"]),
                row["duration_seconds"],
                row["heartbeat_count"],
            ),
        )

    return len(rows)


def migrate_config(
    sqlite_conn: sqlite3.Connection, duck_conn: duckdb.DuckDBPyConnection
) -> int:
    """Migrate config table."""
    cursor = sqlite_conn.execute("SELECT * FROM config")
    rows = cursor.fetchall()

    for row in rows:
        duck_conn.execute(
            """
            INSERT INTO config (key, value, updated_at)
            VALUES (?, ?, ?)
        """,
            (
                row["key"],
                row["value"],  # Already JSON string
                parse_timestamp(row["updated_at"]),
            ),
        )

    return len(rows)


def migrate_shadow_repos(
    sqlite_conn: sqlite3.Connection, duck_conn: duckdb.DuckDBPyConnection
) -> int:
    """Migrate shadow_repos table."""
    cursor = sqlite_conn.execute("SELECT * FROM shadow_repos")
    rows = cursor.fetchall()

    for row in rows:
        duck_conn.execute(
            """
            INSERT INTO shadow_repos (id, folder_path, shadow_path, folder_hash, active,
                                      last_commit_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                row["id"],
                row["folder_path"],
                row["shadow_path"],
                row["folder_hash"],
                bool(row["active"]),
                parse_timestamp(row["last_commit_at"]),
                parse_timestamp(row["created_at"]),
                parse_timestamp(row["updated_at"]),
            ),
        )

    return len(rows)


def migrate_shadow_commits(
    sqlite_conn: sqlite3.Connection, duck_conn: duckdb.DuckDBPyConnection
) -> int:
    """Migrate shadow_commits table."""
    cursor = sqlite_conn.execute("SELECT * FROM shadow_commits")
    rows = cursor.fetchall()

    for row in rows:
        duck_conn.execute(
            """
            INSERT INTO shadow_commits (id, shadow_repo_id, task_id, sha, timestamp,
                                        lines_added, lines_removed, files_changed,
                                        message, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                row["id"],
                row["shadow_repo_id"],
                row["task_id"],
                row["sha"],
                parse_timestamp(row["timestamp"]),
                row["lines_added"],
                row["lines_removed"],
                row["files_changed"],
                row["message"],
                parse_timestamp(row["created_at"]),
            ),
        )

    return len(rows)


def migrate_non_git_activity(
    sqlite_conn: sqlite3.Connection, duck_conn: duckdb.DuckDBPyConnection
) -> int:
    """Migrate non_git_activity table."""
    cursor = sqlite_conn.execute("SELECT * FROM non_git_activity")
    rows = cursor.fetchall()

    for row in rows:
        # Parse file_hashes JSON
        file_hashes = row["file_hashes"]

        duck_conn.execute(
            """
            INSERT INTO non_git_activity (id, date, hour, folder_path, lines_total,
                                          files_changed, file_hashes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                row["id"],
                row["date"],  # Already YYYY-MM-DD format
                row["hour"],
                row["folder_path"],
                row["lines_total"],
                row["files_changed"],
                file_hashes,
                parse_timestamp(row["created_at"]) if row["created_at"] else None,
            ),
        )

    return len(rows)


def verify_migration(
    sqlite_conn: sqlite3.Connection, duck_conn: duckdb.DuckDBPyConnection
) -> dict:
    """Verify row counts match between SQLite and DuckDB."""
    tables = [
        "repositories",
        "tasks",
        "task_updates",
        "heartbeats",
        "sessions",
        "config",
        "shadow_repos",
        "shadow_commits",
        "non_git_activity",
    ]

    results = {}
    for table in tables:
        sqlite_count = sqlite_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[
            0
        ]
        duck_count = duck_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        results[table] = {
            "sqlite": sqlite_count,
            "duckdb": duck_count,
            "match": sqlite_count == duck_count,
        }

    return results


def run_migration(dry_run: bool = False) -> None:
    """Run the full migration."""

    if not SQLITE_PATH.exists():
        print(f"Error: SQLite database not found at {SQLITE_PATH}")
        return

    print(f"Source: {SQLITE_PATH}")
    print(f"Target: {DUCKDB_PATH}")
    print()

    # Connect to SQLite
    sqlite_conn = sqlite3.connect(str(SQLITE_PATH))
    sqlite_conn.row_factory = sqlite3.Row

    if dry_run:
        print("=== DRY RUN MODE ===")
        print()

    # Show source counts
    tables = [
        "repositories",
        "tasks",
        "task_updates",
        "heartbeats",
        "sessions",
        "config",
        "shadow_repos",
        "shadow_commits",
        "non_git_activity",
    ]

    print("Source row counts:")
    for table in tables:
        count = sqlite_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table}: {count}")
    print()

    if dry_run:
        print("Would create DuckDB database with optimized schema")
        print("Would migrate all tables with type conversions:")
        print("  - TEXT timestamps → TIMESTAMP")
        print("  - TEXT JSON → native JSON")
        print("  - INTEGER booleans → BOOLEAN")
        sqlite_conn.close()
        return

    # Backup existing DuckDB if present
    if DUCKDB_PATH.exists():
        backup = DUCKDB_PATH.with_suffix(".duckdb.backup")
        print(f"Backing up existing DuckDB to {backup}")
        shutil.copy(DUCKDB_PATH, backup)
        DUCKDB_PATH.unlink()

    # Create SQLite backup
    print(f"Backing up SQLite to {BACKUP_PATH}")
    shutil.copy(SQLITE_PATH, BACKUP_PATH)

    # Connect to DuckDB
    duck_conn = duckdb.connect(str(DUCKDB_PATH))

    print()
    print("Creating DuckDB schema...")
    create_duckdb_schema(duck_conn)

    print("Migrating data...")
    migrations = [
        ("repositories", migrate_repositories),
        ("tasks", migrate_tasks),
        ("task_updates", migrate_task_updates),
        ("heartbeats", migrate_heartbeats),
        ("sessions", migrate_sessions),
        ("config", migrate_config),
        ("shadow_repos", migrate_shadow_repos),
        ("shadow_commits", migrate_shadow_commits),
        ("non_git_activity", migrate_non_git_activity),
    ]

    for table_name, migrate_func in migrations:
        count = migrate_func(sqlite_conn, duck_conn)
        print(f"  {table_name}: {count} rows")

    print()
    print("Creating indexes...")
    create_indexes(duck_conn)

    print()
    print("Verifying migration...")
    results = verify_migration(sqlite_conn, duck_conn)

    all_match = True
    for table, counts in results.items():
        status = "✓" if counts["match"] else "✗"
        print(
            f"  {status} {table}: SQLite={counts['sqlite']}, DuckDB={counts['duckdb']}"
        )
        if not counts["match"]:
            all_match = False

    print()
    if all_match:
        print("Migration completed successfully!")
        print(f"DuckDB database created at: {DUCKDB_PATH}")
        print(f"SQLite backup at: {BACKUP_PATH}")
    else:
        print("WARNING: Row count mismatch detected!")

    sqlite_conn.close()
    duck_conn.close()


def verify_only() -> None:
    """Verify existing migration."""
    if not SQLITE_PATH.exists():
        print(f"Error: SQLite database not found at {SQLITE_PATH}")
        return

    if not DUCKDB_PATH.exists():
        print(f"Error: DuckDB database not found at {DUCKDB_PATH}")
        return

    sqlite_conn = sqlite3.connect(str(SQLITE_PATH))
    sqlite_conn.row_factory = sqlite3.Row
    duck_conn = duckdb.connect(str(DUCKDB_PATH))

    print("Verifying migration...")
    results = verify_migration(sqlite_conn, duck_conn)

    all_match = True
    for table, counts in results.items():
        status = "✓" if counts["match"] else "✗"
        print(
            f"  {status} {table}: SQLite={counts['sqlite']}, DuckDB={counts['duckdb']}"
        )
        if not counts["match"]:
            all_match = False

    print()
    if all_match:
        print("All tables verified!")
    else:
        print("WARNING: Row count mismatch detected!")

    sqlite_conn.close()
    duck_conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="Migrate task database from SQLite to DuckDB"
    )
    parser.add_argument(
        "--verify", action="store_true", help="Verify existing migration"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be done"
    )
    args = parser.parse_args()

    if args.verify:
        verify_only()
    else:
        run_migration(dry_run=args.dry_run)


if __name__ == "__main__":
    main()

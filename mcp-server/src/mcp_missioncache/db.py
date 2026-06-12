"""Database wrapper for orbit_db."""

from orbit_db import Repository, Task, TaskDB, TaskStatus

from .config import settings

# Module-level singleton
_db: TaskDB | None = None


def get_db() -> TaskDB:
    """Get or create the TaskDB singleton."""
    global _db
    if _db is None:
        _db = TaskDB(db_path=settings.db_path)
        _db.initialize()
    return _db


def repo_to_dict(repo: Repository) -> dict:
    """Convert Repository dataclass to dict."""
    from dataclasses import asdict

    return asdict(repo)


# Re-export for convenience
__all__ = [
    "get_db",
    "repo_to_dict",
    "Task",
    "Repository",
    "TaskDB",
    "TaskStatus",
]

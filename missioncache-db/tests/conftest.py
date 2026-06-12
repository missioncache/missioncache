"""Shared fixtures for orbit-db tests."""

import pytest
from pathlib import Path

from orbit_db import TaskDB


@pytest.fixture
def task_db(tmp_path):
    """TaskDB instance backed by a temporary SQLite database."""
    db_path = tmp_path / "test_tasks.db"
    db = TaskDB(db_path=db_path)
    db.initialize()
    return db

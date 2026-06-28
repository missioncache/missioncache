"""Shared fixtures for missioncache-db tests."""

import pytest
from pathlib import Path

from missioncache_db import TaskDB


@pytest.fixture(autouse=True)
def _clean_missioncache_root_env(monkeypatch):
    """Keep an ambient MISSIONCACHE_ROOT override out of the test environment.

    check_legacy_paths() short-circuits when MISSIONCACHE_ROOT is set, so a
    leaked env var would silence the legacy-guard tests. Tests that need a
    custom root monkeypatch the module constants directly, never the env.
    """
    monkeypatch.delenv("MISSIONCACHE_ROOT", raising=False)


@pytest.fixture
def task_db(tmp_path):
    """TaskDB instance backed by a temporary SQLite database."""
    db_path = tmp_path / "test_tasks.db"
    db = TaskDB(db_path=db_path)
    db.initialize()
    return db

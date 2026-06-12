"""Tests for the legacy-path migration guard.

Covers the OrbitMigrationRequired exception raised by TaskDB.__init__ when
orbit data exists at the pre-Phase-11 ~/.claude/ paths but the new
~/.orbit/tasks.db hasn't been created yet.

The guard reads module-level DB_PATH / _LEGACY_DB / _LEGACY_ORBIT_ROOT at
call time so tests can monkeypatch them to point at tmp paths instead of
the real user home.
"""

from types import SimpleNamespace

import pytest

import orbit_db
from orbit_db import OrbitMigrationRequired, TaskDB


@pytest.fixture
def isolated_paths(tmp_path, monkeypatch):
    """Redirect DB_PATH and the legacy paths into tmp_path subdirs.

    Returns a SimpleNamespace exposing each path so tests can manipulate
    presence (touch the file or skip it) before instantiating TaskDB.
    """
    new_dir = tmp_path / "orbit"
    legacy_dir = tmp_path / "claude"
    new_db = new_dir / "tasks.db"
    legacy_db = legacy_dir / "tasks.db"
    legacy_orbit = legacy_dir / "orbit"

    monkeypatch.setattr(orbit_db, "DB_PATH", new_db)
    monkeypatch.setattr(orbit_db, "_LEGACY_DB", legacy_db)
    monkeypatch.setattr(orbit_db, "_LEGACY_ORBIT_ROOT", legacy_orbit)

    return SimpleNamespace(
        new_db=new_db,
        legacy_db=legacy_db,
        legacy_orbit=legacy_orbit,
    )


def test_fresh_install_no_paths_present(isolated_paths):
    """Neither legacy nor new paths exist -> TaskDB constructs cleanly."""
    db = TaskDB(db_path=isolated_paths.new_db)
    assert db.db_path == isolated_paths.new_db


def test_already_migrated_new_db_exists(isolated_paths):
    """New DB exists -> guard short-circuits regardless of legacy state."""
    isolated_paths.new_db.parent.mkdir(parents=True)
    isolated_paths.new_db.touch()
    isolated_paths.legacy_orbit.mkdir(parents=True)  # legacy data ALSO present
    db = TaskDB(db_path=isolated_paths.new_db)
    assert db.db_path == isolated_paths.new_db


def test_legacy_db_only_raises(isolated_paths):
    """Legacy DB present but new not -> migration required."""
    isolated_paths.legacy_db.parent.mkdir(parents=True)
    isolated_paths.legacy_db.touch()
    with pytest.raises(OrbitMigrationRequired) as exc_info:
        TaskDB(db_path=isolated_paths.new_db)
    msg = str(exc_info.value)
    assert "legacy ~/.claude/ paths" in msg
    assert "mv ~/.claude/tasks.db" in msg


def test_legacy_orbit_dir_only_raises(isolated_paths):
    """Legacy orbit dir present but new DB not -> migration required."""
    isolated_paths.legacy_orbit.mkdir(parents=True)
    with pytest.raises(OrbitMigrationRequired):
        TaskDB(db_path=isolated_paths.new_db)


def test_both_legacy_and_new_present(isolated_paths):
    """Migration completed but legacy not yet cleaned up -> no exception
    (DB_PATH.exists() short-circuits the check)."""
    isolated_paths.new_db.parent.mkdir(parents=True)
    isolated_paths.new_db.touch()
    isolated_paths.legacy_db.parent.mkdir(parents=True)
    isolated_paths.legacy_db.touch()
    isolated_paths.legacy_orbit.mkdir(parents=True)
    db = TaskDB(db_path=isolated_paths.new_db)
    assert db.db_path == isolated_paths.new_db


def test_exception_is_runtime_error_subclass():
    """OrbitMigrationRequired must be catchable by `except Exception`,
    not BaseException-only like SystemExit. Hooks rely on this."""
    assert issubclass(OrbitMigrationRequired, RuntimeError)
    assert issubclass(OrbitMigrationRequired, Exception)

"""Tests for the legacy-path migration guard.

Covers the MissionCacheMigrationRequired exception raised by TaskDB.__init__ when
MissionCache data exists at a legacy location but the canonical
~/.missioncache/tasks.db hasn't been created yet. Two legacy tiers are detected:
the ancient ~/.claude/ layout and the ~/.orbit/ layout (superseded by
~/.missioncache/ in the MissionCache rename).

The guard reads the module-level DB_PATH / _LEGACY_CLAUDE_DB / _LEGACY_CLAUDE_ORBIT_ROOT /
_LEGACY_ORBIT_DB / _LEGACY_ORBIT_ROOT constants at call time so tests can
monkeypatch them to point at tmp paths instead of the real user home.
"""

from types import SimpleNamespace

import pytest

import missioncache_db
from missioncache_db import MissionCacheMigrationRequired, TaskDB


@pytest.fixture
def isolated_paths(tmp_path, monkeypatch):
    """Redirect DB_PATH and every legacy path into tmp_path subdirs.

    Three tiers are exposed so tests can manipulate presence (touch the file or
    skip it) before instantiating TaskDB: the canonical new DB, the ancient
    ~/.claude/ legacy, and the ~/.orbit/ legacy.
    """
    new_db = tmp_path / "missioncache" / "tasks.db"
    legacy_db = tmp_path / "claude" / "tasks.db"
    legacy_orbit = tmp_path / "claude" / "orbit"
    orbit_db = tmp_path / "orbit" / "tasks.db"
    orbit_root = tmp_path / "orbit"

    monkeypatch.setattr(missioncache_db, "DB_PATH", new_db)
    monkeypatch.setattr(missioncache_db, "_LEGACY_CLAUDE_DB", legacy_db)
    monkeypatch.setattr(missioncache_db, "_LEGACY_CLAUDE_ORBIT_ROOT", legacy_orbit)
    monkeypatch.setattr(missioncache_db, "_LEGACY_ORBIT_DB", orbit_db)
    monkeypatch.setattr(missioncache_db, "_LEGACY_ORBIT_ROOT", orbit_root)

    return SimpleNamespace(
        new_db=new_db,
        legacy_db=legacy_db,
        legacy_orbit=legacy_orbit,
        orbit_db=orbit_db,
        orbit_root=orbit_root,
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
    """Ancient ~/.claude/ DB present but new not -> migration required."""
    isolated_paths.legacy_db.parent.mkdir(parents=True)
    isolated_paths.legacy_db.touch()
    with pytest.raises(MissionCacheMigrationRequired) as exc_info:
        TaskDB(db_path=isolated_paths.new_db)
    msg = str(exc_info.value)
    assert "~/.missioncache/" in msg
    assert "mv ~/.claude/tasks.db" in msg


def test_legacy_orbit_dir_only_raises(isolated_paths):
    """Ancient ~/.claude/orbit dir present but new DB not -> migration required
    with the ancient-tier recipe branch."""
    isolated_paths.legacy_orbit.mkdir(parents=True)
    with pytest.raises(MissionCacheMigrationRequired) as exc_info:
        TaskDB(db_path=isolated_paths.new_db)
    assert "mv ~/.claude/orbit/active" in str(exc_info.value)


def test_orbit_legacy_db_raises(isolated_paths):
    """~/.orbit/tasks.db present but new not -> migration required with the
    ~/.orbit recipe branch."""
    isolated_paths.orbit_db.parent.mkdir(parents=True)
    isolated_paths.orbit_db.touch()
    with pytest.raises(MissionCacheMigrationRequired) as exc_info:
        TaskDB(db_path=isolated_paths.new_db)
    msg = str(exc_info.value)
    assert "~/.missioncache/" in msg
    assert "mv ~/.orbit/tasks.db" in msg


def test_orbit_legacy_root_only_raises(isolated_paths):
    """~/.orbit/ dir present (no tasks.db) but new DB not -> migration required
    with the ~/.orbit recipe branch."""
    isolated_paths.orbit_root.mkdir(parents=True)
    with pytest.raises(MissionCacheMigrationRequired) as exc_info:
        TaskDB(db_path=isolated_paths.new_db)
    assert "mv ~/.orbit/active" in str(exc_info.value)


def test_both_legacy_tiers_present_emits_both_recipes(isolated_paths):
    """Data at BOTH legacy tiers + new DB missing -> the message composes both
    recipe blocks (the two `if` branches add, they don't override one another)."""
    isolated_paths.legacy_db.parent.mkdir(parents=True)
    isolated_paths.legacy_db.touch()
    isolated_paths.orbit_db.parent.mkdir(parents=True)
    isolated_paths.orbit_db.touch()
    with pytest.raises(MissionCacheMigrationRequired) as exc_info:
        TaskDB(db_path=isolated_paths.new_db)
    msg = str(exc_info.value)
    assert "mv ~/.claude/tasks.db" in msg
    assert "mv ~/.orbit/tasks.db" in msg


def test_both_legacy_and_new_present(isolated_paths):
    """Migration completed but legacy not yet cleaned up -> no exception
    (DB_PATH.exists() short-circuits the check)."""
    isolated_paths.new_db.parent.mkdir(parents=True)
    isolated_paths.new_db.touch()
    isolated_paths.legacy_db.parent.mkdir(parents=True)
    isolated_paths.legacy_db.touch()
    isolated_paths.legacy_orbit.mkdir(parents=True)
    isolated_paths.orbit_db.parent.mkdir(parents=True)
    isolated_paths.orbit_db.touch()
    db = TaskDB(db_path=isolated_paths.new_db)
    assert db.db_path == isolated_paths.new_db


def test_exception_is_runtime_error_subclass():
    """MissionCacheMigrationRequired must be catchable by `except Exception`,
    not BaseException-only like SystemExit. Hooks rely on this."""
    assert issubclass(MissionCacheMigrationRequired, RuntimeError)
    assert issubclass(MissionCacheMigrationRequired, Exception)

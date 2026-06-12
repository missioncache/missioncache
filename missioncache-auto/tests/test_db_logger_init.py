"""Tests for the DB-logger init paths in worker.py and db_logger.py.

The ``DB_PATH.exists()`` gate in both ``Worker._init_db_logger`` and
``ExecutionLogger._init_db`` used to short-circuit silently when the DB
was missing, even when the cause was unmigrated orbit data at the legacy
``~/.claude/`` paths - the exact scenario the migration guard exists for.
Both sites now route the missing-DB case through
``warn_if_migration_required``, which calls missioncache-db's public
``check_legacy_paths`` directly and prints the FULL migration recipe
(including the ``mv`` commands) to stderr once per process. These tests
pin that contract:

- missing DB + legacy data present -> full migration message, once per
  process, logger no-op, the run itself still proceeds
- missing DB + fresh install -> silent no-op, nothing created on disk
- DB present -> logger enabled (unchanged happy path)
"""

from types import SimpleNamespace

import pytest

import missioncache_db

from missioncache_auto import db_logger as db_logger_module
from missioncache_auto.db_logger import ExecutionLogger
from missioncache_auto.worker import Worker, _WorkerDBLogger


@pytest.fixture
def isolated_paths(tmp_path, monkeypatch):
    """Redirect DB_PATH and the legacy paths into tmp_path subdirs.

    Mirrors missioncache-db's test_legacy_guard.py fixture: check_legacy_paths
    reads module-level DB_PATH / _LEGACY_DB / _LEGACY_MISSIONCACHE_ROOT at call
    time, so monkeypatching the missioncache_db module attributes is enough -
    the production init paths import them at call time too.

    Also resets the module-level warn-once flag so each test observes
    the first-warning behavior independently.
    """
    new_db = tmp_path / "orbit" / "tasks.db"
    legacy_db = tmp_path / "claude" / "tasks.db"
    legacy_orbit = tmp_path / "claude" / "orbit"

    monkeypatch.setattr(missioncache_db, "DB_PATH", new_db)
    monkeypatch.setattr(missioncache_db, "_LEGACY_DB", legacy_db)
    monkeypatch.setattr(missioncache_db, "_LEGACY_MISSIONCACHE_ROOT", legacy_orbit)
    monkeypatch.setattr(db_logger_module, "_migration_warned", False)

    return SimpleNamespace(
        new_db=new_db,
        legacy_db=legacy_db,
        legacy_orbit=legacy_orbit,
    )


def _make_worker(tmp_path, execution_id=7):
    return Worker(
        worker_id=1,
        task_name="sample",
        project_root=tmp_path,
        state_dir=tmp_path / "state",
        prompts_dir=tmp_path / "prompts",
        adjacency_file=tmp_path / "adjacency.json",
        execution_id=execution_id,
    )


class TestWorkerInitDbLogger:
    def test_migration_required_surfaces_full_recipe(
        self, tmp_path, isolated_paths, capsys
    ):
        """Legacy data + missing new DB -> full migration message (with the
        mv commands), logger disabled, and Worker construction still
        succeeds (run is not blocked)."""
        isolated_paths.legacy_orbit.mkdir(parents=True)

        worker = _make_worker(tmp_path)

        err = capsys.readouterr().err
        assert "missioncache-auto: dashboard logging disabled" in err
        assert "mv ~/.claude/tasks.db" in err
        assert worker._db_logger is None

    def test_fresh_install_stays_silent_noop(
        self, tmp_path, isolated_paths, capsys
    ):
        """No DB, no legacy data -> deliberate no-op: no stderr noise and
        no DB created as a side effect."""
        worker = _make_worker(tmp_path)

        assert capsys.readouterr().err == ""
        assert worker._db_logger is None
        assert not isolated_paths.new_db.exists()

    def test_db_present_enables_logger(self, tmp_path, isolated_paths):
        """Existing DB -> logger wired up exactly as before the fix."""
        isolated_paths.new_db.parent.mkdir(parents=True)
        isolated_paths.new_db.touch()

        worker = _make_worker(tmp_path)

        assert isinstance(worker._db_logger, _WorkerDBLogger)


class TestExecutionLoggerInitDb:
    def test_migration_required_surfaces_full_recipe(
        self, isolated_paths, capsys
    ):
        isolated_paths.legacy_orbit.mkdir(parents=True)

        logger = ExecutionLogger("sample")

        err = capsys.readouterr().err
        assert "missioncache-auto: dashboard logging disabled" in err
        assert "mv ~/.claude/tasks.db" in err
        assert logger._enabled is False

    def test_fresh_install_stays_silent_noop(self, isolated_paths, capsys):
        logger = ExecutionLogger("sample")

        assert capsys.readouterr().err == ""
        assert logger._enabled is False
        assert not isolated_paths.new_db.exists()

    def test_db_present_enables_logging(self, isolated_paths):
        isolated_paths.new_db.parent.mkdir(parents=True)
        isolated_paths.new_db.touch()

        logger = ExecutionLogger("sample")

        assert logger._enabled is True


class TestWarnOncePerProcess:
    def test_migration_warning_emitted_once_across_inits(
        self, tmp_path, isolated_paths, capsys
    ):
        """Parallel mode constructs one ExecutionLogger plus N Workers in
        the parent process - the migration recipe must print once, not
        once per init."""
        isolated_paths.legacy_orbit.mkdir(parents=True)

        ExecutionLogger("sample")
        _make_worker(tmp_path)
        _make_worker(tmp_path)

        err = capsys.readouterr().err
        assert err.count("dashboard logging disabled") == 1

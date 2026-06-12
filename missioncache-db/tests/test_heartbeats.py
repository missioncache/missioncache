"""Integration tests for TaskDB heartbeat and time-tracking system.

Tests use a real SQLite database in tmp_path.
"""

from datetime import datetime, timedelta

import pytest

from orbit_db import TaskDB


@pytest.fixture
def db(tmp_path):
    """TaskDB backed by a temporary SQLite database."""
    db_path = tmp_path / "test.db"
    db = TaskDB(db_path=db_path)
    db.initialize()
    yield db
    db.close()


@pytest.fixture
def task_with_id(db):
    """Create a coding task and return (db, task)."""
    task = db.create_task("heartbeat-task")
    return db, task


# ── record_heartbeat ─────────────────────────────────────────────────────


class TestRecordHeartbeat:
    def test_record_heartbeat(self, task_with_id):
        """record_heartbeat inserts a row and returns a positive ID."""
        db, task = task_with_id
        hb_id = db.record_heartbeat(task.id, session_id="sess-1")
        assert hb_id > 0

        # Verify row exists
        with db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM heartbeats WHERE id = ?", (hb_id,)
            ).fetchone()
            assert row is not None
            assert row["task_id"] == task.id
            assert row["session_id"] == "sess-1"


# ── process_heartbeats ───────────────────────────────────────────────────


class TestProcessHeartbeats:
    def test_single_session(self, task_with_id):
        """Two heartbeats close together produce one session."""
        db, task = task_with_id
        now = datetime.now()

        # Insert two heartbeats 60 seconds apart (well within idle_timeout)
        with db.connection() as conn:
            conn.execute(
                "INSERT INTO heartbeats (task_id, timestamp, processed) VALUES (?, ?, 0)",
                (task.id, now.isoformat()),
            )
            conn.execute(
                "INSERT INTO heartbeats (task_id, timestamp, processed) VALUES (?, ?, 0)",
                (task.id, (now + timedelta(seconds=60)).isoformat()),
            )
            conn.commit()

        processed = db.process_heartbeats()
        assert processed == 2

        # Should have exactly one session
        with db.connection() as conn:
            sessions = conn.execute(
                "SELECT * FROM sessions WHERE task_id = ?", (task.id,)
            ).fetchall()
            assert len(sessions) == 1
            assert sessions[0]["duration_seconds"] > 0

    def test_gap_creates_new_session(self, task_with_id):
        """A gap exceeding idle_timeout creates a second session."""
        db, task = task_with_id
        idle_timeout = db.idle_timeout_seconds
        now = datetime.now()

        # Insert two heartbeats with a gap larger than idle_timeout
        with db.connection() as conn:
            conn.execute(
                "INSERT INTO heartbeats (task_id, timestamp, processed) VALUES (?, ?, 0)",
                (task.id, now.isoformat()),
            )
            conn.execute(
                "INSERT INTO heartbeats (task_id, timestamp, processed) VALUES (?, ?, 0)",
                (task.id, (now + timedelta(seconds=idle_timeout + 60)).isoformat()),
            )
            conn.commit()

        db.process_heartbeats()

        with db.connection() as conn:
            sessions = conn.execute(
                "SELECT * FROM sessions WHERE task_id = ?", (task.id,)
            ).fetchall()
            assert len(sessions) == 2


# ── get_task_time ────────────────────────────────────────────────────────


class TestGetTaskTime:
    def test_get_task_time_all(self, task_with_id):
        """get_task_time with period='all' sums all session durations."""
        db, task = task_with_id

        # Insert a session directly
        with db.connection() as conn:
            conn.execute(
                "INSERT INTO sessions (task_id, start_time, duration_seconds) VALUES (?, ?, ?)",
                (task.id, datetime.now().isoformat(), 3600),
            )
            conn.commit()

        total = db.get_task_time(task.id, period="all")
        assert total == 3600

    def test_get_task_time_today(self, task_with_id):
        """get_task_time with period='today' only counts today's sessions."""
        db, task = task_with_id
        now = datetime.now()
        yesterday = now - timedelta(days=1)

        with db.connection() as conn:
            # Today's session
            conn.execute(
                "INSERT INTO sessions (task_id, start_time, duration_seconds) VALUES (?, ?, ?)",
                (task.id, now.isoformat(), 1800),
            )
            # Yesterday's session
            conn.execute(
                "INSERT INTO sessions (task_id, start_time, duration_seconds) VALUES (?, ?, ?)",
                (task.id, yesterday.isoformat(), 3600),
            )
            conn.commit()

        today_total = db.get_task_time(task.id, period="today")
        all_total = db.get_task_time(task.id, period="all")

        assert today_total == 1800
        assert all_total == 5400

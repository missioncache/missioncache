"""Integration tests for TaskDB heartbeat and time-tracking system.

Tests use a real SQLite database in tmp_path.
"""

import sqlite3
import threading
from datetime import datetime, timedelta

import pytest

from missioncache_db import TaskDB


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

    def test_trailing_session_no_tail_when_still_active(self, task_with_id):
        """A trailing session whose last heartbeat is recent gets no
        assumed_work tail - work is likely ongoing and a later call
        aggregates its own batch, so padding every call would inflate time."""
        db, task = task_with_id
        now = datetime.now()

        # Two recent heartbeats 60s apart; the last one is ~now.
        with db.connection() as conn:
            conn.execute(
                "INSERT INTO heartbeats (task_id, timestamp, processed) VALUES (?, ?, 0)",
                (task.id, (now - timedelta(seconds=60)).isoformat()),
            )
            conn.execute(
                "INSERT INTO heartbeats (task_id, timestamp, processed) VALUES (?, ?, 0)",
                (task.id, now.isoformat()),
            )
            conn.commit()

        db.process_heartbeats()

        with db.connection() as conn:
            sessions = conn.execute(
                "SELECT * FROM sessions WHERE task_id = ?", (task.id,)
            ).fetchall()
        assert len(sessions) == 1
        # Only the 60s inter-heartbeat gap, no phantom assumed_work tail.
        assert sessions[0]["duration_seconds"] == 60

    def test_trailing_session_gets_tail_when_idle(self, task_with_id):
        """A trailing session whose last heartbeat is older than idle_timeout
        IS genuinely idle-terminated and gets the assumed_work tail."""
        db, task = task_with_id
        idle_timeout = db.idle_timeout_seconds
        assumed_work = db.assumed_work_seconds
        # Both heartbeats far in the past (older than idle_timeout from now),
        # but 60s apart so they form one session.
        base = datetime.now() - timedelta(seconds=idle_timeout + 700)

        with db.connection() as conn:
            conn.execute(
                "INSERT INTO heartbeats (task_id, timestamp, processed) VALUES (?, ?, 0)",
                (task.id, base.isoformat()),
            )
            conn.execute(
                "INSERT INTO heartbeats (task_id, timestamp, processed) VALUES (?, ?, 0)",
                (task.id, (base + timedelta(seconds=60)).isoformat()),
            )
            conn.commit()

        db.process_heartbeats()

        with db.connection() as conn:
            sessions = conn.execute(
                "SELECT * FROM sessions WHERE task_id = ?", (task.id,)
            ).fetchall()
        assert len(sessions) == 1
        assert sessions[0]["duration_seconds"] == 60 + assumed_work

    def test_reprocessing_does_not_duplicate_sessions(self, task_with_id):
        """Sequential idempotent reprocessing: a second process_heartbeats
        call finds the heartbeats already marked processed, so it does no
        work and creates no duplicate session rows. This verifies the
        idempotent replay path, not concurrent access (see the concurrency
        test below for the BEGIN IMMEDIATE claim under contention)."""
        db, task = task_with_id
        now = datetime.now()

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

        assert db.process_heartbeats() == 2
        assert db.process_heartbeats() == 0

        with db.connection() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE task_id = ?", (task.id,)
            ).fetchone()[0]
        assert count == 1

    def test_no_heartbeats_leaves_no_open_transaction(self, task_with_id):
        """The empty-heartbeat path commits its BEGIN IMMEDIATE claim instead
        of leaving a write transaction open on the reused connection. This
        verifies the empty path cleans up its own transaction; the blocking
        behaviour under contention is covered by the concurrency test."""
        db, task = task_with_id
        assert db.process_heartbeats() == 0
        with db.connection() as conn:
            assert conn.in_transaction is False

    def test_exception_in_aggregation_rolls_back_transaction(self, task_with_id):
        """A failure inside the aggregation loop must not leave the
        BEGIN IMMEDIATE write transaction open on the cached, reused
        connection. connection() rolls it back before re-raising, so
        in_transaction returns to False and the next call still works - a
        leaked transaction would make the next BEGIN IMMEDIATE fail with
        'cannot start a transaction within a transaction'."""
        db, task = task_with_id

        # A heartbeat with a bound task_id (so it passes the tasks JOIN and
        # enters the loop) but an unparseable timestamp. datetime.fromisoformat
        # raises inside the loop, after BEGIN IMMEDIATE has already been run.
        with db.connection() as conn:
            conn.execute(
                "INSERT INTO heartbeats (task_id, timestamp, processed) VALUES (?, ?, 0)",
                (task.id, "not-a-timestamp"),
            )
            conn.commit()

        with pytest.raises(ValueError):
            db.process_heartbeats()

        # The write transaction was rolled back, not left dangling open.
        assert db._connection.in_transaction is False

        # Drop the poison row, then a subsequent call must succeed - proving
        # the connection can BEGIN IMMEDIATE again (a leaked transaction would
        # raise here instead of aggregating cleanly).
        now = datetime.now()
        with db.connection() as conn:
            conn.execute("DELETE FROM heartbeats")
            conn.execute(
                "INSERT INTO heartbeats (task_id, timestamp, processed) VALUES (?, ?, 0)",
                (task.id, now.isoformat()),
            )
            conn.execute(
                "INSERT INTO heartbeats (task_id, timestamp, processed) VALUES (?, ?, 0)",
                (task.id, (now + timedelta(seconds=60)).isoformat()),
            )
            conn.commit()

        assert db.process_heartbeats() == 2
        with db.connection() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE task_id = ?", (task.id,)
            ).fetchone()[0]
        assert count == 1

    def test_begin_immediate_blocks_concurrent_runner(self, tmp_path):
        """Real concurrency check for the BEGIN IMMEDIATE claim: while a
        separate connection holds the write lock, a threaded
        process_heartbeats blocks on its own BEGIN IMMEDIATE until the lock is
        released, and afterwards exactly one session row exists (no
        double-aggregation). Deterministic via events + a bounded
        busy_timeout; no sleep-based synchronization."""
        db_path = tmp_path / "concurrency.db"

        # Seed the schema and two heartbeats (one session's worth) up front,
        # then close so this connection holds no locks during the test.
        seed = TaskDB(db_path=db_path)
        seed.initialize()
        task = seed.create_task("concurrency-task")
        now = datetime.now()
        with seed.connection() as conn:
            conn.execute(
                "INSERT INTO heartbeats (task_id, timestamp, processed) VALUES (?, ?, 0)",
                (task.id, now.isoformat()),
            )
            conn.execute(
                "INSERT INTO heartbeats (task_id, timestamp, processed) VALUES (?, ?, 0)",
                (task.id, (now + timedelta(seconds=60)).isoformat()),
            )
            conn.commit()
        seed.close()

        worker_ready = threading.Event()
        lock_held = threading.Event()
        result = {}

        def worker():
            worker_db = TaskDB(db_path=db_path)
            # Open + fully prime the connection now, BEFORE the lock is taken,
            # so the ONLY place this thread can block is process_heartbeats'
            # BEGIN IMMEDIATE. Shorten busy_timeout so a blocked claim waits a
            # bounded time rather than the 5s default.
            with worker_db.connection() as conn:
                conn.execute("PRAGMA busy_timeout = 3000")
            worker_ready.set()
            lock_held.wait(timeout=5)
            try:
                result["processed"] = worker_db.process_heartbeats()
            except Exception as exc:  # pragma: no cover - surfaced via result
                result["error"] = exc
            finally:
                worker_db.close()

        # Connection A holds the write lock while we probe for blocking.
        conn_a = sqlite3.connect(str(db_path))
        t = threading.Thread(target=worker, daemon=True)
        try:
            t.start()
            assert worker_ready.wait(timeout=5)

            conn_a.execute("BEGIN IMMEDIATE")
            lock_held.set()

            # While A holds the lock the worker cannot proceed: it is parked on
            # BEGIN IMMEDIATE. A short join that leaves it alive proves the block.
            t.join(timeout=0.5)
            assert t.is_alive()
            assert "processed" not in result

            # Release the lock; the worker's claim now succeeds.
            conn_a.rollback()
            t.join(timeout=5)
            assert not t.is_alive()
        finally:
            conn_a.close()

        assert result.get("error") is None
        assert result["processed"] == 2

        # The heartbeats aggregated into exactly one session - no duplicate.
        check = TaskDB(db_path=db_path)
        try:
            with check.connection() as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM sessions WHERE task_id = ?", (task.id,)
                ).fetchone()[0]
        finally:
            check.close()
        assert count == 1


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

    def test_get_task_time_week_excludes_iso_t_boundary(self, task_with_id):
        """period='week' excludes a session just OUTSIDE the 7-day window even
        though its start_time uses the ISO 'T' separator. The query wraps
        start_time in datetime() to normalize 'T' to a space before comparing
        with datetime('now',...,'-7 days'); if that wrapping is reverted, the
        raw-string compare sorts 'T' (0x54) after the space (0x20) and a
        same-date-but-earlier timestamp wrongly counts as inside the window."""
        db, task = task_with_id
        now = datetime.now()
        threshold = now - timedelta(days=7)

        with db.connection() as conn:
            # Clearly inside the window (3 days ago): must be counted.
            conn.execute(
                "INSERT INTO sessions (task_id, start_time, duration_seconds) VALUES (?, ?, ?)",
                (task.id, (now - timedelta(days=3)).isoformat(), 300),
            )
            # 1s past the 7-day threshold (genuinely older than a week), on the
            # threshold's own calendar date so only the 'T' vs ' ' separator
            # decides the compare: must be EXCLUDED.
            conn.execute(
                "INSERT INTO sessions (task_id, start_time, duration_seconds) VALUES (?, ?, ?)",
                (task.id, (threshold - timedelta(seconds=1)).isoformat(), 500),
            )
            conn.commit()

        assert db.get_task_time(task.id, period="week") == 300


# ── get_batch_task_times ─────────────────────────────────────────────────


class TestGetBatchTaskTimes:
    def test_get_batch_task_times_week_excludes_iso_t_boundary(self, db):
        """get_batch_task_times applies the same datetime()-normalized 7-day
        window as get_task_time: a just-outside ISO 'T' session is excluded,
        guarding against the same separator-compare regression."""
        task = db.create_task("batch-week-task")
        now = datetime.now()
        threshold = now - timedelta(days=7)

        with db.connection() as conn:
            conn.execute(
                "INSERT INTO sessions (task_id, start_time, duration_seconds) VALUES (?, ?, ?)",
                (task.id, (now - timedelta(days=3)).isoformat(), 300),
            )
            conn.execute(
                "INSERT INTO sessions (task_id, start_time, duration_seconds) VALUES (?, ?, ?)",
                (task.id, (threshold - timedelta(seconds=1)).isoformat(), 500),
            )
            conn.commit()

        result = db.get_batch_task_times([task.id], period="week")
        assert result[task.id] == 300

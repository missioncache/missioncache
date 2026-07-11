"""DuckDB analytics buckets sessions by their stored LOCAL wall-clock time.

Sessions are persisted as naive local-time strings (missioncache-db writes
datetime.now()), so date/hour buckets must read start_time directly. A prior
`AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Jerusalem'` wrapper re-declared the
already-local value as UTC and then shifted it forward by the host offset,
misplacing every hour/day bucket. These tests pin the buckets to the stored
wall-clock and are independent of the host's timezone.
"""

from __future__ import annotations

import pytest

from missioncache_dashboard.lib import analytics_db


@pytest.fixture
def duck(tmp_path):
    db = analytics_db.AnalyticsDB(db_path=tmp_path / "tasks.duckdb")
    with db.connection() as conn:
        # A late-evening session stored as naive local wall-clock (22:30).
        conn.execute(
            "INSERT INTO sessions (id, task_id, start_time, duration_seconds) "
            "VALUES (1, 1, '2026-03-15 22:30:00', 600)"
        )
    yield db
    db.close()


class TestLocalWallClockBucketing:
    def test_hourly_activity_uses_stored_hour(self, duck):
        """22:30 local buckets at hour 22, not shifted by the host UTC offset."""
        assert duck.get_hourly_activity("2026-03-15") == [
            {"hour": 22, "total_seconds": 600, "session_count": 1}
        ]

    def test_evening_session_stays_on_its_own_day(self, duck):
        """The evening session does not leak onto the next calendar day."""
        assert duck.get_hourly_activity("2026-03-16") == []

    def test_date_stats_counts_session_on_its_local_date(self, duck):
        stats = duck.get_date_stats("2026-03-15")
        assert stats["total_seconds"] == 600
        assert stats["session_count"] == 1

    def test_date_stats_excludes_other_day(self, duck):
        assert duck.get_date_stats("2026-03-16")["session_count"] == 0

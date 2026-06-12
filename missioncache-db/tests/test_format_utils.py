"""Tests for TaskDB static utility methods: format_duration, format_time_ago, encode_path_for_claude."""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from orbit_db import TaskDB


class TestFormatDuration:
    def test_seconds_only(self):
        assert TaskDB.format_duration(45) == "45s"

    def test_minutes(self):
        assert TaskDB.format_duration(150) == "2m"

    def test_hours_and_minutes(self):
        assert TaskDB.format_duration(3720) == "1h 2m"

    def test_exact_hours(self):
        assert TaskDB.format_duration(7200) == "2h"


class TestFormatTimeAgo:
    def test_none_returns_never(self):
        assert TaskDB.format_time_ago(None) == "never"

    def test_recent_returns_just_now(self):
        now = datetime.now()
        ts = now.isoformat()
        assert TaskDB.format_time_ago(ts) == "just now"

    def test_old_returns_date(self):
        old = datetime.now() - timedelta(days=30)
        ts = old.isoformat()
        result = TaskDB.format_time_ago(ts)
        # More than 7 days ago -> formatted as "Mon DD"
        assert old.strftime("%b %d") in result


class TestEncodePathForClaude:
    def test_basic_path(self):
        assert TaskDB.encode_path_for_claude("/home/user/project") == "-home-user-project"

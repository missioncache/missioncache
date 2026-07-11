"""Tests for JSONL parser functions and SessionMetrics."""

import os
from datetime import datetime, timedelta

import pytest

from missioncache_dashboard.lib import jsonl_parser
from missioncache_dashboard.lib.jsonl_parser import (
    SessionMetrics,
    decode_project_path,
    extract_tool_calls_from_content,
    get_project_short_name,
    parse_jsonl_line,
    parse_timestamp,
)


# --- parse_jsonl_line ---


class TestParseJsonlLine:
    def test_valid_json(self):
        result = parse_jsonl_line('{"type": "user", "message": "hello"}')
        assert result == {"type": "user", "message": "hello"}

    def test_invalid_json(self):
        assert parse_jsonl_line("not json at all") is None

    def test_empty_string(self):
        assert parse_jsonl_line("") is None

    def test_whitespace_only(self):
        assert parse_jsonl_line("   \n  ") is None


# --- parse_timestamp ---


class TestParseTimestamp:
    def test_utc_z_suffix(self):
        result = parse_timestamp("2026-04-01T10:00:00Z")
        assert result is not None
        assert isinstance(result, datetime)

    def test_none_input(self):
        assert parse_timestamp(None) is None

    def test_invalid_string(self):
        assert parse_timestamp("not-a-timestamp") is None

    def test_empty_string(self):
        assert parse_timestamp("") is None


# --- extract_tool_calls_from_content ---


class TestExtractToolCalls:
    def test_list_with_tool_use(self):
        content = [
            {"type": "text", "text": "hello"},
            {"type": "tool_use", "name": "read_file"},
            {"type": "tool_use", "name": "write_file"},
        ]
        assert extract_tool_calls_from_content(content) == 2

    def test_string_content(self):
        assert extract_tool_calls_from_content("just a string") == 0

    def test_none_content(self):
        assert extract_tool_calls_from_content(None) == 0

    def test_empty_list(self):
        assert extract_tool_calls_from_content([]) == 0


# --- decode_project_path ---


class TestDecodeProjectPath:
    def test_encoded_path(self):
        result = decode_project_path("-Users-alice-projects-demo")
        assert result == "/Users/alice/projects/demo"

    def test_plain_name(self):
        result = decode_project_path("my-project")
        assert result == "my-project"


# --- get_project_short_name ---


class TestGetProjectShortName:
    def test_encoded_path(self):
        result = get_project_short_name("-Users-alice-projects-demo")
        assert result == "demo"

    def test_single_segment(self):
        result = get_project_short_name("project")
        assert result == "project"


# --- SessionMetrics ---


class TestSessionMetrics:
    def test_active_seconds_normal(self):
        """Events within 5-min gap are counted as active time."""
        now = datetime.now()
        metrics = SessionMetrics(
            session_id="test",
            project_path="test-project",
            event_timestamps=[
                now,
                now + timedelta(seconds=60),
                now + timedelta(seconds=120),
            ],
        )
        assert metrics.active_seconds_for_date(None) == 120

    def test_active_seconds_with_gap(self):
        """Gaps exceeding 5 minutes are excluded from active time."""
        now = datetime.now()
        metrics = SessionMetrics(
            session_id="test",
            project_path="test-project",
            event_timestamps=[
                now,
                now + timedelta(seconds=60),
                # 10-minute gap - should be excluded
                now + timedelta(seconds=660),
                now + timedelta(seconds=720),
            ],
        )
        # 60s from first pair + 60s from second pair = 120s
        assert metrics.active_seconds_for_date(None) == 120

    def test_total_messages(self):
        metrics = SessionMetrics(
            session_id="test",
            project_path="test-project",
            user_message_count=5,
            assistant_message_count=3,
        )
        assert metrics.total_messages == 8

    def test_total_tokens(self):
        metrics = SessionMetrics(
            session_id="test",
            project_path="test-project",
            input_tokens=1000,
            output_tokens=500,
        )
        assert metrics.total_tokens == 1500


# --- get_jsonl_files_for_date (mtime window) ---


class TestGetJsonlFilesForDate:
    """The mtime window must key off the REQUESTED date, not now()-max_age_days.

    A file for a date older than max_age_days is not re-touched after the session
    ends, so keying the lower bound to now()-max_age_days hid those files and zeroed
    the history view's Claude columns for older days.
    """

    def _make_file(self, projects_dir, name, mtime):
        proj = projects_dir / "-Users-alice-demo"
        proj.mkdir(parents=True, exist_ok=True)
        f = proj / f"{name}.jsonl"
        f.write_text("{}\n")
        os.utime(f, (mtime, mtime))
        return f

    def test_old_date_file_is_yielded(self, tmp_path, monkeypatch):
        """A file whose mtime is on a date older than max_age_days is still yielded
        when that older date is explicitly requested (the regression)."""
        monkeypatch.setattr(jsonl_parser, "PROJECTS_DIR", tmp_path)
        old_day = datetime.now() - timedelta(days=5)
        f = self._make_file(
            tmp_path, "old", old_day.replace(hour=12, minute=0, second=0).timestamp()
        )
        got = list(
            jsonl_parser.get_jsonl_files_for_date(
                old_day.strftime("%Y-%m-%d"), max_age_days=2
            )
        )
        assert f in got

    def test_file_before_requested_date_excluded(self, tmp_path, monkeypatch):
        """A file older than the requested date's midnight is excluded (lower bound)."""
        monkeypatch.setattr(jsonl_parser, "PROJECTS_DIR", tmp_path)
        requested = datetime.now() - timedelta(days=5)
        before = requested - timedelta(days=1)
        f = self._make_file(tmp_path, "before", before.replace(hour=12).timestamp())
        got = list(
            jsonl_parser.get_jsonl_files_for_date(
                requested.strftime("%Y-%m-%d"), max_age_days=2
            )
        )
        assert f not in got

    def test_file_after_upper_bound_excluded(self, tmp_path, monkeypatch):
        """A file newer than requested date + max_age_days is excluded (upper bound)."""
        monkeypatch.setattr(jsonl_parser, "PROJECTS_DIR", tmp_path)
        requested = datetime.now() - timedelta(days=5)
        f = self._make_file(tmp_path, "after", datetime.now().timestamp())
        got = list(
            jsonl_parser.get_jsonl_files_for_date(
                requested.strftime("%Y-%m-%d"), max_age_days=2
            )
        )
        assert f not in got

    def test_today_file_yielded_by_default(self, tmp_path, monkeypatch):
        """Today's file is yielded for a today (default) request."""
        monkeypatch.setattr(jsonl_parser, "PROJECTS_DIR", tmp_path)
        f = self._make_file(tmp_path, "today", datetime.now().timestamp())
        got = list(jsonl_parser.get_jsonl_files_for_date(max_age_days=2))
        assert f in got

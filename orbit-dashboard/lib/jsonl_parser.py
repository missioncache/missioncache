#!/usr/bin/env python3
"""
JSONL Parser for Claude Code Session Files.

Parses JSONL files from ~/.claude/projects/ to extract activity metrics
like message counts, tool calls, and token usage for display in the
Orbit Dashboard.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator


# =============================================================================
# Configuration
# =============================================================================

PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Event types to count
USER_MESSAGE_TYPES = {"user"}
ASSISTANT_MESSAGE_TYPES = {"assistant"}


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class SessionMetrics:
    """Metrics for a single Claude Code session."""

    session_id: str
    project_path: str  # The project directory name (encoded path)
    cwd: str | None = None  # Actual working directory
    git_branch: str | None = None
    first_event_time: datetime | None = None
    last_event_time: datetime | None = None
    user_message_count: int = 0
    assistant_message_count: int = 0
    tool_call_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    event_timestamps: list = None  # All event timestamps for active time calculation

    def __post_init__(self):
        if self.event_timestamps is None:
            self.event_timestamps = []

    @property
    def total_messages(self) -> int:
        return self.user_message_count + self.assistant_message_count

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def duration_seconds(self) -> int:
        """Active session duration based on gaps between events.

        Only counts time when consecutive events are within 5 minutes of each other.
        This gives a realistic estimate of actual work time, not wall-clock time.
        """
        return self.active_seconds_for_date(None)

    def active_seconds_for_date(self, target_date=None) -> int:
        """Calculate active time for events, optionally filtered by date.

        Args:
            target_date: If provided, only count events on this date.
                        If None, count all events.

        Returns:
            Active seconds based on gaps between consecutive events (max 5 min gap)
        """
        if not self.event_timestamps or len(self.event_timestamps) < 2:
            return 0

        # Filter timestamps by date if specified
        if target_date:
            filtered_ts = [
                ts for ts in self.event_timestamps if ts.date() == target_date
            ]
        else:
            filtered_ts = self.event_timestamps

        if len(filtered_ts) < 2:
            return 0

        # Sort timestamps and calculate active time
        sorted_ts = sorted(filtered_ts)
        max_gap_seconds = 5 * 60  # 5 minutes
        active_seconds = 0

        for i in range(1, len(sorted_ts)):
            gap = (sorted_ts[i] - sorted_ts[i - 1]).total_seconds()
            if gap <= max_gap_seconds:
                active_seconds += gap

        return int(active_seconds)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "project_path": self.project_path,
            "cwd": self.cwd,
            "git_branch": self.git_branch,
            "first_event_time": self.first_event_time.isoformat()
            if self.first_event_time
            else None,
            "last_event_time": self.last_event_time.isoformat()
            if self.last_event_time
            else None,
            "user_message_count": self.user_message_count,
            "assistant_message_count": self.assistant_message_count,
            "tool_call_count": self.tool_call_count,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "total_messages": self.total_messages,
            "total_tokens": self.total_tokens,
            "duration_seconds": self.duration_seconds,
        }


@dataclass
class HourlyActivity:
    """Activity metrics for a single hour."""

    hour: int
    task_seconds: int = 0  # From orbit sessions
    claude_messages: int = 0
    claude_tool_calls: int = 0
    claude_tokens: int = 0
    claude_seconds: int = 0  # Estimated work time from JSONL timestamps
    session_count: int = 0

    def to_dict(self) -> dict:
        return {
            "hour": self.hour,
            "task_seconds": self.task_seconds,
            "claude_messages": self.claude_messages,
            "claude_tool_calls": self.claude_tool_calls,
            "claude_tokens": self.claude_tokens,
            "claude_seconds": self.claude_seconds,
            "session_count": self.session_count,
        }


# =============================================================================
# JSONL Parsing Functions
# =============================================================================


def parse_jsonl_line(line: str) -> dict | None:
    """Parse a single JSONL line, returning None on error."""
    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def parse_timestamp(ts_str: str | None) -> datetime | None:
    """Parse ISO timestamp string to datetime in local timezone.

    JSONL files store timestamps in UTC. We convert to local time
    so that date/hour extraction matches user's local day boundaries.
    """
    if not ts_str:
        return None
    try:
        # Handle various ISO formats (Z suffix = UTC)
        ts_str = ts_str.replace("Z", "+00:00")
        utc_dt = datetime.fromisoformat(ts_str)
        # Convert to local timezone for correct date/hour extraction
        return utc_dt.astimezone()
    except ValueError:
        return None


def extract_tool_calls_from_content(content: list | str | None) -> int:
    """Count tool_use items in message content."""
    if not content:
        return 0
    if isinstance(content, str):
        return 0
    if isinstance(content, list):
        return sum(
            1
            for item in content
            if isinstance(item, dict) and item.get("type") == "tool_use"
        )
    return 0


def parse_session_file(filepath: Path) -> SessionMetrics | None:
    """Parse a single JSONL session file and extract metrics.

    Args:
        filepath: Path to the JSONL file

    Returns:
        SessionMetrics object or None if file cannot be parsed
    """
    if not filepath.exists():
        return None

    # Extract session ID from filename (UUID or agent-UUID format)
    session_id = filepath.stem
    project_path = filepath.parent.name

    metrics = SessionMetrics(
        session_id=session_id,
        project_path=project_path,
    )

    seen_uuids: set[str] = set()  # Dedupe messages by UUID

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                entry = parse_jsonl_line(line)
                if not entry:
                    continue

                # Dedupe by UUID
                uuid = entry.get("uuid")
                if uuid:
                    if uuid in seen_uuids:
                        continue
                    seen_uuids.add(uuid)

                # Extract common fields
                entry_type = entry.get("type")
                timestamp = parse_timestamp(entry.get("timestamp"))

                # Update time range and collect all timestamps for active time calculation
                if timestamp:
                    metrics.event_timestamps.append(timestamp)
                    if (
                        metrics.first_event_time is None
                        or timestamp < metrics.first_event_time
                    ):
                        metrics.first_event_time = timestamp
                    if (
                        metrics.last_event_time is None
                        or timestamp > metrics.last_event_time
                    ):
                        metrics.last_event_time = timestamp

                # Extract cwd and git branch from first entry that has them
                if not metrics.cwd and entry.get("cwd"):
                    metrics.cwd = entry.get("cwd")
                if not metrics.git_branch and entry.get("gitBranch"):
                    metrics.git_branch = entry.get("gitBranch")

                # Count messages by type
                if entry_type in USER_MESSAGE_TYPES:
                    metrics.user_message_count += 1

                elif entry_type in ASSISTANT_MESSAGE_TYPES:
                    metrics.assistant_message_count += 1

                    # Extract token usage from message
                    message = entry.get("message", {})
                    usage = message.get("usage", {})

                    metrics.input_tokens += usage.get("input_tokens", 0)
                    metrics.output_tokens += usage.get("output_tokens", 0)
                    metrics.cache_read_tokens += usage.get("cache_read_input_tokens", 0)
                    metrics.cache_creation_tokens += usage.get(
                        "cache_creation_input_tokens", 0
                    )

                    # Count tool calls in content
                    content = message.get("content", [])
                    metrics.tool_call_count += extract_tool_calls_from_content(content)

    except Exception:
        return None

    # Only return if we found any messages
    if metrics.total_messages == 0:
        return None

    return metrics


def get_jsonl_files_for_date(
    date: str | None = None, max_age_days: int = 1
) -> Iterator[Path]:
    """Get JSONL files that were modified on or after the given date.

    Uses file mtime for efficient filtering without parsing.

    Args:
        date: Date in YYYY-MM-DD format (defaults to today)
        max_age_days: Only check files modified within this many days

    Yields:
        Path objects for matching JSONL files
    """
    if not PROJECTS_DIR.exists():
        return

    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")

    # Calculate cutoff timestamp
    cutoff_date = datetime.strptime(date, "%Y-%m-%d")
    cutoff_ts = cutoff_date.timestamp()

    # Also limit to recent files for performance
    max_age_cutoff = (datetime.now() - timedelta(days=max_age_days)).timestamp()
    effective_cutoff = max(cutoff_ts, max_age_cutoff)

    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue

        for jsonl_file in project_dir.glob("*.jsonl"):
            try:
                mtime = jsonl_file.stat().st_mtime
                if mtime >= effective_cutoff:
                    yield jsonl_file
            except OSError:
                continue


def get_session_activity_by_hour(date: str | None = None) -> dict[int, HourlyActivity]:
    """Get Claude Code activity aggregated by hour for a specific date.

    Args:
        date: Date in YYYY-MM-DD format (defaults to today)

    Returns:
        Dictionary mapping hour (0-23) to HourlyActivity objects
    """
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")

    target_date = datetime.strptime(date, "%Y-%m-%d").date()
    hourly: dict[int, HourlyActivity] = {}

    # Track sessions we've already counted to avoid double-counting
    counted_sessions: set[str] = set()

    for jsonl_file in get_jsonl_files_for_date(date, max_age_days=2):
        metrics = parse_session_file(jsonl_file)
        if not metrics or not metrics.first_event_time:
            continue

        # Check if session has activity on target date
        session_date = metrics.first_event_time.date()
        use_last_event = False

        if session_date != target_date:
            # Session started on different day - check if it ended on target date
            if (
                metrics.last_event_time
                and metrics.last_event_time.date() == target_date
            ):
                session_date = target_date
                use_last_event = True  # Use last event's hour since that's when it was on target date
            else:
                continue

        # Determine the hour based on which event time qualified the session
        hour = (
            metrics.last_event_time.hour
            if use_last_event
            else metrics.first_event_time.hour
        )

        # Skip if already counted this session
        session_key = f"{metrics.session_id}:{hour}"
        if session_key in counted_sessions:
            continue
        counted_sessions.add(session_key)

        # Initialize hour if needed
        if hour not in hourly:
            hourly[hour] = HourlyActivity(hour=hour)

        # Aggregate metrics
        hourly[hour].claude_messages += metrics.total_messages
        hourly[hour].claude_tool_calls += metrics.tool_call_count
        hourly[hour].claude_tokens += metrics.total_tokens
        hourly[hour].session_count += 1

    return hourly


def get_session_activity_by_date(days: int = 7) -> list[dict]:
    """Get Claude Code activity aggregated by date for the last N days.

    Args:
        days: Number of days to look back

    Returns:
        List of dictionaries with date and activity metrics
    """
    daily: dict[str, dict] = {}

    for i in range(days):
        date = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        hourly = get_session_activity_by_hour(date)

        if hourly:
            daily[date] = {
                "date": date,
                "claude_messages": sum(h.claude_messages for h in hourly.values()),
                "claude_tool_calls": sum(h.claude_tool_calls for h in hourly.values()),
                "claude_tokens": sum(h.claude_tokens for h in hourly.values()),
                "session_count": sum(h.session_count for h in hourly.values()),
            }
        else:
            daily[date] = {
                "date": date,
                "claude_messages": 0,
                "claude_tool_calls": 0,
                "claude_tokens": 0,
                "session_count": 0,
            }

    # Sort by date descending
    return sorted(daily.values(), key=lambda x: x["date"], reverse=True)


def get_all_sessions_for_date(date: str | None = None) -> list[SessionMetrics]:
    """Get all parsed sessions for a specific date.

    Args:
        date: Date in YYYY-MM-DD format (defaults to today)

    Returns:
        List of SessionMetrics objects
    """
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")

    target_date = datetime.strptime(date, "%Y-%m-%d").date()
    sessions: list[SessionMetrics] = []

    for jsonl_file in get_jsonl_files_for_date(date, max_age_days=2):
        metrics = parse_session_file(jsonl_file)
        if not metrics or not metrics.first_event_time:
            continue

        # Check if session has activity on target date
        session_date = metrics.first_event_time.date()
        if session_date != target_date:
            if (
                metrics.last_event_time
                and metrics.last_event_time.date() == target_date
            ):
                pass  # Session spans into target date
            else:
                continue

        sessions.append(metrics)

    # Sort by first event time
    sessions.sort(key=lambda s: s.first_event_time or datetime.min)
    return sessions


# =============================================================================
# Utility Functions
# =============================================================================


def decode_project_path(encoded_path: str) -> str:
    """Decode the project directory name to a readable path.

    Claude Code encodes paths like: -home-user-projects-claude-dev
    This converts to: /home/user/projects/claude_dev
    """
    if encoded_path.startswith("-"):
        # Remove leading dash and replace remaining dashes with slashes
        # But be careful about underscores vs dashes in actual names
        parts = encoded_path[1:].split("-")
        return "/" + "/".join(parts)
    return encoded_path


def get_project_short_name(encoded_path: str) -> str:
    """Extract short project name from encoded path.

    Example: -home-user-projects-claude-dev -> claude-dev
    """
    parts = encoded_path.rstrip("-").split("-")
    if parts:
        return parts[-1]
    return encoded_path

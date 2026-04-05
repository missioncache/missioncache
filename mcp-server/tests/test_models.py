"""Tests for Pydantic models in mcp_orbit.models."""

import pytest

from mcp_orbit.models import (
    HeartbeatResult,
    ListTasksResult,
    TaskDetail,
    TaskProgress,
    TaskSummary,
)


class TestTaskProgress:
    def test_defaults(self):
        progress = TaskProgress(completion_pct=50)
        assert progress.completion_pct == 50
        assert progress.total_items == 0
        assert progress.completed_items == 0
        assert progress.remaining_summary is None


class TestTaskSummary:
    def test_alias_field(self):
        """task_type field accepts 'type' alias."""
        summary = TaskSummary(id=1, name="test", status="active", type="coding")
        assert summary.task_type == "coding"


class TestTaskDetail:
    def test_inherits_from_summary(self):
        detail = TaskDetail(
            id=1,
            name="test",
            status="active",
            type="coding",
            full_path="/tmp/test",
            created_at="2026-04-01",
            updated_at="2026-04-01",
        )
        assert detail.full_path == "/tmp/test"
        assert detail.task_type == "coding"
        assert detail.progress is None
        assert detail.subtasks == []


class TestHeartbeatResult:
    def test_fields(self):
        result = HeartbeatResult(heartbeat_id=1, task_id=2, task_name="my-task")
        assert result.heartbeat_id == 1
        assert result.task_id == 2
        assert result.task_name == "my-task"


class TestListTasksResult:
    def test_serialization(self):
        summary = TaskSummary(id=1, name="test", status="active", type="coding")
        result = ListTasksResult(tasks=[summary], total_count=1)
        data = result.model_dump(by_alias=True)
        assert data["total_count"] == 1
        assert len(data["tasks"]) == 1
        assert data["tasks"][0]["type"] == "coding"
        assert data["filter_applied"] is None

"""Tests for orbit_auto.models module."""

from datetime import datetime

from orbit_auto.models import Config, State, Task, TaskStatus


class TestTaskDisplayId:
    def test_simple_padded(self):
        task = Task(id="01", title="Test")
        assert task.display_id == "1"

    def test_subtask_format(self):
        task = Task(id="01-02", title="Test")
        assert task.display_id == "1.2"

    def test_zero_id(self):
        task = Task(id="00", title="Test")
        assert task.display_id == "0"

    def test_no_leading_zeros(self):
        task = Task(id="12", title="Test")
        assert task.display_id == "12"


class TestConfigPostInit:
    def test_caps_max_workers_at_12(self):
        config = Config(max_workers=20)
        assert config.max_workers == 12

    def test_min_workers_at_1(self):
        config = Config(max_workers=0)
        assert config.max_workers == 1

    def test_negative_timeout_clamped_to_zero(self):
        config = Config(task_timeout=-5)
        assert config.task_timeout == 0

    def test_valid_config_unchanged(self):
        config = Config(max_workers=8, task_timeout=1800)
        assert config.max_workers == 8
        assert config.task_timeout == 1800


class TestStateRoundTrip:
    def test_to_dict_from_dict_preserves_fields(self):
        now = datetime(2026, 4, 3, 12, 0, 0)
        task = Task(id="01", title="Setup", status=TaskStatus.COMPLETED, worker=1, attempts=2)
        original = State(
            status="running",
            started=now,
            tasks={"01": task},
            workers={1: "01"},
        )

        data = original.to_dict()
        restored = State.from_dict(data)

        assert restored.status == "running"
        assert restored.started == now
        assert "01" in restored.tasks
        assert restored.tasks["01"].status == TaskStatus.COMPLETED
        assert restored.tasks["01"].worker == 1
        assert restored.tasks["01"].attempts == 2

    def test_from_dict_with_error_message(self):
        data = {
            "status": "failed",
            "started": "2026-04-03T12:00:00",
            "tasks": {
                "02": {
                    "status": "failed",
                    "worker": 2,
                    "attempts": 3,
                    "error_message": "Something broke",
                }
            },
            "workers": {},
        }
        state = State.from_dict(data)
        assert state.tasks["02"].error_message == "Something broke"

    def test_from_dict_empty_tasks(self):
        data = {
            "status": "completed",
            "started": "2026-04-03T12:00:00",
            "tasks": {},
            "workers": {},
        }
        state = State.from_dict(data)
        assert state.tasks == {}

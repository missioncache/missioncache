"""Tests for error classes - no I/O, no mocking."""

from mcp_orbit.errors import (
    ErrorCode,
    InvalidStateError,
    OrbitError,
    TaskNotFoundError,
    ValidationError,
)


class TestOrbitError:
    def test_to_dict_structure(self):
        """to_dict returns correct keys and values."""
        err = OrbitError(ErrorCode.OPERATION_FAILED, "something broke", {"key": "val"})
        d = err.to_dict()
        assert d == {
            "error": True,
            "code": "OPERATION_FAILED",
            "message": "something broke",
            "details": {"key": "val"},
        }


class TestTaskNotFoundError:
    def test_by_id(self):
        """Constructs message from integer task ID."""
        err = TaskNotFoundError(42)
        assert err.code == ErrorCode.TASK_NOT_FOUND
        assert "42" in err.message
        assert err.details == {"task_id": 42}

    def test_by_name(self):
        """Constructs message from string task name."""
        err = TaskNotFoundError("my-task")
        assert err.code == ErrorCode.TASK_NOT_FOUND
        assert "my-task" in err.message
        assert err.details == {"task_id": "my-task"}


class TestValidationError:
    def test_with_field(self):
        """Includes field name in details when provided."""
        err = ValidationError("bad input", field="task_name")
        assert err.code == ErrorCode.VALIDATION_ERROR
        assert err.message == "bad input"
        assert err.details == {"field": "task_name"}

    def test_without_field(self):
        """Details are empty when no field is provided."""
        err = ValidationError("bad input")
        assert err.details == {}


class TestInvalidStateError:
    def test_with_states(self):
        """Includes current and expected state in details."""
        err = InvalidStateError(
            "cannot complete",
            current_state="completed",
            expected_state="active",
        )
        assert err.code == ErrorCode.INVALID_STATE
        assert err.details == {
            "current_state": "completed",
            "expected_state": "active",
        }

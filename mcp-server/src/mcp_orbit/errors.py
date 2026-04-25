"""Error codes and handling for the orbit MCP server."""

from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    """Standard error codes for structured error responses."""

    TASK_NOT_FOUND = "TASK_NOT_FOUND"
    REPO_NOT_FOUND = "REPO_NOT_FOUND"
    FILE_NOT_FOUND = "FILE_NOT_FOUND"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    DB_ERROR = "DB_ERROR"
    PERMISSION_ERROR = "PERMISSION_ERROR"
    INVALID_STATE = "INVALID_STATE"
    OPERATION_FAILED = "OPERATION_FAILED"
    ALREADY_EXISTS = "ALREADY_EXISTS"


class OrbitError(Exception):
    """Base exception for orbit errors with structured response."""

    def __init__(
        self, code: ErrorCode, message: str, details: dict[str, Any] | None = None
    ):
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(message)

    def to_dict(self) -> dict:
        """Convert to dictionary for MCP response."""
        return {
            "error": True,
            "code": self.code.value,
            "message": self.message,
            "details": self.details,
        }


class TaskNotFoundError(OrbitError):
    """Task not found in database."""

    def __init__(self, task_id: int | str, message: str | None = None):
        super().__init__(
            ErrorCode.TASK_NOT_FOUND,
            message or f"Task not found: {task_id}",
            {"task_id": task_id},
        )


class OrbitFileNotFoundError(OrbitError):
    """File not found on filesystem."""

    def __init__(self, path: str, message: str | None = None):
        super().__init__(
            ErrorCode.FILE_NOT_FOUND,
            message or f"File not found: {path}",
            {"path": path},
        )


class ValidationError(OrbitError):
    """Input validation failed."""

    def __init__(self, message: str, field: str | None = None):
        details = {"field": field} if field else {}
        super().__init__(ErrorCode.VALIDATION_ERROR, message, details)


class InvalidStateError(OrbitError):
    """Operation invalid for current state."""

    def __init__(
        self,
        message: str,
        current_state: str | None = None,
        expected_state: str | None = None,
    ):
        details = {}
        if current_state:
            details["current_state"] = current_state
        if expected_state:
            details["expected_state"] = expected_state
        super().__init__(ErrorCode.INVALID_STATE, message, details)

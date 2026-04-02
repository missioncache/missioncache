"""
Database logging for orbit-auto executions.

Integrates with the orbit task database to log execution runs
and their output for dashboard visualization.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from orbit_auto.models import Config


class ExecutionLogger:
    """
    Logs orbit-auto execution runs to the task database.

    This enables the dashboard to display execution history,
    progress, and streaming logs.

    Retention Policy:
    - Keep last 10 executions per task
    - Delete executions older than 30 days
    - Cleanup runs automatically when starting new executions

    Usage:
        logger = ExecutionLogger(task_name, config)
        if logger.start():
            logger.log("Starting execution", level="info")
            # ... do work ...
            logger.log_task_claimed(worker_id, task_id)
            logger.log_task_completed(task_id)
            logger.update_progress(completed=5, failed=0)
            logger.finish(status="completed")
    """

    # Retention policy defaults
    KEEP_EXECUTIONS_PER_TASK = 10
    DELETE_OLDER_THAN_DAYS = 30

    def __init__(
        self,
        task_name: str,
        config: Optional[Config] = None,
        mode: str = "parallel",
    ) -> None:
        self.task_name = task_name
        self.mode = mode
        self.worker_count = config.max_workers if config else None

        self._db = None
        self._task_id: Optional[int] = None
        self._execution_id: Optional[int] = None
        self._enabled = False

        self._init_db()

    def _init_db(self) -> None:
        """Initialize database connection if orbit_db is available."""
        try:
            from orbit_db import TaskDB

            db_path = Path.home() / ".claude" / "tasks.db"
            if db_path.exists():
                self._db = TaskDB(str(db_path))
                self._enabled = True
        except ImportError:
            pass
        except Exception:
            pass

    @property
    def enabled(self) -> bool:
        """Check if logging is enabled."""
        return self._enabled and self._execution_id is not None

    @property
    def execution_id(self) -> Optional[int]:
        """Get the current execution ID."""
        return self._execution_id

    def start(self, total_subtasks: int = 0) -> bool:
        """
        Start logging a new execution run.

        Looks up the task by name and creates an execution record.
        Also performs cleanup of old executions based on retention policy.

        Args:
            total_subtasks: Total number of subtasks to execute

        Returns:
            True if logging started successfully, False otherwise.
        """
        if not self._enabled or self._db is None:
            return False

        try:
            # Look up task by name
            task = self._db.get_task_by_name(self.task_name, status="active")
            if not task:
                return False

            self._task_id = task.id

            # Perform cleanup of old executions (non-blocking)
            self._cleanup_old_executions()

            # Create execution record
            self._execution_id = self._db.create_auto_execution(
                task_id=self._task_id,
                mode=self.mode,
                worker_count=self.worker_count,
                total_subtasks=total_subtasks,
            )

            # Log start message
            self.log(
                f"Execution started: {self.task_name} ({self.mode} mode"
                + (f", {self.worker_count} workers)" if self.worker_count else ")"),
                level="info",
            )

            return True

        except Exception:
            self._enabled = False
            return False

    def _cleanup_old_executions(self) -> None:
        """Clean up old executions based on retention policy."""
        if not self._enabled or self._db is None:
            return

        try:
            result = self._db.cleanup_old_auto_executions(
                keep_per_task=self.KEEP_EXECUTIONS_PER_TASK,
                older_than_days=self.DELETE_OLDER_THAN_DAYS,
            )
            # Only log if something was cleaned up
            if result.get("executions_deleted", 0) > 0:
                self.log(
                    f"Cleaned up {result['executions_deleted']} old executions "
                    f"({result['logs_deleted']} log entries)",
                    level="debug",
                )
        except Exception:
            pass  # Cleanup failures shouldn't block execution

    def log(
        self,
        message: str,
        level: str = "info",
        worker_id: Optional[int] = None,
        subtask_id: Optional[str] = None,
    ) -> None:
        """
        Add a log entry to the execution.

        Args:
            message: Log message
            level: Log level (debug, info, warn, error, success)
            worker_id: Worker ID if from a specific worker
            subtask_id: Subtask ID if related to a specific subtask
        """
        if not self.enabled or self._db is None:
            return

        try:
            self._db.add_auto_execution_log(
                execution_id=self._execution_id,
                message=message,
                level=level,
                worker_id=worker_id,
                subtask_id=subtask_id,
            )
        except Exception:
            pass

    def log_task_claimed(self, worker_id: int, task_id: str, task_title: str = "") -> None:
        """Log that a worker has claimed a task."""
        msg = f"Claimed task {task_id}"
        if task_title:
            msg += f": {task_title}"
        self.log(msg, level="info", worker_id=worker_id, subtask_id=task_id)

    def log_task_completed(
        self,
        task_id: str,
        worker_id: Optional[int] = None,
        duration: Optional[float] = None,
        summary: Optional[str] = None,
    ) -> None:
        """Log that a task was completed successfully."""
        msg = f"Task {task_id} completed"
        if duration:
            msg += f" ({int(duration)}s)"
        if summary:
            msg += f": {summary}"
        self.log(msg, level="success", worker_id=worker_id, subtask_id=task_id)

    def log_task_failed(
        self,
        task_id: str,
        error: str,
        worker_id: Optional[int] = None,
        attempt: Optional[int] = None,
        max_attempts: Optional[int] = None,
    ) -> None:
        """Log that a task failed."""
        msg = f"Task {task_id} failed"
        if attempt and max_attempts:
            msg += f" (attempt {attempt}/{max_attempts})"
        msg += f": {error}"
        self.log(msg, level="error", worker_id=worker_id, subtask_id=task_id)

    def log_task_retrying(
        self,
        task_id: str,
        attempt: int,
        max_attempts: int,
        previous_error: str,
        worker_id: Optional[int] = None,
    ) -> None:
        """Log that a task is being retried."""
        msg = f"Retrying task {task_id} (attempt {attempt}/{max_attempts}): {previous_error}"
        self.log(msg, level="warn", worker_id=worker_id, subtask_id=task_id)

    def update_progress(
        self,
        completed: Optional[int] = None,
        failed: Optional[int] = None,
    ) -> None:
        """Update execution progress counters."""
        if not self.enabled or self._db is None:
            return

        try:
            self._db.update_auto_execution(
                execution_id=self._execution_id,
                completed_subtasks=completed,
                failed_subtasks=failed,
            )
        except Exception:
            pass

    def finish(
        self,
        status: str = "completed",
        error_message: Optional[str] = None,
    ) -> None:
        """
        Finish the execution run.

        Args:
            status: Final status (completed, failed, cancelled)
            error_message: Error message if failed
        """
        if not self.enabled or self._db is None:
            return

        try:
            # Log final message
            if status == "completed":
                self.log("Execution completed successfully", level="success")
            elif status == "failed":
                msg = "Execution failed"
                if error_message:
                    msg += f": {error_message}"
                self.log(msg, level="error")
            elif status == "cancelled":
                self.log("Execution cancelled", level="warn")

            # Update execution record
            self._db.update_auto_execution(
                execution_id=self._execution_id,
                status=status,
                error_message=error_message,
            )
        except Exception:
            pass


def create_logger(
    task_name: str,
    config: Optional[Config] = None,
    mode: str = "parallel",
) -> ExecutionLogger:
    """Create an execution logger for orbit-auto."""
    return ExecutionLogger(task_name, config, mode)

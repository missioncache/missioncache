"""
State management for orbit-auto parallel execution.

Provides atomic state file operations with file locking to ensure
safe concurrent access between multiple worker processes.
"""

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from orbit_auto.dag import DAG


def _atomic_write_text(path: Path, content: str) -> None:
    """Write content to a file atomically via tmp+rename.

    Uses os.fsync to flush to disk and os.replace for atomic rename.
    """
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.write(fd, content.encode())
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        os.unlink(tmp_path)
        raise
    else:
        os.close(fd)
    os.replace(tmp_path, str(path))


from orbit_auto.models import State, Task, TaskStatus


class StateManager:
    """
    Manages execution state with file-based locking for concurrent access.

    State is persisted as JSON and protected by fcntl locks for safe
    multi-process access.
    """

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.state_file = state_dir / "state.json"
        self.lock_file = state_dir / "state.lock"

    @contextmanager
    def _lock(self, exclusive: bool = True) -> Iterator[None]:
        """Context manager for file locking."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        lock_type = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH

        with open(self.lock_file, "w") as f:
            try:
                fcntl.flock(f.fileno(), lock_type)
                yield
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def init(self, tasks: list[str], pre_completed: set[str] | None = None) -> State:
        """
        Initialize state file with tasks.

        Args:
            tasks: List of all task IDs
            pre_completed: Set of task IDs already completed (from tasks.md)
                          These will be initialized as COMPLETED, not PENDING.
        """
        pre_completed = pre_completed or set()
        state = State(
            status="running",
            started=datetime.now(timezone.utc),
            tasks={
                tid: Task(
                    id=tid,
                    title="",
                    status=TaskStatus.COMPLETED if tid in pre_completed else TaskStatus.PENDING,
                    attempts=0,
                )
                for tid in tasks
            },
            workers={},
        )
        self._write(state)
        return state

    def read(self) -> State:
        """Read current state with shared lock."""
        with self._lock(exclusive=False):
            return self._read_unlocked()

    def _read_unlocked(self) -> State:
        """Read state without locking (caller must hold lock)."""
        data = json.loads(self.state_file.read_text())
        return State.from_dict(data)

    def _write(self, state: State) -> None:
        """Write state atomically."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(self.state_file, json.dumps(state.to_dict(), indent=2))

    def claim_task(self, worker_id: int, dag: DAG) -> str | None:
        """
        Atomically claim an available task for a worker.

        Returns task_id if claimed, None if no tasks available.
        """
        with self._lock(exclusive=True):
            state = self._read_unlocked()

            # Get completed tasks
            completed = {
                tid for tid, task in state.tasks.items() if task.status == TaskStatus.COMPLETED
            }

            # Find first pending task with satisfied dependencies
            for task_id in sorted(state.tasks.keys()):
                task = state.tasks[task_id]
                if task.status != TaskStatus.PENDING:
                    continue

                # Check dependencies via DAG
                if dag.deps_satisfied(task_id, completed):
                    # Claim it
                    task.status = TaskStatus.IN_PROGRESS
                    task.worker = worker_id
                    task.attempts += 1
                    self._write(state)
                    return task_id

        return None

    def complete_task(self, task_id: str) -> None:
        """Mark a task as completed."""
        with self._lock(exclusive=True):
            state = self._read_unlocked()
            if task_id in state.tasks:
                state.tasks[task_id].status = TaskStatus.COMPLETED
                state.tasks[task_id].worker = None
            self._write(state)

    def fail_task(self, task_id: str, error_message: str | None = None) -> None:
        """Mark a task as failed with optional error message."""
        with self._lock(exclusive=True):
            state = self._read_unlocked()
            if task_id in state.tasks:
                state.tasks[task_id].status = TaskStatus.FAILED
                state.tasks[task_id].worker = None
                if error_message:
                    state.tasks[task_id].error_message = error_message
            self._write(state)

    def release_task(self, task_id: str, max_retries: int, error_message: str | None = None) -> str:
        """
        Release a task for retry or mark as failed if max retries reached.

        Returns: "released", "max_retries_reached", or "unknown"
        """
        with self._lock(exclusive=True):
            state = self._read_unlocked()

            if task_id not in state.tasks:
                return "unknown"

            task = state.tasks[task_id]
            if task.attempts >= max_retries:
                task.status = TaskStatus.FAILED
                if error_message:
                    task.error_message = error_message
                result = "max_retries_reached"
            else:
                task.status = TaskStatus.PENDING
                # Keep the error message for the last attempt (useful for debugging retries)
                if error_message:
                    task.error_message = error_message
                result = "released"

            task.worker = None
            self._write(state)
            return result

    def release_orphaned_tasks(self, dead_worker_ids: set[int], max_retries: int) -> list[str]:
        """
        Release tasks claimed by dead workers back to PENDING or FAILED.

        Idempotent - safe to call repeatedly for the same dead workers.
        """
        with self._lock(exclusive=True):
            state = self._read_unlocked()
            released = []

            for task_id, task in state.tasks.items():
                if (
                    task.status == TaskStatus.IN_PROGRESS
                    and task.worker is not None
                    and task.worker in dead_worker_ids
                ):
                    if task.attempts >= max_retries:
                        task.status = TaskStatus.FAILED
                        task.error_message = task.error_message or "Worker process died"
                    else:
                        task.status = TaskStatus.PENDING
                        if not task.error_message:
                            task.error_message = "Worker process died"
                    task.worker = None
                    released.append(task_id)

            if released:
                self._write(state)

            return released

    def get_progress(self) -> tuple[int, int]:
        """Get completed/total counts."""
        state = self.read()
        total = len(state.tasks)
        completed = sum(1 for task in state.tasks.values() if task.status == TaskStatus.COMPLETED)
        return completed, total

    def get_detailed_progress(self) -> dict[str, int]:
        """Get counts per status."""
        state = self.read()
        counts = {status.value: 0 for status in TaskStatus}
        for task in state.tasks.values():
            counts[task.status.value] += 1
        return counts

    def is_complete(self) -> bool:
        """Check if all tasks are completed or failed."""
        state = self.read()
        for task in state.tasks.values():
            if task.status in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS):
                return False
        return True

    def has_failures(self) -> bool:
        """Check if any tasks failed."""
        state = self.read()
        return any(task.status == TaskStatus.FAILED for task in state.tasks.values())

    def get_completed_tasks(self) -> list[str]:
        """Get list of completed task IDs."""
        state = self.read()
        return sorted(
            [tid for tid, task in state.tasks.items() if task.status == TaskStatus.COMPLETED]
        )

    def get_failed_tasks(self) -> list[str]:
        """Get list of failed task IDs."""
        state = self.read()
        return sorted(
            [tid for tid, task in state.tasks.items() if task.status == TaskStatus.FAILED]
        )

    def sync_to_tasks_md(self, tasks_md: Path) -> None:
        """Sync completed tasks to tasks.md file by marking checkboxes."""
        import re

        completed = self.get_completed_tasks()
        if not tasks_md.exists():
            return

        content = tasks_md.read_text()

        for task_id in completed:
            # Convert 01 -> 1 for display
            display_id = task_id.lstrip("0") or "0"

            # Match "- [ ] N." or "- [ ] N:"
            pattern = rf"^(\s*)- \[ \] {display_id}([.:])"
            replacement = rf"\1- [x] {display_id}\2"
            content = re.sub(pattern, replacement, content, flags=re.MULTILINE)

        _atomic_write_text(tasks_md, content)

    def reset(self) -> None:
        """Clear all task state to allow re-execution."""
        if self.state_file.exists():
            self.state_file.unlink()

"""
Data models for the Orbit Auto.

Defines dataclasses for tasks, state, configuration, and execution results.
Uses Python 3.11+ features like str | None syntax.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any


class TaskStatus(str, Enum):
    """Status of a task in the execution pipeline."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


class Visibility(str, Enum):
    """Output visibility level for tool calls."""

    VERBOSE = "verbose"  # timestamps + full paths + command args
    MINIMAL = "minimal"  # timestamps + filenames only
    NONE = "none"  # no tool visibility


@dataclass
class Task:
    """Represents a single task in the orbit-auto loop."""

    id: str
    title: str
    status: TaskStatus = TaskStatus.PENDING
    dependencies: list[str] = field(default_factory=list)
    attempts: int = 0
    worker: int | None = None
    prompt_file: Path | None = None
    error_message: str | None = None  # Last error message when task failed

    @property
    def display_id(self) -> str:
        """Convert task_id to display format (01 -> 1, 01-02 -> 1.2)."""
        if "-" in self.id:
            parts = self.id.split("-")
            parent = parts[0].lstrip("0") or "0"
            child = parts[1].lstrip("0") or "0"
            return f"{parent}.{child}"
        return self.id.lstrip("0") or "0"


@dataclass
class State:
    """State of the entire orbit-auto execution."""

    status: str  # "running", "completed", "failed"
    started: datetime
    tasks: dict[str, Task] = field(default_factory=dict)
    workers: dict[int, str | None] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize state to dictionary for JSON storage."""
        return {
            "status": self.status,
            "started": self.started.isoformat(),
            "tasks": {
                tid: {
                    "status": task.status.value,
                    "worker": task.worker,
                    "attempts": task.attempts,
                    "error_message": task.error_message,
                }
                for tid, task in self.tasks.items()
            },
            "workers": self.workers,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "State":
        """Deserialize state from dictionary."""
        tasks = {}
        for tid, tdata in data.get("tasks", {}).items():
            tasks[tid] = Task(
                id=tid,
                title="",  # Title loaded separately from prompt files
                status=TaskStatus(tdata["status"]),
                worker=tdata.get("worker"),
                attempts=tdata.get("attempts", 0),
                error_message=tdata.get("error_message"),
            )

        return cls(
            status=data["status"],
            started=datetime.fromisoformat(data["started"]),
            tasks=tasks,
            workers=data.get("workers", {}),
        )


@dataclass
class Config:
    """Configuration for orbit-auto execution."""

    max_workers: int = 8
    max_retries: int = 3
    pause_seconds: int = 3
    task_timeout: int = 1800
    fail_fast: bool = False
    visibility: Visibility = Visibility.VERBOSE
    dry_run: bool = False
    use_worktrees: bool = False
    enable_review: bool = False
    spec_review_only: bool = False
    auto_commit: bool = True
    tdd_mode: bool = False

    def __post_init__(self) -> None:
        """Validate and cap configuration values."""
        if self.max_workers > 12:
            self.max_workers = 12
        if self.max_workers < 1:
            self.max_workers = 1
        if self.task_timeout < 0:
            self.task_timeout = 0


@dataclass
class ExecutionResult:
    """Result of executing a single task."""

    task_id: str
    success: bool
    output: str
    duration: float  # seconds
    tools_used: int = 0
    files_modified: list[str] = field(default_factory=list)

    # Learning-centric tags extracted from Claude's response
    learnings: str | None = None
    what_worked: str | None = None
    what_failed: str | None = None
    dont_retry: str | None = None
    try_next: str | None = None
    pattern_discovered: str | None = None
    gotcha: str | None = None

    # Status signals
    is_complete: bool = False  # <promise>COMPLETE</promise>
    is_blocked: bool = False  # <blocker>WAITING_FOR_HUMAN</blocker>

    # CLI error (stderr from Claude CLI)
    cli_error: str | None = None


@dataclass
class TaskPaths:
    """Paths for a task's files and directories."""

    task_dir: Path
    tasks_file: Path
    context_file: Path
    auto_log: Path
    prompts_dir: Path
    state_dir: Path
    logs_dir: Path

    @classmethod
    def from_task_name(cls, task_name: str) -> "TaskPaths":
        """Create TaskPaths from task name. Uses centralized orbit root."""
        task_dir = Path.home() / ".claude" / "orbit" / "active" / task_name
        return cls(
            task_dir=task_dir,
            tasks_file=task_dir / f"{task_name}-tasks.md",
            context_file=task_dir / f"{task_name}-context.md",
            auto_log=task_dir / f"{task_name}-auto-log.md",
            prompts_dir=task_dir / "prompts",
            state_dir=task_dir / ".orbit-parallel-state",
            logs_dir=task_dir / "logs",
        )

    def validate(self) -> list[str]:
        """Validate that required paths exist. Returns list of errors."""
        errors = []
        if not self.task_dir.exists():
            errors.append(f"Task directory not found: {self.task_dir}")
        if not self.tasks_file.exists():
            errors.append(f"Tasks file not found: {self.tasks_file}")
        if not self.context_file.exists():
            errors.append(f"Context file not found: {self.context_file}")
        return errors

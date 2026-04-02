"""Pydantic models for structured MCP responses."""

from typing import Any

from pydantic import BaseModel, Field


class TaskProgress(BaseModel):
    """Progress information parsed from orbit files."""

    completion_pct: int = Field(description="Completion percentage (0-100)")
    total_items: int = Field(default=0, description="Total checklist items")
    completed_items: int = Field(default=0, description="Completed items")
    remaining_summary: str | None = Field(
        default=None, description="Summary of remaining work"
    )


class TaskPromptConfig(BaseModel):
    """Optimized prompt configuration for a task."""

    system: str | None = Field(default=None, description="System prompt for this task")
    constraints: list[str] = Field(
        default_factory=list, description="Constraints/rules for the task"
    )
    context_files: list[str] = Field(
        default_factory=list, description="Files to include as context"
    )


class TaskSummary(BaseModel):
    """Summary of a task for list views."""

    id: int
    name: str
    status: str
    task_type: str = Field(alias="type")
    repo_name: str | None = None
    repo_path: str | None = None
    jira_key: str | None = None
    tags: list[str] = Field(default_factory=list)
    time_total_seconds: int = Field(default=0, description="Total time in seconds")
    time_formatted: str = Field(default="0m", description="Human-readable time")
    last_worked_on: str | None = None
    last_worked_ago: str = Field(
        default="never", description="Relative time since last worked"
    )
    has_orbit_files: bool = Field(
        default=False, description="Whether orbit files exist"
    )

    class Config:
        populate_by_name = True


class TaskDetail(TaskSummary):
    """Full task details including progress and prompt config."""

    full_path: str
    parent_id: int | None = None
    branch: str | None = None
    pr_url: str | None = None
    created_at: str
    updated_at: str
    completed_at: str | None = None
    progress: TaskProgress | None = None
    prompt: TaskPromptConfig | None = None
    subtasks: list["TaskSummary"] = Field(default_factory=list)
    recent_updates: list[dict[str, Any]] = Field(default_factory=list)


class OrbitFiles(BaseModel):
    """Orbit file paths for a task."""

    task_dir: str
    plan_file: str | None = None
    context_file: str | None = None
    tasks_file: str | None = None
    prompts_dir: str | None = None


class HeartbeatResult(BaseModel):
    """Result of recording a heartbeat."""

    heartbeat_id: int
    task_id: int
    task_name: str


class ProcessHeartbeatsResult(BaseModel):
    """Result of processing heartbeats into sessions."""

    processed_count: int


class CreateTaskResult(BaseModel):
    """Result of creating a task."""

    task_id: int
    task_name: str
    task_type: str
    orbit_path: str | None = None


class CompleteTaskResult(BaseModel):
    """Result of completing a task."""

    task_id: int
    task_name: str
    previous_status: str
    new_status: str = "completed"
    completed_at: str
    time_total_formatted: str


class ReopenTaskResult(BaseModel):
    """Result of reopening a task."""

    task_id: int
    task_name: str
    previous_status: str
    new_status: str = "active"


class ListTasksResult(BaseModel):
    """Result of listing tasks."""

    tasks: list[TaskSummary]
    total_count: int
    filter_applied: str | None = None
    other_tasks: list[TaskSummary] | None = None

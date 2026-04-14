# MCP Tools

This document covers the orbit MCP server: the 30 tools that expose orbit's task database, orbit files, time tracking, and planning surfaces to Claude Code over the Model Context Protocol. It is the layer that makes `/orbit:new`, `/orbit:go`, and the rest of the slash commands work - the command files are thin wrappers that tell Claude which MCP tools to call in what order, and this doc is the reference for everything those tools do.

It assumes you have read [`architecture.md`](./architecture.md) for the shared vocabulary (`tasks.db`, `~/.claude/orbit/active/<project>/`, `full_path`, heartbeats and sessions, the repo model). If a term in this doc is not defined here, it is defined there.

If you are just trying to *use* orbit from a command or a script, the short version is: every tool lives under the `mcp__plugin_orbit_pm__` prefix in Claude Code (`mcp__plugin_orbit_pm__list_active_tasks`, `mcp__plugin_orbit_pm__get_task`, etc.), every tool returns a JSON dictionary, and errors come back as `{"error": true, "code": "...", "message": "..."}` instead of raising. The rest of this doc is for when you want to understand exactly what a tool does, what to pass it, and what to expect back.

## What the MCP server is

Orbit ships a single MCP server, registered in `plugin.json` as:

```json
"mcpServers": {
  "pm": {
    "type": "stdio",
    "command": "uvx",
    "args": [
      "--from", "${CLAUDE_PLUGIN_ROOT}/mcp-server",
      "--with", "${CLAUDE_PLUGIN_ROOT}/orbit-db",
      "mcp-orbit"
    ]
  }
}
```

The server name is `pm` (for "project management"), and combined with the plugin name (`orbit`), Claude Code exposes every tool under the prefix `mcp__plugin_orbit_pm__<tool_name>`. So the Python function `list_active_tasks` in `mcp-server/src/mcp_orbit/tools_tasks.py` is callable from Claude as `mcp__plugin_orbit_pm__list_active_tasks`.

The server itself is a [FastMCP](https://github.com/modelcontextprotocol/python-sdk) application. The entry point (`server.py`) is 31 lines long and does nothing but import the tool modules to trigger their `@mcp.tool()` decorators:

```python
from .app import mcp  # noqa: F401

from . import tools_tasks     # noqa: F401
from . import tools_docs      # noqa: F401
from . import tools_tracking  # noqa: F401
from . import tools_iteration # noqa: F401
from . import tools_planning  # noqa: F401

def main():
    mcp.run(transport="stdio")
```

All the logic lives in those five `tools_*` modules. Every tool function is an `async def` decorated with `@mcp.tool()` that takes `Annotated[...]` parameters with `Field(description=...)` for MCP schema generation and returns a `dict`. That return shape is important - tools never raise across the MCP boundary; they catch `OrbitError` and return `e.to_dict()`, and they catch everything else and return `{"error": True, "message": str(e)}`. Claude always sees a dict, never an exception.

### Why async, why dicts

The `async def` signature is required by FastMCP even though orbit's tools are pure sync code internally - none of them await anything. The handlers would work identically if they were sync, and the MCP SDK handles them either way; `async def` is the current FastMCP convention and it costs nothing to follow.

The `dict` return is also a FastMCP quirk. Tools could return Pydantic models directly, and they do internally (`ListTasksResult`, `TaskDetail`, etc. in `models.py`), but then call `.model_dump()` before returning. This is the cheapest way to get a stable JSON-serializable shape without depending on FastMCP's schema inference. The models are still useful as internal contracts - you get type checking, field validation, and one place to update when the shape changes.

### The five modules

| Module | Lines | Tools | Purpose |
|--------|-------|-------|---------|
| `tools_tasks.py` | 521 | 9 | Task lifecycle: list, get, create, complete, reopen, update notes |
| `tools_docs.py` | 289 | 5 | Orbit files: create, get, update context, update tasks, get progress |
| `tools_tracking.py` | 215 | 6 | Time tracking and repository management |
| `tools_iteration.py` | 167 | 3 | Iteration log integration (used by orbit-auto and the iteration loop) |
| `tools_planning.py` | 473 | 7 | Parallel agent execution plans |

**Total: 30 tools.** The rest of this doc walks through them module by module. The style is reference-oriented: each tool gets a brief "when to use this", its parameter list with types and defaults, and what comes back on success. Error behavior is uniform across tools and covered in the [error handling](#error-handling) section instead of being repeated 30 times.

## Task lifecycle tools (`tools_tasks.py`)

These are the tools you reach for when you are working with tasks as first-class entities in `tasks.db`. They do not touch orbit files directly - that is what the `tools_docs` module is for.

### `list_active_tasks`

**When to use:** You want every active task in the DB, optionally filtered or grouped by repo. This is what `/orbit:go` calls first to show the selection table.

**Parameters:**
- `repo_path: str | None = None` - Filter by repo path. Resolves via `db.get_repo_by_path()`, so a canonical absolute path works best.
- `task_type: str | None = None` - Filter to `"coding"` or `"non-coding"` tasks.
- `include_time: bool = True` - Whether to batch-fetch time tracking info. Adds one extra query but the result is shaped for display.
- `prioritize_by_repo: bool = False` - Two-tier output mode. When `True` and `repo_path` is set, returns repo tasks in `tasks` and non-repo tasks in `other_tasks`, instead of filtering non-repo tasks out.

**Returns:** A `ListTasksResult` with `tasks` (list of `TaskSummary`), `total_count`, `filter_applied` (human-readable filter description), and optional `other_tasks` (present only in prioritize-by-repo mode).

Each `TaskSummary` has: `id`, `name`, `status`, `task_type`, `repo_name`, `repo_path`, `jira_key`, `tags`, `time_total_seconds`, `time_formatted`, `last_worked_on`, `last_worked_ago`, `has_orbit_files`. The time fields come from a single batch query (`db.get_batch_task_times`) rather than N individual lookups, which is the reason this tool is faster than calling `get_task` in a loop.

The `last_worked_ago` field is computed via `db.get_effective_last_updated(task)`, which takes the max of the DB's `updated_at` and the mtime of the project's `-tasks.md` file, so editing the task file from outside orbit still advances the "last worked" timestamp.

### `list_completed_tasks`

**When to use:** You want recently finished tasks, usually for a dashboard list, a monthly summary, or to find a project to reopen.

**Parameters:**
- `days: int = 7` - Look-back window in days. `db.get_recent_completed()` filters on `completed_at`.
- `limit: int = 20` - Max tasks to return.

**Returns:** Same `ListTasksResult` shape as `list_active_tasks`. No `other_tasks`, no prioritization.

Time is not batch-fetched here - each `TaskSummary` does its own `db.get_task_time()` call. For small limits (default 20) this is fine; if you set `limit` very high, be aware you are doing N queries.

### `get_task`

**When to use:** You have a task ID or project name and you need everything - progress, time, subtasks, JIRA key, full path, file layout. This is the primary tool for `/orbit:go`, and it is also what the dashboard hits when you open a task modal.

**Parameters:**
- `task_id: int | None = None` - DB primary key.
- `project_name: str | None = None` - Alternative to `task_id`.
- `include_subtasks: bool = True` - Pull subtasks via `get_active_tasks_hierarchical`.
- `include_updates: bool = True` - Pull recent notes (only populated for non-coding tasks).

Provide exactly one of `task_id` or `project_name`. Providing both is not an error, but `task_id` wins.

**Returns:** A `TaskDetail` with every `TaskSummary` field plus `full_path`, `parent_id`, `branch`, `pr_url`, `created_at`, `updated_at`, `completed_at`, `progress` (a `TaskProgress` with `completion_pct`, `total_items`, `completed_items`, `remaining_summary`), `prompt` (optimized prompt config, usually null), `subtasks` (list of `TaskSummary`), and `recent_updates`.

The `progress` field is parsed live from `<project>-tasks.md` via `_parse_task_progress()`, so editing the checklist outside orbit shows up on the next `get_task` call. No caching.

### `find_task_for_directory`

**When to use:** Claude (or a hook) is running in a directory and needs to know "what orbit task, if any, owns this cwd". This is what the activity tracker and session_start hook use to decide whether to record heartbeats.

**Parameters:**
- `directory: str` - Path to look up. Must exist and must not contain null bytes (validated via `_validate_path`).
- `session_id: str | None = None` - Optional Claude session ID. If provided, the lookup checks the per-session task pointer (`~/.claude/hooks/state/projects/<session-id>.json`) in addition to cwd-based matching.

**Returns:** `{"found": bool, "task": TaskDetail | None}`. When `found` is `False`, `task` is `None` and there is nothing to do. When `True`, `task` is a full `TaskDetail` with subtasks disabled.

The resolution order is documented in the task_parser's `find_task_for_cwd`: per-session file first, then `pending-task.json`, then cwd directory match under `~/.claude/orbit/active/`. See `architecture.md` for the gotcha around `pending-project.json` being dead code and only the session file path actually mattering.

### `create_task`

**When to use:** You want a new task in the DB and, for coding tasks, a fresh directory under `~/.claude/orbit/active/<name>/`. Slash commands generally call `create_orbit_files` instead because it does more in one step, but `create_task` is the minimal primitive.

**Parameters:**
- `name: str` - Task name in kebab-case. Validated by `orbit.validate_task_name()`.
- `task_type: str = "coding"` - Must be `"coding"` or `"non-coding"`. Validated inline.
- `repo_path: str | None = None` - Required for coding tasks, ignored for non-coding. Auto-registers the repo via `db.add_repo()` if it is not already tracked.
- `jira_key: str | None = None` - Optional JIRA ticket ID for display.

**Returns:** `CreateTaskResult` with `task_id`, `task_name`, `task_type`, and `orbit_path` (the directory created on disk, or `None` for non-coding tasks).

Non-coding tasks do not get a directory - they exist only as DB rows and use `add_task_update` for progress notes. This is the split between "projects with files you edit" and "projects you log notes against" (meetings, reviews, investigations).

### `complete_task`

**When to use:** You are done with a task and want it out of the active list. For coding tasks, this also moves the orbit files from `active/` to `completed/`.

**Parameters:**
- `task_id: int | None = None` or `project_name: str | None = None` - Provide exactly one.
- `move_files: bool = True` - Whether to physically move the orbit directory to `completed/`. Set to `False` if you want to keep files in `active/` while marking the DB status as completed (rare - mostly useful for recovery scenarios).

**Returns:** `CompleteTaskResult` with `task_id`, `task_name`, `previous_status`, `new_status="completed"`, `completed_at`, `time_total_formatted`.

If the task is already completed, it raises `InvalidStateError` with `current_state="completed"`. No-op completions are surfaced rather than silently tolerated.

### `reopen_task`

**When to use:** A completed task was marked done prematurely and you need it back in the active list. For coding tasks, moves the orbit directory from `completed/` back to `active/`.

**Parameters:** Same as `complete_task` - `task_id` or `project_name`, plus `move_files: bool = True`.

**Returns:** `ReopenTaskResult` with `task_id`, `task_name`, `previous_status`, `new_status="active"`.

Raises `InvalidStateError` if the task is not currently completed. This is a strict check - you cannot "reopen" a task that was never completed in the first place.

### `add_task_update`

**When to use:** You want to append a timestamped note to a task's update log. Primarily used for non-coding tasks (meeting notes, 1:1 followups, investigation progress) where the file-based `<project>-context.md` is overkill.

**Parameters:**
- `task_id: int` - Required. No `project_name` alternative for this one.
- `note: str` - The note text. Stored verbatim.

**Returns:** `{"update_id": int, "task_id": int, "task_name": str, "note": str}`.

Updates are stored in the `task_updates` table (one row per call) and surfaced via `get_task_updates` and via `get_task` when `include_updates=True` on non-coding tasks.

### `get_task_updates`

**When to use:** You want the history of notes for a task, in reverse-chronological order.

**Parameters:**
- `task_id: int` - Required.
- `limit: int = 20` - Max updates to return.

**Returns:** `{"task_id": int, "task_name": str, "updates": list[dict], "total_count": int}`. Each update has `id`, `note`, `created_at`.

## Orbit file tools (`tools_docs.py`)

These tools operate on the `-tasks.md`, `-context.md`, `-plan.md` files under `~/.claude/orbit/active/<project>/`. They are the bridge between the DB (which stores task metadata) and the filesystem (which stores human-readable project state).

### `create_orbit_files`

**When to use:** You are starting a new project and you want the DB row, the directory, and the template files in one call. This is what `/orbit:new` runs - it is the orbit equivalent of `git init` for a task.

**Parameters:**
- `repo_path: str` - Required. Auto-registered if missing.
- `project_name: str` - Kebab-case task name.
- `description: str = "TBD"` - Short description, embedded into the template files. Max 12 words is the convention but not enforced.
- `jira_key: str | None = None` - JIRA ticket ID.
- `branch: str | None = None` - Git branch.
- `tasks: list[str] | None = None` - Initial task list to seed `<project>-tasks.md` with. Each string becomes a `- [ ]` line.
- `plan: dict | None = None` - Plan content dict with keys like `summary`, `goals`, `approach`. Structure is flexible - the template renderer picks up what it finds.

**Returns:** `{"success": True, "task_id": int, "task_name": str, "files": OrbitFiles}` where `OrbitFiles` has `task_dir`, `plan_file`, `context_file`, `tasks_file`, `prompts_dir`.

Internally this is a four-step dance:

1. Ensure the repo is registered (`db.add_repo` if new).
2. Call `orbit.create_orbit_files()` to generate the files from templates.
3. Call `db.scan_all_repos()` to register the task row in the DB.
4. Reconcile the task's `repo_id` via `db.find_task_by_full_path` / `db.update_task_repo` - this is the fix for the gotcha where `scan_all_repos` can assign the task to the wrong repo when multiple repos share a prefix.

The reconciliation step is the load-bearing part. Without it, the task would appear under the wrong repo in the dashboard's per-repo view.

### `get_orbit_files`

**When to use:** You need the filesystem paths for a task's orbit files. Usually followed by a direct Read/Edit via Claude's standard file tools rather than more orbit tool calls.

**Parameters:**
- `task_id: int | None = None` or `project_name: str | None = None` - At least one required.

**Returns:** `{"task_id": int | None, "task_name": str, "files": OrbitFiles}`.

If the task exists in the DB, uses `task.full_path` to resolve subtask directories correctly (nested under parent). If only `project_name` is given and the task is not in the DB, falls back to the default `active/<name>/` layout.

### `update_context_file`

**When to use:** You want to append entries to `<project>-context.md` without loading the whole file, editing it by hand, and writing it back. This is the main tool for `/orbit:save` and is much faster than multiple Read/Edit calls.

**Parameters:**
- `context_file: str` - Absolute path to the context file. Validated to be under `ORBIT_ROOT` (prevents escape via `..`).
- `next_steps: list[str] | None = None` - Lines to add under "Next Steps". Replaces the section if provided.
- `recent_changes: list[str] | None = None` - Lines to add under a new "Recent Changes (timestamp)" header.
- `key_decisions: list[str] | None = None` - Lines to add under "Key Architectural Decisions".
- `gotchas: list[str] | None = None` - Lines to add under "Gotchas".
- `key_files: dict[str, str] | None = None` - `{path: description}` map to add to the "Key Files" table.

**Returns:** `{"success": True, "file": str, "timestamp": str, "sections_updated": list[str]}`.

All sections are optional. Passing `None` for a section means "don't touch it". Passing an empty list means "touch the section but add nothing" - surprisingly useful for forcing a timestamp update without changing content. The `sections_updated` field tells you which sections actually received non-empty input.

The underlying writer (`orbit.update_context_file`) updates the "Last Updated" timestamp atomically on every call, regardless of which sections you touched.

### `update_tasks_file`

**When to use:** You want to mark tasks as completed, add new tasks, or update the "Remaining" summary line in `<project>-tasks.md` without editing the file directly.

**Parameters:**
- `tasks_file: str` - Path validated to be under `ORBIT_ROOT`.
- `completed_tasks: list[str] | None = None` - Task descriptions to mark as `[x]`. Matching is substring-based against task lines - pass enough of the title to be unambiguous.
- `new_tasks: list[str] | None = None` - New `- [ ]` lines to append.
- `remaining_summary: str | None = None` - The "Remaining:" metadata line summary (max 15 words convention).
- `notes: list[str] | None = None` - Notes to append under the "Notes" section.

**Returns:** `{"success": True, ...}` plus the progress result from `orbit.update_tasks_file` (completion percentage, counts, etc.).

### `get_orbit_progress`

**When to use:** You want the completion percentage and counts for a task without the full `TaskDetail` payload. Cheaper than `get_task` if you only care about progress.

**Parameters:**
- `task_id: int | None = None` - Resolves the tasks file path from the DB.
- `tasks_file: str | None = None` - Direct path (validated under `ORBIT_ROOT`).

**Returns:** `{"task_id": int | None, "file": str, "progress": TaskProgress}`.

You can pass either - if both are provided, `tasks_file` wins. If neither is provided, returns a `VALIDATION_ERROR`.

## Time tracking and repo tools (`tools_tracking.py`)

These tools drive the orbit heartbeat system and the repository registry. Most of them are called by hooks, not by Claude directly, but they are exposed as MCP tools so commands and scripts can use them too.

### `record_heartbeat`

**When to use:** You want to ping the DB to say "I am working on this task now". The orbit activity tracker hook calls this on every `UserPromptSubmit`, but you can also call it manually from a slash command (as `/orbit:go` does after loading a project).

**Parameters:**
- `task_id: int | None = None` - Direct task ID. If provided, records under that task with no lookup.
- `directory: str | None = None` - Auto-detect mode. If provided, runs `db.record_heartbeat_auto(directory)` which internally calls `find_task_for_cwd`.
- `session_id: str | None = None` - Claude session ID. Optional, used to key per-session task lookups in auto-detect mode.
- `context: dict | None = None` - Arbitrary JSON blob stored with the heartbeat. Currently unused by orbit itself but available for custom hooks.

Provide exactly one of `task_id` or `directory`.

**Returns:** `HeartbeatResult` with `heartbeat_id`, `task_id`, `task_name`. In auto-detect mode when no task is found, returns `{"recorded": False, "message": "..."}` instead.

Heartbeats are raw timestamps - they don't become time on a task until `process_heartbeats` aggregates them into sessions.

### `process_heartbeats`

**When to use:** You want to flush accumulated heartbeats into `sessions` rows, which is what the dashboard actually reads for time totals. The orbit PreCompact and Stop hooks call this automatically, but you can call it on demand after a batch of work.

**Parameters:** None.

**Returns:** `ProcessHeartbeatsResult` with `processed_count` (how many heartbeats were aggregated in this call).

The aggregation is idempotent - processed heartbeats are marked and never re-aggregated. See `architecture.md`'s invariants section for the detailed guarantee.

### `get_task_time`

**When to use:** You want the total time spent on a task over a period.

**Parameters:**
- `task_id: int` - Required.
- `period: str = "all"` - One of `"all"`, `"today"`, `"week"`.

**Returns:** `{"task_id": int, "task_name": str, "period": str, "total_seconds": int, "formatted": str, "session_count": int}`.

The `formatted` string is `"1h 23m"`-style output from `db.format_duration`. The `session_count` is per the full period, not per the current day.

### `list_repos`

**When to use:** You want to see every tracked repository. Useful for debugging "why is this task not showing up under the right repo".

**Parameters:**
- `active_only: bool = True` - Filter to `active=1` repos (the default DB flag).

**Returns:** `{"repos": list[dict], "total_count": int}`. Each repo dict has `id`, `path`, `short_name`, `is_active`.

### `add_repo`

**When to use:** You want to register a repository explicitly. `create_task` and `create_orbit_files` both auto-register on demand, so this tool is mainly for one-off setup.

**Parameters:**
- `path: str` - Absolute path to the repo root. Validated.
- `short_name: str | None = None` - Display name for the dashboard. If omitted, defaults to the last path component.

**Returns:** `{"repo_id": int, "path": str, "short_name": str}`.

### `scan_repos`

**When to use:** You manually created orbit files outside of orbit tools (or moved them around) and you need the DB to pick them up.

**Parameters:**
- `repo_id: int | None = None` - Scan only one repo. If omitted, scans all tracked repos.

**Returns:** `{"scanned_count": int, "tasks": list[{"id", "name", "repo_id"}]}`.

Scanning walks `~/.claude/orbit/active/` and `~/.claude/orbit/completed/` under each repo's orbit paths, creates missing DB rows, and updates `full_path` for existing ones. It does not delete DB rows for tasks whose files are gone - that cleanup is manual.

## Iteration log tools (`tools_iteration.py`)

These tools are a thin wrapper around `iteration_log.py`, which writes to `<project>-auto-log.md`. They exist so orbit-auto and the sequential iteration loop can log their progress without having to duplicate file-writing logic. You will rarely call them manually.

### `log_iteration`

**When to use:** You completed one iteration of a long-running task and want to record what happened to the auto log.

**Parameters:**
- `task_id: int | None = None` or `project_name: str | None = None` - One required.
- `iteration: int = 1` - Iteration number.
- `status: str = "SUCCESS"` - One of `"SUCCESS"`, `"FAILED"`, `"BLOCKED"`.
- `task_title: str | None = None` - Title of the task being worked on (for the log entry header).
- `what_done: list[str] | None = None` - Bullet list of things attempted.
- `files_modified: list[str] | None = None` - List of paths touched.
- `validation: dict | None = None` - Validation results as `{"check_name": "PASS" | "FAIL"}`.
- `error_details: str | None = None` - Error text for FAILED iterations.
- `next_steps: list[str] | None = None` - Suggested next steps for retries.

**Returns:** `{"success": True, "task_name": str, "iteration": int, "status": str, "log_file": str}`.

### `log_iteration_completion`

**When to use:** The iteration loop finished (either successfully or by timing out) and you want to write the final summary entry.

**Parameters:**
- `task_id: int | None = None` or `project_name: str | None = None` - One required.
- `total_iterations: int = 1` - How many iterations ran.
- `duration_seconds: int = 0` - Total wall clock duration.
- `timed_out: bool = False` - `True` for timeout, `False` for success.

**Returns:** `{"success": True, "task_name": str, "completed": bool, "timed_out": bool, "total_iterations": int, "duration_seconds": int}`.

### `get_iteration_status`

**When to use:** You want to know where an iteration loop left off without reading the log file by hand.

**Parameters:**
- `task_id: int | None = None` or `project_name: str | None = None` - One required.

**Returns:** `{"task_name": str, "iteration_log": {...}, "prompts": {...}}`. The `iteration_log` dict has the parsed state from the auto-log file (iteration count, last status, completed/timed_out flags). The `prompts` dict has status info from `prompts/` if it exists.

## Planning tools (`tools_planning.py`)

These are the newest set of tools and they serve a different use case than everything above: instead of tracking a single project's progress, they coordinate *parallel subagent execution* via the Claude `Task` tool. The workflow is (1) create a plan, (2) register each agent with its dependencies, (3) ask the orchestrator for ready agents, (4) spawn them in parallel, (5) each subagent reports back via `update_agent_status`, (6) mark the plan complete when done.

This is orthogonal to orbit-auto's parallel mode - orbit-auto uses multiprocessing and spawns its own Claude CLI subprocesses, whereas these tools let an orchestrating Claude drive a parallel agent swarm via the existing Task tool in a single conversation. They write to the DB (the `plans`, `agent_executions`, `agent_dependencies` tables) and the dashboard can surface them, but the plan tables are considered scoped follow-up territory and some of the DB-layer code for them is still sitting dormant in `analytics_db.py` (see the `orbit-public-release` context file for the cleanup plan).

If you are not building a parallel-agent orchestrator, you do not need any of these. If you are, they give you the primitives.

### `create_plan`

**Parameters:**
- `name: str` - Plan name/description.
- `task_id: int | None = None` - Optional association with an orbit task.
- `metadata: str | None = None` - Optional JSON string. Parsed with `json.loads`; invalid JSON returns a `VALIDATION_ERROR`.

**Returns:** `{"plan_id": int, "name": str, "task_id": int | None, "status": "draft"}`.

Plans start in `draft` state and transition to `running` as soon as any agent is running.

### `register_agent_execution`

**Parameters:**
- `plan_id: int` - Plan to register under.
- `agent_id: str` - Short identifier like `"01"`, `"02"`. Used for dependency refs.
- `agent_name: str` - Human-readable name for display.
- `prompt: str` - The task/prompt text this agent will execute.
- `dependencies: list[str] | None = None` - List of `agent_id`s this agent depends on. Agents with satisfied dependencies become ready to run.
- `max_attempts: int = 3` - Retry budget for this agent.

**Returns:** `{"execution_id": int, "plan_id": int, "agent_id": str, "agent_name": str, "dependencies": list, "dependency_records": list[int], "max_attempts": int}`.

### `update_agent_status`

**When to use:** A subagent finished (or failed) and is reporting back. This is the callback the subagent includes in its own prompt instructions (via `spawn_parallel_agents`).

**Parameters:**
- `plan_id: int`, `agent_id: str` - Identify which agent is updating.
- `status: str` - One of `"pending"`, `"running"`, `"completed"`, `"failed"`, `"blocked"`.
- `result: str | None = None` - Result text for completed agents.
- `error_message: str | None = None` - Error text for failed agents.

**Returns:** `{"updated": True, "plan_id": int, "agent_id": str, "status": str}`.

After the update, the tool calls `_update_plan_counters()` to recompute the plan's status based on all its agents. A plan is `completed` when every agent is in a terminal state (`completed`, `failed`, or `skipped`) with no failures; `failed` if any agent failed; `running` if any agent is running.

### `get_plan_status`

**Parameters:**
- `plan_id: int` - Required.

**Returns:** `{"plan": dict, "agents": list[dict], "summary": {"total", "pending", "blocked", "running", "completed", "failed"}}`.

This is the status snapshot for a plan - the plan metadata, every agent's current state, and a count breakdown by status.

### `get_ready_agents`

**When to use:** You are the orchestrator and you want the list of agents that can run next.

**Parameters:**
- `plan_id: int` - Required.

**Returns:** `{"ready_agents": list[dict], "count": int}`. Each ready agent has `agent_id`, `agent_name`, `prompt`, `execution_id`.

An agent is ready if its status is `pending` and all its dependencies are `completed`. Note: **failed dependencies do not block**. If agent `02` depends on `01` and `01` failed, `02` is still ready to run. This is intentional - the plan can complete partially and you have to deal with the failures yourself if you care about strict cascade blocking.

### `spawn_parallel_agents`

**When to use:** You want the actual `Task` tool invocation arguments for every ready agent in a plan. This is the tool the orchestrator calls when it is about to invoke `Task` for each agent.

**Parameters:**
- `plan_id: int` - Required.
- `subagent_type: str = "general-purpose"` - The `Task` tool's `subagent_type` to use for every spawned agent. If your plan has heterogeneous agent types, you will need to call `get_ready_agents` yourself and build the Task calls by hand.

**Returns:** `{"ready_count": int, "task_calls": list[dict], "instructions": str, "plan_id": int, "plan_name": str}`.

Each entry in `task_calls` has `subagent_type`, `description`, `prompt`, `run_in_background: True`. The `prompt` for each agent is wrapped with explicit `update_agent_status` call instructions so the subagent knows how to report completion. The `instructions` field is a human-readable reminder that you should dispatch every Task call in a single message to get true parallelism (multiple tool calls in one message run concurrently; multiple messages serialize).

### `complete_plan`

**When to use:** Every agent has finished and you want to finalize the plan's `completed_at` timestamp.

**Parameters:**
- `plan_id: int` - Required.
- `status: str = "completed"` - Must be `"completed"` or `"failed"`.

**Returns:** `{"plan_id": int, "status": str, "completed": True}`.

Most plans will auto-transition via `_update_plan_counters`, but this tool is the explicit way to finalize a plan regardless of its automatic state.

## Error handling

Every tool in the server follows the same error-handling pattern, which is worth memorizing because it is the contract for the entire MCP surface.

### Success shape

On success, a tool returns a JSON dict. Keys vary by tool, but there is no top-level `"success": true` marker on every response - some tools include it for convenience (`create_orbit_files`, `update_context_file`, `log_iteration`), others don't (`list_active_tasks`, `get_task`). The presence of an `"error"` key is the reliable way to tell success from failure.

### Error shape

On error, a tool returns:

```json
{
  "error": true,
  "code": "TASK_NOT_FOUND",
  "message": "Task not found: my-project",
  "details": {"task_id": "my-project"}
}
```

The `code` is one of the values defined in `errors.py:ErrorCode`:

| Code | Meaning |
|------|---------|
| `TASK_NOT_FOUND` | Task ID or name does not resolve |
| `REPO_NOT_FOUND` | Repository path is not registered |
| `FILE_NOT_FOUND` | Orbit file (or other file) does not exist |
| `VALIDATION_ERROR` | Input parameter failed validation |
| `DB_ERROR` | SQLite operation failed |
| `PERMISSION_ERROR` | Filesystem permission denied |
| `INVALID_STATE` | Operation not allowed in current state (e.g., reopening a task that isn't completed) |
| `OPERATION_FAILED` | Catch-all for operation-specific failures |

The `details` field is tool-specific and may contain things like `task_id`, `current_state`, `expected_state`, `field` (for validation errors), or `path` (for file errors).

### Unexpected errors

If a tool hits an exception that is not an `OrbitError` subclass, it logs via `logger.exception(...)` and returns:

```json
{
  "error": true,
  "message": "<exception str>"
}
```

No `code` field, no `details`. This is the "something unexpected broke" path and should show up in the MCP server's stderr log (`uvx` pipes logs through stdio; check wherever your Claude Code logs MCP server stderr). If you see an error response without a `code`, treat it as a bug and go read the server logs.

### Why no raises

Tools do not raise across the MCP boundary because FastMCP would serialize the exception as a protocol error, and Claude would see a generic MCP failure instead of the structured orbit error. Returning a dict lets Claude reason about *what* went wrong (task not found vs validation error vs DB crash) and react appropriately from a slash command.

The tradeoff is that tool callers must check `"error" in result` after every call. A helper wrapper could raise on error dicts, but none exists today - the MCP contract is "dicts in, dicts out" and every call site handles the check inline.

## Shared patterns

These are patterns that show up in multiple tools. If you are writing a new one, following them keeps the surface consistent.

### `task_id` vs `project_name`

Many tools accept either a `task_id: int` or a `project_name: str`. The convention is:

- Provide exactly one of them.
- If neither is provided, return a `VALIDATION_ERROR`.
- If both are provided, `task_id` wins and `project_name` is ignored. (This is not a validation error, so be careful - passing both does not warn you.)
- Resolve via `db.get_task(task_id)` or `db.get_task_by_name(project_name)`, then check for `None` and raise `TaskNotFoundError` if the lookup failed.

Tools that only accept `task_id` (`add_task_update`, `get_task_time`, `get_task_updates`) are the exception - they exist because the parameter is almost always known at call time.

### Path validation

Any tool that takes a filesystem path runs it through `helpers._validate_path(path, field_name, must_be_under=...)`. This:

1. Rejects empty strings.
2. Rejects null bytes (common injection vector).
3. Resolves the path (`Path(path).resolve()`).
4. If `must_be_under` is set, verifies the resolved path is inside that directory.

The `must_be_under` check is what prevents `update_context_file` from writing to arbitrary files - it is always called with `must_be_under=settings.orbit_root`, so a path like `/tmp/evil.md` or `~/.claude/orbit/../evil.md` gets rejected.

Tools that should *not* restrict paths (e.g., `record_heartbeat` with a `directory` arg pointing at a repo root) omit `must_be_under` and just get null-byte and empty-string checks.

### Time filters

Time-related tools that accept a `period` parameter (`get_task_time`, some batch helpers) all use the same vocabulary:

- `"all"` - all time, unbounded.
- `"today"` - from midnight local time to now.
- `"week"` - from 7 days ago to now (rolling, not calendar week).

Pass anything else and the underlying DB method either returns `"all"` implicitly or raises, depending on which method you hit. Stick to the three documented values.

### Single-shot reads vs batch reads

Two patterns show up when fetching time:

- **Per-task lookup** - `db.get_task_time(task_id, period)`. One SQL query per call. Fine for individual tasks.
- **Batch lookup** - `db.get_batch_task_times(task_ids)`. One SQL query for N tasks. Required for list views that would otherwise do N queries.

`list_active_tasks` uses the batch form when `include_time=True` to avoid the N+1 problem. `list_completed_tasks` does not, because its `limit` is small by default. If you are writing a new tool that returns a list with time info, prefer the batch form.

## Adding a new tool

If you have a new operation that belongs in the MCP server, the pattern is:

1. **Pick the right module.** Task lifecycle goes in `tools_tasks.py`, file ops in `tools_docs.py`, time/repo in `tools_tracking.py`, iteration log in `tools_iteration.py`, planning in `tools_planning.py`. If it does not fit any of these, consider whether it belongs in the server at all before creating a sixth module.
2. **Write the signature.**
   ```python
   @mcp.tool()
   async def my_tool(
       param1: Annotated[str, Field(description="Clear description")],
       param2: Annotated[int | None, Field(description="Optional param")] = None,
   ) -> dict:
       """Short description shown in MCP help."""
       db = get_db()
       try:
           # ... do work ...
           return {"success": True, ...}
       except OrbitError as e:
           return e.to_dict()
       except Exception as e:
           logger.exception("Error in my_tool")
           return {"error": True, "message": str(e)}
   ```
3. **Use typed returns where it helps.** If your tool returns a fixed shape, add a Pydantic model in `models.py` and `.model_dump()` it at the return site. If the shape is more ad-hoc, return a plain dict.
4. **Validate inputs early.** Call `_validate_path` for paths, return `VALIDATION_ERROR` dicts for bad enum values, raise `OrbitError` subclasses for expected failures.
5. **No new imports in `server.py`.** The tool is registered automatically when its module is imported - `server.py` imports `tools_<module>` once, and that triggers every `@mcp.tool()` in the file. Do not reach into `server.py`.
6. **Reload the plugin to pick it up.** `claude plugins install orbit@local` and restart Claude Code. The MCP server restart happens on the next Claude session.
7. **Add a test.** `mcp-server/tests/` has fixtures for DB setup and patterns for calling tools directly (bypassing MCP transport). Follow them.

The thing that makes this server pleasant to extend is that tools are flat, independent functions with no shared state beyond the DB instance. There is no plugin registry, no base class, no middleware. Adding a tool is additive - you cannot accidentally break an existing one.

## Troubleshooting

### "The tool is missing from Claude's tool list"

**Cause:** The plugin cache is stale. MCP tools are discovered at plugin load, and a new tool added after you installed the plugin won't show up until you reinstall.

**Fix:** `claude plugins install orbit@local` (or `orbit@claude-orbit` if you are on the marketplace path) and restart your Claude Code session. `/reload-plugins` does not cover MCP servers.

### "I'm calling the tool but getting `error: true, message: <python exception>` with no code"

**Cause:** An unhandled exception hit the `except Exception` fallback. This is not an `OrbitError` - it is something unexpected.

**Fix:** Check the MCP server stderr. `uvx` pipes stderr through stdio to Claude Code, which logs it to its own files. The server uses `logger.exception` which includes a full traceback. Once you have the traceback, treat it as a normal Python bug: find the line, fix the root cause.

### "Tool returns `FILE_NOT_FOUND` but the file exists"

**Cause:** Path validation is resolving to a different place than you expected. `_validate_path` calls `Path.resolve()`, which follows symlinks and normalizes `..`. If the resolved path falls outside `ORBIT_ROOT`, you get `VALIDATION_ERROR` - if it resolves inside but the file is not there, you get `FILE_NOT_FOUND`.

**Fix:** Print the path you are passing and run it through `Path(path).resolve()` manually in a Python REPL. Compare against the expected `~/.claude/orbit/active/<name>/...` shape.

### "Task shows up under the wrong repo after `create_orbit_files`"

**Cause:** This is the bug that motivated the `find_task_by_full_path` / `update_task_repo` reconciliation in `create_orbit_files`. `scan_all_repos()` matches tasks to repos by prefix, and when two repos share a prefix, it can pick the wrong one.

**Fix:** Already fixed in current code. If you see it anyway, make sure you are on a recent orbit version and that `create_orbit_files` is the tool you are calling (not a manual `create_task` + `scan_all_repos` sequence, which does not include the reconciliation step).

### "`list_active_tasks` is slow for big project counts"

**Cause:** `include_time=True` is the default, and even with batch lookups, 100+ tasks means 100+ rows joined across `heartbeats`, `sessions`, and `tasks`.

**Fix:** Pass `include_time=False` if you are not displaying time. The tool returns in under a millisecond without time, versus tens of milliseconds with time. For the dashboard, which always wants time, the batch query is fine; for scripts that just want the task list, skip it.

### "Heartbeats aren't showing up as time"

**Cause:** `record_heartbeat` writes to `heartbeats` (unprocessed) but `get_task_time` reads from `sessions` (processed). The aggregation step in between only happens when `process_heartbeats` runs.

**Fix:** Call `process_heartbeats` manually, or wait for the next PreCompact / Stop hook. See `architecture.md`'s invariants for the full guarantee.

### "Plan agents report status but plan status doesn't update"

**Cause:** `_update_plan_counters` only runs inside `update_agent_status`. If you are mutating the DB directly (e.g., via `sqlite3` or through `orbit_db` internals), the plan counter sync is skipped.

**Fix:** Always go through `update_agent_status` for status updates. If you need to bulk-update a plan, consider making a new MCP tool that takes a list of (agent_id, status) pairs and loops through `update_agent_status` - don't bypass it.

## Where to go from here

- [`architecture.md`](./architecture.md) - if you need the big picture on `tasks.db`, the hook model, or what `full_path` means.
- [`dashboard.md`](./dashboard.md) - if you want to see how the tools' return shapes get rendered in the UI.
- [`orbit-auto.md`](./orbit-auto.md) - if you are specifically curious about how `log_iteration` / `get_iteration_status` fit into an autonomous run.
- `mcp-server/src/mcp_orbit/tools_*.py` - the source. Each file is flat and independent; if you know which module owns a tool from the prefix, you can jump straight there.
- `mcp-server/src/mcp_orbit/models.py` - the full Pydantic models for every typed return. The canonical source for "what fields does this tool give me".
- `mcp-server/src/mcp_orbit/errors.py` - the full error code enum and exception types. Useful when writing a new tool or adding new error cases.

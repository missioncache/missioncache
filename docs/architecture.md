# Architecture

This document is for contributors and people who want to extend orbit. It describes how the pieces fit together, where state lives, and what invariants you need to respect if you want to add a new MCP tool, hook, or slash command without breaking anything downstream.

If you are just looking to *use* orbit, start with the [README](../README.md) instead.

## Mental model

Orbit is not one program. It is six small programs that agree on two files and one database:

- **Two files**: `~/.claude/orbit/<status>/<project>/<project>-tasks.md` and `<project>-context.md`. These are the human- and Claude-readable source of truth for what a project is doing.
- **One database**: `~/.claude/tasks.db` (SQLite). This is the source of truth for cross-project metadata: which projects exist, when they were last worked on, how much time was spent, and which repository they belong to.

Everything else is either a producer (hooks write heartbeats, MCP tools create files) or a consumer (the dashboard reads the DB, the statusline reads both DB and files, orbit-auto reads the tasks file and writes iteration logs). When you are trying to understand any component, ask: is it writing to the files, writing to the DB, or reading them?

## Component map

| Component | Directory | What it does | When it runs |
|-----------|-----------|--------------|--------------|
| MCP server | `mcp-server/` | Exposes ~30 tools to Claude Code over stdio for managing projects, files, time, plans, and iteration logs | One subprocess per Claude Code session, started on demand via `uvx` |
| orbit-db | `orbit-db/` | SQLite schema, data classes, and all direct DB operations. Every other component calls into this instead of opening the DB themselves | In-process library - embedded by MCP server, hooks, orbit-auto, and dashboard |
| Hooks | `hooks/` | Short-lived Python scripts Claude Code invokes on session lifecycle events (prompt submitted, session started, context compacting, session stopped) | One shot per event, all four hooks have a timeout budget between 5 and 30 seconds |
| Commands | `commands/` | Slash command markdown files that describe workflows to Claude. They do not run code - Claude reads them and decides which MCP tools to call | Parsed by Claude Code on plugin load, rendered when user types `/orbit:<name>` |
| orbit-auto | `orbit-auto/` | Standalone CLI that runs Claude in a loop over a task file, in either sequential or parallel-with-DAG mode | A long-running user-invoked process, one execution per `orbit-auto <project>` call |
| Dashboard | `orbit-dashboard/` | FastAPI backend plus a single-file HTML frontend at `http://localhost:8787` for visualizing time, tasks, sessions, and orbit-auto runs | One persistent process, usually managed by launchd |
| Statusline | `statusline/` | ~1,350-line Python script that produces a 6-7 line ANSI status block shown at the bottom of the Claude Code TUI | Invoked by Claude Code after every message, gets JSON on stdin, must be fast (sub-200ms target) |
| Rules | `rules/` | Plain markdown files describing orbit conventions to Claude. Auto-installed into `~/.claude/rules/` by the `SessionStart` hook using a write-if-different copy. Legacy installs via `setup.sh` instead use a symlink; the hook replaces stale symlinks on first run | Refreshed on every `SessionStart` event |

Two files at `.claude-plugin/` wire the plugin into Claude Code: `plugin.json` registers the MCP server and metadata, and `marketplace.json` catalogs orbit as an installable plugin so the repo itself doubles as a one-plugin marketplace. End users install via `/plugin marketplace add tomerbr1/claude-orbit` followed by `/plugin install orbit@claude-orbit`. Maintainers typically use `setup.sh`, which creates a separate local marketplace at `~/.claude/plugins/local-marketplace/` and installs the plugin from there as `orbit@local` for fast iteration without pushing to GitHub.

## Talking to the database through orbit-db

`orbit-db` is the only place in the codebase that writes raw SQL. Everything else goes through its Python API. This is intentional - the schema has grown over time, there are triggers, and there is an invariant about where `claude_session_cache` lives (covered below) that you will violate the first time you try to open the DB directly from somewhere else.

The package exposes a single `TaskDB` class that wraps a `sqlite3.Connection` with WAL mode enabled. You get it by calling `TaskDB()` (no arguments - it finds the DB at `~/.claude/tasks.db`). Every other component constructs its own instance; there is no global or singleton, because the consumers (hooks, MCP subprocess, orbit-auto, dashboard) are all different processes.

The file is ~3,400 lines, but the mental model is small:

- A handful of schema tables (see [Storage](#storage) below).
- One method per use case (`find_task_for_cwd`, `record_heartbeat`, `process_heartbeats`, `get_task_time`, `create_task`, `complete_task`, `get_repo_breakdown`, `scan_repos`, ...).
- Helpers for path-to-repo resolution, tag extraction, and heartbeat-to-session aggregation.

If you find yourself wanting to add an SQL query anywhere else in the repo, add a method to `TaskDB` first and call it from your component. This keeps the schema refactorable.

## How a Claude session flows through orbit

This is the best way to build intuition for the component boundaries: follow one user prompt from the moment it is typed to the moment the dashboard reflects it.

### 1. Session start

When Claude Code starts a new session in a repo, it fires the `SessionStart` hook. `hooks/session_start.py` does four things, in order:

1. Resolves the session ID from `CLAUDE_SESSION_ID` or stdin JSON.
2. Writes a `term-session` mapping file so that the statusline can later map terminal IDs back to session IDs.
3. Calls `db.find_task_for_cwd(cwd, session_id)` to see if the current directory belongs to a tracked orbit project.
4. If a task is found, writes `~/.claude/hooks/state/projects/<session-id>.json` - the per-session project pointer used both by the statusline (to render the active project name) and by `TaskDB.find_task_for_cwd` on subsequent prompts (to resolve which task a heartbeat belongs to). A legacy `pending-task.json` file is also written for historical reasons but is currently not consumed anywhere and is scheduled for removal.

The hook also prints a short "Active Task Detected" block to stdout, which Claude Code injects into the conversation context. This is how Claude learns which project it is working on without the user having to say so.

### 2. User submits a prompt

Every time the user hits enter, Claude Code fires `UserPromptSubmit`. Orbit registers two independent scripts on this hook:

- `hooks/activity_tracker.py` spawns a short-lived subprocess (`python -m orbit_db heartbeat-auto`) with a hard 2-second timeout and `PYTHONPATH` set to the plugin-bundled `orbit-db`. The subprocess calls `TaskDB.record_heartbeat_auto(cwd, session_id)`, which delegates to `find_task_for_cwd` - that checks `projects/<session-id>.json` and then pattern-matches `cwd` against `~/.claude/orbit/active/<task>/` and tracked-repo legacy paths. If a match is found, a row is inserted into the `heartbeats` table with the current timestamp and session ID. The subprocess boundary is deliberate: SQLite lock contention on the task DB can otherwise stall the heartbeat call for up to its 5-second `busy_timeout`, which would eat the entire `UserPromptSubmit` hook budget. The 2-second subprocess deadline bounds that worst case. Skip patterns filter out slash commands, shell commands, yes/no confirmations, and empty prompts so they do not inflate the count.
- `hooks/task_tracker.py` checks for "divergence" between the tasks file and the context file (headings like `### Task 3` present in context while `- [ ] 3. ...` is still unchecked in tasks) and prints a reminder to stdout so Claude sees it. This is the guardrail that keeps the tasks file honest.

Both hooks have a 5-second budget. If no task matches the current directory or session, they both exit silently; there is no penalty for running orbit in a repo that has no projects.

### 3. Claude uses MCP tools

When the user (or a slash command) asks Claude to do something that touches orbit state - "mark task 3 complete", "give me the current project status", "record an iteration" - Claude calls one of the `mcp__plugin_orbit_pm__*` tools. Those tools live in the MCP server subprocess that was started by the plugin manifest.

The MCP server is stdio-based and very thin. `mcp-server/src/mcp_orbit/server.py` is 30 lines: it imports five tool modules, each of which registers its tools against a shared `FastMCP` instance from `app.py`. Every tool:

1. Opens a `TaskDB` via a lazy `get_db()` helper.
2. Calls a handful of `TaskDB` methods.
3. Returns a Pydantic-validated dict.
4. Catches `OrbitError` and returns it as a structured error dict.

No tool writes to the DB on its own. No tool opens a second database file. No tool spawns subprocesses. If a new tool needs to do something weird, the fix is usually to add a method to `TaskDB` or a helper to `helpers.py`, not to deviate from this pattern.

The MCP tool call is what turns "user intent expressed in English" into "row inserted in SQLite". This is the primary write path for projects, task updates, completion, and orbit file creation.

### 4. Context compaction

Claude Code fires `PreCompact` when it is about to compress the conversation to fit in the context window. `hooks/pre_compact.py` has 30 seconds to do three things:

1. Find the task for the current cwd.
2. Open `<project>-context.md`, update the `**Last Updated:**` line, and add a compaction note under `## Recent Changes`.
3. Call `db.process_heartbeats()` to aggregate unprocessed heartbeats into the `sessions` table.

This is the load-bearing piece of orbit's memory story. Without it, the context file would go stale between sessions and Claude would lose the thread on resume. The time budget is larger than the other hooks because heartbeat processing is not free on large DBs.

### 5. Session stop

`hooks/stop.py` runs when the session ends. It reads the transcript file Claude Code points it at, checks whether any `Write` or `Edit` tool calls happened during the session, and if there is an active task with orbit files, prints a stderr reminder to run `/orbit:save`. It does not touch the DB - its job is purely to nudge the user.

### 6. Dashboard reflection

Separately from the per-session flow above, the dashboard is a persistent process reading from a DuckDB copy of the SQLite DB. At startup (or on explicit `GET /api/sync`) it copies tables from `~/.claude/tasks.db` into `~/.claude/tasks.duckdb`, which it then uses for fast aggregate queries. The dashboard also reads orbit files directly from disk to render task modals, and it reads `~/.claude/projects/<cwd>/*.jsonl` files (Claude Code's own transcript store) to render untracked activity.

See [The dual-database pattern](#the-dual-database-pattern) for the details of why there are two DB files and what trade-offs it makes.

## Storage

Orbit's state lives in three places:

1. **Structured data** in SQLite at `~/.claude/tasks.db`, plus the DuckDB analytics mirror at `~/.claude/tasks.duckdb`.
2. **Human-readable project state** in markdown files under `~/.claude/orbit/{active,completed}/<project>/`.
3. **Ephemeral hook state** in JSON files under `~/.claude/hooks/state/`.

### SQLite schema

The canonical tables are all defined in `orbit-db/orbit_db/__init__.py`. There is one more table (`claude_session_cache`) that is created by the dashboard, which is why this table list has two sections.

**Defined by orbit-db:**

| Table | Purpose | Key columns |
|-------|---------|-------------|
| `repositories` | Tracked git repos | `id`, `path` (UNIQUE), `short_name`, `active`, `last_scanned_at` |
| `tasks` | Projects (both coding and non-coding) | `id`, `repo_id`, `name`, `full_path`, `parent_id`, `status`, `type`, `tags` (JSON), `jira_key`, `branch`, `pr_url`, `last_worked_on` |
| `task_updates` | Append-only progress notes for non-coding projects | `task_id`, `note`, `created_at` |
| `heartbeats` | WakaTime-style activity pings, one per non-skipped user prompt | `task_id`, `timestamp`, `session_id`, `processed` |
| `sessions` | Aggregated work sessions, computed from heartbeats by `process_heartbeats()` | `task_id`, `session_id`, `start_time`, `end_time`, `duration_seconds`, `heartbeat_count` |
| `config` | Key-value settings (idle timeout, assumed work seconds, prune threshold, tag keywords) | `key`, `value` |
| `auto_executions` | One row per `orbit-auto` run | `task_id`, `started_at`, `completed_at`, `status`, `mode`, `worker_count`, `total_subtasks`, `completed_subtasks`, `failed_subtasks` |
| `auto_execution_logs` | Streaming log lines from orbit-auto workers | `execution_id`, `worker_id`, `subtask_id`, `level`, `message`, `timestamp` |

Triggers keep `updated_at` fresh on every row update and set `completed_at`/`archived_at` when `status` transitions into the corresponding state. The `tasks` table has a `UNIQUE(repo_id, full_path)` constraint that matters during scan - rescanning the same repo should not produce duplicate rows.

**Created by the dashboard (also in `~/.claude/tasks.db`):**

| Table | Purpose | Key columns |
|-------|---------|-------------|
| `claude_session_cache` | Cache of Claude Code's JSONL session transcripts, populated by `orbit-dashboard/lib/analytics_db.py`. Used to compute "untracked" sessions (Claude activity with no orbit project loaded) and to merge JSONL time with orbit heartbeat time | `session_id`, `file_path`, `date`, `hour`, `cwd`, `git_branch`, `project_path`, `message_count`, `tool_call_count`, `input_tokens`, `output_tokens`, `duration_seconds` |

**Invariant:** `claude_session_cache` and `sessions` must be in the same SQLite file. The dashboard's "show me untracked Claude activity" query is a `LEFT JOIN ... WHERE s.session_id IS NULL` anti-join between the two tables. If you split them into separate databases, the anti-join silently returns empty and all untracked sessions disappear from the dashboard.

This is the thing that will bite you first if you refactor the storage layer. There is a note in the context file about it and it is worth respecting.

### The dual-database pattern

The dashboard reads from DuckDB (`~/.claude/tasks.duckdb`), not from SQLite directly. The trade-off is explicit:

- **Writes go to SQLite.** Heartbeats, task creation, completion, scanning, session aggregation - all of that is standard SQLite via `orbit-db`. SQLite handles concurrent writers across processes well enough for orbit's write volume.
- **Analytics reads go to DuckDB.** The dashboard runs a lot of aggregate queries (day-of-week buckets, 30-day heatmaps, repo breakdowns, trend deltas), and DuckDB is 10-100x faster than SQLite for those.
- **Sync is explicit.** On dashboard startup and on `GET /api/sync`, the dashboard copies tables from SQLite into DuckDB. There is no continuous streaming replication. `migrate_to_duckdb.py` is the standalone script version of the same sync.

The practical consequence is that the dashboard is always slightly stale. A heartbeat recorded ten seconds ago will not show up until the next sync. In exchange, the "Activity History" screen renders 90 days of data in under a second.

If you are adding a new dashboard endpoint: do the read against DuckDB via `analytics_db.py`. If you are adding a new MCP tool: do the write against SQLite via `orbit-db`. Cross the streams only if you have a very specific reason.

### Files on disk

Projects have their own directory on disk under `~/.claude/orbit/`:

```
~/.claude/orbit/
├── active/
│   └── <project-name>/
│       ├── <project-name>-plan.md       # Optional: implementation plan
│       ├── <project-name>-context.md    # Key decisions, gotchas, next steps
│       ├── <project-name>-tasks.md      # Checkboxes - source of truth for progress
│       ├── <project-name>-auto-log.md   # Written by orbit-auto during runs (optional)
│       └── prompts/                     # Optional: optimized per-subtask prompts
│           ├── README.md
│           ├── task-01-prompt.md
│           └── ...
└── completed/
    └── <project-name>/                  # Same layout, moved on /orbit:done
```

The `full_path` column in the `tasks` table stores `"active/<name>"` or `"completed/<name>"`, which is how the DB stays in sync with the on-disk status. `/orbit:done` moves the directory and updates the row in one step.

There is also a legacy layout under `<repo>/dev/{active,completed}/` that older projects still use. Dashboard and hooks fall back to it if the centralized path does not exist. If you are touching path resolution, check both `mcp-server/src/mcp_orbit/helpers.py` (for MCP writes) and `parse_orbit_progress()` in `orbit-dashboard/server.py` (for dashboard reads). They are independent implementations - keep them consistent.

### Ephemeral hook state

`~/.claude/hooks/state/` holds small JSON files that coordinate between hooks and the statusline:

| File | Written by | Read by | Purpose |
|------|------------|---------|---------|
| `projects/<session-id>.json` | `session_start.py` only | `statusline.py`, `TaskDB.find_task_for_cwd` | Which project is active for a given session. Read on both the statusline rendering path (to show the project name) and the heartbeat path (to attribute time to the right task when `cwd` does not match a known task directory). Only written when `session_start.py` resolves a task at `SessionStart` time; mid-session project loads via slash commands do not write it |
| `term-sessions/<term-id>` | `session_start.py` | `statusline.py` | Maps terminal-emulator session IDs back to Claude session IDs so mid-session lookups work from any tab |
| `pending-task.json` | `session_start.py`, `/orbit:go`, `/orbit:save` | *(nothing)* | Legacy state file. Written for historical reasons but not consumed by any current code path. Safe to remove in a cleanup pass |
| `pending-project.json` | *(nothing)* | `TaskDB.find_task_for_cwd` priority-1 branch | Inverse legacy: read but never written. The priority-1 branch in `find_task_for_cwd` is effectively dead code - task resolution always falls through to the `projects/<session-id>.json` branch |

These files are deliberately plain JSON and deliberately per-session. Early versions of orbit used a single shared project file that race-conditioned badly once multiple Claude sessions ran concurrently. If you add new state here, shard by session ID by default.

## Process model

It is easy to get confused about what is running where, because orbit has a mix of long-running processes, stdio subprocesses, per-event short-lived scripts, and an embedded library. Here is the full picture:

| Runs | Process type | Lifetime | Triggered by |
|------|--------------|----------|--------------|
| Dashboard | Long-running (launchd-managed in practice) | Until stopped | Manual start or launchd |
| MCP server | Stdio subprocess | One per Claude Code session | Claude Code on plugin load |
| Hooks (`session_start`, `pre_compact`, `stop`, `activity_tracker`, `task_tracker`) | Short-lived one-shot | Single event | Claude Code hook events |
| orbit-auto | User-invoked CLI | Until task complete or failed | Manual `orbit-auto <project>` |
| orbit-auto workers | Subprocesses of orbit-auto | One per subtask in parallel mode | `parallel.py` |
| Statusline | Very short-lived | ~50-200ms per invocation | Claude Code after every message |

Rules and slash commands are not processes - they are files read by Claude Code.

Two implications worth keeping in mind:

1. **There is no orbit daemon.** Everything except the dashboard runs on demand. If the dashboard is down, heartbeats still accumulate in SQLite; the dashboard will just look stale until it syncs.
2. **There is no IPC except the database.** Hooks do not call the MCP server. The statusline does not call the dashboard (well, it does fetch usage data from a separate endpoint, but that is unrelated to orbit state). Dashboards and MCP tools do not coordinate. All of them read and write the same SQLite file (plus, optionally, the files on disk).

This shape was a deliberate choice. It makes the system easy to reason about and easy to run partially (you can skip the dashboard, skip the statusline, even skip the hooks and still have a working orbit), at the cost of losing some real-time-ness that a centralized daemon would give you for free.

## Extension points

Here is how to do the six most common things you might want to extend orbit to do.

### 1. Add a new MCP tool

Pick the tool module that matches your use case:

- Task lifecycle (create, list, complete, reopen, update): `tools_tasks.py`
- Orbit file operations (create files, update context, mark tasks complete in the markdown, get progress): `tools_docs.py`
- Time tracking (heartbeats, sessions, repos): `tools_tracking.py`
- Orbit-auto iteration logging: `tools_iteration.py`
- Multi-agent planning: `tools_planning.py`

Then follow the pattern every existing tool uses:

```python
from typing import Annotated
from pydantic import Field

from .app import mcp
from .db import get_db
from .errors import OrbitError

@mcp.tool()
async def my_tool(
    param: Annotated[str, Field(description="What this parameter is for")],
) -> dict:
    """One-line summary shown in Claude's tool picker. Keep it short."""
    db = get_db()
    try:
        return {"success": True, ...}
    except OrbitError as e:
        return e.to_dict()
    except Exception as e:
        logger.exception("Error in my_tool")
        return {"error": True, "message": str(e)}
```

The `@mcp.tool()` decorator registers your function against the shared `mcp` instance. As long as the containing module is imported by `server.py`, the tool will show up as `mcp__plugin_orbit_pm__my_tool` in Claude Code. If you add a new module file, add the `from . import tools_mything` line to `server.py` too.

**Important:** MCP tool docstrings and parameter descriptions are the only thing Claude sees when picking tools. A vague docstring is a bug - the tool will never get called or will be called at the wrong time. Write the docstring like you are writing a commit title.

After adding the tool, reinstall the plugin. If you are hacking on orbit locally from a clone, the fast path is the local marketplace that `setup.sh` set up:

```bash
claude plugins install orbit@local
```

If you are iterating against a marketplace-installed copy instead, push your changes to GitHub and run `claude plugins update orbit@claude-orbit`. Either way, restart the Claude Code session afterwards - MCP tool registration is cached at plugin load time.

### 2. Add a new hook

Decide which Claude Code hook event you want to react to. The supported events are documented in Claude Code's hook reference - orbit currently uses four: `SessionStart`, `UserPromptSubmit`, `PreCompact`, `Stop`.

Write a Python script in `hooks/` that:

- Reads the hook JSON from stdin (for events that provide it).
- Does its work within the event's time budget (typically 5-10 seconds; `PreCompact` gets 30).
- Exits silently on any error. Hooks should never block Claude Code from running.
- Uses `from orbit_db import TaskDB` if it needs DB access - never opens SQLite directly.

Add it to `hooks/hooks.json` under the corresponding event:

```json
"UserPromptSubmit": [
  {
    "hooks": [
      { "type": "command", "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/my_hook.py", "timeout": 5 }
    ]
  }
]
```

Multiple hooks on the same event run in the order they are listed. They share no state other than what they write to disk. If you need to coordinate with another hook, use a file under `~/.claude/hooks/state/`.

Reinstall the plugin and restart Claude Code for the hook definition changes to take effect.

### 3. Add a new slash command

Slash commands are markdown files in `commands/`. There is no code. Create `commands/<name>.md`:

```markdown
---
description: "Short description shown in /help"
argument-hint: "[optional args]"
---

# What this command does

<instructions for Claude, written as prose>
```

The frontmatter is parsed by Claude Code for the help menu. The body is prose instructions that Claude follows when the user types `/orbit:<name>`. You can reference MCP tools by their `mcp__plugin_orbit_pm__*` names and Claude will call them in order.

If your command needs to be explicit about when to call which tool, write a numbered workflow with MCP tool names inline. See `commands/go.md` for a good example of a multi-step workflow with conditional branches.

Reinstall the plugin (or use `/reload-plugins` for command-only changes) to pick up new commands.

### 4. Add a new orbit-auto mode or worker behavior

orbit-auto is structured around three files you will probably want to touch:

- `orbit-auto/orbit_auto/dag.py` - builds the dependency graph from prompt frontmatter. Touch this if you are changing how subtasks discover their dependencies.
- `orbit-auto/orbit_auto/sequential.py` or `parallel.py` - the two execution strategies. They share `worker.py` for the actual Claude subprocess spawning.
- `orbit-auto/orbit_auto/worker.py` - what a single iteration does: load task file, spawn Claude, parse learning tags from the response, decide if the task is done, update state.

If you add a new mode (say, a "review mode" that spawns a code reviewer after every task), put the top-level orchestration in a new module parallel to `sequential.py` and `parallel.py`, and route to it from `cli.py`. Do not fork worker.py unless you have a very good reason - the completion-detection logic (`<what_worked>`, `<promise>COMPLETE</promise>`) is subtle and worth sharing.

`db_logger.py` is what writes to `auto_executions` and `auto_execution_logs`. If your new mode needs to be visible in the dashboard, log through it.

### 5. Customize the statusline

`statusline/statusline.py` is a single ~1,350-line file because it is performance-sensitive - every import costs milliseconds at the bottom of every Claude message. The layout is:

- **Constants and colors** at the top.
- **Data collection functions** that read from the DB, the projects state dir, git, and the file system.
- **Rendering functions** that build each of the 6-7 lines.
- `main()` at the bottom that stitches them together.

If you want to add a new line, add a rendering function and call it from `main()` before the final print. If you want to change what an existing line shows, find the matching rendering function by grep-ing for its label.

The statusline has its own environment-variable-based configuration (search for `STATUSLINE_` in the file). Document any new variable at the top of the file next to the existing ones.

Do not add dependencies. The statusline uses stdlib only because installing packages into the wrong Python would silently break it for every user. If you absolutely need a third-party library, wire it up via `orbit-db` or `analytics_db.py` instead and call that.

### 6. Run the dashboard somewhere non-default

The dashboard reads from `~/.claude/tasks.db` and `~/.claude/tasks.duckdb` and listens on port 8787 by default. To change:

- `DUCKDB_PATH` and `SQLITE_PATH` in `orbit-dashboard/lib/analytics_db.py` control the read paths.
- The port is the argument to `uvicorn.run(...)` in `orbit-dashboard/server.py`.
- `ORBIT_DASHBOARD_URL` is the env var the statusline uses to build OSC 8 hyperlinks to the dashboard. If you change the dashboard's host or port, set this in your shell init.

launchd config lives at `~/Library/LaunchAgents/com.orbit.dashboard.plist`. It explicitly points at `/opt/homebrew/bin/python3.11` because orbit's DuckDB install is Python-version-specific. Do not switch it to system Python or `python3` without reinstalling DuckDB under that Python.

## Invariants and gotchas

These are the things that will trip you up if you are new to the codebase, collected from bugs that have actually shipped.

- **Never open `~/.claude/tasks.db` outside of `orbit-db` or `analytics_db`.** The schema has triggers, the WAL settings matter, and `claude_session_cache` is implicitly required to live in that same file. Opening it from a new script almost always breaks something.
- **`claude_session_cache` must stay in `tasks.db`, not `tasks.duckdb`.** The untracked-session anti-join is a `LEFT JOIN` against the `sessions` table. Separate databases means the join silently returns empty.
- **`full_path` on the `tasks` row must match the actual directory location under `~/.claude/orbit/`.** `/orbit:done` moves the directory *and* updates `full_path` *and* sets `status=completed`. If you write code that does one and not the others, you get orphan tasks that the dashboard renders in the wrong list.
- **Hook failures must be silent.** Any exception in any hook will be swallowed by a top-level `try/except` and printed to stderr. This is intentional - a broken hook should never block Claude Code from working. If your new hook throws uncaught exceptions, Claude Code will still keep running, but you will never know the hook is broken.
- **MCP tool docstrings are the tool description.** They are not optional. Claude reads them to decide when to call the tool.
- **`projects/<session-id>.json` is the only path that routes automatic heartbeats to a task when `cwd` is outside `~/.claude/orbit/active/<task>/`.** `session_start.py` is its sole writer, and it only writes it when it resolves a task at `SessionStart`. If a user loads a project mid-session via `/orbit:go` in a session that did not resolve at start, subsequent `UserPromptSubmit` heartbeats will not attribute to the new task (cwd pattern matching will not help, and this file will not exist). `/orbit:go` masks this by recording an explicit initial heartbeat, but that covers only the one call. If you add a new "load project mid-session" command, write this file yourself or accept the tracking gap.
- **`process_heartbeats()` drains unprocessed heartbeats into aggregated sessions in a single pass.** Each row is aggregated exactly once and then marked `processed=1`, so calling it twice is safe (the second call is a no-op). Calling it zero times means newly accumulated time will not appear in the dashboard until `PreCompact` fires. orbit-auto and some MCP tools invoke it manually to keep the dashboard fresh.
- **Repo ID resolution is order-dependent in `scan_repos()`.** If you call `create_orbit_files` with a brand-new repo path, the same call will register the repo and may assign the task's `repo_id` to the wrong row if there are unresolved ambiguities. This has been fixed multiple times; if you are touching that path, re-test the "brand new repo with existing tasks" case manually.
- **The dashboard's `_get_jsonl_task_times()` joins by `cwd = repo.path`.** Multiple tasks sharing a repo all draw from the same JSONL pool, and the only disambiguator is `c.date >= DATE(t.created_at)`. Overlapping tasks on the same repo can therefore double-count - known limitation.
- **`_effective_time(task_id, heartbeat, jsonl) = max(heartbeat, jsonl)` is applied in `/api/tasks/active`, `/api/tasks/completed`, and `/api/task/<id>`.** Dashboard-reported times are merged orbit+Claude-JSONL, not pure heartbeats. If you are debugging a time discrepancy between orbit-db and the dashboard, this is almost always why.
- **`UserPromptSubmit` can receive `prompt` as a list of content blocks** (when the user attaches an image). Any hook or tool that treats `prompt` as a string without flattening the list will crash. `activity_tracker.py` and `task_tracker.py` both handle this - copy the pattern.

## Where to go from here

This doc is the foundation; the other component docs assume you have read it.

- [`dashboard.md`](./dashboard.md) - dashboard screens, API endpoints, sync, customization.
- [`orbit-auto.md`](./orbit-auto.md) - sequential vs parallel, DAG resolution, learning tags, worker model.
- [`mcp-tools.md`](./mcp-tools.md) - full MCP tool reference with parameters and return shapes.
- [`statusline.md`](./statusline.md) - statusline layout, configuration, icons.
- [`hooks.md`](./hooks.md) - hook event reference, state files, adding new hooks.

# Orbit - Project Manager for Claude Code

Orbit is a comprehensive project management plugin for [Claude Code](https://claude.ai/code). It provides structured task tracking, autonomous execution, time analytics, and a web dashboard - all integrated directly into your Claude Code workflow.

## Features

- **Slash Commands** - Create, continue, save, and complete projects with simple commands
- **30+ MCP Tools** - Task management, documentation, time tracking, iteration logging, and planning
- **Lifecycle Hooks** - Auto-detect active tasks on session start, preserve context before compaction
- **Orbit Auto** - Autonomous execution CLI with parallel, dependency-aware task scheduling
- **Orbit Dashboard** - Web UI with task tracking, time analytics, and execution monitoring
- **Statusline** (optional) - Rich terminal status display showing project, git, model, and usage info

## Quick Start

```bash
git clone https://github.com/tomerbr1/claude-orbit.git
cd claude-orbit-projects-manager
./setup.sh
```

The interactive setup script will:
1. Install the Orbit plugin for Claude Code
2. Set up the task database
3. Install the Orbit Dashboard (web UI at localhost:8787)
4. Install the Orbit Auto CLI
5. Optionally install the statusline

## Commands

| Command | Description |
|---------|-------------|
| `/orbit:new` | Create a new project with plan, context, and task files |
| `/orbit:go` | Resume work on an active project |
| `/orbit:save` | Persist progress before session end or compaction |
| `/orbit:done` | Mark a project as completed and archive |
| `/orbit:prompts` | Generate optimized prompts for subtasks |
| `/orbit:mode` | Assign workflow mode (interactive/autonomous) to tasks |

## Project Lifecycle

1. **Create**: `/orbit:new my-feature`
   - Creates plan, context, and tasks files
   - User approves the plan
   - Agent/skill discovery and gap analysis
   - Generates optimized prompts for each subtask

2. **Work**: Execute prompts manually or via Orbit Auto
   ```bash
   orbit-auto my-feature              # Parallel (default: 8 workers)
   orbit-auto my-feature -w 12        # 12 workers
   orbit-auto my-feature --sequential # One task at a time
   ```

3. **Checkpoint**: `/orbit:save` persists context (auto-runs on compaction)

4. **Complete**: `/orbit:done` archives to `~/.claude/orbit/completed/`

## Components

### Plugin (root)
Claude Code plugin providing slash commands, MCP tools, and lifecycle hooks. Installed via the local marketplace.

### orbit-db
Core database layer for task and time tracking. Uses SQLite at `~/.claude/tasks.db` with WakaTime-style heartbeat aggregation.

### orbit-auto
Autonomous execution CLI that runs Claude Code in a loop to complete project tasks. Supports parallel execution with dependency-aware DAG scheduling.

### orbit-dashboard
Web dashboard at `localhost:8787` showing:
- Active and completed tasks with time tracking
- Productivity heatmaps and analytics
- Orbit Auto execution monitoring with DAG visualization
- Claude Code usage statistics

### statusline (optional)
Rich multi-line status display for Claude Code terminal showing active project, git status, model info, context usage, and API limits.

## MCP Tools

All tools are available via the `mcp__plugin_orbit_pm__` prefix:

### Task Management
- `list_active_tasks` - List active projects with time tracking
- `get_task` - Get full project details
- `create_task` - Create project in database
- `complete_task` - Mark project as completed
- `reopen_task` - Reopen a completed project

### File Operations
- `create_orbit_files` - Create plan/context/tasks files
- `get_orbit_files` - Get file paths for a project
- `update_context_file` - Update context.md atomically
- `update_tasks_file` - Update tasks.md atomically
- `get_orbit_progress` - Parse progress from tasks.md

### Time Tracking
- `record_heartbeat` - Record activity heartbeat
- `process_heartbeats` - Aggregate into sessions
- `get_task_time` - Get time spent on a project

### Iteration Logging
- `log_iteration` - Log autonomous iteration
- `log_completion` - Log iteration completion/timeout
- `get_iteration_status` - Get loop state

### Repository
- `list_repos` - List tracked repositories
- `add_repo` - Add repository to track
- `scan_repos` - Scan for orbit projects

## Hooks

- **SessionStart** - Auto-detect active project on session start
- **PreCompact** - Auto-save context before compaction
- **Stop** - Remind about `/orbit:save` if files were edited

## Data Storage

| Path | Purpose |
|------|---------|
| `~/.claude/orbit/active/` | Active project files (plan, context, tasks) |
| `~/.claude/orbit/completed/` | Archived completed projects |
| `~/.claude/tasks.db` | SQLite database (task tracking, time) |
| `~/.claude/tasks.duckdb` | DuckDB analytics (synced from SQLite) |

## Requirements

- Python 3.11+
- Claude Code CLI
- pip

## License

MIT

# MissionCache Auto

Autonomous AI development tool for completing programming tasks iteratively.

## Installation

Requires Python 3.11+.

```bash
cd missioncache-auto
pip install -e .
```

Or run directly:
```bash
python -m missioncache_auto <task-name>
```

## Quick Start

```bash
# Initialize a new task
missioncache-auto init my-feature "Add user authentication"

# Run in parallel mode (default, 8 workers)
missioncache-auto my-feature

# Run in sequential mode
missioncache-auto my-feature --sequential

# Show execution plan without running
missioncache-auto my-feature --dry-run

# Check task status
missioncache-auto status my-feature
```

## Usage

```
missioncache-auto <task-name> [options]
missioncache-auto init <task-name> "description"
missioncache-auto status <task-name>
```

### Options

| Option | Description |
|--------|-------------|
| `-w, --workers N` | Number of parallel workers (default: 8, max: 12) |
| `-r, --retries N` | Max retries per task (default: 3) |
| `--sequential, -s` | Run in sequential mode |
| `--parallel, -p` | Run in parallel mode (default) |
| `--dry-run` | Show execution plan without running |
| `--fail-fast` | Stop all workers on first failure |
| `--worktree` | Isolate each worker in its own git worktree (default on git repos) |
| `--no-worktree` | Run all workers in the shared checkout instead of per-worker worktrees |
| `--no-commit` | Disable the automatic git commit after each task |
| `-v, --visibility` | Output level: verbose, minimal, none |
| `--no-color` | Disable colored output |

On a git repository, parallel runs isolate each worker in its own worktree by default, then commit each task and merge the results back. Two flag combinations are refused with exit code 3 because they would silently lose work:

- `--no-commit` while worktrees are on: worker output lives only on per-worker branches with nothing committed, so it is discarded when the worktrees are removed. Drop `--no-commit`, or add `--no-worktree` (optionally with `-w 1`).
- `--no-worktree` with auto-commit and more than one worker: the shared workers race on `git add -A`/commit and can lose each other's work. Isolate them (drop `--no-worktree`), pass `--no-commit`, or run `-w 1`.

Worktrees branch from `HEAD`, so parallel runs also refuse (exit code 3) when the main checkout has uncommitted tracked changes - commit or stash them first, or pass `--no-worktree`. Untracked files only warn.

### Environment Variables

| Variable | Description |
|----------|-------------|
| `MISSIONCACHE_AUTO_VISIBILITY` | Default visibility level (verbose, minimal, none) |

## Task Structure

Tasks are organized in `~/.missioncache/active/<task-name>/`:

```
~/.missioncache/active/my-feature/
+-- my-feature-tasks.md      # Checkbox task list
+-- my-feature-context.md    # Project context and learnings
+-- my-feature-plan.md       # Implementation plan
+-- my-feature-auto-log.md   # Iteration history (auto-created)
+-- prompts/                 # Optimized prompts (optional)
    +-- README.md
    +-- task-01-prompt.md
    +-- task-02-prompt.md
    +-- ...
```

## Modes

### Sequential Mode

Runs tasks one at a time, in order. Good for:
- Simple linear workflows
- Tasks that need careful human oversight
- Debugging specific task failures

### Parallel Mode

Runs multiple tasks concurrently, respecting dependencies. Good for:
- Tasks with clear dependency graphs
- Maximizing throughput
- Large task sets with independent work

Requires prompts directory with YAML frontmatter defining dependencies:

```yaml
---
task_id: "01"
task_title: "Add priority field"
dependencies: []
---
```

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | All tasks completed successfully |
| 1 | Max retries reached (failed) |
| 2 | Blocked on [WAIT] task |
| 3 | Configuration or setup error |

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Type checking
mypy missioncache_auto

# Linting
ruff check missioncache_auto
```

## Architecture

```
missioncache_auto/
+-- __init__.py          # Package exports
+-- __main__.py          # Entry point: python -m missioncache_auto
+-- cli.py               # Argument parsing, commands
+-- models.py            # Data models (Task, State, Config)
+-- dag.py               # Dependency graph builder
+-- state.py             # State management with file locking
+-- task_parser.py       # Parse tasks.md and prompts
+-- claude_runner.py     # Claude CLI integration
+-- display.py           # Terminal output and colors
+-- sequential.py        # Sequential execution
+-- parallel.py          # Parallel orchestration
+-- worker.py            # Worker process
+-- init_task.py         # Task initialization
+-- templates/           # Task templates
```

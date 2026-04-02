# Orbit Auto

Autonomous AI development tool for completing programming tasks iteratively.

## Installation

Requires Python 3.11+.

```bash
cd orbit-auto
pip install -e .
```

Or run directly:
```bash
python -m orbit_auto <task-name>
```

## Quick Start

```bash
# Initialize a new task
orbit-auto init my-feature "Add user authentication"

# Run in parallel mode (default, 8 workers)
orbit-auto my-feature

# Run in sequential mode
orbit-auto my-feature --sequential

# Show execution plan without running
orbit-auto my-feature --dry-run

# Check task status
orbit-auto status my-feature
```

## Usage

```
orbit-auto <task-name> [options]
orbit-auto init <task-name> "description"
orbit-auto status <task-name>
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
| `-v, --visibility` | Output level: verbose, minimal, none |
| `--no-color` | Disable colored output |

### Environment Variables

| Variable | Description |
|----------|-------------|
| `ORBIT_AUTO_VISIBILITY` | Default visibility level (verbose, minimal, none) |

## Task Structure

Tasks are organized in `~/.claude/orbit/active/<task-name>/`:

```
~/.claude/orbit/active/my-feature/
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
mypy orbit_auto

# Linting
ruff check orbit_auto
```

## Architecture

```
orbit_auto/
+-- __init__.py          # Package exports
+-- __main__.py          # Entry point: python -m orbit_auto
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

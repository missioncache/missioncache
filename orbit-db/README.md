# orbit-db

SQLite-based task and time tracking database for the
[orbit](https://github.com/tomerbr1/orbit-pm) Claude Code plugin.

Provides cross-repo task tracking with WakaTime-style heartbeat time
aggregation. Used as the storage layer for the orbit MCP server, hooks,
CLI, and dashboard, but it's a standalone library and can be used on its
own.

## Install

```bash
pip install orbit-db
```

## Use as a library

```python
from orbit_db import TaskDB

db = TaskDB()            # defaults to ~/.claude/tasks.db
db.initialize_db()
repo_id = db.add_repo("/path/to/repo")
task = db.create_task(name="my-task", repo_id=repo_id)
db.record_heartbeat(task_id=task.id, directory="/path/to/repo")
```

## Use as a CLI

```bash
orbit-db list-active
orbit-db heartbeat-auto
orbit-db task-time <task_id>
orbit-db --help
```

## Storage

All state lives in a single SQLite database at `~/.claude/tasks.db`
(override with `TASK_DB_PATH`). The database is WAL-mode, auto-initializes
on first access, and is safe for concurrent readers.

## License

MIT

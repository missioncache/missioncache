# missioncache-db

SQLite-based task and time tracking database for the
[MissionCache](https://github.com/tomerbr1/orbit-pm) Claude Code plugin.

Provides cross-repo task tracking with WakaTime-style heartbeat time
aggregation. Used as the storage layer for the MissionCache MCP server, hooks,
CLI, and dashboard, but it's a standalone library and can be used on its
own.

## Install

```bash
pip install missioncache-db
```

## Use as a library

```python
from missioncache_db import TaskDB

db = TaskDB()            # defaults to ~/.missioncache/tasks.db, override via TaskDB(db_path=...)
repo_id = db.add_repo("/path/to/repo")
task = db.create_task(name="my-task", repo_id=repo_id)
db.record_heartbeat(task_id=task.id, directory="/path/to/repo")
```

## Use as a CLI

```bash
missioncache-db list-active
missioncache-db heartbeat-auto
missioncache-db task-time <task_id>
missioncache-db --help
```

## Storage

All state lives in a single SQLite database at `~/.missioncache/tasks.db`
(override by passing `db_path` to `TaskDB`). The database is WAL-mode,
auto-initializes on first access, and is safe for concurrent readers.

## License

MIT

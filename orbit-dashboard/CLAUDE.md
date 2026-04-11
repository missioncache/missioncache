# Orbit Dashboard

Dashboard for productivity tracking and task analytics.

## Quick Start

```bash
# Start server (usually runs via launchd)
python3.11 server.py

# Access dashboard
open http://localhost:8787
```

## Architecture

### Database (Dual-DB Pattern)

| Database | File | Purpose |
|----------|------|---------|
| SQLite | `~/.claude/tasks.db` | Source of truth for **writes** (heartbeats, sessions) |
| DuckDB | `~/.claude/tasks.duckdb` | Analytics database for **reads** (fast columnar queries) |

**Data flow:** Claude Code hooks -> SQLite -> sync -> DuckDB -> Dashboard

**Sync happens:**
- On server startup
- Via `/api/sync` endpoint
- Via `migrate_to_duckdb.py` script

### Key Files

| File | Lines | Purpose |
|------|-------|---------|
| `server.py` | ~4000 | FastAPI backend with all APIs |
| `index.html` | ~7700 | Single-page app (embedded CSS/JS) |
| `lib/analytics_db.py` | ~1100 | DuckDB operations layer |
| `migrate_to_duckdb.py` | ~600 | SQLite -> DuckDB migration |

### Deployment

- **Launchd plist**: `~/Library/LaunchAgents/com.orbit.dashboard.plist`
- **Python**: Must use `/opt/homebrew/bin/python3.11` (not system Python)
- **Port**: 8787

## API Reference

### Project & Activity APIs

```
GET /api/tasks/active      # Active tasks (excludes orphans with orbit files in completed/)
GET /api/tasks/completed   # Completed tasks + orphans (orbit files in completed/)
GET /api/stats/today       # Today's activity (tasks, LOC, sessions)
GET /api/stats/day?date=   # Historical day stats
GET /api/stats/history?days=N  # Aggregate with heatmap, trends
GET /api/task/{id}/files   # Task orbit files (plan, context, tasks.md)
```

### Utility APIs

```
GET /api/all       # Combined initial load data
GET /api/sync      # Trigger SQLite -> DuckDB sync
GET /health        # Health check
```

## Frontend Views

### #projects (default)
- Active projects table (clickable for modal)
- Completed projects table

### #activity
- Header stats card (time, LOC, commits, tasks)
- Today's activity with date navigation
- Hourly activity bar chart + timeline
- Activity history with heatmap and trends

## Orbit Location Detection

The `parse_orbit_progress()` function intelligently finds orbit files in the centralized location:
- Primary path: `~/.claude/orbit/{active,completed}/<task-name>/`
- Legacy fallback: repo-local `dev/{active,completed}/` paths (for older projects)

### Search Order

For a task with `full_path = "active/task-name"`:

1. Centralized active: `~/.claude/orbit/active/task-name/`
2. Centralized completed: `~/.claude/orbit/completed/task-name/`
3. Legacy repo-local: `{repo_path}/dev/active/task-name/` (fallback)
4. Legacy completed: `{repo_path}/dev/completed/task-name/` (fallback)

### File Name Fallbacks

Within a task directory, looks for files in order:
- Tasks: `{task-name}-tasks.md` -> `tasks.md`
- Context: `{task-name}-context.md` -> `context.md` -> `shared-context.md`

### Orphan Task Handling

Tasks can become "orphans" when orbit files are moved to `~/.claude/orbit/completed/` but the database `status` field isn't updated.

**Detection:** `parse_orbit_progress()` returns `orbit_in_completed: true` when orbit files are found in a completed path.

**API Behavior:**
- `/api/tasks/active` - Filters OUT tasks where `orbit_in_completed=true`
- `/api/tasks/completed` - Includes orphan tasks (DB status='active' but orbit files in completed)

This ensures the dashboard shows tasks in the correct list based on actual orbit file location, not stale DB status.

## Common Issues

### Dashboard empty after Mac restart
**Cause:** DuckDB tables missing (corruption or Python version mismatch)
**Fix:**
```bash
# Stop service
launchctl unload ~/Library/LaunchAgents/com.orbit.dashboard.plist

# Re-run migration
/opt/homebrew/bin/python3.11 migrate_to_duckdb.py

# Restart service
launchctl load ~/Library/LaunchAgents/com.orbit.dashboard.plist
```

### Wrong Python version in launchd
**Symptom:** `ModuleNotFoundError: No module named 'fastapi'`
**Fix:** Ensure plist uses `/opt/homebrew/bin/python3.11`, not `/usr/bin/python3`

### DuckDB locked error
**Cause:** Server process holding exclusive lock
**Fix:** Stop the dashboard service before running migrations

### Time tracking gaps
**Cause:** Heartbeats only sent on `UserPromptSubmit` (when user sends prompt)
**Mitigation:** Config uses 30-min idle timeout, 5-min assumed work per heartbeat

## Dependencies

```
fastapi
uvicorn
httpx
duckdb
```

Install for Python 3.11:
```bash
/opt/homebrew/bin/python3.11 -m pip install fastapi uvicorn httpx duckdb
```

## Related Files

| Location | Purpose |
|----------|---------|
| `~/.claude/tasks.db` | SQLite source database |
| `~/.claude/tasks.duckdb` | DuckDB analytics database |
| `~/Library/LaunchAgents/com.orbit.dashboard.plist` | Launchd service config |

## Code Style

- Python 3.11+ syntax (`str | None`, `list[dict]`, match statements)
- Type hints on all function signatures
- FastAPI with Pydantic models for request validation
- Single HTML file with embedded CSS/JS (no build tools)
- CSS variables for theming, dark/light toggle with localStorage persistence

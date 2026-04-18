# Dashboard

This document covers the orbit dashboard: a FastAPI backend plus a single-file HTML frontend that runs at `http://localhost:8787` and visualizes everything orbit knows about your projects, your time, and any orbit-auto runs in progress.

It assumes you have read [`architecture.md`](./architecture.md) for the shared vocabulary (dual-DB pattern, heartbeats, sessions, `claude_session_cache`, `full_path`, orbit file layout). If a term in this doc is not defined here, it is defined there.

If you are just trying to *use* the dashboard, the short version is: run `python3.11 server.py` in `orbit-dashboard/` (or have launchd do it for you), open `http://localhost:8787`, and the two screens you will spend time on are **Projects** and **Activity**. The rest of this doc is for when you want to understand what you are looking at, hit the API directly, or extend the dashboard.

## What the dashboard shows

The frontend has three top-level views, all hash-routed:

| Hash | View | What it shows |
|------|------|---------------|
| `#projects` (default) | Projects | Active and completed projects in two tables, with click-through to a per-project modal |
| `#activity` | Activity | Today's time and LOC stats, hourly timeline, and a multi-week activity history with heatmap, trends, and top projects |
| `#auto` | Auto | Live graph of active orbit-auto executions (DAG visualization, worker status, per-task state) |

There are no pages beyond these three. All data is fetched lazily on first view switch and cached in-browser for the remainder of the session (a refresh button on each card forces a re-fetch).

### Projects view

The Projects view is built from two API calls: `GET /api/tasks/active` and `GET /api/tasks/completed`. Both return a tree of parent projects with optional subtasks. The active table includes a remaining-tasks summary and a completion percentage parsed live from the project's `-tasks.md` file, so editing the checklist in another editor and refreshing the dashboard gives you immediate feedback.

Clicking a row opens a modal with four tabs:

- **Tasks** - rendered markdown of `<project>-tasks.md`, with the checklist styled as aligned checkboxes.
- **Context** - rendered markdown of `<project>-context.md`, including the "Next Steps" and "Recent Changes" sections.
- **Plan** - rendered markdown of `<project>-plan.md` if it exists, empty otherwise.
- **Structure** - a D3.js graph visualization of per-task modes (interactive vs auto) and their dependencies, with a zoomable viewport. Only meaningful if `/orbit:mode` has been run on the project.

The modal is driven by `GET /api/task/{id}/files` (for markdown content) and `GET /api/task/{id}/structure` (for the graph). Both re-parse the orbit files on the server side on every request - there is no caching of file contents, only of the DuckDB row, so edits outside the dashboard show up on the next modal open.

The active table filters out "orphan" tasks where the DB still says `status=active` but `<project>-tasks.md` has been moved to `~/.claude/orbit/completed/<project>/`. Orphans appear in the completed table instead. This is handled server-side in `parse_orbit_progress()` at `orbit-dashboard/server.py:809`, which flags `orbit_in_completed=True` when it finds the files under the completed path, and the `/api/tasks/active` handler skips those rows.

### Activity view

The Activity view is the richest part of the dashboard and the most complicated, because it merges two independent sources of time data:

1. **Orbit heartbeats** from `~/.claude/tasks.db` - these are WakaTime-style per-prompt activity pings that the `UserPromptSubmit` hook records when the current directory matches a tracked orbit project. They are aggregated into `sessions` rows by `TaskDB.process_heartbeats()`.
2. **Claude Code JSONL transcripts** from `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl` - these are the full conversation logs Claude Code writes for its own reasons (session history, resume). Every message has a timestamp, so the dashboard can reconstruct active time even when no orbit project was loaded.

The Activity view surfaces both, separately where it matters and merged where the user just wants "how much time did I spend today":

- **Header stats card** - total time (Claude JSONL-only, capped at wall-clock elapsed), task count, session count, commit count, LOC added/removed. The time figure is *not* orbit heartbeat time - it is pure JSONL time, so it reflects actual Claude Code usage whether or not a project was loaded.
- **Hourly chart** - a 24-hour bar chart of active time per hour, using `max(task_seconds, claude_seconds)` so Claude-only hours still show up even without heartbeats. Status badges on each row indicate whether the task was Active, Done, or Untracked (Claude activity with no orbit project loaded).
- **Session timeline** - a horizontal Gantt-style view of every session in the selected day, color-coded by repo. Sessions crossing midnight are clipped to the day boundary, and sessions with a `duration_seconds / wall_clock_span < 10%` ratio are filtered out as idle.
- **Activity History card** - 7×24 heatmap (day-of-week by hour), day-of-week totals, 30-day repo breakdown, top-5 projects by effort, and a period-over-period trend comparison. All of these come from a single `GET /api/stats/history?days=N` call, which is cached server-side for 5 minutes because it runs `git log` across every tracked repo to compute LOC.
- **History source toggle** - the "All / Tracked Only" switch at the top of the history card toggles between showing merged orbit+Claude time (the default) and orbit-only time. Useful when you want to see "how much did I spend on tracked projects this week" without Claude-only noise.

Date navigation at the top of the view lets you browse any past day via `GET /api/stats/day?date=YYYY-MM-DD`, which returns the same shape as `/api/stats/today`.

### Auto view

The Auto view lists active orbit-auto executions across all tracked repos and renders a D3 graph of each one's task DAG. Each task node is color-coded by state (pending, in_progress, completed, failed), dependencies are drawn as arrows, and a side panel shows per-worker status.

This view is driven by `GET /api/auto/projects` (list with per-project task graphs parsed live from each project's `-tasks.md` and `prompts/` directory) and `GET /api/auto/executions` / `GET /api/auto/executions/{task_id}` (historical execution records from the `auto_executions` SQLite table).

DAG state (task status, worker assignments, progress counts) is fetched on tab open and does not auto-refresh - reopen the tab or click the execution to get fresh state. The per-execution *log output* panel is different: once you open an execution's detail view, it subscribes to `GET /api/auto/output/{execution_id}/stream` (SSE) and new log lines appear as they are written.

## Time accounting in detail

This is the one part of the dashboard that trips people up, so it is worth a proper walkthrough.

### Two sources, one number

Every active task is reported with a `time_spent_seconds` field, computed in `/api/tasks/active` like this (see `orbit-dashboard/server.py:1128-1139`):

```python
task_ids = [t.id for t in tasks]
times = db.get_batch_task_times(task_ids, period="all")    # heartbeat-based
jsonl_times = _get_jsonl_task_times(task_ids)              # JSONL-based

for task in tasks:
    etime = _effective_time(task.id, times, jsonl_times)
    task_dict["time_spent_seconds"] = etime
    task_dict["time_spent_formatted"] = db.format_duration(etime)
```

Where:

```python
def _effective_time(task_id, heartbeat_times, jsonl_times) -> int:
    return max(heartbeat_times.get(task_id, 0), jsonl_times.get(task_id, 0))
```

The dashboard picks the **larger** of the two estimates. The rationale is that heartbeat time undercounts when the user works in a repo but outside an orbit project directory (heartbeats only fire when `find_task_for_cwd` matches), and JSONL time undercounts when multiple projects share a repo (because the join scopes by repo path, not project path). Taking the max gives you a conservative upper bound on "time you spent that could plausibly be attributed to this task."

This matters most during debugging. If you look at a task, see "5h 30m" in the dashboard, and then check the orbit heartbeat-based CLI report and see "2h 15m," the dashboard is not lying - it is telling you that Claude JSONL transcripts say you spent 5h 30m in that repo since the task was created, and 2h 15m of that was attributed to the task by orbit's heartbeats.

### Heartbeat time

Heartbeat time is the pure orbit-native answer: how much activity did `activity_tracker.py` attribute to this task. It is computed by `TaskDB.get_batch_task_times()`, which groups the `sessions` table by `task_id` and sums `duration_seconds`. It is accurate when the user loaded the project via `/orbit:new`, `/orbit:go`, or a SessionStart hook resolution, because the `projects/<session-id>.json` pointer then routes every subsequent heartbeat to the right task.

It is under-counting-prone when:

- The user worked on the project from a cwd outside `~/.claude/orbit/active/<project>/` and without loading the project at session start (covered in `architecture.md` invariants).
- The user spent time in the repo but outside the orbit directory tree (for example, editing source files in `src/`).
- The user used `/orbit:go` mid-session in a session that did not resolve a task at SessionStart, because mid-session loads do not write `projects/<session-id>.json`.

### JSONL time

JSONL time is the reconstruction from Claude Code's transcript files. Every message in a JSONL file has a timestamp; a session's "duration" is the sum of the gaps between consecutive messages, capped at 5 minutes per gap (longer gaps are treated as idle). This gives a realistic "time spent typing and waiting for Claude" figure rather than a wall-clock-span figure.

The gap-capping logic lives in `SessionMetrics.active_seconds_for_date()` at `orbit-dashboard/lib/jsonl_parser.py:77`:

```python
max_gap_seconds = 5 * 60  # 5 minutes
active_seconds = 0
for i in range(1, len(sorted_ts)):
    gap = (sorted_ts[i] - sorted_ts[i - 1]).total_seconds()
    if gap <= max_gap_seconds:
        active_seconds += gap
return int(active_seconds)
```

JSONL sessions are parsed lazily and cached in the `claude_session_cache` table in `~/.claude/tasks.db` (not in DuckDB). The cache key is `(session_id, file_mtime)`, so when a new message is appended to an existing JSONL file the mtime changes and the dashboard reparses; unchanged files hit the cache and cost nothing.

### Joining JSONL time to tasks

The tricky bit is attributing cached JSONL sessions back to orbit tasks, because JSONL files are keyed by `cwd`, not by task. The join is at `_get_jsonl_task_times()` at `orbit-dashboard/server.py:1082`:

```sql
SELECT t.id, SUM(c.duration_seconds) as total
FROM tasks t
JOIN repositories r ON t.repo_id = r.id
JOIN claude_session_cache c ON c.cwd = r.path
WHERE t.id IN (?, ?, ...)
  AND c.duration_seconds > 0
  AND c.date >= DATE(t.created_at)
GROUP BY t.id
```

This does two things worth noting:

1. **Join by repo path, not project path.** A task's JSONL time pool is every session whose `cwd` equals the repo path. This is the broadest possible attribution, which is intentional for tasks without orbit-specific directories, but it has a known cost (below).
2. **Lower-bounded by task creation date.** `c.date >= DATE(t.created_at)` means sessions from before the task was created are not counted. This is the only disambiguator when multiple tasks share a repo.

**Known limitation:** overlapping tasks on the same repo will double-count each other's JSONL time. If you create task A on Monday, finish it Tuesday, create task B on Wednesday, then both tasks will report every Wednesday-or-later JSONL session as their own time. This is mentioned in `architecture.md` as one of the invariants/gotchas to know about; the fix requires per-task cwd disambiguation (which would require the orbit project to have its own directory that JSONL sessions run out of), and that is a larger change than the dashboard currently takes on. If you are debugging suspicious double-counting, this is almost always why.

### Caps and overrides

One more subtlety: the `/api/stats/today` and `/api/stats/day` endpoints cap the reported `claude_seconds` at wall-clock elapsed time for the day (see `orbit-dashboard/server.py:1550-1561`). This handles the case where multiple Claude sessions run in parallel and the naive sum of their per-session durations exceeds the actual elapsed time. For the current day, the cap is `(now - midnight).total_seconds()`; for past days, it is a hard 24 hours.

The `/api/stats/history` endpoint does a similar merged-time override at `orbit-dashboard/server.py:1841-1843`: the `trends.time.current` field is bumped to `max(trends, merged_total)` so the history trend number never undercounts when JSONL time exceeds orbit-tracked time.

## API reference

Every dashboard endpoint lives in `orbit-dashboard/server.py`. Request bodies are JSON for all POST endpoints, and responses are JSON unless otherwise noted. There is no authentication - the server binds to `127.0.0.1:8787` only.

### Projects and tasks

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/tasks/active` | Active tasks with orbit progress, effective time, and subtasks. Filters out orphans. |
| GET | `/api/tasks/completed` | Completed tasks plus orphans. Accepts `days` query param (default 30). |
| GET | `/api/task/{id}/files` | Parsed markdown content of `-plan.md`, `-context.md`, `-tasks.md`. |
| GET | `/api/task/{id}/structure` | Per-task mode assignments (interactive/auto) + dependency adjacency for the Structure tab graph. |
| GET | `/api/task/{id}/updates` | Append-only `task_updates` rows for non-coding tasks. |
| GET | `/api/task/{id}/prompt/{subtask_id}` | Contents of a specific `prompts/task-NN-prompt.md` file. |
| GET | `/api/repos` | Tracked repositories with their metadata. |

The `/api/tasks/active` response shape is the largest; the relevant fields are:

```json
{
  "tasks": [
    {
      "id": 1,
      "name": "orbit-public-release",
      "status": "active",
      "repo_name": "claude-orbit",
      "repo_path": "/Users/alice/projects/claude-orbit",
      "jira_key": null,
      "time_spent_seconds": 480,
      "time_spent_formatted": "8m",
      "last_worked_ago": "1m ago",
      "description": "Prepare orbit for public open-source release",
      "remaining_summary": "...",
      "completion_pct": 48,
      "completed_count": 19,
      "total_count": 39,
      "project_mode": "interactive",
      "task_modes": [...],
      "auto_count": 0,
      "inter_count": 39,
      "subtasks": [],
      "subtask_count": 0,
      "combined_time_seconds": 480,
      "combined_time_formatted": "8m"
    }
  ],
  "count": 1,
  "total_with_subtasks": 1,
  "timestamp": "2026-04-13T01:52:00"
}
```

`completion_pct`, `completed_count`, `total_count`, `remaining_summary`, `project_mode`, and `task_modes` all come from parsing `<project>-tasks.md` live on the server side. The other fields come from DuckDB.

### Activity and stats

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/stats/today` | Header stats, hourly chart, timeline, repo breakdown, LOC, tasks for current day. |
| GET | `/api/stats/day?date=YYYY-MM-DD` | Same shape as `/api/stats/today` but for an arbitrary day. |
| GET | `/api/stats/history?days=N` | 7×24 heatmap, day-of-week totals, repo breakdown, top tasks, trend comparison. Cached 5 min. |

`/api/stats/today` is the main data source for the Activity view. Its response merges:

- `db.get_sessions_from_sqlite(today)` - live orbit sessions from SQLite (not DuckDB, for freshness).
- `db.get_hourly_activity_from_sqlite(today)` - hourly task-seconds rollup.
- `db.get_tasks_today_from_sqlite(today)` - tasks with activity today.
- `get_claude_hourly_activity(today)` - hourly JSONL activity from the `claude_session_cache`.
- `get_loc_for_date(today)` - git LOC stats across tracked repos for the day.
- `merge_hourly_activity(task_hourly, claude_hourly)` - the two sources joined into one 24-element array.
- `_merge_untracked_sessions(...)` - untracked Claude sessions (anti-joined against the `sessions` table) grouped by cwd and appended to the task list.

`_merge_untracked_sessions` is what makes "Claude Code activity with no orbit project loaded" visible in the dashboard. The anti-join query lives in `ClaudeSessionCache.get_untracked_sessions(date)` at `orbit-dashboard/lib/analytics_db.py:3165` and returns JSONL sessions that do not appear in any orbit `sessions` row for the same `session_id`. Because the anti-join relies on both tables living in the same SQLite file, the invariant in `architecture.md` about not moving `claude_session_cache` to DuckDB matters here.

### Orbit-auto execution tracking

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/auto/projects` | Active orbit projects with their task graphs, parsed live from each project's `-tasks.md` and `prompts/` directory. Drives the Auto view overview. |
| GET | `/api/auto/executions` | All `auto_executions` rows across all projects, with optional `running_only` and `limit` filters. |
| GET | `/api/auto/executions/{task_id}` | Execution history for a specific task. |
| GET | `/api/auto/output/{execution_id}` | Full output log for an execution, read from `auto_execution_logs` in SQLite. |
| GET | `/api/auto/output/{execution_id}/stream` | SSE stream of log lines for a running execution. Sends log/status/heartbeat events. |

The orbit-auto endpoints read from the `auto_executions` and `auto_execution_logs` tables in SQLite (via `OrbitTaskDB`, the SQLite-only wrapper), which contain the running and historical execution records. The live DAG graph rendered in the Auto view comes from parsing each project's markdown (`/api/auto/projects`) rather than from a worker-maintained state file, so there is no separate "live state" source besides what the worker writes back into `auto_execution_logs`.

### Hook receivers

Orbit hooks can talk to the dashboard via `/api/hooks/*` endpoints. Three callers use these:

- The orbit plugin's own slash commands (`/orbit:new`, `/orbit:go`, `/orbit:save`) POST to `/api/hooks/project` to register active projects, and resolve terminal session IDs via `/api/hooks/term-session/...`.
- The orbit MCP server fires `/api/hooks/task-created` internally whenever a task-creation tool runs, so the dashboard syncs immediately instead of waiting up to 60s.
- `setup.sh` wires `/api/hooks/edit-count` into `~/.claude/settings.json` as a `PostToolUse` HTTP hook with matcher `Edit|Write|NotebookEdit`. That is what populates the statusline edit counter.

`heartbeat` is exposed but not wired by the plugin - power users can optionally POST to it from their own `UserPromptSubmit` HTTP hook if they want a second redundant heartbeat path; the plugin already records heartbeats via its in-process subprocess hook, so this is a duplicate and most users should skip it.

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/hooks/heartbeat` | Record an activity heartbeat. Optional - power-user `UserPromptSubmit` HTTP hook wiring only; the plugin already records heartbeats via its subprocess hook. |
| POST | `/api/hooks/edit-count` | Increment per-session edit count. Wired by `setup.sh` as a `PostToolUse` HTTP hook (matcher `Edit\|Write\|NotebookEdit`). Feeds the statusline edit counter. |
| POST | `/api/hooks/task-created` | Trigger an immediate SQLite → DuckDB sync. Called internally by the orbit MCP server after `create_task` and `create_orbit_files`. |
| POST | `/api/hooks/action` | Record current tool action for tab title display. |
| POST | `/api/hooks/project` | Set the active project for a session. Called by `/orbit:new`, `/orbit:go`, `/orbit:save`. |
| POST | `/api/hooks/qa-review` | Mark QA review as suggested for a session. |
| GET | `/api/hooks/term-session/{term_session_id}` | Resolve a terminal-emulator session ID to a Claude session ID. |
| GET | `/api/hooks/session/{session_id}` | Read session state row (edit count, action, last prompt time, etc.). |

All of these write to (or read from) `~/.claude/hooks-state.db`, a separate SQLite file from `tasks.db`. That file holds ephemeral per-session state that has a shorter lifetime than task tracking: `session_state`, `project_state`, `term_sessions`, `validation_state`, `guard_warned`. The statusline reads from it, the hooks write to it, and neither interacts with the main orbit task database.

The fallback flow for writing project state is instructive: `/orbit:go` first tries to POST to `/api/hooks/project` (which handles JSON escaping correctly), and if the dashboard is down it falls back to a direct SQLite write against `hooks-state.db`. See the `orbit.md` rule file in `rules/` for the exact bash snippet. This is the only case where orbit writes to `hooks-state.db` from outside the dashboard process.

### Sync and health

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Health check. Returns DuckDB path and existence. |
| POST | `/api/sync` | Trigger SQLite → DuckDB sync and return result counts. |
| GET | `/api/stream` | SSE stream of today's stats, updated every 30 seconds. |

The SSE stream is not heavily used by the frontend - the UI uses lazy per-view fetches and manual refresh buttons - but it is useful for third-party integrations that want a "what is happening now" feed without polling.

## The dual-database sync

The sync from SQLite to DuckDB is the mechanism that lets the dashboard read fast without locking out writes. The sync itself is simple: it reads all rows from the orbit-db tables in `~/.claude/tasks.db` and upserts them into `~/.claude/tasks.duckdb`. There is no incremental log; every sync is a full-table copy, though only changed rows end up actually writing.

Sync runs in four places:

1. **On startup** - `lifespan()` at `orbit-dashboard/server.py:158` calls `db.sync_from_sqlite()` before taking traffic. This ensures the dashboard shows current data immediately, even after a long downtime.
2. **Every 60 seconds** - the `background_sync()` async task runs on a `SYNC_INTERVAL_SECONDS=60` loop. This is why the dashboard always lags heartbeats by at most a minute.
3. **On demand via `POST /api/sync`** - useful for the "I just committed, show me the latest" case where you do not want to wait for the background loop.
4. **After task creation** - the orbit MCP server POSTs to `/api/hooks/task-created` after `create_task` and `create_orbit_files` so a newly created task shows up in the active table right away instead of waiting up to 60s for the next background sync.

The `migrate_to_duckdb.py` script is a standalone version of the same logic; you run it manually after a DuckDB corruption or when you want to rebuild the analytics DB from scratch.

### Why DuckDB is not the source of truth

A reasonable question: if DuckDB is 10-100x faster than SQLite for aggregates, why not write directly to DuckDB?

Three reasons:

1. **Write concurrency.** Multiple orbit components (hooks, MCP server, orbit-auto) write to the task database concurrently. SQLite handles this well with its standard locking. DuckDB is much more restrictive about concurrent writers and does not cope well with orbit's write pattern.
2. **Crash safety.** SQLite with WAL mode survives process crashes cleanly and has been battle-tested for decades. A corrupted DuckDB file means losing recent data; a corrupted SQLite file is rare and recoverable.
3. **Schema flexibility.** Triggers, WAL pragmas, and the `claude_session_cache` table are all SQLite features. Moving them to DuckDB would require rewriting the orbit-db layer.

The trade-off is the ~60-second staleness window on the dashboard. For analytics, that is fine. For live trading or safety-critical applications, you would pick differently.

## Deep linking and statusline integration

The dashboard supports hash-based deep links so external tools (the statusline, other dashboards, notes) can point at specific views and task modals:

- `#projects` or empty hash - Projects view.
- `#activity` - Activity view.
- `#auto` - Auto view.
- `#projects?task=<name>&tab=<tab>` - Projects view with a specific task modal opened. `tab` can be `tasks` (default), `context`, `plan`, or `structure`.

The routing is handled by `handleHashChange()` at `orbit-dashboard/index.html:4997`. Deep links resolve against both `/api/tasks/active` and `/api/tasks/completed?days=90`, so you can link to completed projects too. After opening the modal, the query string is stripped from the hash so that a page refresh does not re-open the modal unexpectedly.

The statusline uses this for its clickable project name and progress fraction. When orbit-statusline renders a line like `Project: orbit-public-release (19/39)`, the project name is wrapped in an OSC 8 hyperlink to `#{ORBIT_DASHBOARD_URL}/#projects` and the progress fraction is wrapped in a link to `#{ORBIT_DASHBOARD_URL}/#projects?task=orbit-public-release&tab=tasks`. Terminals that support OSC 8 (iTerm2, Ghostty, cmux, modern Windows Terminal) render these as clickable; terminals that do not, just see plain text with the same content.

The `ORBIT_DASHBOARD_URL` environment variable is read at `statusline/statusline.py:951` and defaults to `http://localhost:8787`. If you move the dashboard to a different host or port, set this in your shell init so the statusline builds the right links.

## Customization

The dashboard is deliberately minimal about configuration - there are very few knobs because most things can be overridden by editing the 4,000-line `server.py` directly. The knobs that do exist:

| What | Where | How to change |
|------|-------|---------------|
| Listen port | `server.py:2664` (`uvicorn.run`) | Change `port=8787` in the `if __name__ == "__main__":` block |
| SQLite path | `orbit-dashboard/lib/analytics_db.py:29` (`SQLITE_PATH`) | Edit the constant |
| DuckDB path | `orbit-dashboard/lib/analytics_db.py:28` (`DUCKDB_PATH`) | Edit the constant |
| Sync interval | `server.py:124` (`SYNC_INTERVAL_SECONDS`) | Default 60s |
| History cache TTL | `server.py:128` (`HISTORY_CACHE_TTL_SECONDS`) | Default 300s |
| SSE refresh rate | `server.py:205` (`REFRESH_INTERVAL`) | Default 30s |
| JIRA URL mapping | `server.py:208` (`JIRA_URLS`) | Add `"PREFIX-": "https://your-jira/browse/"` |
| Statusline link base | `ORBIT_DASHBOARD_URL` env var | Set in shell init |
| Dark/light theme | Frontend only | Toggle in the top-right of the UI (persisted in localStorage) |

The JIRA URL mapping is empty by default. The dashboard turns any `jira_key` field into a clickable link only when the prefix matches an entry in `JIRA_URLS`, so until you populate it your task badges will show the JIRA key as plain text. Add an entry per prefix you use (e.g. `"PROJ-": "https://your-jira.example.com/browse/"`) to wire up your own JIRA. A user-facing settings screen for managing this mapping at runtime is planned (see the `dashboard-settings-screen` follow-up task).

### launchd deployment (macOS)

The common way to run the dashboard in the background on macOS is via launchd. The plist lives at `~/Library/LaunchAgents/com.orbit.dashboard.plist` and `setup.sh` can generate one for you. The minimum you need is:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.orbit.dashboard</string>
    <key>ProgramArguments</key>
    <array>
        <string>/opt/homebrew/bin/python3.11</string>
        <string>/Users/YOU/work/personal/claude-orbit/orbit-dashboard/server.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/orbit-dashboard.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/orbit-dashboard.stderr.log</string>
</dict>
</plist>
```

Load it with `launchctl load ~/Library/LaunchAgents/com.orbit.dashboard.plist`; unload with the matching `unload` command. The `KeepAlive` key ensures the dashboard restarts automatically after a crash.

**Python version matters.** DuckDB is distributed as a compiled wheel that is Python-version-specific. Orbit is known to work with Python 3.11; if your launchd plist uses `/usr/bin/python3` (system Python, usually 3.9 on macOS) you will get `ModuleNotFoundError: No module named 'duckdb'` even after installing DuckDB, because you installed it under a different interpreter. Use the exact path `/opt/homebrew/bin/python3.11` in the plist, and install dashboard dependencies with `/opt/homebrew/bin/python3.11 -m pip install -r orbit-dashboard/requirements.txt`.

### Running without launchd

`python3.11 orbit-dashboard/server.py` works fine as a foreground process. You just lose auto-restart on crash, and you have to remember to start it again after rebooting. If you use tmux or a similar session manager, a dedicated pane for the dashboard is a reasonable middle ground.

## Extending the dashboard

### Adding a new endpoint

The rule is simple: **reads go to DuckDB via `analytics_db.py`, writes go to SQLite via `orbit-db`**. If you are adding a new aggregate query to the Activity view, you add a method to the `AnalyticsDB` class and call it from a new `@app.get()` handler. If you are adding a new mutation, you call into `OrbitTaskDB` (imported at `server.py:50`) and then trigger a sync.

The pattern for a new read endpoint:

```python
@app.get("/api/my-new-view")
async def api_my_new_view(days: int = 7):
    db = get_db()  # AnalyticsDB singleton
    result = db.my_new_aggregate_method(days=days)
    return {
        "result": result,
        "timestamp": datetime.now().isoformat(),
    }
```

The pattern for a new write endpoint:

```python
@app.post("/api/my-new-mutation")
async def api_my_new_mutation(body: dict):
    sqlite_db = get_sqlite_db()  # OrbitTaskDB singleton
    sqlite_db.my_new_write_method(body)
    # Trigger sync so the read-side reflects the change
    get_db().sync_from_sqlite()
    return {"success": True}
```

Do not open either database directly from inside a route handler. The singletons exist for a reason - they manage connections, WAL mode, and thread safety.

### Adding a new frontend view

The frontend is a single HTML file (`index.html`) with embedded CSS and JavaScript. There is no build tool, no framework, and no bundler. To add a view:

1. Add a nav item in the `<nav>` block (around `orbit-dashboard/index.html:4194`).
2. Add a `<div class="view" id="myNewView">...</div>` section below the existing views.
3. Add a lazy loader function (`loadMyNewData`) and hook it into `switchView()` at around `orbit-dashboard/index.html:5064`.
4. Write any render functions your view needs (convention: `renderMyNewThing(data)`).

The CSS variable system at the top of `index.html` drives theming. Use `var(--bg)`, `var(--fg)`, `var(--accent)`, etc. and both the light and dark modes will Just Work.

For anything more complex than a table or a bar chart, you may want to pull in D3.js the same way the Structure tab and Auto view do - it is already loaded from a CDN at the top of the file.

### Touching path resolution

If you are changing how orbit files are located on disk, you need to update two places: `parse_orbit_progress()` in `orbit-dashboard/server.py:809` (dashboard read path) and `helpers.py` in the MCP server (MCP write path). They are independent implementations of the same logic, so keep them consistent or the dashboard will render stale state.

## Troubleshooting

### "Dashboard empty after Mac restart"

**Symptom:** All tables are empty, no sessions, no stats, but orbit-db reports normal data via the CLI.

**Cause:** DuckDB analytics tables are missing or corrupt. This happens occasionally when macOS force-kills processes during shutdown.

**Fix:**

```bash
launchctl unload ~/Library/LaunchAgents/com.orbit.dashboard.plist
/opt/homebrew/bin/python3.11 orbit-dashboard/migrate_to_duckdb.py
launchctl load ~/Library/LaunchAgents/com.orbit.dashboard.plist
```

The migration script rebuilds `tasks.duckdb` from `tasks.db`. It is idempotent, safe to run anytime, and takes under a second on a typical install.

### "ModuleNotFoundError: No module named 'fastapi' / 'duckdb'"

**Cause:** launchd is using a different Python than the one you installed dependencies into.

**Fix:** Edit `~/Library/LaunchAgents/com.orbit.dashboard.plist` to point `ProgramArguments` at `/opt/homebrew/bin/python3.11` explicitly, and install the dashboard requirements under that exact Python: `/opt/homebrew/bin/python3.11 -m pip install -r orbit-dashboard/requirements.txt`. `which python3.11` should agree with the plist path.

### "DuckDB locked" errors in the logs

**Cause:** Two processes are holding the DuckDB file open at the same time, usually because you ran `migrate_to_duckdb.py` without stopping the server first, or you have two dashboard processes running.

**Fix:** `launchctl unload` the dashboard, verify `ps aux | grep orbit-dashboard` shows nothing, and then run the migration or restart.

### "Task time on the dashboard does not match the CLI"

This is almost always the heartbeat-vs-JSONL merge at work. See [Time accounting in detail](#time-accounting-in-detail). In summary: the dashboard shows `max(heartbeat_time, jsonl_time)`, so the dashboard number will be equal to or larger than the CLI number. If it is much larger, the likely cause is overlapping tasks on the same repo double-counting JSONL sessions - check whether another task was created around the same time in the same repo.

### "Untracked sessions not showing up"

**Symptom:** You worked in Claude Code outside any orbit project, but the Activity view does not show any untracked sessions.

**Cause:** Most likely the `claude_session_cache` is missing or got moved to a different database file. The anti-join that identifies untracked sessions requires `claude_session_cache` and `sessions` to live in the same SQLite file.

**Fix:** Check `sqlite3 ~/.claude/tasks.db ".tables"` - you should see both `claude_session_cache` and `sessions`. If the cache is missing, restart the dashboard (it is created on first access by `ClaudeSessionCache._ensure_table()` at `orbit-dashboard/lib/analytics_db.py:2947`). If it is present but queries return nothing, run `POST /api/sync` to force a rebuild.

### "Dashboard shows stale data"

**Cause:** The 60-second background sync has not run yet, or the History API is serving a cached response (5-minute TTL).

**Fix:** Hit `POST /api/sync` to force an immediate sync. For the History card specifically, the cache is keyed by the `days` parameter, so clicking a different preset (7/14/30) fetches fresh data.

### "Deep link opens the Projects view but not the modal"

**Cause:** The task name in the URL does not exactly match a task in `/api/tasks/active` or `/api/tasks/completed?days=90`. Task-name matching is exact, case-sensitive, and does not fuzzy-match.

**Fix:** Use `GET /api/tasks/active` to verify the exact task name, and reconstruct the URL as `#projects?task=<exact-name>&tab=tasks`. For completed projects older than 90 days, you can bump the window by calling `/api/tasks/completed?days=365` and linking to that name - the deep-link resolver will find it there too.

## Where to go from here

- [`architecture.md`](./architecture.md) - if you need a refresher on the dual-DB pattern or the orbit-db invariants this doc assumes.
- `orbit-dashboard/server.py` - the source. It is long but flat; grep for any endpoint path or helper name and you will find it.
- `orbit-dashboard/lib/analytics_db.py` - if you are touching aggregate queries or adding a new one.
- `orbit-dashboard/lib/jsonl_parser.py` - if you are debugging JSONL time estimation, especially the gap-capping logic.

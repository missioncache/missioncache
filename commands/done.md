---
description: "Mark an active project as completed and archive files"
argument-hint: "[project-name]"
---

# Complete Project

Mark a project as completed and optionally move orbit files to the completed folder.

## Quick Start

1. **If project name provided:**
   ```
   mcp__plugin_orbit_pm__complete_task(project_name="<name>", move_files=true)
   ```

2. **If no project name, list active projects:**
   ```
   mcp__plugin_orbit_pm__list_active_tasks()
   ```
   Then ask user to select one.

## Workflow

### Step 1: Confirm Project

If project name not provided, list active projects and ask user to select.

### Step 2: Show Summary

Before completing, show the user:
- Total time invested
- Progress (should be 100%)
- What will happen (files moved, status changed)

### Step 3: Complete

Call `mcp__plugin_orbit_pm__complete_task` which:
1. Updates project status to "completed" in database
2. Moves files from `~/.claude/orbit/active/<name>/` to `~/.claude/orbit/completed/<name>/`
3. Records completion timestamp

### Step 4: Process Time Tracking

Call `mcp__plugin_orbit_pm__process_heartbeats()` to finalize time tracking.

### Step 5: Clear Statusline

Remove the project pointer so the statusline stops showing the completed project name. Mirrors the resolver in `/orbit:new` / `/orbit:go` (filesystem primary, term-env fallback) and uses direct SQL because the dashboard has no DELETE endpoint for project_state. Silently no-ops on quick-install setups without `hooks-state.db`.

```bash
# Primary: most-recently-modified transcript in ~/.claude/projects/<sanitized-cwd>/ = current session.
CWD_KEY=$(pwd | sed 's|/|-|g')
SESSION_ID=$(ls -t "$HOME/.claude/projects/${CWD_KEY}"/*.jsonl 2>/dev/null | head -1 | xargs -I{} basename {} .jsonl)

# Fallback: legacy terminal-env-var lookup (iTerm2, Windows Terminal only).
if [ -z "$SESSION_ID" ]; then
  TERM_KEY="${TERM_SESSION_ID:-$WT_SESSION}"
  if [ -n "$TERM_KEY" ]; then
    SESSION_ID=$(curl -s "http://localhost:8787/api/hooks/term-session/${TERM_KEY}" --connect-timeout 1 --max-time 2 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null)
    [ -z "$SESSION_ID" ] && SESSION_ID=$(TERM_KEY="$TERM_KEY" python3 -c '
import os, sqlite3
conn = sqlite3.connect(os.path.expanduser("~/.claude/hooks-state.db"))
row = conn.execute("SELECT session_id FROM term_sessions WHERE term_session_id = ?", (os.environ["TERM_KEY"],)).fetchone()
print(row[0] if row else "")
' 2>/dev/null)
  fi
fi

# Delete project_state row. String concatenation, not f-strings, because
# Python <=3.11 rejects backslashes inside f-string expressions and the
# outer single-quoted bash heredoc forces escaped double quotes inside.
if [ -n "$SESSION_ID" ]; then
  SESSION_ID="$SESSION_ID" python3 -c '
import os, sqlite3
sid = os.environ["SESSION_ID"]
conn = sqlite3.connect(os.path.expanduser("~/.claude/hooks-state.db"))
cur = conn.execute("DELETE FROM project_state WHERE session_id = ?", (sid,))
conn.commit()
print("Cleared project_state for session " + sid + " (rows: " + str(cur.rowcount) + ")")
' 2>/dev/null

  # Also delete the per-session project pointer written by /orbit:go and /orbit:new.
  # Read by find_task_for_cwd (orbit-db); leaving it in place would make /orbit:save
  # still find this task after completion.
  rm -f "$HOME/.claude/hooks/state/projects/${SESSION_ID}.json" 2>/dev/null
fi

# Clean up the vestigial activity-tracking pointer.
rm -f ~/.claude/hooks/state/pending-task.json 2>/dev/null
```

### Step 6: Share Dashboard Link (if running)

Probe the dashboard and, if reachable, include a deep link in the completion summary so the user can jump straight to the archived project view. Skip silently if the dashboard is not installed or not running - dead links train users to ignore the dashboard entirely.

Replace `<project-name>` with the kebab-case project name, then run:

```bash
PROJECT_NAME='<project-name>'
DASHBOARD_URL="${ORBIT_DASHBOARD_URL:-http://localhost:8787}"
if curl -sf -o /dev/null --max-time 1 "${DASHBOARD_URL}/health" 2>/dev/null; then
  echo "Dashboard: ${DASHBOARD_URL}/#projects?task=$PROJECT_NAME"
fi
```

If the probe succeeds, include the emitted URL as a "Dashboard" line in the completion summary shown below. If it emits nothing, omit the line entirely.

## Example Output

```
## Completing Project: kafka-consumer-fix

**Time Invested:** 4h 30m
**Progress:** 8/8 tasks (100%)
**Status:** active -> completed

Moving files:
  ~/.claude/orbit/active/kafka-consumer-fix/ -> ~/.claude/orbit/completed/kafka-consumer-fix/

Project completed successfully!

Summary:
- Total time: 4h 30m
- Sessions: 12
- Completed at: 2026-01-20 15:30
- Dashboard: http://localhost:8787/#projects?task=kafka-consumer-fix
```

## Options

- `move_files=true` (default): Move orbit files to completed/
- `move_files=false`: Keep files in active/ (useful for reference)

## MCP Tools Used

| Tool | Purpose |
|------|---------|
| `mcp__plugin_orbit_pm__list_active_tasks` | List projects if none specified |
| `mcp__plugin_orbit_pm__get_task` | Get project details for summary |
| `mcp__plugin_orbit_pm__complete_task` | Mark complete and move files |
| `mcp__plugin_orbit_pm__process_heartbeats` | Finalize time tracking |

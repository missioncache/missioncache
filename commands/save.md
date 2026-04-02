---
description: "Save progress on an active project before compaction or session end"
argument-hint: ""
---

# Update Project

Save progress on an active project using atomic MCP calls.

## Quick Start

1. **Find active project:**
   ```
   mcp__plugin_orbit_pm__find_task_for_directory(directory="<cwd>")
   ```

1b. **If not found, try detecting from orbit files and register session:**
   ```
   mcp__plugin_orbit_pm__get_orbit_files(task_name="<name>")
   # If found, create pending-task.json and record heartbeat
   ```

2. **Update context file:**
   ```
   mcp__plugin_orbit_pm__update_context_file(
     context_file="<path>",
     next_steps=["...", "..."],
     recent_changes=["...", "..."]
   )
   ```

3. **Update tasks file (if tasks completed):**
   ```
   mcp__plugin_orbit_pm__update_tasks_file(
     tasks_file="<path>",
     completed_tasks=["task description"],
     remaining_summary="what's left"
   )
   ```

4. **Process heartbeats:**
   ```
   mcp__plugin_orbit_pm__process_heartbeats()
   ```

## Workflow

### Step 1: Find Current Project

Call `mcp__plugin_orbit_pm__find_task_for_directory(directory="<cwd>")` to detect the active project.

**If project not found but orbit files exist:** Sometimes the session isn't registered (no `pending-task.json`) but the project exists. In this case:

1. Try to detect the project from `~/.claude/orbit/active/<project-name>`
2. Call `mcp__plugin_orbit_pm__get_orbit_files(task_name="<name>")` to confirm
3. If found, **register the session** (see Step 1b)

### Step 1b: Register Session (if not registered)

If `find_task_for_directory` returned `found: false` but `get_orbit_files` found the project:

```bash
echo '{"projectName": "<project-name>", "cwd": "<repo-path>", "timestamp": "'$(date -u +%Y-%m-%dT%H:%M:%SZ)'"}' > ~/.claude/hooks/state/pending-task.json && SESSION_ID=$(curl -s "http://localhost:8787/api/hooks/term-session/${TERM_SESSION_ID:-$WT_SESSION}" --connect-timeout 1 --max-time 2 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null) && [ -z "$SESSION_ID" ] && SESSION_ID=$(sqlite3 ~/.claude/hooks-state.db "SELECT session_id FROM term_sessions WHERE term_session_id = '${TERM_SESSION_ID:-$WT_SESSION}'" 2>/dev/null); [ -n "$SESSION_ID" ] && curl -s -X POST http://localhost:8787/api/hooks/project -H "Content-Type: application/json" -d "{\"session_id\":\"$SESSION_ID\",\"project_name\":\"<project-name>\"}" --connect-timeout 1 --max-time 2 >/dev/null 2>&1 && echo "done" || echo "done"
```

Then record initial heartbeat:
```
mcp__plugin_orbit_pm__record_heartbeat(task_id=<id>, directory="<cwd>")
```

This ensures activity tracking and statusline display work for the rest of the session.

### Step 2: Gather Updates

Ask the user or infer from conversation:
- What was accomplished this session?
- What are the next steps?
- Any key decisions made?
- Any gotchas discovered?

### Step 3: Update Files Atomically

Use the MCP tools to update files in one call each (much faster than multiple Read/Edit cycles):

**Context file:**
```
mcp__plugin_orbit_pm__update_context_file(
  context_file="<path>",
  next_steps=["First thing to do", "Second thing"],
  recent_changes=["Added retry logic", "Fixed config parsing"],
  key_decisions=["Using exponential backoff"],
  gotchas=["Config path must be absolute"]
)
```

**Tasks file (if tasks completed):**
```
mcp__plugin_orbit_pm__update_tasks_file(
  tasks_file="<path>",
  completed_tasks=["Add retry logic to consumer"],
  remaining_summary="Add tests, update docs"
)
```

### Step 4: Finalize Time Tracking

Call `mcp__plugin_orbit_pm__process_heartbeats()` to aggregate time.

## Example Output

```
## Updated: kafka-consumer-fix

**Context file:** Updated
  - Added 2 next steps
  - Added 3 recent changes
  - Timestamp: 2026-01-20 15:30

**Tasks file:** Updated
  - Marked 2 tasks complete
  - Progress: 5/8 (62%)
  - Remaining: Add tests, update docs

**Time tracking:** Processed 15 heartbeats

Ready to continue or safe to compact.
```

## When to Use

- Before running `/compact`
- Before ending a session
- After completing a significant milestone
- When the PreCompact hook fires (automatic)

## MCP Tools Used

| Tool | Purpose |
|------|---------|
| `mcp__plugin_orbit_pm__find_task_for_directory` | Find current project |
| `mcp__plugin_orbit_pm__get_orbit_files` | Get file paths |
| `mcp__plugin_orbit_pm__update_context_file` | Update context atomically |
| `mcp__plugin_orbit_pm__update_tasks_file` | Update tasks atomically |
| `mcp__plugin_orbit_pm__process_heartbeats` | Finalize time tracking |

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
   mcp__plugin_orbit_pm__get_orbit_files(project_name="<name>")
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

First resolve the current Claude session id so `find_task_for_directory` can use the per-session project pointer written by `/orbit:go` and `/orbit:new`. Without this, the lookup can only match when cwd is under `~/.claude/orbit/active/<task>/`, which fails from the repo root.

```bash
CWD_KEY=$(pwd | sed 's|/|-|g')
DIR="$HOME/.claude/projects/${CWD_KEY}"
POINTER_FILE="$HOME/.claude/hooks/state/cwd-session/${CWD_KEY}.json"

# Primary: SessionStart hook writes the authoritative current-session pointer.
SESSION_ID=""
if [ -r "$POINTER_FILE" ]; then
  SESSION_ID=$(python3 -c "import json,sys; print(json.load(sys.stdin)['sessionId'])" < "$POINTER_FILE" 2>/dev/null)
fi
# Fallback: transcript mtime (covers sessions that started before this pointer landed).
[ -z "$SESSION_ID" ] && SESSION_ID=$(ls -t "$DIR"/*.jsonl 2>/dev/null | head -1 | xargs -I{} basename {} .jsonl)

# Safety check: count recently-active transcripts. If >1, concurrent sessions
# share this cwd and even the pointer may be wrong (last-writer-wins race).
RECENT=$(find "$DIR" -maxdepth 1 -name "*.jsonl" -mmin -10 2>/dev/null | wc -l | tr -d ' ')
echo "SESSION_ID=$SESSION_ID RECENT=$RECENT"
```

**Ambiguity check:** If `RECENT > 1`, multiple Claude sessions have been active in this cwd within the last 10 minutes. Under concurrency the pointer or mtime may not reflect the current invocation, and `/orbit:save` could silently bind to the wrong project. **Do NOT proceed with the resolved SESSION_ID directly.** Instead:

1. Enumerate each recent `*.jsonl` in `$DIR` and look up its `~/.claude/hooks/state/projects/<sid>.json` (if it exists) to get the `projectName`.
2. Deduplicate by project name.
3. For each distinct project, call `mcp__plugin_orbit_pm__get_task(project_name=...)` to confirm it's still active.
4. Ask the user which project they intend to save and wait for their reply. Show one option per distinct project, using `<project name>` as the label and `last-worked <ago>` as the description. If your tool supports a structured option picker (Claude Code's `AskUserQuestion`), use it; otherwise present the options as a numbered prose list.
5. Use the selected project name to drive the save directly via `mcp__plugin_orbit_pm__get_orbit_files(project_name=...)` - skip the session_id-based lookup entirely.

If `RECENT <= 1`, proceed normally: call `mcp__plugin_orbit_pm__find_task_for_directory(directory="<cwd>", session_id="<SESSION_ID>")` to detect the active project. If `$SESSION_ID` is empty (extremely rare - means no Claude transcript for this cwd), omit the arg and rely on cwd-pattern matching.

**If project not found but orbit files exist:** Sometimes the session isn't registered (no `projects/<session-id>.json`) but the project exists. In this case:

1. Try to detect the project from `~/.claude/orbit/active/<project-name>`
2. Call `mcp__plugin_orbit_pm__get_orbit_files(project_name="<name>")` to confirm
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

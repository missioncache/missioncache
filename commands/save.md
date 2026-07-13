---
description: "Save progress on an active project before compaction or session end"
argument-hint: ""
---

# Update Project

Save progress on an active project using atomic MCP calls.

## Quick Start

1. **Find active project:**
   ```
   mcp__plugin_missioncache_pm__find_task_for_directory(directory="<cwd>")
   ```

1b. **If not found, try detecting from MissionCache files and register session:**
   ```
   mcp__plugin_missioncache_pm__get_missioncache_files(project_name="<name>")
   # If found, create pending-task.json and record heartbeat
   ```

2. **Update context file:**
   ```
   mcp__plugin_missioncache_pm__update_context_file(
     context_file="<path>",
     next_steps=["...", "..."],
     recent_changes=["...", "..."]
   )
   ```

3. **Update tasks file (if tasks completed):**
   ```
   mcp__plugin_missioncache_pm__update_tasks_file(
     tasks_file="<path>",
     completed_tasks=["task description"],
     remaining_summary="what's left"
   )
   ```

4. **Process heartbeats:**
   ```
   mcp__plugin_missioncache_pm__process_heartbeats()
   ```

## Workflow

### Step 1: Find Current Project

First resolve the current Claude session id so `find_task_for_directory` can use the per-session project pointer written by `/missioncache:load` and `/missioncache:new`. Without this, the lookup can only match when cwd is under `~/.missioncache/active/<task>/`, which fails from the repo root.

```bash
CWD_KEY=$(pwd | sed 's|/|-|g')
DIR="$HOME/.claude/projects/${CWD_KEY}"
POINTER_FILE="$HOME/.claude/hooks/state/cwd-session/${CWD_KEY}.json"

# Primary: authoritative current-session id (Claude Code 2.1.132+).
SESSION_ID="$CLAUDE_CODE_SESSION_ID"
# Fallback: SessionStart hook's cwd-session pointer (last-writer-wins, can be
# stale under concurrency), then transcript mtime for older Claude Code.
if [ -z "$SESSION_ID" ] && [ -r "$POINTER_FILE" ]; then
  SESSION_ID=$(python3 -c "import json,sys; print(json.load(sys.stdin)['sessionId'])" < "$POINTER_FILE" 2>/dev/null)
fi
[ -z "$SESSION_ID" ] && SESSION_ID=$(ls -t "$DIR"/*.jsonl 2>/dev/null | head -1 | xargs -I{} basename {} .jsonl)

# Safety check: count recently-active transcripts. If >1, concurrent sessions
# share this cwd and even the pointer may be wrong (last-writer-wins race).
RECENT=$(find "$DIR" -maxdepth 1 -name "*.jsonl" -mmin -10 2>/dev/null | wc -l | tr -d ' ')
echo "SESSION_ID=$SESSION_ID RECENT=$RECENT"
```

**Ambiguity check:** If `RECENT > 1`, multiple Claude sessions have been active in this cwd within the last 10 minutes. Under concurrency the pointer or mtime may not reflect the current invocation, and `/missioncache:save` could silently bind to the wrong project. **Do NOT proceed with the resolved SESSION_ID directly.** Instead:

1. Enumerate each recent `*.jsonl` in `$DIR` and look up its `~/.claude/hooks/state/projects/<sid>.json` (if it exists) to get the `projectName`.
2. Deduplicate by project name.
3. For each distinct project, call `mcp__plugin_missioncache_pm__get_task(project_name=...)` to confirm it's still active.
4. Ask the user which project they intend to save and wait for their reply. Show one option per distinct project, using `<project name>` as the label and `last-worked <ago>` as the description. If your tool supports a structured option picker (Claude Code's `AskUserQuestion`), use it; otherwise present the options as a numbered prose list.
5. Use the selected project name to drive the save directly via `mcp__plugin_missioncache_pm__get_missioncache_files(project_name=...)` - skip the session_id-based lookup entirely.

If `RECENT <= 1`, proceed normally: call `mcp__plugin_missioncache_pm__find_task_for_directory(directory="<cwd>", session_id="<SESSION_ID>")` to detect the active project. If `$SESSION_ID` is empty (extremely rare - means no Claude transcript for this cwd), omit the arg and rely on cwd-pattern matching.

**If project not found but MissionCache files exist:** Sometimes the session isn't registered (no `projects/<session-id>.json`) but the project exists. In this case:

1. Try to detect the project from `~/.missioncache/active/<project-name>`
2. Call `mcp__plugin_missioncache_pm__get_missioncache_files(project_name="<name>")` to confirm
3. If found, **register the session** (see Step 1b)

### Step 1b: Register Session (if not registered)

If `find_task_for_directory` returned `found: false` but `get_missioncache_files` found the project:

```bash
SESSION_ID="${CLAUDE_CODE_SESSION_ID}"; [ -z "$SESSION_ID" ] && SESSION_ID=$(ls -t "$HOME/.claude/projects/$(pwd | sed 's|/|-|g')"/*.jsonl 2>/dev/null | head -1 | xargs -I{} basename {} .jsonl); [ -n "$SESSION_ID" ] && curl -s -X POST http://localhost:8787/api/hooks/project -H "Content-Type: application/json" -d "{\"session_id\":\"$SESSION_ID\",\"project_name\":\"<project-name>\"}" --connect-timeout 1 --max-time 2 >/dev/null 2>&1; echo "done"
```

Then record initial heartbeat:
```
mcp__plugin_missioncache_pm__record_heartbeat(task_id=<id>, directory="<cwd>")
```

This ensures activity tracking and statusline display work for the rest of the session.

### Step 2: Gather Updates

Ask the user or infer from conversation:
- What was accomplished this session?
- What are the next steps?
- Any key decisions made?
- Any gotchas discovered?
- **Waiting on changes:** Did this session send an ask that now gates work (a message to a colleague, a ticket assigned out, a CI run someone else owns)? Add it as a `waiting_on_add` row. Did an external reply/event arrive that a Waiting-on row was tracking? Resolve it via `waiting_on_resolve` - the resolution is recorded in Recent Changes automatically, do NOT also add it as a recent_changes entry.

### Step 3: Update Files Atomically

Use the MCP tools to update files in one call each (much faster than multiple Read/Edit cycles):

**Context file:**
```
mcp__plugin_missioncache_pm__update_context_file(
  context_file="<path>",
  next_steps=["First thing to do", "Second thing"],
  recent_changes=["Added retry logic", "Fixed config parsing"],
  key_decisions=["Using exponential backoff"],
  gotchas=["Config path must be absolute"],
  waiting_on_add=[{"what": "Broker config review", "who": "Dana", "gates": "Retry rollout"}],
  waiting_on_resolve=[{"match": "Schema signoff", "outcome": "approved as-is"}]
)
```

Waiting-on notes:
- `waiting_on_add` rows: `since` defaults to today; the section is created before Next Steps if the file predates the convention.
- `waiting_on_resolve` removes the first row whose What cell contains `match` and writes "Resolved (was waiting on <who>): <what> - <outcome>" into today's Recent Changes. Check `waiting_on_unmatched` in the response - a non-empty list means a resolve found no row (typo or already resolved); tell the user, never drop it silently.
- The response's `journal_rolled_over` reports Recent Changes entries moved to `<name>-journal.md` by the cap - no action needed, it is informational.

**Tasks file (if tasks completed):**
```
mcp__plugin_missioncache_pm__update_tasks_file(
  tasks_file="<path>",
  completed_tasks=["Add retry logic to consumer"],
  remaining_summary="Add tests, update docs"
)
```

**Fork branch - restamp your own shared-seen marker if you wrote the SHARED (parent) layer.** When this project is a fork and this save updated the PARENT's context (the shared layer that siblings read), restamp this session's marker to the parent's new mtime. Without this, your own write reads back as a parallel-session update on your next `/missioncache:load` and the statusline dot stays lit over your own edit. Skip it when you only updated this project's own context. Resolve the parent's context path + fresh mtime from the parent itself, then run:

```bash
PARENT='<parent-name>'
SESSION_ID='<SESSION_ID from Step 1>'
PARENT_CTX=$(ls "$HOME/.missioncache/active/$PARENT/$PARENT-context.md" "$HOME/.missioncache/completed/$PARENT/$PARENT-context.md" 2>/dev/null | head -1)
if [ -n "$SESSION_ID" ] && [ -n "$PARENT_CTX" ]; then
  mkdir -p "$HOME/.claude/hooks/state/shared-seen"
  PARENT="$PARENT" PARENT_CTX="$PARENT_CTX" SESSION_ID="$SESSION_ID" python3 -c '
import json, os, pathlib, datetime
ctx = pathlib.Path(os.environ["PARENT_CTX"])
marker = pathlib.Path.home() / ".claude" / "hooks" / "state" / "shared-seen" / (os.environ["SESSION_ID"] + ".json")
marker.write_text(json.dumps({
    "parent": os.environ["PARENT"],
    "parent_context_path": str(ctx),
    "seen_mtime": ctx.stat().st_mtime,
    "seen_at": datetime.datetime.now().astimezone().isoformat(),
}))
'
fi
```

This is the one place restamping from a fresh `stat()` is correct: this session just wrote the file, so its on-disk mtime IS the version this session has seen. (The read paths, load/fork, stamp from the digest's snapshot-coupled mtime instead, because they did not write it.)

### Step 4: Finalize Time Tracking

Call `mcp__plugin_missioncache_pm__process_heartbeats()` to aggregate time.

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
| `mcp__plugin_missioncache_pm__find_task_for_directory` | Find current project |
| `mcp__plugin_missioncache_pm__get_missioncache_files` | Get file paths |
| `mcp__plugin_missioncache_pm__update_context_file` | Update context atomically |
| `mcp__plugin_missioncache_pm__update_tasks_file` | Update tasks atomically |
| `mcp__plugin_missioncache_pm__process_heartbeats` | Finalize time tracking |

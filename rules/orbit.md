<!-- orbit-plugin:managed - do not remove this line if you want the plugin to keep this file up to date. Remove it to take ownership of the file yourself. -->
# Orbit Rules

## Orbit Skills Reference

All orbit skills use the `orbit:` prefix:

| Skill | Purpose |
|-------|---------|
| `/orbit:new` | Create new project with plan, context, tasks files |
| `/orbit:prompts` | Generate optimized prompts for subtasks |
| `/orbit:save` | Save progress before compaction or session end |
| `/orbit:go` | Resume work on an active project |
| `/orbit:done` | Mark project complete and archive |
| `/orbit:mode` | Assign workflow mode to tasks |

## Orbit Project Updates

After finishing a coding task and updating orbit files (`~/.claude/orbit/active/<project>/*`):

1. **Update timestamps** in both `-tasks.md` and `-context.md`:
   - Run `date '+%Y-%m-%d %H:%M'` to get local time
   - Update the "Last Updated" field with this timestamp

2. **Aggregate time tracking**:
   ```bash
   orbit-db process-heartbeats 2>/dev/null
   ```

   The `orbit-db` CLI is installed by `uvx orbit-install` and put on PATH. Do NOT
   use `python3 -m orbit_db` here - the system `python3` rarely has the module
   available, and `2>/dev/null` would silently swallow the import error.

This ensures session time is properly recorded in the task database.

## Context Preservation for Orbit Projects

When working on a project with orbit files (`~/.claude/orbit/active/<project-name>/`), proactively keep context updated to survive auto-compaction.

### Milestone-Based Updates

Run `/orbit:save` after these milestones:

**Progress milestones:**
- Completing any item from the task checklist
- Making code edits (not just reading files)
- Finishing a debugging or investigation session

**Decision milestones:**
- Discovering information that affects the approach
- Making architectural or implementation choices
- Hitting errors or blockers that require direction change

**Transition milestones:**
- Before switching focus to a different part of the project
- Before running long operations (tests, builds, deployments)
- When conversation feels long (proactive compaction protection)

**Do NOT run for:** simple file reads, exploratory searches, minor clarifications.

### After Auto-Compaction

Context is lost after compaction. To restore:

1. **User runs**: `/orbit:go <project-name>` to reload context from orbit files
2. **If user says "continue my project" without specifying**: Check active projects via `mcp__plugin_orbit_pm__list_active_tasks` and ask which one
3. **Resume from "Next Steps"**: Always check the `-context.md` file's Next Steps section first

### Multiple Concurrent Sessions

Each Claude Code session is independent. The orbit files are the shared state - keep them updated so any session can pick up where another left off.

## Statusline Integration

The statusline displays the active project name automatically when set correctly.

### Setting Project in Statusline

When creating, continuing, or resuming an orbit project, resolve the current Claude session ID and set the project. The primary resolver uses the filesystem (works on any terminal including Ghostty), with the legacy term-session lookup as fallback:

```bash
# Caller MUST set PROJECT_NAME (orbit project name, kebab-case recommended).
PROJECT_NAME='<project-name>'

# Primary: most-recently-modified transcript in ~/.claude/projects/<sanitized-cwd>/ = current session.
# Session IDs are UUIDs so ls|head is safe here.
CWD_KEY=$(pwd | sed 's|/|-|g')
SESSION_ID=$(ls -t "$HOME/.claude/projects/${CWD_KEY}"/*.jsonl 2>/dev/null | head -1 | xargs -I{} basename {} .jsonl)

# Fallback: legacy terminal-env-var lookup (iTerm2, Windows Terminal).
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

# Write project_state. Dashboard API first (handles escaping via JSON), direct SQL fallback
# uses Python parameter binding so single-quotes in project names never corrupt the query.
if [ -n "$SESSION_ID" ]; then
  PROJECT_JSON=$(python3 -c 'import json,sys; print(json.dumps({"session_id":sys.argv[1],"project_name":sys.argv[2]}))' "$SESSION_ID" "$PROJECT_NAME")
  curl -s -X POST http://localhost:8787/api/hooks/project \
    -H "Content-Type: application/json" \
    -d "$PROJECT_JSON" \
    --connect-timeout 1 --max-time 2 >/dev/null 2>&1 \
  || SESSION_ID="$SESSION_ID" PROJECT_NAME="$PROJECT_NAME" python3 -c '
import os, sqlite3
conn = sqlite3.connect(os.path.expanduser("~/.claude/hooks-state.db"))
conn.execute(
    "INSERT INTO project_state (session_id, project_name, updated_at) "
    "VALUES (?, ?, datetime(\"now\", \"localtime\")) "
    "ON CONFLICT(session_id) DO UPDATE SET project_name = excluded.project_name, "
    "updated_at = datetime(\"now\", \"localtime\")",
    (os.environ["SESSION_ID"], os.environ["PROJECT_NAME"]),
)
conn.commit()
' 2>/dev/null
fi
```

**How it works:** Session state is stored in `~/.claude/hooks-state.db` (SQLite). The statusline reads `project_state` keyed by session_id. Claude writes transcript files to `~/.claude/projects/<cwd-sanitized>/<session-id>.jsonl`, and the most-recently-modified one corresponds to the active session - that's our universal resolver. The legacy `term_sessions` lookup only works on terminals that set `TERM_SESSION_ID` (iTerm2) or `WT_SESSION` (Windows Terminal), NOT on Ghostty/cmux.

The statusline will automatically display:
```
Project: <project-name>
```

### State Storage

All session state is stored in `~/.claude/hooks-state.db` (SQLite with WAL mode):

| Table | Purpose |
|-------|---------|
| `session_state` | Context %, edit count, action, warned, task name |
| `project_state` | Active project for each session |
| `term_sessions` | Maps iTerm tab to Claude session ID |
| `validation_state` | Rules validation tracking |
| `guard_warned` | MCP guard warning tracking |

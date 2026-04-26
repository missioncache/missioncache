---
description: "Resume work on an active orbit project"
argument-hint: "[project-name]"
---

# Continue Project

Resume work on an active project with full context loading.

## Quick Start

1. **If project name provided:** Jump to Step 2 (Get Project Details)

2. **If no project name, list active projects:**
   ```
   mcp__plugin_orbit_pm__list_active_tasks(repo_path="<cwd>", prioritize_by_repo=True, include_time=True)
   ```
   Then display the selection table (see below) and ask user to select one.

## Selection Table Format

Display projects as a markdown table sorted in two groups:

**Group 1 - This Repo** (projects whose `repo_path` matches current working directory):

**Group 2 - Other Repos** (all other projects, already sorted by last_worked_on from MCP):

Table columns:

| # | Project | Repo | JIRA | Last Worked | Time |
|---|---------|------|------|-------------|------|
| 1 | project-name | repo-short-name | PROJ-12345 | 2h ago | 4h 30m |

- `#` - sequential number for easy selection
- `Project` - task name
- `Repo` - `repo_name` from TaskSummary
- `JIRA` - `jira_key` (show `-` if none)
- `Last Worked` - `last_worked_ago` (e.g., "2h ago", "3d ago")
- `Time` - `time_formatted` (total time invested)

Add a visual separator between the two groups (e.g., a row with "--- Other repos ---" or a blank line with header).

Ask the user to pick a project by number or name.

## Repo Mismatch Check

**CRITICAL:** After the user selects a project, compare the project's `repo_path` with the current working directory (use `git rev-parse --show-toplevel` to get the cwd's git root).

If they differ, ask the user to choose how to proceed via `AskUserQuestion`:

```
AskUserQuestion(questions=[{
    "question": "This project is recorded as belonging to <repo_name> (<repo_path>), but you're currently in <cwd_repo>. How should I handle this?",
    "header": "Repo Mismatch",
    "multiSelect": false,
    "options": [
        {
            "label": "Continue here for this session only",
            "description": "Resume the project without changing the recorded repo. The mismatch warning will fire again next time."
        },
        {
            "label": "Update the project's repo to match my current location",
            "description": "Rewrite the task's repo association in the database so future /orbit:go calls work cleanly. Use this when the project was created with the wrong repo (e.g. /orbit:new captured the wrong cwd) or when the project's source of truth has moved."
        },
        {
            "label": "Cancel",
            "description": "Abort /orbit:go without resuming."
        }
    ]
}])
```

**If the user picks "Update the project's repo to match my current location":**

Call the `set_task_repo` MCP tool with the current repo path:
```
mcp__plugin_orbit_pm__set_task_repo(
    task_id=<task_id>,
    repo_path="<cwd git root from git rev-parse --show-toplevel>"
)
```

If the response has `error: True` with `code: REPO_NOT_FOUND`, register the repo first via `add_repo`, then retry. Otherwise proceed with the resume flow as if there was no mismatch.

**If the user picks "Cancel":** stop and do nothing.

**If the user picks "Continue here for this session only":** proceed with the resume flow without touching the database.

## Workflow

### Step 1: Get Project Details

Call `mcp__plugin_orbit_pm__get_task(project_name="<name>")` which returns:
- Project ID and status
- Time invested (formatted)
- Progress (completion %)
- JIRA key (if any)
- File paths

### Step 2: Read Context Files

Read the key files:
- `<project-name>-context.md` - For current state and next steps
- `<project-name>-tasks.md` - For checklist progress

### Step 3: Display Resume Summary

Before rendering the summary, probe the dashboard so the output can include a deep link, and check for a sticky PreCompact error from a previous session that needs surfacing. The PreCompact hook writes `~/.claude/hooks/state/last-precompact-error.json` when its snapshot run fails (e.g. SQLite lock contention); /orbit:go is the natural place to tell the user since they are about to act on stale context.

Replace `<project-name>` with the resumed project name, then run:

```bash
PROJECT_NAME='<project-name>'
DASHBOARD_URL="${ORBIT_DASHBOARD_URL:-http://localhost:8787}"
if curl -sf -o /dev/null --max-time 1 "${DASHBOARD_URL}/health" 2>/dev/null; then
  echo "Dashboard: ${DASHBOARD_URL}/#projects?task=$PROJECT_NAME"
fi

# Sticky PreCompact error from previous session, if any. Surface in the
# summary if it matches the resumed project, then clear it so we do not
# re-warn on later resumes.
ERROR_FILE="$HOME/.claude/hooks/state/last-precompact-error.json"
if [ -f "$ERROR_FILE" ]; then
  PROJECT_NAME="$PROJECT_NAME" python3 - <<'PY' 2>/dev/null
import json, os, pathlib, sys
project = os.environ["PROJECT_NAME"]
err_path = pathlib.Path.home() / ".claude" / "hooks" / "state" / "last-precompact-error.json"
try:
    data = json.loads(err_path.read_text())
except Exception:
    sys.exit(0)
task = data.get("task_name")
# Surface only when the failure was on THIS project (or had no task at all).
if task and task != project:
    sys.exit(0)
ts = data.get("timestamp", "unknown time")
reason = data.get("reason", "unknown reason")
print(f"PRECOMPACT_WARNING: {ts}: {reason}")
try:
    err_path.unlink()
except Exception:
    pass
PY
fi
```

If the dashboard probe emits a line, include it as a **Dashboard** field. If `PRECOMPACT_WARNING:` is emitted, surface it as a `**PreCompact warning:**` line near the top of the resume summary so the user knows the previous session's auto-snapshot did not land. If neither is emitted, omit those fields.

```
## Project: <name> (active, <time>)

**PreCompact warning:** <from PRECOMPACT_WARNING line> *(only if surfaced)*

**Where You Left Off:** <from context.md Next Steps>

**Progress:** <X/Y tasks complete (Z%)>

**Dashboard:** http://localhost:8787/#projects?task=<name> *(only if probe emitted a line)*

**Key Decisions:**
<from context.md Key Architectural Decisions>

**Next Steps:**
1. <first item from Next Steps>
2. <second item>
```

### Step 4: Register Session for Time Tracking

Write pending-task.json for activity tracking and register the project against the current Claude session so the statusline picks it up. Uses the filesystem resolver (works on any terminal, including Ghostty and cmux) with a legacy term-session fallback. Silently no-ops if the dashboard and `hooks-state.db` aren't present - quick-install users don't have a statusline to update.

Replace `<project-name>` with the actual project name and `<repo-path>` with the repo path from project details, then run:

```bash
PROJECT_NAME='<project-name>'
REPO_PATH='<repo-path>'

# Activity tracking pointer (read by session_start hook on next session).
echo "{\"projectName\": \"$PROJECT_NAME\", \"cwd\": \"$REPO_PATH\", \"timestamp\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" > ~/.claude/hooks/state/pending-task.json

# Primary: SessionStart hook writes the authoritative current-session pointer
# at ~/.claude/hooks/state/cwd-session/<sanitized-cwd>.json. Falls back to
# transcript mtime for sessions that started before the pointer mechanism landed.
CWD_KEY=$(pwd | sed 's|/|-|g')
POINTER_FILE="$HOME/.claude/hooks/state/cwd-session/${CWD_KEY}.json"
SESSION_ID=""
if [ -r "$POINTER_FILE" ]; then
  SESSION_ID=$(python3 -c "import json,sys; print(json.load(sys.stdin)['sessionId'])" < "$POINTER_FILE" 2>/dev/null)
fi
[ -z "$SESSION_ID" ] && SESSION_ID=$(ls -t "$HOME/.claude/projects/${CWD_KEY}"/*.jsonl 2>/dev/null | head -1 | xargs -I{} basename {} .jsonl)

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

# Write project_state. Dashboard API first, direct SQL fallback with parameter binding.
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

  # Write per-session project pointer read by find_task_for_cwd (orbit-db/__init__.py:1270).
  # Without this, /orbit:save cannot find the task when cwd is the repo root (only when
  # cwd is under ~/.claude/orbit/active/<task>/). Format matches session_start.py's
  # write_session_project() exactly so either writer is interchangeable.
  SESSION_ID="$SESSION_ID" PROJECT_NAME="$PROJECT_NAME" python3 -c '
import os, json, datetime, pathlib
projects_dir = pathlib.Path.home() / ".claude" / "hooks" / "state" / "projects"
projects_dir.mkdir(parents=True, exist_ok=True)
(projects_dir / (os.environ["SESSION_ID"] + ".json")).write_text(json.dumps({
    "projectName": os.environ["PROJECT_NAME"],
    "updated": datetime.datetime.now().astimezone().isoformat(),
    "sessionId": os.environ["SESSION_ID"],
}))
' 2>/dev/null
fi
```

Then record initial heartbeat:
```
mcp__plugin_orbit_pm__record_heartbeat(task_id=<id>, directory="<cwd>")
```

## Example Output

### Selection Table

```
### This Repo (my-app)

| # | Project           | JIRA      | Last Worked | Time   |
|---|-------------------|-----------|-------------|--------|
| 1 | auth-refactor     | PROJ-123  | 2h ago      | 1h 15m |
| 2 | kafka-consumer-fix| PROJ-124  | 1d ago      | 8h 30m |

### Other Repos

| # | Project              | Repo         | JIRA      | Last Worked | Time   |
|---|----------------------|--------------|-----------|-------------|--------|
| 3 | docs-rewrite         | website      | -         | 3h ago      | 2h 45m |
| 4 | login-rate-limit     | website      | -         | 1d ago      | 5h 10m |
| 5 | api-gateway          | backend-svc  | PROJ-125  | 2d ago      | 3h 20m |

Which project? (number or name)
```

Note: Omit the Repo column for "This Repo" group since it's redundant.

### Resume Summary

```
## Project: kafka-consumer-fix (active, 2h 30m)

**JIRA:** PROJ-12345
**Progress:** 3/8 tasks complete (37%)

**Where You Left Off:**
1. Implement retry logic in consumer.py:145
2. Add unit tests for retry

**Key Decisions:**
- Exponential backoff (2^n seconds, max 30s)
- Max 3 retries before dead-letter queue

**Key Files:**
- `src/consumer.py:145` - Main consumer logic
- `tests/test_consumer.py` - Test file to update

Ready to continue. What would you like to work on?
```

## MCP Tools Used

| Tool | Purpose |
|------|---------|
| `mcp__plugin_orbit_pm__list_active_tasks` | List projects with repo prioritization |
| `mcp__plugin_orbit_pm__get_task` | Get full project details |
| `mcp__plugin_orbit_pm__get_orbit_files` | Get file paths |
| `mcp__plugin_orbit_pm__get_orbit_progress` | Get checklist progress |
| `mcp__plugin_orbit_pm__record_heartbeat` | Start time tracking |
| `mcp__plugin_orbit_pm__set_task_repo` | Reassign task to current repo when mismatch detected |

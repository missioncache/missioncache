---
description: "Resume work on an active MissionCache project"
argument-hint: "[project-name]"
---

# Continue Project

Resume work on an active project via the context digest.

## Quick Start

1. **If project name provided:** Jump to Step 2 (Get Project Details)

2. **If no project name, list active projects:**
   ```
   mcp__plugin_missioncache_pm__list_active_tasks(repo_path="<cwd>", prioritize_by_repo=True, include_time=True)
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

If they differ, ask the user how to handle the mismatch and wait for their reply. Present these three options:

> This project is recorded as belonging to **<repo_name>** (`<repo_path>`), but you're currently in **<cwd_repo>**. How should I handle this?
>
> 1. **Continue here for this session only** - Resume the project without changing the recorded repo. The mismatch warning will fire again next time.
> 2. **Update the project's repo to match my current location** - Rewrite the task's repo association in the database so future `/missioncache:load` calls work cleanly. Use this when the project was created with the wrong repo (e.g. `/missioncache:new` captured the wrong cwd) or when the project's source of truth has moved.
> 3. **Cancel** - Abort `/missioncache:load` without resuming.

If your tool supports a structured option picker (Claude Code's `AskUserQuestion`), use it. Otherwise present the options as prose and wait for the user to reply with a number or label.

**If the user picks option 2 ("Update the project's repo to match my current location"):**

Call the `set_task_repo` MCP tool with the current repo path:
```
mcp__plugin_missioncache_pm__set_task_repo(
    task_id=<task_id>,
    repo_path="<cwd git root from git rev-parse --show-toplevel>"
)
```

If the response has `error: True` with `code: REPO_NOT_FOUND`, register the repo first via `add_repo`, then retry. Otherwise proceed with the resume flow as if there was no mismatch.

**If the user picks option 3 ("Cancel"):** stop and do nothing.

**If the user picks option 1 ("Continue here for this session only"):** proceed with the resume flow without touching the database.

## Workflow

### Step 1: Get Project Details

<!-- claude-code-only -->
First resolve the current Claude session id so `get_task` can atomically bind the session to this project (server-side, mirroring the create_missioncache_files binding pattern). Without this, the binding falls back to the bash step in Step 4, which Claude can stream past silently.

```bash
CWD_KEY=$(pwd | sed 's|/|-|g')
POINTER_FILE="$HOME/.claude/hooks/state/cwd-session/${CWD_KEY}.json"
# Primary: env var set by Claude Code 2.1.132+ in every Bash subprocess.
SESSION_ID="$CLAUDE_CODE_SESSION_ID"
# Fallbacks for older Claude Code: cwd-session pointer (SessionStart hook), then transcript-mtime walk.
if [ -z "$SESSION_ID" ]; then
  [ -r "$POINTER_FILE" ] && SESSION_ID=$(python3 -c "import json,sys; print(json.load(sys.stdin)['sessionId'])" < "$POINTER_FILE" 2>/dev/null)
  [ -z "$SESSION_ID" ] && SESSION_ID=$(ls -t "$HOME/.claude/projects/${CWD_KEY}"/*.jsonl 2>/dev/null | head -1 | xargs -I{} basename {} .jsonl)
fi
echo "SESSION_ID=$SESSION_ID"
```

Capture the printed `SESSION_ID`.
<!-- /claude-code-only -->

Then call `mcp__plugin_missioncache_pm__get_task(project_name="<name>")` - in Claude Code, also pass `session_id="<SESSION_ID>"` so the session binds server-side. It returns:
- Project ID and status
- Time invested (formatted)
- Progress (completion %)
- JIRA key (if any)
- File paths
- `session_bound: true | false | <omitted>` indicating whether the session-to-project binding succeeded server-side. If the response includes `session_bound: true`, the statusline will reflect this project immediately - the Step 4 bash binding is still run as defense-in-depth and to refresh the dashboard list view, but it is no longer load-bearing.

<!-- claude-code-only -->
If `$SESSION_ID` is empty (extremely rare on Claude Code 2.1.132+), omit the `session_id` arg from `get_task`; Step 4 reuses this same value, so its binding is skipped too and the user can populate the statusline later by re-running `/missioncache:load`.
<!-- /claude-code-only -->

### Step 2: Get the Context Digest

Do NOT read the full context file - call the digest tool instead:

```
mcp__plugin_missioncache_pm__get_context_digest(project_name="<name>")
```

It returns the resume-critical slices parsed server-side (so it works on context files past the 256KB Read-tool cap): `last_updated`, `hub` / `related_projects` header lines, `waiting_on` (verbatim), `next_steps` (verbatim), `recent_changes_last3`, a `section_index` (name + line number), `file_size_bytes`, and `health_warnings`.

Checklist progress comes from `get_task`'s `progress` field (Step 1) - no need to read the tasks file either.

Read the FULL context file (or a specific section via the `section_index` line numbers with Read offset/limit) only when the user asks for it or the digest is not enough for the task at hand. Older Recent Changes history lives in `<project-name>-journal.md` - grep it on demand, never load it on resume.

If `get_context_digest` returns an error (older MCP server without the tool), fall back to reading `<project-name>-context.md` and `<project-name>-tasks.md` directly.

**Fork branch (when the digest returns a non-null `parent_digest`):** this project is a fork and the parent's context file is its shared knowledge layer.

Read the shared layer: call `get_context_digest(project_name="<parent>")` and fold its Next Steps / newest Recent Changes into the resume summary.

<!-- claude-code-only -->
Freshness tracking refines that read via this session's shared-seen marker:

1. BEFORE calling the digest, read this session's shared-seen marker if it exists (`~/.claude/hooks/state/shared-seen/<SESSION_ID>.json`) and pass its `seen_mtime` to the digest call: `get_context_digest(project_name="<name>", seen_mtime=<marker seen_mtime>)`. No marker -> call without `seen_mtime`.
2. Read the parent digest (pass `session_id="<SESSION_ID from Step 1>"`) only when `parent_digest.changed_since_seen` is `true` OR there was no marker (first fork resume in this session). The marker means "this session consumed the shared layer at this version" - never stamp what was not read. On mcp-missioncache 1.0.15+ this parent read AUTO-STAMPS the marker server-side (the response carries `shared_seen_stamped: true`), baselined to the exact bytes it read - when you see that flag, SKIP Step 4's manual stamp entirely (its mtime comes from the earlier Step 2 response and can be older; overwriting the auto-stamp with it would re-light the statusline note over content you already read).
3. Step 4's bash block then stamps the marker with `parent_digest.context_mtime` FROM THE DIGEST RESPONSE (the snapshot-coupled value), never a fresh stat of the file - a sibling write between the digest read and the stamp must not be silently baselined.
<!-- /claude-code-only -->

### Step 3: Display Resume Summary

Before rendering the summary, probe the dashboard so the output can include a deep link, and check for a sticky PreCompact error from a previous session that needs surfacing. The PreCompact hook writes `~/.claude/hooks/state/last-precompact-error.json` when its snapshot run fails (e.g. SQLite lock contention); /missioncache:load is the natural place to tell the user since they are about to act on stale context.

Replace `<project-name>` with the resumed project name, then run:

```bash
PROJECT_NAME='<project-name>'
DASHBOARD_URL="${MISSIONCACHE_DASHBOARD_URL:-http://localhost:8787}"
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

**Hub:** <digest hub line, if any>
**Fork of:** <parent name> - shared context [up to date | UPDATED by a parallel session since your last sync - re-read before continuing] *(only when parent_digest is non-null)*
**Related projects:** <digest related_projects line, if any>

**Progress:** <X/Y tasks complete (Z%)>

**Dashboard:** http://localhost:8787/#projects?task=<name> *(only if probe emitted a line)*

**Waiting on:** *(from digest waiting_on; omit if the table is empty)*
| What | Who | Since | Gates |
|------|-----|-------|-------|
<rows - mark any row whose Since is older than 7 days with a trailing " (stale)">

**Next Steps:** *(from digest next_steps)*
1. <first item>
2. <second item>

**Recent activity:** <1-2 line synthesis of digest recent_changes_last3>

**Health:** <digest health_warnings, one line, only when non-empty>
```

Waiting on renders NEXT TO Next Steps by design - both are the "what now" surface. When a Waiting-on row's external reply has arrived (the user mentions it, or you see it in the conversation), act on what it gates and resolve the row via `/missioncache:save`'s `waiting_on_resolve`. Offer the full context file ("say 'full context' for the whole file") instead of dumping it.

<!-- claude-code-only -->
### Step 4: Register Session for Time Tracking

Register the project against the current Claude session so the statusline picks it up, reusing the `SESSION_ID` resolved in Step 1. Silently no-ops if the dashboard and `hooks-state.db` aren't present - quick-install users don't have a statusline to update.

This step is defense-in-depth: Step 1's `get_task(session_id=...)` already performs the server-side binding via the MCP tool. The bash block additionally hits the dashboard API so the dashboard list view refreshes immediately (instead of waiting for the next periodic SQLite -> DuckDB sync) and writes the per-session pointer in case the MCP binding raced.

Replace `<project-name>` with the actual project name, `<SESSION_ID from Step 1>` with the value captured in Step 1, and `<task id>` with the numeric `id` from Step 1's `get_task` response (leave TASK_ID empty if you don't have it - the pointer then keeps the legacy name-only shape), then run:

```bash
PROJECT_NAME='<project-name>'
SESSION_ID='<SESSION_ID from Step 1>'
TASK_ID='<task id>'

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

  # Write per-session project pointer read by find_task_for_cwd. Without this,
  # /missioncache:save cannot find the task when cwd is the repo root (only when
  # cwd is under ~/.missioncache/active/<task>/). Format MUST match
  # missioncache_db.write_session_binding (the owner of the convention);
  # taskId is the durable identity resolution prefers - omitting it here
  # would clobber the MCP server's richer binding with a name-only one.
  SESSION_ID="$SESSION_ID" PROJECT_NAME="$PROJECT_NAME" TASK_ID="$TASK_ID" python3 -c '
import os, json, datetime, pathlib
projects_dir = pathlib.Path.home() / ".claude" / "hooks" / "state" / "projects"
projects_dir.mkdir(parents=True, exist_ok=True)
payload = {
    "projectName": os.environ["PROJECT_NAME"],
    "updated": datetime.datetime.now().astimezone().isoformat(),
    "sessionId": os.environ["SESSION_ID"],
}
task_id = os.environ.get("TASK_ID", "").strip()
if task_id.isdigit():
    payload["taskId"] = int(task_id)
(projects_dir / (os.environ["SESSION_ID"] + ".json")).write_text(json.dumps(payload))
' 2>/dev/null
fi
```
<!-- /claude-code-only -->

Then record initial heartbeat:
```
mcp__plugin_missioncache_pm__record_heartbeat(task_id=<id>, directory="<cwd>")
```

<!-- claude-code-only -->
**Fork marker stamp (forks only):** when Step 2 found a `parent_digest` AND the shared layer was actually consumed (it was fresh, or you read the parent digest per the fork branch), stamp this session's shared-seen marker with the mtime FROM the digest response. SKIP this entirely when the parent digest response carried `shared_seen_stamped: true` - the server already stamped a fresher, bytes-coupled value and this bash stamp would overwrite it with an older one. Do not stat the file here - `parent_digest.context_mtime` is coupled to the bytes the digest actually read, and a fresh stat could silently baseline a sibling's newer write. Replace the values, then run:

```bash
PARENT='<parent name from parent_digest>'
PARENT_CTX='<parent_digest.context_file>'
SEEN_MTIME='<parent_digest.context_mtime, verbatim from the digest response>'
SESSION_ID='<SESSION_ID from Step 1>'
if [ -n "$SESSION_ID" ] && [ -n "$SEEN_MTIME" ]; then
  mkdir -p "$HOME/.claude/hooks/state/shared-seen"
  PARENT="$PARENT" PARENT_CTX="$PARENT_CTX" SEEN_MTIME="$SEEN_MTIME" SESSION_ID="$SESSION_ID" python3 -c '
import json, os, pathlib, datetime
marker = pathlib.Path.home() / ".claude" / "hooks" / "state" / "shared-seen" / (os.environ["SESSION_ID"] + ".json")
marker.write_text(json.dumps({
    "parent": os.environ["PARENT"],
    "parent_context_path": os.environ["PARENT_CTX"],
    "seen_mtime": float(os.environ["SEEN_MTIME"]),
    "seen_at": datetime.datetime.now().astimezone().isoformat(),
}))
'
fi
```

The marker stamp does not suppress stderr (matching `/missioncache:fork` and `/missioncache:save`): a persistently failing stamp - e.g. a non-float `SEEN_MTIME` - should surface, not silently disable freshness tracking. A single dropped stamp is self-correcting anyway (the next load finds no marker and re-reads the shared layer).
<!-- /claude-code-only -->

<!-- claude-code-only -->
### Step 5: Track the Active Checklist Task (when known)

If the user signals which MissionCache checklist task they want to work on
(either by number like "let's do 54a" or by description that matches a
``[ ]`` line in tasks.md), call ``set_active_missioncache_tasks`` so the
statusline ``Task:`` field reflects the current focus.

```
mcp__plugin_missioncache_pm__set_active_missioncache_tasks(
    project_name="<project-name>",
    task_numbers=["54a"],          # or ["56", "57"] for parallel work
    session_id="<SESSION_ID from Step 1>"
)
```

Pass MULTIPLE numbers when the user is genuinely working on multiple
items in parallel (e.g. ``["54a", "54b"]``). The statusline collapses
sibling subtasks under their parent automatically. Pointer auto-clears
on ``update_tasks_file`` when items get marked ``[x]``; call
``clear_active_missioncache_tasks`` explicitly when focus shifts off-task
without completing anything.

If the user resumes without naming a specific task, skip this step -
the field hides cleanly. Do NOT guess or use the first ``[ ]`` line as
a fallback; misleading data is worse than missing data.
<!-- /claude-code-only -->

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

**Waiting on:**
| What | Who | Since | Gates |
|------|-----|-------|-------|
| Broker config review | Dana | 2026-07-02 (stale) | Retry rollout |

**Next Steps:**
1. Implement retry logic in consumer.py:145
2. Add unit tests for retry

**Recent activity:** Retry backoff shipped behind a flag; consumer tests still red on the timeout path.

**Health:** context file is 112KB (> 100KB budget)

Ready to continue. What would you like to work on? (say "full context" for the whole file)
```

## MCP Tools Used

| Tool | Purpose |
|------|---------|
| `mcp__plugin_missioncache_pm__list_active_tasks` | List projects with repo prioritization |
| `mcp__plugin_missioncache_pm__get_task` | Get full project details |
| `mcp__plugin_missioncache_pm__get_context_digest` | Resume digest of the context file (replaces the full-file read) |
| `mcp__plugin_missioncache_pm__get_missioncache_files` | Get file paths |
| `mcp__plugin_missioncache_pm__get_missioncache_progress` | Get checklist progress |
| `mcp__plugin_missioncache_pm__record_heartbeat` | Start time tracking |
| `mcp__plugin_missioncache_pm__set_task_repo` | Reassign task to current repo when mismatch detected |
| `mcp__plugin_missioncache_pm__set_active_missioncache_tasks` | Mark which checklist tasks are in progress (for statusline Task field) |
| `mcp__plugin_missioncache_pm__clear_active_missioncache_tasks` | Clear the Task field when focus shifts off-task without completing |

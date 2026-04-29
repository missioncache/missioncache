---
description: "Rename the current orbit project"
argument-hint: "[new-name]"
---

# Rename Project

Rename the orbit project bound to the current Claude session. Updates the
DB row, moves the orbit directory, renames files inside, and rewrites the
template H1 titles. Time tracking, heartbeats, sessions, and JIRA links
all survive because they're keyed by task_id, not by name.

Use when the current name no longer fits the work scope - keep your
context, lose the misleading label.

## Workflow

### Step 1: Validate the argument

The user invokes this as `/orbit:rename <new-name>`. If `$ARGUMENTS` is
empty, stop and show:

> Usage: `/orbit:rename <new-name>` - provide the new kebab-case name as
> a single argument.

Do not proceed without a new-name argument.

The MCP tool normalizes (trim + lowercase) and validates server-side, so
no client-side validation is needed - just pass the user's input through.

### Step 2: Find the current project

Resolve the current Claude session id so `find_task_for_directory` can
use the per-session project pointer written by `/orbit:go` and
`/orbit:new`. Without this, the lookup can only match when cwd is under
`~/.orbit/active/<task>/`, which fails from the repo root.

```bash
CWD_KEY=$(pwd | sed 's|/|-|g')
DIR="$HOME/.claude/projects/${CWD_KEY}"
POINTER_FILE="$HOME/.claude/hooks/state/cwd-session/${CWD_KEY}.json"

SESSION_ID=""
if [ -r "$POINTER_FILE" ]; then
  SESSION_ID=$(python3 -c "import json,sys; print(json.load(sys.stdin)['sessionId'])" < "$POINTER_FILE" 2>/dev/null)
fi
[ -z "$SESSION_ID" ] && SESSION_ID=$(ls -t "$DIR"/*.jsonl 2>/dev/null | head -1 | xargs -I{} basename {} .jsonl)
echo "SESSION_ID=$SESSION_ID"
```

Then:
```
mcp__plugin_orbit_pm__find_task_for_directory(
    directory="<cwd>",
    session_id="<SESSION_ID>"
)
```

If the lookup returns `found: false`, stop and tell the user:

> No active orbit project bound to this session. Run `/orbit:go <name>`
> to bind one, then rename.

Do not guess by cwd, do not rename by name without the binding - the
slash command operates on the CURRENT session's project, period.

### Step 3: Call rename_task

Once the project is identified, call:

```
mcp__plugin_orbit_pm__rename_task(
    task_id=<task_id from Step 2>,
    new_name="<$ARGUMENTS>"
)
```

Pre-launch check: do not run this when an `orbit-auto` execution is
active for this project. The MCP tool refuses with INVALID_STATE in that
case; if the user sees that error, surface it and tell them to stop the
auto run first.

### Step 4: Report the result to the user

The response always carries the canonical stored name in `result.name` -
display THAT, not the user's typed input.

If the rename succeeded (`changed: true`):

```markdown
## Renamed: <old_name> -> <name>
```

If `result.normalized` is `true`, prefix the line with a normalization
notice so the user knows their input was lowercased / trimmed:

```markdown
**Normalized your input** (trim + lowercase).
## Renamed: <old_name> -> <name>
```

If `result.h1_skipped` is non-empty, append:

```markdown
H1 skipped (you've edited these): <comma-separated filenames>
```

If `result.changed: false` (same-name no-op), say:

```markdown
No change - new name matches the current name.
```

If the response is an error (`error: true`), surface the friendly
`message` directly without paraphrasing.

### Step 5: Update the statusline

The MCP tool's session-pointer sweep already updates
`hooks-state.db.project_state` and `hooks/state/projects/<sid>.json`,
but the running statusline only re-reads on the next prompt render.
Tell the user the statusline updates on the next prompt.

The dashboard's read path (DuckDB) is refreshed by the dashboard's own
`POST /api/tasks/{id}/rename` endpoint, NOT by the MCP tool. When the
rename comes through this slash command, the dashboard list will
reflect the new name on its next periodic SQLite -> DuckDB sync (a few
seconds, not "immediately"). Surface that to the user so they don't
think the rename failed.

If the response includes a non-empty ``warnings`` list, surface each
warning verbatim - those are best-effort failures (session-pointer
sweep targets that couldn't be rewritten) that affect statusline
behavior on existing sessions.

## MCP Tools Used

| Tool | Purpose |
|------|---------|
| `mcp__plugin_orbit_pm__find_task_for_directory` | Resolve the current session's project to its task_id |
| `mcp__plugin_orbit_pm__rename_task` | Atomic rename across DB, filesystem, H1s, and session pointers |

## Notes

- **Subtasks are out of scope.** Rename the parent project; subtasks ride
  along.
- **The CLI equivalent** is `orbit-db rename-task <old-name> <new-name>`
  for batch / external use. The dashboard has a Rename button in the
  project modal for the same purpose.
- **The new name must be kebab-case** (lowercase letters, digits, and
  hyphens only, starting with a letter or digit). The MCP tool normalizes
  trim+lowercase before validating, so `Kafka-Fix` becomes `kafka-fix`,
  but `kafka fix` (with space) is rejected.

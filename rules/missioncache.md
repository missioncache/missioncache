<!-- missioncache-plugin:managed - do not remove this line if you want the plugin to keep this file up to date. Remove it to take ownership of the file yourself. -->
# MissionCache Rules

## MissionCache Skills Reference

All MissionCache skills use the `missioncache:` prefix:

| Skill | Purpose |
|-------|---------|
| `/missioncache:new` | Create new project with plan, context, tasks files |
| `/missioncache:prompts` | Generate optimized prompts for subtasks |
| `/missioncache:save` | Save progress before compaction or session end |
| `/missioncache:load` | Resume work on an active project |
| `/missioncache:done` | Mark project complete and archive |
| `/missioncache:mode` | Assign workflow mode to tasks |

## MissionCache Project Updates

After finishing a coding task and updating MissionCache files (`~/.missioncache/active/<project>/*`):

1. **Update timestamps** in both `-tasks.md` and `-context.md`:
   - Run `date '+%Y-%m-%d %H:%M'` to get local time
   - Update the "Last Updated" field with this timestamp

2. **Aggregate time tracking**:
   ```bash
   missioncache-db process-heartbeats 2>/dev/null
   ```

   The `missioncache-db` CLI is installed by `uvx missioncache-install` and put on PATH. Do NOT
   use `python3 -m missioncache_db` here - the system `python3` rarely has the module
   available, and `2>/dev/null` would silently swallow the import error.

This ensures session time is properly recorded in the task database.

## Context Preservation for MissionCache Projects

When working on a project with MissionCache files (`~/.missioncache/active/<project-name>/`), proactively keep context updated to survive auto-compaction.

### Milestone-Based Updates

Run `/missioncache:save` after these milestones:

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

1. **User runs**: `/missioncache:load <project-name>` to reload context from MissionCache files
2. **If user says "continue my project" without specifying**: Check active projects via `mcp__plugin_missioncache_pm__list_active_tasks` and ask which one
3. **Resume from "Next Steps"**: Always check the `-context.md` file's Next Steps section first

### Multiple Concurrent Sessions

Each Claude Code session is independent. The MissionCache files are the shared state - keep them updated so any session can pick up where another left off.

## Context File Conventions

Every context file shares one structure so a fresh session can rely on where things live.

### Canonical section order (new projects; existing files are NEVER reordered)

```
# <Name> - Context
**Last Updated:** <ts>
Hub: [[vault-hub]]                    <- optional, when a vault hub exists
**Related projects:** [[x]] (why)     <- optional, when a real relationship exists

## Description
## Definition of Done                 <- acceptance criteria; estimates stay gated until it exists
## Key People                         <- optional; only when colleagues own parts of the work
## <project-specific sections>        <- free-form, any number
## Gotchas
## Waiting on
## Next Steps
## Recent Changes                     <- capped at 12 dated subsections
## Key Architectural Decisions
## Key Files
```

The resume-critical tail (Gotchas, Waiting on, Next Steps, Recent Changes) is the fixed contract that `/missioncache:load`'s digest and `/missioncache:save`'s automation rely on. Section names are exact - code targets them by name.

### Waiting on

External replies/events that gate work, as a table: `| What | Who | Since | Gates |`. Check it on every resume. When a row resolves, act on what it gates and resolve the row via `update_context_file(waiting_on_resolve=...)` - the resolution moves into Recent Changes automatically. Add new external dependencies at save time via `waiting_on_add`. Rows older than 7 days get flagged by the health check - stale rows mean chase or drop.

### Falsified hypotheses live in Gotchas

When an investigation DISPROVES a theory, record it so no future session rebuilds it:

```
- WRONG (falsified 2026-07-11): <theory> - <what disproved it>. Do not resurface.
```

### Recent Changes cap + journal

Recent Changes keeps the newest 12 dated `### <timestamp>` subsections; older entries roll automatically into `<name>-journal.md` (oldest first) in the project dir. The journal is greppable history - never load it on resume, grep it when archaeology is needed. The pointer line at the section bottom says where the history went. The pre-compact hook may leave the section temporarily over cap; the next save re-trims.

### Cross-project events

When another project's meeting/decision changes THIS project's reality, write a self-contained imported-event section ABOVE Waiting on:

```
## <event> (<date>) - what changed for THIS project
```

Open it by naming the source project, then list only what changed for this project (no pointers into the other project's files - the section must stand alone). Add a `**Related projects:** [[name]] (what's shared)` header line so both sides know the link exists.

### Parallel-session discipline

Two sessions may work sibling projects at once. Before writing to a context file another session may share: re-read the digest first (`get_context_digest`), write ONLY via the locked MCP tools (`update_context_file` / `update_tasks_file` serialize on a sidecar lock), and treat Recent Changes as prepend-only - never rewrite older subsections.

### Health check

`missioncache-db health` reports per-project: stale Last Updated (>14d), stale Waiting-on rows (>7d), context file over the 100KB budget, missing core sections, Recent Changes over cap. The same warnings surface in the `/missioncache:load` digest. Report-only - warnings are prompts to tidy, not blockers.

## Statusline Integration

The statusline displays the active project name automatically when set correctly.

### Setting Project in Statusline

When creating, continuing, or resuming a MissionCache project, the `/missioncache:new` and `/missioncache:load` commands bind the current Claude session to the project so the statusline picks it up. They resolve the session id from `$CLAUDE_CODE_SESSION_ID` (Claude Code 2.1.132+), fall back to the cwd-session pointer then a transcript-mtime walk on older versions, and write `project_state` (keyed by session_id) to `~/.claude/hooks-state.db` via the dashboard API with a direct-SQL fallback. The canonical bash lives in `commands/load.md` (session-id resolution in Step 1, the `project_state` write in Step 4) - reproduce it from there rather than from memory.

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

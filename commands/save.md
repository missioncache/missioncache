---
description: "Save progress on an active project before compaction or session end"
argument-hint: ""
---

# Update Project

Save what this session learned so the next session, or this one after a compaction, can resume from it.

## Goal

The save is done when:

- The context file says where things stand now: what changed, what comes next, what blocks the work, and what turned out to be true or false.
- The tasks file matches the work, so the progress counter is right.
- Anything that is no longer true has come out, not only been added to.
- Other live sessions on the project know what changed.

Use it before `/compact`, before ending a session, at a milestone, and when the PreCompact hook asks.

## Finding the Project

Call `mcp__plugin_missioncache_pm__find_task_for_directory(directory="<cwd>")`.
<!-- claude-code-only -->
Pass `session_id="$CLAUDE_CODE_SESSION_ID"` (read it with `echo`), so the project this session loaded wins over a guess from the directory. When the variable is empty, do not guess a session from transcript files: several sessions can share a directory, and the wrong guess saves into the wrong project. Ask the user instead.
<!-- /claude-code-only -->

When nothing is found, ask which project, offering the list from `list_active_tasks`. Then bind it with `mcp__plugin_missioncache_pm__get_task(project_name="<name>")` (plus `session_id` in Claude Code), and start time tracking with `record_heartbeat(task_id=<id>, directory="<cwd>")`. Get the file paths from `get_missioncache_files(project_name="<name>")`.

## What to Save

Take it from the conversation, and ask only for what you cannot tell. Read the project's digest once with `get_context_digest(project_name="<name>")`. Propose every closure, removal and distill below, and apply it only after a yes.

- **Recent Changes:** what this session did, one short line each. Never paste session output, a report or a transcript into an entry. Resolving a Waiting-on row and the action-item tools write their own lines, so leave those out.
- **Next Steps:** what comes next, replacing the old list.
- **Decisions and gotchas:** a decision and why, a trap the next session would fall into. A theory that turned out wrong goes in Gotchas marked `WRONG (falsified <date>)`, so nobody rebuilds it.
- **Waiting on:** an ask this session sent that now blocks the work (a message, a ticket assigned out, a CI run someone else owns) is a new row. A reply that arrived resolves its row.
- **Stale Waiting-on rows:** when the digest marks rows `stale`, list them together (what, who, days) and ask per row: chase it (keep it, and offer a one-line nudge for the user to send) or drop it. Apply every drop in one call.
- **Action items:** close one the work finished, and add each new commitment (the user's or a colleague's) with `requested_by`, `assignee` ("me" for the user's), `due_date` and `source`. When something is both a commitment and a blocker, it is a Waiting-on row, not an action item.
- **What should come out:** a dead section, a gotcha or decision that proved wrong, a task that was superseded. A superseded task left open skews the counter, and ticking it claims work that never happened. Content that belongs to another project moves there with `move_to_project`.
- **Gotchas over budget:** when `health_warnings` says Gotchas is past its 15KB budget, distill it. A gotcha that a skill, a rule or a vault note already covers becomes a one-line pointer. Other old ones move out with `bullets_remove` and `"archive": true`, which keeps them searchable in `<name>-archive.md`. Keep the ones that still matter for the current work.

## Writing It

One call per file:

```
mcp__plugin_missioncache_pm__update_context_file(
  context_file="<path>",
  next_steps=["Run the e2e suite", "Ship the retry path"],
  recent_changes=["Added retry logic", "Fixed config parsing"],
  key_decisions=["Using exponential backoff"],
  gotchas=["Config path must be absolute"],
  waiting_on_add=[{"what": "Broker config review", "who": "Dana", "gates": "Retry rollout"}],
  waiting_on_resolve=[{"match": "Schema signoff", "outcome": "approved as-is"}]
)

mcp__plugin_missioncache_pm__update_tasks_file(
  tasks_file="<path>",
  completed_tasks=["Add retry logic to consumer"],
  remaining_summary="Add tests, update docs"
)
```

Then `mcp__plugin_missioncache_pm__process_heartbeats()` to total the time.

Read the response before reporting success:

- **A miss is never silent.** A non-empty `waiting_on_unmatched`, `sections_unmatched` or `bullets_unmatched` means a resolve or a remove found nothing. Tell the user.
- **`waiting_on_resolve` takes a `kind`** when the row was not answered: `moved` (another project owns it now) or `dropped` (nobody chases it). Without it the record claims a reply that never came.
- **`live_sessions`** lists other live sessions on this project. Tell each one what changed, per "Cross-session notifications" in the MissionCache rules.
- **`journal_rolled_over`** is information only: old Recent Changes moved to the journal.

**Writing into another project:** get its path from `get_missioncache_files` and pass `imported_event={"heading", "body", "related_project", "related_note"}`. Never edit that file directly, because only the tool takes the lock.

<!-- claude-code-only -->
**A fork that wrote its parent's context:** read the parent's digest afterwards with `get_context_digest(project_name="<parent>", session_id=...)`. That read restamps this session's shared-seen marker, so the next load does not report your own write as a change from a parallel session.
<!-- /claude-code-only -->

## Report

A few lines: what was written to each file, the new progress (X/Y), anything that missed, and that it is safe to continue or compact.

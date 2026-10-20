---
description: "Resume work on an active MissionCache project"
argument-hint: "[project-name]"
---

# Continue Project

Resume a project so the user can pick up where the last session stopped, without reading the whole context file.

## Goal

The load is done when:

- The session is bound to the project, so the statusline, time tracking and `/missioncache:save` all find it.
- The user sees a short resume summary built from the context digest: where things stand, what is open, and what comes next.
- Nothing was read that the summary did not need.

## Picking the Project

With a project name in the arguments, use it. Without one, call:

```
mcp__plugin_missioncache_pm__list_active_tasks(repo_path="<cwd>", prioritize_by_repo=True, include_time=True)
```

Show the projects as a numbered table: the ones whose `repo_path` matches the current repo first, then the rest, already sorted by last worked. Columns: `#`, Project, Repo (omit it for this repo's group), JIRA (`-` when none), Last Worked, Time. Ask for a number or a name.

When the chosen project's `repo_path` differs from the current git root (`git rev-parse --show-toplevel`), ask before going on. Offer three choices: continue here for this session only, move the project to this repo, or cancel. Moving it is `mcp__plugin_missioncache_pm__set_task_repo(task_id=..., repo_path="<git root>")`. On `REPO_NOT_FOUND`, register the repo with `add_repo` and retry. Use a structured picker (`AskUserQuestion`) when the client has one.

## Loading It

Run the first two in parallel, then the third with the id from the first.

1. **Bind and fetch the project:** `mcp__plugin_missioncache_pm__get_task(project_name="<name>")`. It returns the id, time invested, progress and file paths.
<!-- claude-code-only -->
   Also pass `session_id="$CLAUDE_CODE_SESSION_ID"` (read it with `echo`). The server writes the binding itself and reports `session_bound`. When the variable is empty, omit the argument: the server falls back to its own copy of the id.
<!-- /claude-code-only -->
2. **Read the digest, not the file:** `mcp__plugin_missioncache_pm__get_context_digest(project_name="<name>")`. It carries the header lines (hub, related projects, due date), Waiting on with each row's age and `stale` flag, open action items with an `overdue` flag, Next Steps, the last three Recent Changes, a section index with sizes, and health warnings. Progress comes from `get_task`, so the tasks file stays closed too. If the digest tool is missing (a very old server), read the context and tasks files directly.
3. **Start time tracking:** `mcp__plugin_missioncache_pm__record_heartbeat(task_id=<id>, directory="<cwd>")`.

### Forks

A digest with a non-null `parent_digest` belongs to a fork. The parent's context is its shared layer, so fold the parent's Next Steps and newest Recent Changes into the summary. Read one hop only, never the grandparent, even when the parent is itself a fork.

<!-- claude-code-only -->
Read the parent only when it may have changed. Before the digest call, read this session's marker at `~/.claude/hooks/state/shared-seen/<session id>.json` and pass its `seen_mtime`. Then call `get_context_digest(project_name="<parent>", session_id=...)` when `parent_digest.changed_since_seen` is true or there was no marker. That call stamps the marker itself, so write nothing to it by hand.
<!-- /claude-code-only -->

## The Resume Summary

Lead with the project line, then only the fields that have something in them:

```
## Project: <name> (active, <time>)

**Hub:** / **Fork of:** / **Related projects:**   <header lines>
**Progress:** X/Y tasks complete (Z%)
**Due:** <date> (in N days | OVERDUE by N days)
**Dashboard:** <deep link>

**Action items:** <table: ID, What, From, Owner, Due, overdue marked>
**Waiting on:** <table: What, Who, Since, Gates, stale rows marked>
**Next Steps:** <numbered, verbatim from the digest>
**Recent activity:** <one or two lines synthesized from the last three changes>
**Health:** <warnings on one line>
**Update:** <only when an update is available>

Ready to continue. What would you like to work on? (say "full context" for the whole file)
```

Criteria for the fields:

- **Waiting on sits next to Next Steps:** together they answer "what now".
- **For a fork, say whether the shared layer is up to date** or was changed by a parallel session since this session last read it.
- **Dashboard:** include `http://localhost:8787/#projects?task=<name>` only when `curl -sf --max-time 1 http://localhost:8787/health` succeeds (or `$MISSIONCACHE_DASHBOARD_URL`).
- **Update:** read `~/.missioncache/update-check.json` and show its `command` only when `update_available` is true and `checked_at` is under 7 days old. Never fetch anything yourself.
<!-- claude-code-only -->
- **PreCompact warning:** when `~/.claude/hooks/state/last-precompact-error.json` exists and its `task_name` is this project or empty, show its `timestamp` and `reason` near the top, because the last automatic snapshot did not save. Then delete the file so the warning shows once.
<!-- /claude-code-only -->

## During the Session

- When a Waiting-on reply arrives or an action item gets done, act on what it gates and record it at the next `/missioncache:save`. Propose closing an action item before calling `update_action_item(status="done")`.
- Open the full context file, or one section by its line in the digest's `section_index`, only when the user asks or the task needs it. Older history is in `<name>-journal.md`: grep it, never load it.
<!-- claude-code-only -->
- When the user names the checklist task they are on ("let's do 54a"), call `set_active_missioncache_tasks(project_name=..., task_numbers=["54a"], session_id=...)` so the statusline shows it. Pass several numbers for parallel work. Never guess one from the first unchecked box: a wrong task is worse than none. Marking the task done clears it, and `clear_active_missioncache_tasks` clears it when focus moves on.
<!-- /claude-code-only -->

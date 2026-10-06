<!-- Written 2026-10-06 by the lead session after two days of running /missioncache:lead over 10-11 parallel projects. Code references verified against v2026.10.05. -->
# Lead Session Improvements - Build Plan

Three phases that turn the lead from a relay into a reader of a shared event log. Phase 1 ships in two steps:

- **1a:** the `events` table, recording from the write tools, `missioncache-db events list`, the `get_events` MCP tool, the watermark and the 90-day prune. It changes no session's behavior.
- **1b:** `lead_notice` in write responses, the rule that peers send it verbatim, and `--delta` reading events instead of a snapshot file. It comes after 1a has run for a few days.

What shipped in 1a differs from the Phase 1 text below in these ways:

- **Where recording happens.** Database writes (action items, due dates, the dashboard's Waiting-on resolve, complete, reopen, rename) record inside missioncache-db, so the dashboard and the CLI produce events too. Markdown-only writes record from the MCP server.
- **Writer and schema.** The writer is `record_events(db, project, items)` with items as `(kind, what, section)`, and rows also carry a nullable `task_id`, which follows a project across a rename.
- **Reading.** `get_events` and `events list` take `after_id`, an exact cursor for 1b's delta. `since` is normalized and validated.
- **Watermark.** It uses the newest event `id`, not `created_at`.
- **Kinds.** A project reopen is `reopened`. There is no `task_reopened`, because no tool un-ticks a task.

Event text is work data in practice. It lives only in the local `tasks.db`, never in this repo, its tests or its docs.

## What two days of use showed

1. The lead is a relay today. About 40 notices a day arrive as free text, and the lead re-types each one for the user and for a tracking session. Nothing is stored. When the lead session closes, the day's notices are gone.
2. The delta loop adds little. On one day 8 of 14 half-hour ticks were empty, and most real changes had already arrived as peer notices.
3. The meeting trigger misses. A 30-minute tick with a 15-minute window skipped the one meeting that mattered that day.
4. The default `max_rows_per_project=3` clips waiting-on rows, so a delta that compares row keys reports old rows as new.
5. Things the user must act on (unsent drafts, open decisions) appear only as a clause inside a peer notice. There is no list of them.
6. Nobody joins waiting-on rows by person. One person owed answers in five projects and the lead found that by reading.
7. The same question was open in two projects, and a finding in one project answered a wait in another. Peers do not hear about each other.
8. Routing rules (which projects are personal, which topics belong to which session) were learned mid-day from peer messages and kept in the lead's memory file.

## Verified facts the design rests on

- Peers notify the lead by prompt instruction only (`rules/missioncache.md`, "Telling the lead"). There is no code path.
- `attach_lead_session(response)` in `mcp-server/src/mcp_missioncache/helpers.py` takes only the response dict. It has no project name or change text. It is called from 3 sites in `tools_docs.py`, 3 in `tools_tasks.py`, and through `_with_live_sessions(response, project)` in `tools_pm.py` (7 call sites).
- No event or change-log table exists. Recent Changes lives only in markdown. `task_updates` is the user-notes table with its own tool.
- `portfolio_watermark()` (`missioncache_db/portfolio.py`) hashes task and action-item timestamps, `project_state`, and file mtimes.
- The dashboard is FastAPI plus one `index.html` with hash-routed views. `/api/today` calls `build_portfolio`. `/api/stream` sends a `portfolio` SSE event when the watermark moves. It binds `127.0.0.1` with no auth.
- `/missioncache:brief --delta` keeps its snapshot as a JSON file under `$MISSIONCACHE_ROOT/brief-state/`.

## Core decision

The write tools record events. Peers keep messaging the lead, but the record no longer depends on a lead session being alive, and every consumer (the lead, the dashboard, a tracking session) reads the same rows.

## Phase 1 - event log

1. **Schema.** New table `events` in `tasks.db`: `id`, `project`, `kind`, `what`, `ticket`, `section`, `source_session`, `created_at`. A new table, not an extension of `task_updates`, because that table holds user-authored notes.
2. **Writer.** `record_event(project, kind, what, section=None, ticket=None)` in a new `missioncache_db/events.py`. Best-effort: it never fails a successful write. Call it at the same sites that call `attach_lead_session` / `_with_live_sessions`.
3. **Kinds**, derived from the calling tool and what it changed: `recent_change`, `waiting_added`, `waiting_resolved`, `task_done`, `task_reopened`, `action_item`, `due_date`, `moved`, `renamed`, `completed`. `what` is the summary line the tool already has (the Recent Changes line, the row text, the item text).
4. **Ticket.** The project's ticket label when set, else the first `[A-Z]+-\d+` match in `what`, else null.
5. **Watermark.** Add `MAX(events.created_at)` to `portfolio_watermark()` so the dashboard SSE fires on a new event.
6. **Ready-made notice.** Write responses carry `lead_notice`: the one-line text the peer sends to the lead (`<project>: <what>. Ticket <key or none>.`). Update `rules/missioncache.md` so peers send it verbatim. This ends free-text notices.
7. **CLI.** `missioncache-db events list [--since ISO] [--project NAME] [--kind K] [--json]`.
8. **MCP.** `get_events(since, project, kinds)` so any session can read the log.
9. **Brief.** `--delta` reads events since the last tick instead of diffing a snapshot file. Row clipping stops mattering. Step 5 of `commands/lead.md` ("record what changed") goes away.
10. **Retention.** `prune` drops events older than 90 days.
11. **Tests.** Writer and kinds in `mcp-server/tests`, watermark in `missioncache-db/tests/test_portfolio.py`, CLI in `missioncache-db/tests`.

Done when (1a): a write through any recording path produces one event row per item it changed, with the right kind, the watermark moves, and `events list --after-id` returns it.

Done when (1b): a `--delta` tick reports it with no snapshot file.

## Phase 2 - Lead page in the dashboard

1. **API.** `GET /api/lead/events?since=` and `GET /api/lead/today` next to `/api/today`. "Waiting on you" reuses `on_me` from `build_portfolio`.
2. **View.** `data-view="lead"` beside `attention`. Two blocks only in this phase:
   - **Waiting on you**, at the top: the user's open items with due dates, and waiting-on rows where the user is the `who`.
   - **Today's timeline**: events grouped by project, colored by kind, newest first, live through the existing SSE stream.
3. **Do not duplicate Attention.** Attention shows state. The Lead page shows flow: what happened and what needs the user.
4. **Tests.** Endpoint tests beside `test_pm_endpoints.py`.

Done when: with no lead session running, the page shows today's events from several projects and updates within one SSE interval of a new write.

## Phase 3 - lead behaviors

Each item is independent. Suggested order.

1. **Project scope field.** A `scope` on the project (`work` / `personal`, free text allowed) in the DB, settable by CLI and MCP. Routing filters read it instead of the lead's memory. Events carry it.
2. **Notice tiers.** The lead interrupts the user only for: a meeting starting in 10 minutes, a blocker on a project with a near due date, an item only the user can clear. Everything else goes into one digest (hourly, or on demand). The page is where the rest lives.
3. **Meeting reminders.** At `/missioncache:lead`, schedule one-shot wakeups 10 minutes before each remaining meeting from the agenda, instead of hoping a tick falls in the window. Offer it once, like the loop.
4. **Meeting prep by attendee.** For each reminder, join the agenda event's attendees (when the calendar source provides them) or the title against waiting-on `who` values across all projects, and list what each person owes. Needs item 5.
5. **Per-person view.** `GET /api/lead/people`: waiting-on rows grouped by `who` across projects, with oldest age. A third block on the Lead page.
6. **Pending drafts.** A `draft_pending` event kind, written when a session saves a draft for the user to send, cleared by a `draft_sent` event. Feeds "Waiting on you". Needs a small rule for sessions on when to write it.
7. **Overlap hints.** When a new `waiting_added` or `recent_change` shares a ticket key or a `who` plus a strong term with an open row in another project, the lead tells the user and may send that project's session an information-only line. Peers act on nothing in it.
8. **Escalation ladder for stale waits.** 7 days: offer a reminder draft. 14 days: add it to the next meeting prep with that person's manager, when known. 30 days: offer to close it. Weekly list on the page.
9. **Planned vs reactive.** Tag each event as tied to a planned item (the project has a due date or a ticket label) or reactive. Show the split per day and week. Local only.
10. **Day close and morning open.** On `/missioncache:unlead` or on request: ask live sessions to save, then write a day summary from the event log. On `/missioncache:lead` in the morning: the first brief includes "since you left" from events.

## Small fixes found on the way

- `who` parsing in the waiting-on table produced a truncated token from a row naming an `@group` handle, and `Name'S` from a possessive (`Name's`). Check the title-casing and the token split.
- After a fork or a move, a session bound to the new project still carried the old project's title in `live_sessions`, so the brief showed project and title that did not match. The title hook should follow the binding.
- A project's due date stayed stale after its real date moved, because the date lived in prose. Not a code bug. A `due_date` event from another project's `imported_event` could prompt for it.
- The brief skill should always call `get_portfolio` with a row cap high enough for the delta, until the delta reads events.

## Deferred, needs its own decision

- **Remote access to the dashboard.** It binds `127.0.0.1` with no auth. Exposing it is public-facing and hard to undo. Do not change the bind or CORS in this work. Until an auth design exists, remote value stays in the lead session's chat.

## Constraints

- A peer session carries no user authority. Any line the lead sends a peer is information only.
- Per-person data (who owes what, for how long) stays local and is never written to a shared surface.
- Event text is work data. It lives in the local DB only and never enters the repo, tests, or docs. Test fixtures use invented projects.
- The lead's memory rule that forwards each notice to a tracking session stays in force until that session reads `get_events` itself. Then it is retired.
- Related backlog: a mod that sets the lead's update pace. Fold items 2 and 3 of Phase 3 into it or sequence them after it.

# The MissionCache mod and the lead board: internals

For maintainers. This covers the lead board's data layer and what the Claude Code mod API was measured to do. It assumes [`architecture.md`](../architecture.md) for `hooks-state.db`, `project_state`, `term_sessions` and the event log.

## The pieces

| Piece | Where | What it does |
|-------|-------|--------------|
| `agent_status` table | `hooks-state.db` | One row per session: its state, why it needs you, the prompt it was given, the tool running long, context use, cost, turns today, the start of its last answer. |
| `lead_board.report_agent_status` | missioncache-db | Upserts a row. `state_since` moves only when the state changes. |
| `lead_board.build_lead_board` | missioncache-db | Everything the board draws, in one call. |
| `lead_board.focus_terminal` | missioncache-db | Brings a session's iTerm2 tab to the front. |
| `report_session_status`, `get_lead_board`, `focus_terminal` | MCP server, `tools_pm.py` | Thin wrappers. See [mcp-tools.md](../mcp-tools.md). |

`agent_status` is separate from the statusline's `session_state` on purpose. The statusline owns that table and rewrites it on every render. `agent_status` is written by the mod, once per state change, so a session that sits idle writes nothing.

## The board

The unit is the project, not the session. A terminal already shows whether an agent is working. What the board adds is where each project stands: progress from the tasks file, the item a session marked active, the last significant change, the last save, the oldest thing waiting on someone else, and what is next.

- **Which projects.** `focus` keeps projects with a live session or an event in the last 12 hours, and lists the rest under `quiet` with their newest change. Hours rather than "today", so a day that runs past midnight stays one day. `at_risk` alone no longer brings a project in: on a real install it marked 17 of 18 projects. `all` keeps every active project.
- **Counts.** `counts` carries the numbers every reader shows (needs you, working, sessions, changes in the window), so the band and the Lead view never disagree. `build_portfolio` alone is not enough: it lists only projects with action items, Waiting-on rows or a due date, so a project with a live session and none of those would be missing.
- **Last save.** Read from the context file's `**Last Updated:**` header, which every `update_context_file` call stamps. There is no `saved` event: one per write would bury the change log the lead's `--delta` tick and the feed read.
- **Last significant change.** The project's newest event. Every event kind counts.
- **The lead.** The designated lead session is never a project's session, and its own writes are left out of the feed.
- **Order.** Needs you, then working, then compacting, then idle, then no session. Inside each, at-risk projects first, then the most recent activity.
- **More than one session on a project.** The card shows the session that most needs you, by the same order. `needs_you` lists every live session waiting on you, so a second one is never hidden behind the card.
- **The feed cursor.** `last_event_id` is the newest id the answer covered, counted before the lead's own rows are dropped. A backlog longer than one page comes in over several polls, oldest first, with no gap, and `has_more` says another poll is due. The first call, with no cursor, shows today's newest page and streams forward from there: older events from today beyond that page are not paged back.

### Polling

`lead_board_watermark` is `portfolio_watermark` plus the `agent_status` writes, the newest active-task pointer file (the MCP server rewrites those without any DB trace), and the lead's session id. Each `agent_status` row carries a `version` that every write bumps, so two state changes in the same second still move the token. When `hooks-state.db` cannot be read the token never repeats, so the next poll rebuilds and the answer's `status_error` says why. With the caller's previous watermark unchanged, `build_lead_board` returns `{changed: false}` without building anything. Measured on a real install with 17 projects in focus: a full build takes about 150ms, an unchanged poll about 3ms.

A session that dies without a last report changes no stored value, so the watermark cannot see it leave. The mod therefore ignores the watermark on about one poll a minute, and `r` forces a rebuild.

The status table is kept out of `portfolio_watermark` itself. The dashboard's stream compares that token, and a table written on every turn of every session would make it rebuild the Attention view each time anyone typed.

### Today's picks

The lead session writes up to three picks at the end of each full brief through `set_lead_picks`. The lead check sits inside the write statement, so a lead replaced while its brief was finishing cannot overwrite its successor's picks. They are one row in `hooks-state.db`, replaced on every write, and `build_lead_board` returns them as `picks` with the time they were written. The watermark reads the row's items as well as its time, so two writes in the same second still move it. The dashboard shows picks from an earlier day as stale rather than hiding them.

### Free text

`prompt`, `now`, `last_answer` and `needs_reason` are reduced to one line without control characters (C0, DEL, C1, ESC included) or format characters (bidi overrides and isolates, zero-width marks), and clipped server-side to 80, 60, 60 and 80 characters. They live only in the local `hooks-state.db`. Every string the board returns goes through the same cleaning, because context files and the event log can carry these characters too, and iTerm2 draws an isolate as a replacement glyph that scrambles the row.

### Jumping to a tab

The statusline records each session's `TERM_SESSION_ID` in `term_sessions`. In iTerm2 that value is `w0t1p0:<UUID>`, and the UUID is the AppleScript `unique id` of the session. `focus_terminal` takes the newest row for the session (a session that moved tabs has several, because the table is keyed by terminal), and runs an osascript that walks windows, tabs and sessions for that id. The UUID is passed as an argument, never spliced into the script. macOS may ask once for Automation permission for the process that runs the MCP server. On the machine this was measured on no prompt appeared, which may only mean it was already granted. On any failure the answer carries osascript's own error text, so a refusal is visible. The MCP tool answers only the lead session: raising another app's window is the board's action, not something a worker session, or a model following text it read, should do. The dashboard's Lead view calls the same function through `POST /api/lead/focus` from a "go to tab" button on each live session, shown only when `focus_available` is true. It moves a window and never a record, so the view stays read-only. The body is JSON, so a page on another origin needs a CORS preflight, which the allow-list refuses. The first click may make macOS ask for Automation permission for the dashboard's Python.

### Pruning

`prune_session_state` sweeps `agent_status` by the same rule as `project_state`: older than the cutoff and not proven alive. The counts dict has an `agent_status_rows` key.

## The mod

`hooks/mod/missioncache.tsx` is listed under `modules` in `hooks/hooks.json`, beside the Python hooks. It has two jobs.

- **The reporter**, in every session. `turn.start` writes `working` with the prompt, a permission prompt or an `AskUserQuestion` writes `needs_you`, the next tool call clears it, a tool that runs past 5 seconds writes `now`, and `turn.complete` writes `idle` with context, cost, turns today and the start of the answer. Between a turn's start and end, a write goes out only when the state or its reason changed. Subagent turns are skipped, and so are the mod's own MCP calls, which raise `tool.call` under their `mcp__plugin_missioncache_pm__*` names.
- **The band**, in the lead session only. `/missioncache:lead` starts it, `/missioncache:unlead` stops it, and `session.start` starts it again after a resume when `get_lead_board` says this session is the lead. It polls every 5 seconds with the previous watermark, and every twelfth poll without one. `/missioncache:lead` returns before the model has run `lead set`, so the band waits up to two minutes for the role and closes only when another session takes it.

`board.tsx` draws two rows: the counts (needs you, working, sessions), the first project that needs you, and a link to the dashboard's Lead view. The board itself lives in the dashboard: a band capped at half the terminal could not hold seventeen projects readably, and the first build, a table in the band, wrapped names and folded most projects away. `parse.ts` holds the pure helpers. `0` hides the band until the next poll that finds a change.

**Tests.** `claude plugin test .` runs `hooks/mod/tests`, and CI runs it beside `plugin validate`. The tests mount the band on the terminal surface, so a prop the surface does not take fails a test. In a live session the same mistake draws nothing at all: the engine drops a tree with an unknown prop as a whole, while the command that asked for it still reported success. Beneath the mod, a test answers each event the mod calls down to. `mcp.call` and `session.usage` answer `{ value }`, `tool.call` answers `{ result }`, and `turn.start`, `turn.complete` and `ui.render` answer their result directly.

**Managed installs.** `classic.PermissionRequest` and `session.compact` do not reach a plugin a person installed where an organization manages Claude Code. There the board never shows `needs_you` for a permission prompt, or `compacting`.

## What the mod API was measured to do

Measured on Claude Code 2.1.302 in iTerm2 3.7, fullscreen TUI, 230 by 53 cells, with a probe mod.

**Surfaces**

- The `AbovePrompt` band got `maxRows` of 12. The engine caps it at half the terminal, prompt included, and the statusline takes part of that. A taller tree scrolls.
- A `Pane` has two placements, `dock` beside the transcript and `inline` above the prompt. Nothing covers the whole screen.
- A docked pane shares terminal rows with the transcript. iTerm2 orders bidi per row, so a transcript row that opens with Hebrew mirrors the pane's part of that row. The band takes full rows and avoids this, which is why the board is a band.

**Right-to-left text inside the band**

- iTerm2 picks each row's direction from its first strong letter.
- `Text dir="rtl"` does nothing on a terminal that orders bidi itself.
- Unicode isolates (RLI, PDI) and embeddings (RLE, PDF) draw as a replacement glyph and scramble the whole row.
- A number that follows Hebrew in the same row joins the Hebrew run and moves.
- The rule that works: free text never shares a row with other columns. It gets a full row that opens with a Hebrew label when the text has Hebrew, so the row reads right to left even when the text starts or ends with a Latin word. A trailing English label fails, because it joins a Latin word that ends the sentence.

**Keys and focus**

- A Button `hotkey` letter works only while the band holds focus: `ctrl+x tab` or a click. A bare digit also works from an empty composer.
- The engine draws a Button with brackets around its label.

**Events**

- `command.run` sees plugin commands with the namespace and without the slash: `missioncache:lead`, `missioncache:unlead`.
- `classic.PermissionRequest` fires in a mod, and a `tool.call` hook's `await next(e)` returns after the person answers.
- `turn.complete` carries `answer` and `durationMs`. `$.session.usage()` gives `context.percent`.
- `$.ui.ask` works from a command handler and from inside a `tool.call` hook mid-turn. The tool waits for the answer.
- After a resume, `$.mcp.call` in `session.start` can fail because the MCP server is not up yet. Retry it.
- A `$.mcp.call` result stays out of the model's context. Ten `get_portfolio` calls returned 177,830 characters and the session's token estimate stayed at 141,675 before and after.
- The MCP server's name for `$.mcp.call` is `plugin:missioncache:pm`. Results carry a text block, not `structuredContent`, and the mod's own call raises a `tool.call` event the reporter must ignore.

**Messages between sessions**

- `$.session.send` to a session in another `claude` process returns `isDelivered: true`.
- The receiver's `session.receive` gets `origin: {kind: "peer", plugin: "<sender's plugin>"}`. The engine documents `plugin` as what the sender claims, so nothing may trust it.
- The text arrives wrapped in a `<cross-session-message from=... from-name=... from-mode=... from-plugin=...>` element. An envelope sits on a line of its own inside it, never at the start of `e.text`.
- Returning `{consumed}` keeps the message away from the model, and a `$.ui.ask` from that hook shows the confirm.

**Older Claude Code**

- A `hooks.json` with both `hooks` and `modules` passes `plugin validate` on 2.1.280 and 2.1.139. One with only `modules` fails on 2.1.139, so the existing hooks stay in the same file.

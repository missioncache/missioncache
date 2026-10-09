# CLI Reference internals

For maintainers. The user guide is [../cli.md](../cli.md).

`missioncache-db` is the command-line entry point to the task database. Every install path (`uvx missioncache-install`, marketplace, manual) puts it on your PATH; maintainer checkouts with editable installs can equivalently run `python3 -m missioncache_db`.

Day-to-day project work does not need this CLI. The slash commands (`/missioncache:new`, `/missioncache:load`, `/missioncache:save`, ...) and the [MCP tools](../mcp-tools.md) behind them cover creating, resuming, and updating projects from inside your AI tool. A handful of operations are deliberately CLI-only, though: multi-step flows like cross-machine import, and rare maintenance jobs that would make poor MCP tools. This doc covers those. Running `missioncache-db` with no arguments prints the full built-in usage, including the everyday commands not detailed here.

## Cross-machine sharing: export, import, config

Move a project between machines (say, a work laptop and a personal desktop) as a self-contained bundle. The bundle carries the project's MissionCache markdown files plus a `missioncache.json` manifest with the project's identity and its references to things that live outside the bundle: the repo it belongs to (recorded as a git remote, which is portable, rather than a local path, which is not), Obsidian-style vault links, and absolute paths embedded in the docs.

### export

```bash
missioncache-db export <name> [--out <path>] [--no-time] [--json]
```

Builds the bundle for an active project and prints where it landed, how many files it contains, and which external references the manifest recorded. Flags:

- `--out <path>` - write the bundle somewhere other than the default location.
- `--no-time` - omit the origin machine's total tracked time. Time tracking is per-machine either way; when included, the value is display-only on the importing side and never merges into local time data.
- `--json` - print the raw manifest instead of the summary.

### import

```bash
missioncache-db import <bundle> [--repo <path>] [--force] [--rewrite-paths] [--dry-run] [--json]
```

Imports a bundle: creates the project locally, or updates it when the same project (matched by its stable origin UUID) was imported or exported here before. A name collision with an unrelated local project aborts rather than overwriting it, even with `--force`. Fields the bundle does not carry keep their local values.

After placing files, import reconciles the manifest's external references against this machine and prints a three-bucket alignment report: `resolved` (found locally), `needs mapping` (exists in the manifest but this machine does not know where it lives - each entry comes with a ready-to-run `config set-path` fix line), and `missing`. The exit code is 0 only when everything resolved, so the command is scriptable. Flags:

- `--repo <path>` - bind the project to this local repo path instead of resolving the manifest's git remote.
- `--force` - overwrite the local project files when they differ from the bundle (without it, a differing re-import aborts so local edits are never silently lost).
- `--rewrite-paths` - rewrite absolute paths embedded in the docs to this machine's equivalents, using the path map below.
- `--dry-run` - print the full report without writing anything.
- `--json` - print the report as JSON.

### config (the per-machine path map)

```bash
missioncache-db config set-path <repo|vault|anchor>:<name> <localpath>
missioncache-db config list-paths [kind] [--json]
missioncache-db config show
missioncache-db config seed [--dry-run]
```

The path map (`~/.missioncache/machine.json`) tells import how portable identifiers translate to THIS machine: which local checkout a git remote lives at (`repo:`), where a named vault is (`vault:`), and what a named path prefix expands to (`anchor:`). You rarely write it by hand - `config seed` pre-fills it from your registered repos' git remotes, and the import report's fix hints print the exact `config set-path` command for anything left.

## Keyword management (tags)

```bash
missioncache-db add-keyword <keyword>
missioncache-db remove-keyword <keyword>
missioncache-db list-keywords
missioncache-db backfill-tags
```

Projects get auto-tagged by matching the parts of their name against a keyword list (a project named `kafka-consumer-fix` picks up the `kafka` tag). `add-keyword` extends the built-in list with your own stack's vocabulary; `remove-keyword` removes custom keywords (the built-in defaults cannot be removed); `backfill-tags` re-derives tags for existing projects after the list changes - new keywords only affect newly created projects until you run it.

## Maintenance: prune and cleanup

```bash
missioncache-db prune [days]
missioncache-db prune-sessions [--days N] [--dry-run]
missioncache-db cleanup [--dry-run]
```

`prune` archives completed projects older than the retention period (default 30 days, or pass a number explicitly). Archived projects drop out of the completed lists but stay in the database.

`prune-sessions` deletes the per-session state left behind by sessions that are gone: the pid record (`~/.claude/hooks/state/session-pids/<id>.json`), the project pointer (`projects/<id>.json`), the `project_state` binding row, and the `lead_session` row when the designated lead is gone. Nothing removes these on session exit, so they accumulate one set per session for the life of the install - an install a few months old can hold a couple of thousand pid records. Run `--dry-run` first; it prints the same counts without deleting.

A record goes only when it is older than `--days` (default 7, minimum 1) and its session is not still running. Pid records are also kept while the session's transcript was touched in the last 30 minutes, so a session that closed a moment ago is not mistaken for a live one. The `lead_session` row is the exception: it has no age cutoff and goes as soon as its session is gone.

`cleanup` is the broader housekeeping pass, in four phases: archive orphaned active tasks whose files no longer exist on disk, move stray repo-local MissionCache files into the centralized `~/.missioncache/` layout, resolve duplicate task names, and normalize non-standard paths. Run it with `--dry-run` first - it prints exactly what each phase would touch.

## Project management (action items, stakeholders, tickets, due dates)

`<task>` accepts a numeric task id or a project name.

```bash
missioncache-db action-item add <task> "send the numbers" --from Alex --due 2026-08-01 --source "weekly sync"
missioncache-db action-item list [<task>] [--status open|done|dropped] [--owner me] [--overdue] [--due-within 7]
missioncache-db action-item done <item_id> [outcome...]
missioncache-db action-item update <item_id> [--status S] [--due YYYY-MM-DD|none] [--owner WHO] [--what W] [--from WHO] [--notes N] [--source SRC]
missioncache-db stakeholder add <task> <name> [--role ROLE] [--notes NOTES]
missioncache-db stakeholder remove <task> <name>
missioncache-db stakeholder list <task>
missioncache-db ticket add <task> <label> [--url URL] [--system SYS] [--status STATUS] [--notes NOTES]
missioncache-db ticket remove <task> <label>
missioncache-db ticket list <task>
missioncache-db due-date <task> <YYYY-MM-DD|none>
```

SQLite is the source of truth; every mutation also re-renders the read-only `## Action Items` / `## Stakeholders` / `## Tickets` sections (and the `**Due:**` header line) in the project's context file, plus a Recent Changes line. The same write path serves the MCP tools and the dashboard, so all three surfaces stay consistent. `action-item list` without a task spans every active/paused project (each item carries its project name). Ticket references are system-agnostic: `label` + `--url` is the whole contract; when `--url` is omitted a JIRA-style label gets its URL from the dashboard's prefix map if one matches. An `owner` of `me` means your own commitment; any other name is a follow-up you are tracking on someone else.

## Lead session (the project-manager role)

```bash
missioncache-db lead set "$CLAUDE_CODE_SESSION_ID"   # designate this session
missioncache-db lead mark --latest                   # baseline the delta cursor at the newest event
missioncache-db lead mark --event-id 812             # stamp the cursor after a tick (no id: time only)
missioncache-db lead show [--json]                   # who the lead is, whether it still runs, and the cursor
missioncache-db lead stop                            # end the role
```

One session at a time can be the lead: the project manager that `/missioncache:lead` turns a session into, and `/missioncache:unlead` ends. `set` replaces any previous lead, and the replaced session finds out on its next delta brief, whether that comes from an opted-in loop or from a brief you ask for. The lead carries the fixed session title `missioncache-lead`, applied by the title hook, so every working session that saves context or a PM item can address it without a lookup (the write tools return a `lead_session` field saying whom to notify). `show` distinguishes a running lead from a designated row whose process is gone; the latter is dropped by `prune-sessions`. The role lives in `~/.claude/hooks-state.db`, so it survives compaction.

The same row carries the delta cursor: `last_event_id`, the newest change-log event the lead's `--delta` tick reported, and `last_tick_at`, when it ran. `mark` stamps both (`--latest` reads the newest event id from `tasks.db`, `--event-id` takes one, neither moves only the time) and only the designated session can stamp, so a replaced lead cannot move the new lead's cursor. `set` on the same session keeps the cursor; a new lead starts with none, and its first tick baselines itself. `show --json` returns the cursor and `is_me`, whether the designated session is the one running the command.

## Event log

```bash
missioncache-db events list --since 2026-10-06 --json   # today's changes, every project
missioncache-db events list --after-id 812              # what is new since row 812, oldest page first
missioncache-db events list --project my-proj --kind task_done,waiting_resolved
missioncache-db events prune --days 90
missioncache-db events clear my-proj                    # one project's history
```

The `events` table in `tasks.db` holds one row per change. Two layers write it:

- **The database writes record inside missioncache-db,** in the same transaction as the change where there is one: action items (added, updated, done, dropped), the project due date, the dashboard's Waiting-on resolve, and complete / reopen / rename. So a session, the dashboard and the CLI all produce these.
- **The markdown-only writes record from the MCP server's file layer:** each Recent Changes line, each Waiting-on row added or resolved through `update_context_file` (from the rows the write actually removed, so a match that hit nothing records nothing), an imported event's heading, each ticked task with its full line, and a move between projects (one row on each, and nothing when the move matched nothing). That recording is best-effort: a failure is logged and never fails the write.

Each row carries the project name and its `task_id`, so history stays readable after a delete and follows a project across a rename. The ticket is the project's first `tickets` row, else its legacy `jira_key`, else the first `ABC-123`-shaped key in the text, else null. `source_session` is `CLAUDE_CODE_SESSION_ID` when a Claude Code session made the change, null otherwise. A new event moves `portfolio_watermark`, so the dashboard stream fires. `since` is normalized to the stored local-time shape (an ISO `T`, fractions and an offset are folded or dropped) and anything else is refused. Retention is manual: `prune` and `events prune` delete rows older than 90 days. The plan behind it is [`lead-dashboard-plan.md`](../lead-dashboard-plan.md).

## Calendar agenda

```bash
missioncache-db agenda                          # today, human-readable
missioncache-db agenda --date tomorrow --json   # machine-readable
missioncache-db agenda --source Work --no-cache # one source, skip the cache
```

Reads the `calendar` key of `~/.claude/missioncache-dashboard-config.json` and prints the events for one day. Two source kinds: `ics` (a URL, a file path, or a glob) and `command` (an argv list that prints JSON events on stdout, which is how you wire a tool MissionCache must not depend on). A `{date}` placeholder in any command argument is replaced with the ISO date being asked for. The command runs without a shell and its first element must be an absolute path that exists, the same rule the statusline addons follow.

With nothing configured it says so and exits 0. That is deliberately different from an error: callers render no calendar block at all rather than an empty one.

Nothing here raises. A dead URL, a timeout, unparseable ICS or a command that exits non-zero becomes that source's `status` and a short reason on the trailing `sources:` line, and every other source still renders. Check that line first when a calendar looks wrong, because a hand-typed ICS URL is the most likely thing to be broken.

Recurring events are answered with a single-day hit test rather than a full expansion. `FREQ=DAILY|WEEKLY|MONTHLY` with `INTERVAL`, `COUNT`, `UNTIL`, `BYDAY` (weekly), `BYMONTHDAY` (monthly) and `EXDATE` are supported, and a `RECURRENCE-ID` override replaces its instance instead of duplicating it. Anything else, including `FREQ=YEARLY` and ordinal weekdays like `BYDAY=2TU`, is counted on the `sources:` line as rules not understood rather than dropped in silence.

On Windows, stdlib `zoneinfo` has no system database to read, so each `TZID` falls back to the local zone and the source says which name it could not resolve. The dashboard package carries `tzdata` for its own process; if you drive this CLI directly there, `pip install tzdata` in that environment.

## Health / diagnostics

```bash
missioncache-db health
```

Scans every active project's context file and reports, per project: a stale `Last Updated` (older than 14 days), stale Waiting-on rows (`Since` older than 7 days), a context file over the 100KB size budget, missing core sections (`Description`, `Gotchas`, `Waiting on`, `Next Steps`, `Recent Changes`), and a Recent Changes section over its 12-entry cap (meaning a journal rollover is pending on the next save). A project directory without a context file is itself a finding. Projects with a DB row also get the PM checks: overdue open action items, a project due date within 7 days (or past), and items open more than 14 days with no due date.

Three of the findings are structural rather than stale: an unbalanced code fence (naming the line the opener sits on), duplicate `## <name>` sections, and dated Recent Changes entries stranded outside the section. `repair` fixes the last two and only reports the fence.

Report-only: exit code is always 0, warnings or not. The thresholds are constants in `missioncache_db/context_health.py`, not config keys. The same warnings surface per-project in the `/missioncache:load` digest, so `health` is mainly the fleet-wide sweep.

## Repairing a damaged context file

```bash
missioncache-db repair                    # every active project, dry run
missioncache-db repair --all              # active + completed, dry run
missioncache-db repair qa-guild       # one project (searches completed too)
missioncache-db repair --all --apply      # actually write
```

Dry run by default - it prints what it would do and changes nothing until you pass `--apply`.

What it fixes, all mechanical:

- **Duplicate sections** are merged into the first one of their name, bodies concatenated in document order. Until they are merged, `update_context_file` refuses to write into that section at all, because picking one of five `## Key Files` is a guess.
- **Stranded Recent Changes entries** are moved back under `## Recent Changes`, re-sorted newest-first, and the 12-entry cap then rolls the overflow into the journal the way it should have. Duplicate journal pointer lines collapse to the single one at the section bottom as part of this. Only writer-shaped `### YYYY-MM-DD HH:MM` headings are moved; a hand-written dated heading inside someone's prose is left alone (the health warning says so rather than pointing you here).

It also runs the cap on its own, so a file whose only problem is an overdue rollover gets one even with no structural damage. That is the most common finding on a healthy fleet.

It does not close an unbalanced code fence (it reports the line and stops) and it does not delete stray pasted-output sections, which you drop yourself with `update_context_file(sections_remove=[...])`.

Writes go through the same sidecar lock, journal-first, atomic-replace path every other context writer uses, so a repair cannot interleave with a live session's save.

## Editor-extension snapshot

```bash
missioncache-db extension-state [--dir PATH]
missioncache-db extension-project <task_id>
```

`extension-state` is the one-call JSON snapshot consumed by the MissionCache editor extension (VSCode/Cursor): every active project newest-first, each with task progress, resolved tasks/context file paths, the context file's last-save time, fork parent, and a `dir_match` flag when `--dir` (walked up to its git root) equals the project's registered repo path, plus update-availability from `update-check.json`. The output is machine-oriented. Humans want `list-active`. The `schema` field versions the payload - the extension refuses shapes it does not know.

`extension-project` (`get_extension_project` in `missioncache_db`) is the sidebar's outline for one project, looked up by the numeric id `extension-state` returns: the tasks from the tasks file (each with its 1-based line, checklist number and the `## ` section it sits under), plus Next Steps and Waiting on from the context file, each item with its line so a click can open the file there. An unknown id comes back with `found: false`.

## Session and shell helpers

```bash
missioncache-db current-session [task_id]   # working time of the current session, formatted
missioncache-db list-names [active|completed] # project names only, one per line (for shell completion)
missioncache-db encode-cwd [path]           # Claude Code's projects-dir key for a path (default: cwd)
```

`current-session` prints the WakaTime-style working time of the current session from unprocessed heartbeats, for one task when given an id. `list-names` prints active project names by default, or projects completed in the last 90 days with `completed`. `encode-cwd` prints the key Claude Code uses for a directory under `~/.claude/projects/`, correct on Windows and for dots and underscores, which is what the slash commands' bash blocks use instead of a `sed` on `pwd`.

## Bulk repo registration

```bash
missioncache-db add-repos-glob "~/work/*"
```

Registers every directory matching a glob pattern as a tracked repo in one shot (hidden directories are skipped). Quote the pattern so your shell does not expand it first. Useful on a fresh machine instead of one `add-repo` call per project.

## Everything else

The rest of the CLI (`list-active`, `create-task`, `set-jira`, `set-category`, `complete-task`, `task-time`, and friends) overlaps what the MCP tools and slash commands already do conversationally; the CLI variants exist for scripts and quick shell checks. `missioncache-db` with no arguments prints the complete usage.

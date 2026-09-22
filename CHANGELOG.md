# Changelog

All notable changes to MissionCache, newest first. Entries dated before the 2026-06 rebrand name the project as it was then (orbit, orbit-db, mcp-orbit).

**Updating:** `uvx --refresh missioncache-install@latest --update` brings everything you have installed current. Your data in `~/.missioncache/` is never touched by an update.

**Writing an entry:** one line per change, 25 words or fewer, saying what changed for the person reading. Mechanism, measurements, and why it was hard belong in the commit message, not here. Affected packages in parentheses at the end.

## Unreleased

- `/missioncache:lead` no longer loops on its own. It asks how often to check and for how long, stopping after 7 hours by default. (plugin, hooks, docs)
- `/missioncache:brief` takes `--until <ISO>`, which ends the lead loop at that time. (plugin)

## 2026-09-16

Published package versions: missioncache-db 1.0.27, mcp-missioncache 1.0.33, missioncache-dashboard 1.0.22, missioncache-install 1.0.17. Claude Code plugin 1.0.26. missioncache-auto is unchanged.

- New `/missioncache:brief` reports across all your projects at once: what is urgent, who you are waiting on, and a suggested order around today's calendar. (missioncache-db, mcp-missioncache, missioncache-dashboard, missioncache-install, plugin, docs)
- New `/missioncache:lead` makes one session the manager, and every other session that saves reports its changes to it. Claude Code only. (missioncache-db, plugin, docs)
- New `get_portfolio` tool returns the cross-project rollup the dashboard's Attention view already used, so the two cannot disagree. (mcp-missioncache, missioncache-db)
- New `missioncache-db agenda` reads your calendar from ICS or a command, so the brief can schedule around meetings. (missioncache-db)
- `update_context_file` takes `hub`, which writes the `Hub: [[name]]` line pointing a project at its Obsidian note. (mcp-missioncache, missioncache-db)
- The PreCompact snapshot no longer corrupts the context file it saves. Headings and open code fences in the snapshot could end a section early. (hooks, missioncache-db, mcp-missioncache, plugin, docs)
- An unclosed code fence no longer hides the rest of a context file from every parser. (missioncache-db, mcp-missioncache)
- A section name that appears twice is refused rather than guessed at. (missioncache-db, mcp-missioncache)
- `update_context_file` takes `sections_remove` and `bullets_remove`, and `update_tasks_file` takes `tasks_remove`, which strikes a task through with a reason instead of faking completion. (mcp-missioncache, missioncache-db, plugin, docs)
- `waiting_on_resolve` takes a `kind` of `resolved`, `moved` or `dropped`, so a row that moved is not recorded as answered. (mcp-missioncache)
- New `move_to_project` moves sections, rows and tasks between two projects in one locked operation, so a split cannot half-apply. (mcp-missioncache, missioncache-db, plugin, docs)
- New `missioncache-db repair` fixes context files with duplicate or stranded sections. Dry run by default. (missioncache-db, docs)
- The Windows dashboard survives login again. The hidden-console autostart died on its first print and left an empty log. (missioncache-dashboard)

## 2026-08-25

Published package versions: missioncache-install 1.0.16, mcp-missioncache 1.0.30. Claude Code plugin 1.0.24. missioncache-db, missioncache-auto and missioncache-dashboard are unchanged.

- `create_missioncache_files` accepts lists and nested dicts in the plan sections instead of crashing. (mcp-missioncache)
- Updating on Windows no longer risks a half-deleted dashboard install. Running processes are stopped first, and the upgrade is refused if they survive. (missioncache-install)

## 2026-08-24

Published package versions: missioncache-dashboard 1.0.21. Claude Code plugin 1.0.23. Everything else is unchanged since 2026-08-23.

- The per-model usage counter renders on Windows and Linux. The token reader only checked an env var, never the credentials file. (missioncache-dashboard)

## 2026-08-23

Published package versions: missioncache-db 1.0.24, missioncache-dashboard 1.0.20. Claude Code plugin 1.0.22. mcp-missioncache, missioncache-auto and missioncache-install are unchanged.

- Windows renders the statusline instead of a blank block. A piped stdout defaulted to cp1252, which cannot encode the emoji every line carries. (missioncache-dashboard, plugin)
- New `missioncache-db extension-state` returns a JSON snapshot of every active project. It is the data layer for the editor extension now in the repo. (missioncache-db)

## 2026-08-19.7

Published package versions: missioncache-install 1.0.15. Claude Code plugin 1.0.21. Everything else is unchanged since 2026-08-19.6.

- Codex gets all eight workflows as native skills, invoked as `$missioncache-<name>`, replacing the commands Codex could not load. (missioncache-install)

## 2026-08-19.6

Published package versions: missioncache-db 1.0.23, mcp-missioncache 1.0.29. Claude Code plugin 1.0.20. missioncache-auto, missioncache-dashboard and missioncache-install are unchanged.

- The seven planning tools work for the first time. Every one crashed on first call: they were written against a database interface that lived on another class. (missioncache-db, mcp-missioncache)

## 2026-08-19.5

Published package versions: missioncache-install 1.0.14. Everything else is unchanged since 2026-08-19.4.

- `--update` upgrades the MCP server for Codex, OpenCode and VSCode instead of skipping it whenever it was already on PATH. (missioncache-install)

## 2026-08-19.4

Published package versions: mcp-missioncache 1.0.28, missioncache-dashboard 1.0.19. Claude Code plugin 1.0.19, which is what carries the 1.0.27 MCP fixes to Claude Code for the first time. missioncache-db 1.0.22, missioncache-auto 1.0.5 and missioncache-install 1.0.13 are unchanged.

- Claude Code gets the previous release's MCP fixes. It runs the server from the plugin, so a plugin bump is required even when only packages changed. (plugin, docs)
- Every MCP client now receives the behavioural rules, not only Claude Code, through the server instructions field. (mcp-missioncache)
- The Structure panel reports the gap when its header count and its table disagree, instead of quietly rendering fewer rows. (missioncache-dashboard)

## 2026-08-19.3

Published package versions: missioncache-install 1.0.13. Everything else is unchanged since 2026-08-19.2.

- `uvx missioncache-install --update` installs the update. It resolved from a cached index and reinstalled the version you already had, reporting every component green. (missioncache-install)

## 2026-08-19.2

Published package versions: mcp-missioncache 1.0.27, missioncache-dashboard 1.0.18, missioncache-auto 1.0.5. Claude Code plugin 1.0.18, missioncache-db 1.0.22 and missioncache-install 1.0.12 are unchanged.

- Completing a task in missioncache-auto no longer ticks its subtasks along with it. (missioncache-auto)
- The project page's Tasks tab is readable. A completed item's shipping narrative collapses behind a dated disclosure. (missioncache-dashboard)
- The Tasks tab states done, open and percent, and the Plan tab says plainly when a project never wrote one. (missioncache-dashboard)
- Adding a task no longer corrupts the file it is added to. New tasks go into a dated `## Additions` section at the end. (mcp-missioncache)

## 2026-08-19.1

Published package versions: missioncache-install 1.0.12. Claude Code plugin 1.0.18 and the other four packages are unchanged since 2026-08-19.

- The missioncache-install test suite no longer rewrites your own install state, which broke the next update from outside the clone. (missioncache-install)

## 2026-08-19

Published package versions: missioncache-dashboard 1.0.17, missioncache-install 1.0.11. Claude Code plugin 1.0.18. (missioncache-db 1.0.22, mcp-missioncache 1.0.26 and missioncache-auto 1.0.4 are unchanged this cycle and keep their versions.)

- `/missioncache-fork` and `/missioncache-rename` ship to Codex, OpenCode and VSCode, with the Claude-only session steps stripped rather than shipped. (missioncache-install, plugin)
- Checkboxes in the Tasks tab sit beside their task text instead of above it behind a stray bullet. (missioncache-dashboard)
- The task-numbering reference moved out of the Tasks tab's reading flow into a `?` button in the corner. (missioncache-dashboard)
- An absent `ListAgents` row is no longer read as proof a peer is gone, since the list can be too long to check. (rules, plugin)
- The landing page gained a feature card for parallel sessions. (site)
- The landing page's Windows FAQ and MCP tool count had been stale for three weeks. (site)
- Corrected a claim in three places that native Windows had no real-hardware CI. That job landed on 2026-08-15. (docs)

## 2026-08-17.1

Published package versions: missioncache-db 1.0.22, mcp-missioncache 1.0.26, missioncache-auto 1.0.4, missioncache-dashboard 1.0.16. Claude Code plugin 1.0.16. (missioncache-install is unchanged since 2026-08-17 and stays at 1.0.10.)

- A relative `MISSIONCACHE_ROOT` is refused rather than resolved. The consumers do not share a working directory, so it names a different place in each. (missioncache-db)
- `MISSIONCACHE_ROOT` moves every data path, not only some. An empty value no longer falls back to the working directory. (missioncache-db, mcp-missioncache, missioncache-dashboard, plugin)
- `installation.md` covers native Windows and WSL2 in a section of its own, starting with the fact that the two are separate installs. (docs)

## 2026-08-17

Published package versions: missioncache-db 1.0.20, mcp-missioncache 1.0.25, missioncache-auto 1.0.3, missioncache-dashboard 1.0.15, missioncache-install 1.0.10. Claude Code plugin 1.0.15.

### Native Windows

**Requires Claude Code 2.1.139 or newer**, where hook `args` (exec form) was added. An older client drops `args` and runs bare `uv`, which exits non-zero.

- Plugin hooks run on native Windows, with no WSL. hooks.json moved to the exec form, and each hook makes the bundled database importable itself. (plugin, missioncache-install, docs)
- The slash commands stop assuming `python3` exists, probe-running each candidate instead of trusting PATH. (plugin)
- The dashboard registers for autostart: a Task Scheduler task, falling back to a Run-key entry when schtasks refuses without elevation. (missioncache-dashboard)
- missioncache-auto runs Claude portably. `.cmd` shims resolve, and the process tree is reaped with `taskkill`. (missioncache-auto)
- Bare-name executable resolution is centralised, and refuses a result found in the current directory. (missioncache-install, missioncache-dashboard, missioncache-auto)
- The installer copies where Windows refuses symlinks, and writes the statusline path quoted. (missioncache-install, missioncache-dashboard)
- A bundle exported on macOS or Linux imports on native Windows. Export from Windows stays out of scope. (missioncache-db, docs)

### Cross-session notifications

- Every MCP tool that rewrites a project's files returns `live_sessions`, not only `update_context_file`. (mcp-missioncache, rules)
- The send protocol tries the bare `ListAgents` name first and falls back to the ref the rejection itself prints. (rules)
- Session titles recompute on every prompt, so a session left holding a `-2` suffix drops back to the plain name. (plugin)
- The SessionStart hook takes its session identity from stdin rather than the environment, which a child session inherits. (plugin, docs)
- New `missioncache-db prune-sessions` deletes the per-session state of sessions that are gone. One machine held 2,318 pid records. (missioncache-db, docs)
- A notify target with no `ListAgents` row is skipped with a one-line note instead of blocking on a question. (rules)

## 2026-08-13

Published package versions: missioncache-db 1.0.19, mcp-missioncache 1.0.24, missioncache-dashboard 1.0.14, missioncache-install 1.0.9. Claude Code plugin 1.0.13.

### Cross-session notifications

- A session writing into another project's context now tells that project's live sessions, instead of leaving them on what they read at load time. (mcp-missioncache, rules)
- New `session_title` hook names each session after its project, so a peer can be addressed at all. (plugin)
- New helpers `live_sessions_for_project`, `bound_project_for_session` and `session_is_alive`. (missioncache-db)
- `update_context_file` excludes the calling session from `live_sessions`. One `claude` process hosts many sessions at once. (missioncache-db, mcp-missioncache)
- A session is listed as live only when its pid proves it, not merely when it is not proven dead. (missioncache-db)
- The send protocol documents that a send is not a delivery: a held message still returns success to the sender. (rules)
- A cross-session peer is addressed by its `ListAgents` name and the row's `[ref]`, which is per-listing. (rules)

### Statusline

- The usage line shows a per-model weekly counter, read from the `limits` array where the number actually appears. (missioncache-dashboard)

### Context files

- `imported_event` constrains its `heading` and `related_project`, both of which are interpolated into markdown the digest parses. (mcp-missioncache)
- Repeating an `imported_event` whose heading exists is a no-op instead of stacking a duplicate section. (mcp-missioncache)
- A failed hooks-state read logs a warning instead of returning "no peers" indistinguishably from success. (missioncache-db)
- The `session_title` hook no longer raises on stdin carrying valid JSON that is not an object. (plugin)
- `live_sessions_for_project` returns `last_active` rather than `bound_at`. (missioncache-db)
- `update_context_file` gained `imported_event`, which writes the cross-project event section under the lock. (mcp-missioncache)

## 2026-07-30

Published package versions: mcp-missioncache 1.0.20, missioncache-dashboard 1.0.13, missioncache-install 1.0.8. Claude Code plugin 1.0.9.

- Every third-party dependency caps its major version, after an upstream 2.0.0 reached users with no release on our side. (mcp-missioncache, missioncache-dashboard, missioncache-install)
- The MCP server migrated to mcp SDK 2.0. (mcp-missioncache)
- A project deleted outside the dashboard no longer lingers as a ghost row. The mirror sync reconciles deletions now. (missioncache-dashboard)
- The demo seeder mirrors action items into the context files, so the demo matches a real project. (missioncache-dashboard)

## 2026-07-29.1

Published package versions: mcp-missioncache 1.0.19. Claude Code plugin 1.0.8.

- The MCP server crashed on startup in freshly resolved environments. `mcp` 2.0.0 removed the module we imported, and our spec had no upper bound. (mcp-missioncache, plugin)
- Random characters after a terminal resize were root-caused to Claude Code itself. What was ours: rows no longer pad to full width. (missioncache-dashboard)
- Column padding is no longer emitted after a row's last cell, where it aligned nothing and could force a spurious ellipsis. (missioncache-dashboard)
- Statusline addon output is stripped of C1 control characters as well as C0. (missioncache-dashboard)
- A statusline crash holds its height instead of silently removing the whole statusline area. (missioncache-dashboard)
- The action-items badge is a real icon, and its tooltip says open action items rather than just a number. (missioncache-dashboard)

## 2026-07-29

Published package versions: missioncache-install 1.0.7, missioncache-dashboard 1.0.12.

- The update-available notice clears after the update that fixes it. Nothing invalidated the shared cache, so it persisted for hours. (missioncache-install, missioncache-dashboard)
- An editable install no longer nags forever. Its metadata version is frozen at install time and is excluded from the verdict. (missioncache-install, missioncache-dashboard)
- `--update` refreshes the Claude Code plugin, which it never did, so users stayed on the plugin version they first installed. (missioncache-install)
- A component that failed during install is recorded and retried on every `--update`. (missioncache-install)
- `--update` on a machine with a reset state file reports installed-but-untracked components instead of saying there is nothing to do. (missioncache-install)
- A `statusLine` you have since pointed elsewhere is left alone during an update. (missioncache-install)
- A second install run no longer replaces the backup the first run made. (missioncache-install)
- A rules file without the managed marker is yours and is never touched on install. (missioncache-install)

## 2026-07-28

Published package versions: missioncache-db 1.0.15, mcp-missioncache 1.0.18, missioncache-dashboard 1.0.11. Claude Code plugin 1.0.7.

- New project-management layer: action items, stakeholders, tickets and due dates, mirrored into read-only context-file sections. (missioncache-db, mcp-missioncache, missioncache-dashboard, plugin)
- New `action-item`, `stakeholder`, `ticket` and `due-date` CLI groups, and `health` flags overdue items and near due dates. (missioncache-db)
- Six new PM tools, and `get_context_digest` carries the due date and open action items. (mcp-missioncache)
- `/missioncache:load` renders action items above Waiting on, and `/missioncache:save` proposes completions and captures new commitments. (plugin)
- The dashboard opens on a new Attention view: what needs you today, who you are waiting on, and which projects are outstanding. (missioncache-dashboard)
- Colour on the Attention view carries one meaning each. Red is you are late, amber is someone else's latency. (missioncache-dashboard)
- The Attention view reported zero overdue on days you were the blocker. A Waiting-on row naming you counts now. (missioncache-dashboard)
- Project groups sort on a total order, so the same day's list cannot come back in a different order. (missioncache-dashboard)
- `/api/today` splits by who owes the work rather than by which table stored it. (missioncache-dashboard)
- Tickets are system-agnostic: a label and a URL is the whole interface. A legacy `jira_key` migrates on the first PM write. (missioncache-db)
- Bundles carry the PM layer, and older bundles without it import unchanged. (missioncache-db)
- The Attention view uses the app's own design tokens instead of a palette of its own. (missioncache-dashboard)
- Page headings are readable in light mode. The gradient built for the dark surface measured 1.44:1 against white. (missioncache-dashboard)
- The demo seeder produces the Attention view: PM data, real git history per repo, and a category per project. (missioncache-dashboard)
- Re-seeding starts each repository from scratch, so the second run no longer reports commits that are empty. (missioncache-dashboard)

## 2026-07-21

Plugin-only release: no PyPI packages changed. Claude Code plugin 1.0.6.

- The statusline's task counter no longer sits stale between explicit saves. Its reminder now fires on a signal real projects use. (plugin)

## 2026-07-19.3

Published package versions: missioncache-dashboard 1.0.10.

- Updating no longer stops to ask about its own port. The probe recognises the machine's own dashboard and continues. (missioncache-dashboard)

## 2026-07-19.2

Published package versions: missioncache-db 1.0.14, mcp-missioncache 1.0.17, missioncache-dashboard 1.0.9.

- Complete and reopen a project from the dashboard, with the same file moves the MCP tool performs. (missioncache-db, mcp-missioncache, missioncache-dashboard)
- The statusline's Saved cell has its own colour and sits next to Project, instead of looking identical to Last Action. (missioncache-dashboard)
- The dashboard starts on a clean machine. A missing dependency, a missing directory, and a swallowed traceback that hid both. (missioncache-dashboard)
- New CI smoke test installs from the tree on a clean runner and asserts the dashboard actually serves. (missioncache-install)

## 2026-07-19.1

Published package versions: missioncache-dashboard 1.0.8.

- You now learn when a release is out, in the dashboard, the statusline and `/missioncache:load`. Nothing watched PyPI before. (missioncache-dashboard, plugin)
- The Project row gained a Saved cell: when the project's own context file last changed, always with a date. (missioncache-dashboard)

## 2026-07-19

Published package versions: missioncache-db 1.0.13, mcp-missioncache 1.0.16, missioncache-install 1.0.6, missioncache-dashboard 1.0.7.

- The dashboard service installs on systemd-less Linux, falling back to a profile autostart instead of crashing. (missioncache-dashboard)
- The Codex install works end to end: a rejected manifest key, a stanza that never populated the cache, and per-call approvals. (missioncache-install, plugin)
- Non-Claude commands can no longer touch Claude's session state. A Codex run had hijacked a live Claude session's binding. (missioncache-install, plugin)
- A session's explicit project binding is no longer vetoed by cwd, which silently broke forks and cross-repo work. (missioncache-db, mcp-missioncache, plugin)
- The binding file records the task id, so it survives a rename and cannot route into a reused project name. (missioncache-db)
- The PreCompact snapshot never fired: it looked for the project files in the wrong place. No snapshot had landed since the redesign. (plugin)
- The PreCompact hook leaves a breadcrumb when it cannot find the project, instead of bailing quietly. (plugin)
- The docs stop overpromising Windows support. (docs, site)
- The fork staleness indicator says who and when, and clears as soon as you read the parent. (missioncache-dashboard, mcp-missioncache)
- The landing page leads with the problem rather than the machinery. (site)
- Fork families render as a tree in the projects table, with the parent keeping its own row. (missioncache-dashboard)
- The website describes forks as project forks with shared memory, not conversation forks or git forks. (site, docs)
- The dashboard sidebar links to the website and the changelog, and shows the running version. (missioncache-dashboard)
- The dashboard's OpenAPI metadata reports the real package version instead of a hardcoded one. (missioncache-dashboard)
- New `docs/forks.md`, including the part that was missing: when two separate projects is the better answer. (docs)
- The landing page opens in dark theme and explains how to get uvx. (site)
- Links on the landing page are underlined at rest and have focus rings. (site)

## 2026-07-14

Published package versions: missioncache-db 1.0.12, mcp-missioncache 1.0.14, missioncache-dashboard 1.0.6.

- A project can be created as a fork of a parent, sharing the parent's context while keeping its own tasks. (missioncache-db, mcp-missioncache, missioncache-dashboard, plugin)
- The `**Fork of:**` header is the source of truth. The scan re-heals a lost link and refuses ambiguous matches. (missioncache-db)
- Parallel sessions on a fork see when a sibling updated the shared layer. (mcp-missioncache, missioncache-dashboard)
- Completing a parent with active forks warns, and its context stays readable from `completed/`. (missioncache-db)
- Every dashboard screen opens with the same title-and-description header. (missioncache-dashboard)
- The Structure tab links into the Auto page with that project's graph already selected. (missioncache-dashboard)
- New `addons_after_status` setting places statusline addon rows below the Claude status line. (missioncache-dashboard)

## 2026-07-13

Published package versions: missioncache-db 1.0.11, missioncache-dashboard 1.0.5.

- The statusline can carry your own cells. An addon names a command, and its output renders as a cell. (missioncache-dashboard)
- Addons fail closed. A command that breaks, times out or is slow renders a blank cell instead of taking down the statusline. (missioncache-dashboard)
- Delete, export and import projects from the dashboard instead of only from the CLI. (missioncache-dashboard, missioncache-db)
- Export streams a bundle of the markdown tree. The database itself never travels. (missioncache-db)
- The dashboard ships a favicon and a web app manifest, so it installs as a standalone app. (missioncache-dashboard)

## 2026-07-11.1

Published package versions: missioncache-dashboard 1.0.4, missioncache-install 1.0.5.

- The dashboard pins its markdown renderer with an integrity hash, so a compromised CDN cannot swap it. (missioncache-dashboard)
- missioncache-install ships a rebuildable sdist. (missioncache-install)

## 2026-07-11

Published package versions: missioncache-db 1.0.10, mcp-missioncache 1.0.13, missioncache-auto 1.0.2, missioncache-dashboard 1.0.3, missioncache-install 1.0.4.

- Parallel missioncache-auto runs give each worker its own git worktree and branch by default. (missioncache-auto)
- Three pre-run refusals prevent lost work when the worktree and auto-commit settings conflict. (missioncache-auto)
- A dirty worktree is left on disk with its branch and a warning, instead of being force-removed. (missioncache-auto)
- Auto-commit detects untracked-only output, and never commits `.env*` at any depth. (missioncache-auto)
- The statusline's context percent matches Claude Code's own number, and `NO_COLOR` is honoured. (missioncache-dashboard)
- Installer config writes are atomic and leave one `.bak` per run. (missioncache-install)
- Dashboard hardening: remaining unescaped values, error and freshness states, and keyboard access. (missioncache-dashboard)
- Timestamps use the local timezone instead of a hardcoded `Asia/Jerusalem`. (missioncache-dashboard)
- `process_heartbeats` rolls back on failure. (missioncache-db)
- Dropped the hardcoded legacy-path migration from the cleanup command. (plugin)
- The Stop hook detects edited project files correctly before reminding you to save. (plugin)
- New `## Waiting on` section, maintained by `waiting_on_add` and `waiting_on_resolve` and shown beside Next Steps on resume. (missioncache-db, mcp-missioncache)
- Recent Changes keeps its newest 12 entries. Older ones roll into `<name>-journal.md`. (missioncache-db, mcp-missioncache)
- New `get_context_digest` tool, so `/missioncache:load` reads a digest instead of the whole context file. (mcp-missioncache)
- New `missioncache-db health`: stale saves, stale waiting-on rows, oversized context files and missing sections. (missioncache-db)
- The new-project template follows the canonical section order. (missioncache-db, plugin)
- One-time migration script brings existing context files onto the conventions. (missioncache-db)
- Section-heading matches are anchored to line start. A bullet mentioning `## Recent Changes` had become the insertion anchor. (mcp-missioncache, plugin)
- Every structure scan is fence-aware, so a heading inside a code sample cannot shadow the real section. (missioncache-db, mcp-missioncache)
- Waiting-on cell values are pipe-escaped, so a literal `|` no longer shifts every column on the next rewrite. (missioncache-db)
- The server's dependency floor moved to the missioncache-db version carrying the module it imports at load. (mcp-missioncache)
- Custom categories: a name, an emoji and a colour, managed from Settings and accepted everywhere the built-ins are. (missioncache-db, mcp-missioncache, missioncache-dashboard)
- Deleting a custom category always succeeds. Projects still carrying it keep the value. (missioncache-dashboard)
- Dashboard CORS is scoped to its own origin, and the credentials flag is gone. (missioncache-dashboard)
- Categories are editable after creation, from the dashboard and from the new `update_task` tool. (missioncache-db, mcp-missioncache, missioncache-dashboard)
- Task updates reach the dashboard's read path again on DuckDB files created by the migrate script. (missioncache-dashboard)
- `jira_key` is escaped in the task lists, in element bodies and in attributes. (missioncache-dashboard)
- `migrate_to_duckdb.py` no longer crashes on databases without the lazily-created feature tables. (missioncache-dashboard)
- New projects filter bar: search, category chips and a repo dropdown, across both tables at once. (missioncache-dashboard)
- New `docs/cli.md` for the deliberately CLI-only operations, with the MCP server pointing every client at it. (mcp-missioncache)
- New project category taxonomy, assigned at creation instead of guessed from the project name. (missioncache-db, mcp-missioncache, missioncache-dashboard)
- The statusline can hide model-suspension notices, which never resolve and otherwise pin for weeks. (missioncache-dashboard)
- The statusline's Last Action moved to the top row. (missioncache-dashboard)
- Removed the statusline's `Task:` field. (missioncache-dashboard)
- `__version__` derives from installed metadata, so it cannot drift from the published version. (missioncache-install, mcp-missioncache, missioncache-auto, missioncache-dashboard)
- The installer banner no longer wraps mid-word on terminals narrower than 80 columns. (missioncache-install)
- Removed the `pending-task.json` legacy state file and its writers. Leftover files are harmless and can be deleted by hand. (orbit-db, mcp-orbit, plugin)
- `get_task` accepts `session_id` and binds the session atomically, so a skipped bash step cannot leave the statusline stale. (mcp-orbit, plugin)
- The statusline no longer shows a stale project after resuming at a parent directory holding several repos. (plugin)

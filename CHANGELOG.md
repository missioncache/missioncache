# Changelog

All notable changes to MissionCache are documented in this file. Dates are ISO 8601; sections are grouped by behavioral concern, not by sub-package version. Entries dated before the 2026-06 rebrand reference the project's former name (orbit) and its old package names (orbit-db, mcp-orbit, etc.); those are left as-is as accurate historical records.

## Unreleased

### Added - context-file conventions: Waiting on, capped Recent Changes + journal, load digest, health check (missioncache-db 1.0.8, mcp-missioncache 1.0.11)

Context files now share a canonical structure, and the pieces that made big files painful are automated:

- **Waiting on section** (`| What | Who | Since | Gates |` before Next Steps) is first-class: the new-project template generates it, `update_context_file` maintains it via `waiting_on_add` / `waiting_on_resolve` (a resolve removes the row and records the resolution in today's Recent Changes; unmatched resolves come back in `waiting_on_unmatched`, never silently dropped), and `/missioncache:load` renders it next to Next Steps on resume. The section self-heals into files that predate the convention on the first `waiting_on_add`.
- **Recent Changes cap + per-project journal**: the section keeps its newest 12 dated subsections; overflow rolls automatically into `<name>-journal.md` (oldest first, greppable, never read on resume), with a pointer line at the section bottom. Rollover happens under the same sidecar lock as the context write, journal written first so a crash duplicates rather than loses entries. The pre-compact hook deliberately does not enforce the cap (stays import-light); the next save re-trims.
- **`get_context_digest` MCP tool**: `/missioncache:load` now reads a server-side digest (Waiting on + Next Steps verbatim, last 3 Recent Changes subsections, section index with line numbers, size, health warnings) instead of the whole context file - which also unblocks resumes on files past the 256KB Read-tool cap.
- **`missioncache-db health` CLI command**: fleet-wide report of stale Last Updated (>14d), stale Waiting-on rows (>7d), context files over the 100KB budget, missing core sections, and over-cap Recent Changes. Report-only, exit 0. Same warnings surface in the load digest. Thresholds are plain constants in the new `missioncache_db/context_health.py`, which owns all context-file parsing for the CLI, the MCP server, and the migration script.
- **New-project template rewritten to the canonical order** (Description, Definition of Done, Gotchas, Waiting on, Next Steps, Recent Changes, Key Architectural Decisions, Key Files; "Patterns Being Followed" dropped, "Key People" recognized-optional). The repo-root `templates/` copy is byte-identical; missioncache-auto's embedded template is updated to the same conventions while keeping its auto-specific sections. A test guards the cross-file invariants (usage note, table header, core sections) in all three copies. `/missioncache:new` now instructs filling Definition of Done at creation.
- **Cross-project conventions documented** in the managed rules file: `**Related projects:**` header line, the self-contained imported-event section pattern, falsified-hypothesis Gotcha entries, and parallel-session discipline.
- **One-time migration script** (`scripts/migrate_context_conventions.py`, dry-run first): inserts Waiting on into existing projects, consolidates legacy `## Recent Changes (timestamp)` h2 fragments, repairs entries misplaced by the heading-regex bug below, and rolls overflow into journals.

### Fixed - unanchored section-heading regexes misplaced Recent Changes entries (mcp-missioncache 1.0.11, hooks)

The Recent Changes prepend in `update_context_file` and the pre-compact hook matched `## Recent Changes` anywhere in the file, not just at line start. A prose bullet that mentioned the literal string (missioncache-release's own context file did, inside a Key Decisions entry) became the insertion anchor, sending weeks of dated entries into the middle of another section. All section-heading matches in the write path (`Recent Changes` prepend, `_update_section`, `_append_to_section`) are now `^`-anchored with MULTILINE; the migration script repairs the already-misplaced entries. Measured blast radius: 1 of 17 active projects.

Two adjacent hardenings from the same review round: (1) every structure scan is now FENCE-AWARE - lines inside ``` / ~~~ code blocks are invisible to heading/subsection/table detection, so a code sample containing a column-0 `## Recent Changes` can neither shadow the real section nor trigger a false rollover that tears the fence apart (measured: no current file contained the shape; the fix is for arbitrary users' files). The prepend shape itself now has ONE owner (`context_health.prepend_recent_changes`), shared by `update_context_file` and the pre-compact hook, so this bug class can no longer require a double fix. (2) Waiting-on cell values are pipe-escaped and newline-flattened on render and unescaped on parse - previously a literal `|` in any cell silently shifted every column on the next table rewrite.

### Added - custom categories (missioncache-db 1.0.6, mcp-missioncache 1.0.9, missioncache-dashboard)

The built-in 13-value category taxonomy is now extensible. A new dashboard Settings section manages custom categories - a name (kebab-case, built-in names and the `'none'` sentinel are reserved), an emoji (content-validated: must contain non-ASCII and no HTML metacharacters, so plain text and markup fragments never reach the DB and render-time escaping is not the only XSS defense), and a palette color (validated server-side as strict `#RRGGBB`, since the value lands in style attributes). Custom categories surface everywhere the built-ins do: project-table icons (the emoji, in the chosen color), filter-bar chips, and the modal selector, and every category write path accepts them (the dashboard PUT endpoint, `update_task`, `create_task`, `create_missioncache_files`, and the CLI via the shared `TaskDB` validation). Cross-machine import counts locally-defined customs as known; a category only defined on the exporting machine still degrades to uncategorized with a warning, since custom definitions do not travel with bundles.

Deleting a custom category always succeeds: projects still carrying the value keep it (rendered with default styling, still selectable per-task so a modal save cannot wipe it), and re-adding the name restores the emoji and color. New assignments of a deleted name are rejected. Storage is a new `custom_categories` SQLite table created by the idempotent schema DDL, so existing installs pick it up on the next open with no migration step; the dashboard reads it directly from SQLite (`GET/POST /api/categories`, `DELETE /api/categories/{name}`) with no DuckDB involvement. The dashboard's 15-minute auto-refresh re-fetches the category map, so customs created from another tab, another machine, or the CLI stop rendering as the generic fallback within one cycle; a failed categories fetch keeps the previous map and says so in Settings instead of showing a false "No custom categories yet".

### Fixed - dashboard CORS was wildcard with credentials (missioncache-dashboard)

The dashboard's CORS middleware allowed `*` origins with credentials, letting any website open in the user's browser read every API response and drive the mutating endpoints cross-origin (the 127.0.0.1 bind blocks remote hosts, not the user's own browser tabs). CORS is now scoped to the dashboard's own origin (`localhost:8787` / `127.0.0.1:8787`) and the credentials flag is gone (nothing uses cookies). Non-browser consumers - the statusline, hooks, curl - are unaffected, since CORS only gates browser-initiated cross-origin requests.

### Added - edit category in place (missioncache-db 1.0.5, mcp-missioncache 1.0.8, missioncache-dashboard)

Categories are now editable after creation, from both surfaces:

- **Dashboard:** the task modal header carries an inline category selector (icon + dropdown next to the repo badge). It shows the stored value ("uncategorized" for NULL rows, even when the row icon renders a heuristic guess), refreshed from SQLite via the `/api/task/{id}/files` response so it stays correct across MCP/CLI writes, and saves through the new `PUT /api/tasks/{id}/category` endpoint; on success the modal icon, row icons, and filter-bar chips update in place. The endpoint validates against `CATEGORIES` server-side (the selector is not the validation layer), mirrors the rename endpoint's DuckDB-resync contract, and reports refresh problems in its `warnings` list - which the selector surfaces as a visible "Saved (list refresh delayed)" status instead of a clean green "Saved".
- **MCP:** a new `update_task` tool sets `jira_key` and/or `category` post-creation in any MCP client - the conversational equivalent of the CLI's `set-jira`/`set-category`. Fields are optional, the literal string `'none'` clears (an empty string is rejected rather than stored or treated as clear), and all validation runs before ANY write so invalid input never half-applies. Backed by a new `TaskDB.set_task_jira()` primitive mirroring `set_task_category()`.

### Fixed - task updates silently frozen out of the dashboard read path (missioncache-dashboard)

On a DuckDB file created by `migrate_to_duckdb.py`, every task row referenced by sessions or heartbeats failed to sync updates from SQLite: the migrate script's schema declared foreign keys, DuckDB rejects upserts of FK-referenced parent rows ("still referenced by a foreign key"), and the sync's per-row try/except reduced each failure to a stdout print. Renames, completions, and category changes never reached `/api/tasks/active` on such files - the server-created schema (no FKs) was unaffected, which is why the drift went unnoticed. Fixed by removing the FK constraints from the migrate script's schema (the DuckDB mirror is a disposable read replica; SQLite owns integrity - the two schema definitions now agree), counting per-row sync failures into the sync result (`tasks_sync_failed`, `sessions_sync_failed`, `repos_sync_failed` - the sessions case previously dropped time-tracking data with no signal at all), and surfacing them as warnings from the rename/category endpoints, whose except-only handling could never fire for sync errors (`sync_from_sqlite` reports problems in its result dict rather than raising). Recovery for affected files: rerun `migrate_to_duckdb.py`. The missing-table leniency added to the migrate script is scoped to the lazily-created feature tables only - an absent core table still crashes loudly.

### Fixed - jira_key rendered unescaped in the dashboard task lists (missioncache-dashboard)

Both task-list renderers interpolated `task.jira_key` (and `task.jira_url`) raw into `innerHTML` templates - in element-body AND `title`/`href` attribute contexts - while every sibling field was escaped, a stored-XSS sink for hostile jira_key values (reachable via the CLI and the new `update_task` tool, which do not constrain the key's format). Both sites now escape, and `escapeHtml` itself switched from the textContent/innerHTML trick to an explicit replace chain that also escapes quotes, making it safe in attribute contexts (the old version was not, which the taxonomy-bounded `category` values masked).

### Fixed - migrate_to_duckdb.py crashed on DBs without the shadow-repo feature tables (missioncache-dashboard)

The script assumed `shadow_repos` / `shadow_commits` / `non_git_activity` exist in the source SQLite, but those are created lazily by their feature - any user who never enabled it got `sqlite3.OperationalError: no such table`. Missing feature tables now migrate as empty, reported explicitly in the source-counts and per-table output.

### Added - projects filter bar (missioncache-dashboard)

The Projects view gained a filter bar above the Active and Completed tables: a search box matching project name and description (subtask names/descriptions count toward their parent), category chips showing only the categories present in the loaded data (multi-select, OR semantics, icons matching the table rows), and a repo dropdown. Filtering is client-side and applies to both tables at once; on the Completed table it runs before the newest-10 display cap, so a search can surface older completions. Filters are session-only by design - a filter silently restored from a past visit would read as missing data.

### Added - CLI reference and MCP signpost for CLI-only operations (mcp-missioncache 1.0.6)

New `docs/cli.md` documents the deliberately-CLI-only operations - cross-machine `export`/`import` with the per-machine `config` path map, tag keyword management, `prune`/`cleanup` maintenance, and `add-repos-glob` bulk registration - linked from the README docs section. The MCP server now sets the FastMCP `instructions` field with a short pointer at that surface, so every MCP client (Claude Code, Codex, OpenCode, VSCode) learns per session that those operations live in the `missioncache-db` CLI rather than in MCP tools.

### Added - project category taxonomy (missioncache-db 1.0.4, mcp-missioncache 1.0.5)

Tasks gained a nullable `category` column validated against a 13-value taxonomy (`CATEGORIES` in missioncache-db: bug, feature, refactor, test, docs, infra, ui, api, database, security, perf, coding, noncoding). The category is assigned at creation time - `/missioncache:new` derives it from the project description via a rubric in `commands/new.md` and echoes it in the creation summary - replacing the dashboard's render-time name-keyword guess that mislabeled every `missioncache-*` project as `perf` via the embedded "cache".

- **missioncache-db:** `create_task(category=...)`, new `set_task_category()`, CLI `create-task --category` flag and new `set-category <id> <category|none>` command. The idempotent column migration runs at connection-open as well as `initialize()`, so bare-`TaskDB()` consumers (CLI, hooks) migrate too. Cross-machine bundles now carry `category` (export manifest + import upsert); an incoming NULL preserves the local value on re-import, and an unknown bundle value imports as uncategorized with a warning.
- **mcp-missioncache:** `category` param on `create_task` and `create_missioncache_files` (validated, echoed in results); `TaskSummary` exposes `category`.
- **missioncache-dashboard:** the DuckDB mirror carries the column (schema, idempotent ALTER for existing files, sync upsert, `migrate_to_duckdb.py`); the frontend renders the stored category first and only falls back to the name heuristic for NULL, now with word-boundary matching. The icon `title` attribute is HTML-escaped.

Existing rows stay NULL (heuristic fallback renders them); fill by hand via `missioncache-db set-category`.

### Added - statusline can hide model-suspension notices (missioncache-dashboard 1.0.2)

The statusline's Claude status field pulls live incidents from status.claude.com. Anthropic posts model-access suspensions (e.g. "We've suspended access to Claude Mythos 5 and Claude Fable 5") as `monitoring` incidents that never resolve, so they pin to the field for weeks. A new "Show model suspension / deprecation notices" toggle in the dashboard Settings (Statusline visibility) controls these, defaulting to off so they stay hidden. Classification is a keyword match on the incident name/body (`suspend`, `deprecat`, `sunset`, `retir`, `no longer available`); genuine operational outages, which use different phrasing, still show regardless of the toggle. The filter runs on cache read, so flipping the toggle applies on the next prompt render rather than waiting for the 60s health cache to expire.

### Changed - statusline Last Action moved to the top row (missioncache-dashboard 1.0.2)

The "Last Action" timestamp moved from the bottom Vitals row to the end of the top Project row. When no MissionCache project is loaded, it takes the row's first slot. The Vitals row now carries only the Claude Code version and Claude status.

### Removed - statusline current-task field (missioncache-dashboard 1.0.2)

The `Task:` field (the active checklist item, set via `set_active_missioncache_tasks`) was removed from the statusline; it rarely appeared and added little over what Claude already prints in chat. The project progress count (`[82/102]`) next to the project name is unchanged and still auto-updates each render. The MCP tools that write the active-task pointer are unaffected.

### Fixed - `__version__` drifted from the packaged version (missioncache-install 1.0.2)

`missioncache-install --version` reported `1.0.0` while the package was actually 1.0.1, because `__version__` was a hardcoded string in `__init__.py` that was not bumped alongside `pyproject.toml`. Every package's `__version__` now derives from its installed metadata via `importlib.metadata.version(...)`, so it can no longer drift from the published version. Same change applied to `mcp-missioncache`, `missioncache-auto`, and `missioncache-dashboard` (they republish on their next release).

### Fixed - installer banner wraps mid-word on narrow terminals (missioncache-install 1.0.1)

The `uvx missioncache-install` start banner rendered the `MissionCache` wordmark as a single ~76-column `ansi_shadow` line, which wrapped mid-word (`MISSIONCAC` / `HE`) on terminals narrower than 80 columns. It now renders as two stacked words (`MISSION` over `CACHE`, 52 columns) that fit comfortably.

### Removed - `pending-task.json` legacy state file (orbit-db 1.0.4, mcp-orbit 0.2.13)

The shared `~/.claude/hooks/state/pending-task.json` file is no longer written or read by any code path. It had been documented as vestigial state since the per-session `projects/<session-id>.json` pointer landed; this release deletes the writers too.

Removed:
- `hooks/session_start.py:write_pending_task` and its call site in `main`.
- The `pending-task.json` echo at the start of `commands/go.md` Step 4.
- The `pending-task.json` echo at the start of `commands/save.md` Step 1b.
- The `rm -f pending-task.json` cleanup in `commands/done.md` Step 5.
- The `pending-task.json` sweep in `TaskDB.rename_task` (`orbit-db/__init__.py`).

**Migration:** old `pending-task.json` files left over from pre-0.2.13 installs are harmless and can be deleted by hand. No active code reads them, and the rename-sweep no longer maintains them on task renames.

Cosmetic cleanup in `commands/save.md` Step 1b: the previous one-liner ended with `&& echo "done" || echo "done"`, a no-op short-circuit pair. Rewritten as a single `echo "done"` after the curl call.

orbit-db bumped 1.0.3 → 1.0.4 to invalidate uvx's wheel cache for the rename-sweep change; mcp-server bumped 0.2.12 → 0.2.13 to invalidate uvx's source-keyed venv cache (per `CLAUDE.local.md` - the orbit-db code is reachable from the MCP server, so both versions need to advance).

### Changed - `get_task` MCP tool accepts optional `session_id` for atomic binding (mcp-orbit 0.2.12)

`mcp__plugin_missioncache_pm__get_task` now accepts an optional `session_id` parameter. When provided, the tool atomically writes the `project_state` row in `~/.claude/hooks-state.db` and the per-session `~/.claude/hooks/state/projects/<sid>.json` pointer alongside the task lookup, mirroring the `create_orbit_files` binding pattern shipped in 0.2.11.

**Motivation:** `/missioncache:load`'s slash-command bash step (which used to be the sole binding writer for the resume path) can be silently skipped by Claude when it streams past Step 4 to the next instruction. The server-side binding makes it impossible to call `get_task` with a session_id without binding, eliminating the failure mode where the user runs `/missioncache:load new-project` and the statusline keeps showing the previous project.

**Response shape:**
- When `session_id` is provided: response includes `session_bound: bool` (True on success, False on validation/IO failure).
- When `session_id` is omitted: `session_bound` field is omitted entirely. Existing read-only callers (UI, list views, tests) are unaffected.

**`/missioncache:load` updates** to pass the resolved session_id to `get_task` via the same `$CLAUDE_CODE_SESSION_ID` env-var pattern adopted in `commands/save.md` / `commands/done.md` / `commands/new.md`. The Step 4 bash binding stays as defense-in-depth and to refresh the dashboard list view immediately, but the statusline binding is now driven server-side.

### Fixed - Statusline shows stale project after resume at umbrella cwd

When a Claude Code session resumed at a parent directory holding multiple project repos (e.g. `~/work`), the SessionStart hook would unconditionally inherit whatever project the previous session at that cwd was bound to. The inherited binding then routed heartbeats to the wrong task, made the statusline display the wrong project name, and survived subsequent `/missioncache:load <other-project>` invocations when the slash command's bash step did not fire correctly.

**Root cause:** `_pickup_previous_session_binding` (`hooks/session_start.py`) used the cwd-session pointer match alone as sufficient evidence to inherit. For umbrella cwds, the previous session's specific project is unrelated to the new session's intent.

**Fix:** added a cwd-compatibility gate. The inherited project is now accepted only when the project's repo path is the current cwd or an ancestor of it (i.e. the new session is sitting *inside* the project repo). If the repo lives *under* the cwd or in an unrelated location, the inherit is skipped and the statusline starts blank - the user resolves intent explicitly via `/missioncache:load`.

**Conservative on failure:** if orbit-db is unavailable, the task lookup raises, or the task/repo row no longer exists, the inherit proceeds as before. The gate only fires on affirmative evidence that the inherit is wrong.

**Non-coding tasks:** unaffected. Inherit always proceeds for tasks with no `repo_id`.

**New contract for users:** if you have been relying on a parent cwd auto-inheriting the previous session's project, that behavior is gone. Run `/missioncache:load <project>` in the new session to bind explicitly, or `cd` into the project's repo before opening Claude Code.

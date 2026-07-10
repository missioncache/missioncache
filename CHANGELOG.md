# Changelog

All notable changes to MissionCache are documented in this file. Dates are ISO 8601; sections are grouped by behavioral concern, not by sub-package version. Entries dated before the 2026-06 rebrand reference the project's former name (orbit) and its old package names (orbit-db, mcp-orbit, etc.); those are left as-is as accurate historical records.

## Unreleased

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

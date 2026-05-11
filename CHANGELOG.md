# Changelog

All notable changes to orbit-pm are documented in this file. Dates are ISO 8601; sections are grouped by behavioral concern, not by sub-package version.

## Unreleased

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

`mcp__plugin_orbit_pm__get_task` now accepts an optional `session_id` parameter. When provided, the tool atomically writes the `project_state` row in `~/.claude/hooks-state.db` and the per-session `~/.claude/hooks/state/projects/<sid>.json` pointer alongside the task lookup, mirroring the `create_orbit_files` binding pattern shipped in 0.2.11.

**Motivation:** `/orbit:go`'s slash-command bash step (which used to be the sole binding writer for the resume path) can be silently skipped by Claude when it streams past Step 4 to the next instruction. The server-side binding makes it impossible to call `get_task` with a session_id without binding, eliminating the failure mode where the user runs `/orbit:go new-project` and the statusline keeps showing the previous project.

**Response shape:**
- When `session_id` is provided: response includes `session_bound: bool` (True on success, False on validation/IO failure).
- When `session_id` is omitted: `session_bound` field is omitted entirely. Existing read-only callers (UI, list views, tests) are unaffected.

**`/orbit:go` updates** to pass the resolved session_id to `get_task` via the same `$CLAUDE_CODE_SESSION_ID` env-var pattern adopted in `commands/save.md` / `commands/done.md` / `commands/new.md`. The Step 4 bash binding stays as defense-in-depth and to refresh the dashboard list view immediately, but the statusline binding is now driven server-side.

### Fixed - Statusline shows stale project after resume at umbrella cwd

When a Claude Code session resumed at a parent directory holding multiple project repos (e.g. `~/work`), the SessionStart hook would unconditionally inherit whatever project the previous session at that cwd was bound to. The inherited binding then routed heartbeats to the wrong task, made the statusline display the wrong project name, and survived subsequent `/orbit:go <other-project>` invocations when the slash command's bash step did not fire correctly.

**Root cause:** `_pickup_previous_session_binding` (`hooks/session_start.py`) used the cwd-session pointer match alone as sufficient evidence to inherit. For umbrella cwds, the previous session's specific project is unrelated to the new session's intent.

**Fix:** added a cwd-compatibility gate. The inherited project is now accepted only when the project's repo path is the current cwd or an ancestor of it (i.e. the new session is sitting *inside* the project repo). If the repo lives *under* the cwd or in an unrelated location, the inherit is skipped and the statusline starts blank - the user resolves intent explicitly via `/orbit:go`.

**Conservative on failure:** if orbit-db is unavailable, the task lookup raises, or the task/repo row no longer exists, the inherit proceeds as before. The gate only fires on affirmative evidence that the inherit is wrong.

**Non-coding tasks:** unaffected. Inherit always proceeds for tasks with no `repo_id`.

**New contract for users:** if you have been relying on a parent cwd auto-inheriting the previous session's project, that behavior is gone. Run `/orbit:go <project>` in the new session to bind explicitly, or `cd` into the project's repo before opening Claude Code.

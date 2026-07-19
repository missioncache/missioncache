# Changelog

All notable changes to MissionCache are documented in this file. Dates are ISO 8601; sections are grouped by behavioral concern, not by sub-package version. Entries dated before the 2026-06 rebrand reference the project's former name (orbit) and its old package names (orbit-db, mcp-orbit, etc.); those are left as-is as accurate historical records.

**Updating:** `uvx --refresh missioncache-install@latest --update` brings everything you have installed current (PyPI packages and the Claude Code plugin). Your data in `~/.missioncache/` is never touched by an update.

## Unreleased

## 2026-07-19.2

Published package versions: missioncache-db 1.0.14, mcp-missioncache 1.0.17, missioncache-dashboard 1.0.9.

### Added - complete and reopen a project from the dashboard (missioncache-db, mcp-missioncache, missioncache-dashboard)

- The project detail modal gains a Complete action with a confirmation step: the project moves to the completed list, its files move to `completed/`, time history is kept, and a fork parent's shared context stays readable for its children (the fork warning is surfaced after completion). Each row in the completed table gains a Reopen button - the undo, moving the project and its files back to active.
- The completion composition (status flip, file move, fork advisory) moved down into missioncache-db as `complete_project` / `reopen_project`, and BOTH the MCP tools (complete_task / reopen_task, unchanged behavior) and the new dashboard endpoints (`POST /api/tasks/{id}/complete`, `/reopen`) delegate to it - one source, two surfaces, no drift. The file-move source resolution also got more robust: the canonical `active/<name>` location is tried before the stored `full_path` (which can carry legacy shapes). missioncache-db bumped to 1.0.14, mcp-missioncache to 1.0.17 with a matching dependency floor, and the dashboard's floor raised to match.
- Dashboard-complete is the administrative complete, same as the MCP tool: it does not run `/missioncache:done`'s final context save - files move as-is. Sessions still bound to a completed project resolve by task id, find nothing active, and drop it from the statusline.

### Changed - statusline Saved cell gets its own color and position (missioncache-dashboard)

- The Saved cell now renders in its own blue instead of sharing Last Action's gray (the two sat side by side and looked identical), and the Project row order is now Project, Saved, Fork of, Last Action.

### Fixed - the dashboard actually starts on a clean machine (missioncache-dashboard)

- Three fresh-install bugs, all invisible on machines that already ran Claude Code or had a populated Python environment, all found by the new CI installer smoke test: (1) `python-multipart` was never declared as a dependency, and FastAPI refuses to even import the app's Form/File upload routes without it - on a clean pipx venv the server could not start at all; (2) server startup connected to `~/.claude/hooks-state.db` without creating the directory, and sqlite cannot create a database file inside a missing directory, so a host with no `~/.claude` yet crash-looped; (3) the statusline's stderr suppression ran at module import rather than in its own main(), so the dashboard server (which imports the statusline module) lost every startup traceback - which is why the two crashes above were SILENT. missioncache-dashboard bumped to 1.0.9.
- The smoke test itself: installs MissionCache from the tree on a clean runner and asserts the dashboard actually serves, on both Linux service paths (systemd unit on the runner, profile-autostart fallback in a genuinely systemd-less container), then asserts the install shape and a clean `--update` re-run, with per-path failure diagnostics.

## 2026-07-19.1

Published package versions: missioncache-dashboard 1.0.8.

### Added - users now learn when a new MissionCache release is out, in three places (missioncache-dashboard, plugin commands)

- MissionCache had no update discovery at all: nothing watched PyPI, so the only way to learn a release existed was reading the repo. Now a small stdlib-only checker compares the installed sentinel packages (missioncache-db, missioncache-dashboard, missioncache-auto - every release to date bumped at least one) against PyPI, cached 6 hours in `~/.missioncache/update-check.json`. A newer LOCAL version (the maintainer machine) does not count as an update, and a fetch failure keeps the previous answer while stamping the check time so offline machines do not re-fetch on every render.
- Three surfaces consume the one cache: the dashboard shows a dismissible banner with the outdated packages and a copyable `uvx --refresh missioncache-install@latest --update` command (dismissal is keyed to the exact newer versions, so the banner returns for the next release; `/api/update-check` serves it); the statusline adds an upgrade cell to the Vitals line linking to the changelog, with zero footprint when current; and `/missioncache:load` adds a one-line Update field to the resume summary, reading the cache only - dashboard-less installs just never see it.

- The Project row gains a "Saved" cell: the last time the project's own context file changed - a `/missioncache:save`, an MCP context update, a pre-compact snapshot, or a manual edit. It always shows the date, never a bare clock time, so a project resumed days after its last save does not read as saved today. The stamp links to the project's Context tab in the dashboard modal, and Last Action moved one cell right to make room. Forks show their own Saved stamp next to the existing "parent updated" signal - one is "my layer", the other is "the shared layer moved under me". missioncache-dashboard bumped to 1.0.8.

## 2026-07-19

Published package versions: missioncache-db 1.0.13, mcp-missioncache 1.0.16, missioncache-install 1.0.6, missioncache-dashboard 1.0.7.

### Fixed - the dashboard service install no longer crashes on systemd-less Linux, and actually runs there (missioncache-dashboard)

- On a machine where systemd is not PID 1 - the WSL default unless enabled in /etc/wsl.conf - `missioncache-dashboard install-service` crashed with a raw traceback ("Failed to connect to bus") because it ran systemctl unconditionally, and it left an orphan unit file behind since the unit was written before the first systemctl call. Found on a fresh WSL Ubuntu install.
- Linux service registration now checks for systemd first and, when absent (or when systemctl fails despite systemd being present), falls back to a profile autostart: a managed marker block in `~/.bash_profile`/`~/.profile` that starts the dashboard on login if it is not already running, plus an immediate background start so the dashboard works the moment the install finishes. Uninstall removes the block and stops the process; the fallback also cleans up an orphan unit left by the pre-fix version. missioncache-dashboard bumped to 1.0.7.

### Fixed - the Codex install actually works end to end, and non-Claude commands can no longer touch Claude's session state (missioncache-install, plugin commands)

- Codex rejected the generated marketplace manifest outright - `"authentication": "OFF"` is not a valid policy value on codex 0.144.1 (expected ON_INSTALL or ON_USE) - so the slash-command install failed at registration on every machine. The key is simply omitted now (no auth is the default), verified live.
- Activation goes through `codex plugin add missioncache@missioncache` instead of hand-appending a bare config stanza. The bare stanza never populated Codex's plugin cache, so even when registration succeeded the plugin listed as "not installed" and the commands never loaded. The uninstall path gained the matching `codex plugin remove` (legacy stanza cleanup kept for old installs).
- The Codex MCP registration now sets `default_tools_approval_mode = "approve"` on the missioncache server (inserted into the existing section too, not just fresh installs). Without it every tool call needed per-call approval, which `codex exec` auto-cancels - so every slash command died on its first MCP call. An existing user-chosen value is never overwritten.
- The non-Claude command render strips Claude-session machinery instead of shipping it: `<!-- claude-code-only -->` regions in the command sources (session-id resolution bash, statusline/binding registration steps) are dropped whole, and a runtime notice is inserted telling the executing model to skip session machinery and omit session_id arguments. This is not just dead-weight removal - the leaked resolver's transcript-mtime fallback picks whatever LIVE Claude session touched the cwd last and writes its binding: a Codex run hijacked a real Claude session's project binding this way. Verified live after the fix: a Codex /missioncache-load leaves every binding file untouched.
- A failed Codex commands install now raises instead of warn-and-return, so it lands in the failed list and the end-of-install summary stops rendering a green checkmark for a component that never installed. The same honesty now applies across the installer: a registration failure in any of the three MCP integrations (failed prereq install, failed codex mcp add, unparseable or comment-bearing config the installer refuses to rewrite) fails the component and the exit code, instead of warning into a green checkmark - and a commands component whose MCP parent failed in the same run fails alongside it rather than "skipping" into a checkmark of its own. Skips for genuinely absent tools stay skips.
- Review hardening on the same change: rendering fails loudly on unbalanced claude-code-only markers (an unpaired marker previously leaked the session bash silently - the regex just stopped matching), and a sweep test renders all six real command sources and rejects any surviving Claude-state access; it immediately caught two more leaks (save.md's fork restamp bash, load.md's shared-seen marker read) plus mode.md's unmarked resolver instruction, all now wrapped. The Codex plugin version went to 1.1.0 (Codex caches the plugin by version, so command-shape changes must bump it or updated installs keep serving the old cache) and a same-version "already installed" now refreshes via remove and re-add. The config-toml approval edit survives a header with no trailing newline, validates its result with tomllib before writing (refusing edits that would produce unparseable TOML), treats a commented-out approval key as the user's choice, and warns instead of aborting the install when the write itself fails.

### Fixed - a session's explicit project binding is no longer vetoed by cwd, and now carries a durable task identity (missioncache-db, mcp-missioncache, plugin hooks)

- `find_task_for_cwd` required the session's cwd to sit inside the bound project's registered repo before honoring the binding, so any project worked from outside that repo - normal for cross-repo work, and the default for forks, which inherit the parent's repo_id - silently lost pre-compact snapshots, heartbeat time tracking, and session-start detection. The binding is explicit (the user named the project via /missioncache:load), so cwd carries no signal there and no longer vetoes resolution. Found live on a fork worked from outside its inherited repo; the failing session resolves correctly after the fix.
- The binding file now records the task id, not just the name, and resolution prefers it - so the binding survives project renames and can never route a session's snapshots into an unrelated project that happens to reuse a completed project's name (a dead id resolves to nothing rather than falling back to the name). Name-only bindings from before this change still resolve by name as legacy, guarded: an ambiguous name (two active projects sharing it) refuses to guess and leaves a stderr breadcrumb at the one chokepoint every consumer inherits - heartbeats, stop, session-start included - instead of time tracking silently vanishing.
- The binding file's path and format have a single owner now: `missioncache_db.session_binding_path` / `write_session_binding` / `read_session_binding`. The session-start hook, the MCP server's binding, and the pre-compact check all go through them (the load command's bash fallback matches the format and now carries the task id too, so it no longer clobbers the server's richer binding with a name-only one).
- The PreCompact hook's first bail now distinguishes three cases instead of one silent exit: no binding on file (benign, stays silent), a binding whose resolution came back empty (stale id, project completed elsewhere, duplicate name - writes the sticky error that /missioncache:load surfaces on next resume), and a binding file that exists but cannot be read (also a sticky error - a corrupt binding must alarm, not masquerade as "not on a project"). missioncache-db bumped to 1.0.13 and mcp-missioncache to 1.0.16 with a matching dependency floor, since the MCP server reaches the same resolution in find_task_for_directory and heartbeat recording.

### Fixed - the PreCompact snapshot never fired: it looked for the project files in the wrong place (plugin hooks)

- The PreCompact hook resolved the project directory as repo path + active/name - path math from before the data-dir migration to ~/.missioncache. The directory never existed, the hook bailed at its existence check, and no Pre-Compact Snapshot has ever landed since the hook's redesign (which shipped the same day as the migration). Manual and auto compaction were equally affected, silently. It now resolves through MISSIONCACHE_ROOT like every other component, and the needless get_repo call (the join's only consumer, and a lock-contention failure source) is gone.
- The session-start context message had the same wrong join, so its "MissionCache files:" tip line never rendered. Fixed the same way.
- The hook tests passed throughout because the fixture placed the task directory under the mock repo path - replicating the bug's world instead of the real layout. The fixtures for both hooks now build the real ~/.missioncache layout with the repo deliberately elsewhere, and both halves of the fix were mutation-tested: re-introducing the wrong join in either hook turns the suite red.
- Because this class of failure was silent for months, the hook now leaves a breadcrumb: when an active project's directory or context file is missing at snapshot time, it writes the sticky error that /missioncache:load surfaces on the next resume, instead of bailing quietly.

### Changed - the docs stop overpromising Windows support (docs, site)

- The FAQ, README, and hooks doc now say plainly that the lifecycle hooks - including the pre-compaction snapshot and session tracking - do not run on Windows yet (they are registered as `python3` commands, a name most Windows setups lack, and the snapshot hook takes a Unix-only fcntl lock). Previously only the dashboard service registration was flagged. A Windows port is tracked as follow-up work.

### Changed - the fork staleness indicator says who, when, and clears on read (missioncache-dashboard, mcp-missioncache)

- The statusline's fork note was an orange "shared updated" that borrowed the compact-now alarm color, named no time, and only cleared on the next /missioncache:load. It is now a cyan "parent updated HH:MM" (the fork-family accent used by the dashboard tree, not an alarm), showing the parent change's local wall-clock time - absolute on purpose, since the statusline only re-renders on conversation events and a relative "25m ago" would sit frozen while idle. Changes not from today render in the Last Action cell's month-name form (Jul 14 14:32), which stays unambiguous internationally.
- Reading the parent now clears the note immediately: get_context_digest restamps the calling session's shared-seen marker when that session's bound project is a fork of the digested project, so "re-read the shared context" is enough - no /missioncache:load round-trip. The digest response carries a shared_seen_stamped flag, and the stamped mtime is coupled to the exact bytes read. The tool also takes an explicit session_id (like its sibling binding tools) so older and non-Claude clients can stamp, the stamp skips its DB and file probes when the digested project is itself a fork (forks are never parents), and /missioncache:load defers to the server's stamp when it fired instead of overwriting it with an older value from an earlier response. mcp-missioncache bumped to 1.0.15 for the uvx cache.

### Changed - the landing page leads with the problem, not the machinery (site)

- The hero headline is now "Your AI agent forgets everything. Your projects remember." instead of the mission-control metaphor, and the kit chip row moved below the headline and subtitle so the promise reads before the parts list. The meta description and social-card description carry the same problem-first line. The GitHub star button is no longer the loudest button in the hero - the install command is the primary action.
- The fork feature block opens with the situation it solves ("Building several things on the same base?") instead of defining a fork by what it is not. The headline is "Related projects, one shared memory." The FAQ keeps the term definition.
- The compare matrix was corrected against the competitors' current docs: claude-mem works across 7+ agents (was shown as a miss), and Agent Teams' task list does persist locally (was shown as a miss, now a partial with a note). The "use X instead" list dropped from six rows to four.
- Trimmed for density: the stats-counter section and the works-everywhere table are gone (the per-tool support line moved into the compare caption, and the FAQ already covers it), the statusline section went from six cards to three, and the /whats-new blurb is one sentence.
- Accuracy fixes: "Five commands" is now "Five steps" (only three of the five are commands), and the install time claim now matches the measurement - about three minutes to first value, with machine time under 30 seconds.
- The statusline section's screenshot was replaced by a text rendering of the line - same layout, same palette, demo data only - now showing the fork cell with the cyan "parent updated" note on the ios-tests/data-pipeline demo family, plus the full field set: elapsed, K8s context, version watch, effort, split token counts, Claude and Codex limit lines with reset times, and the installed-tool indicators. The old statusline.jpg and its build mapping were removed.
- The hero dashboard screenshot was retaken with the demo data and now shows a fork family: a data-pipeline parent with ios-tests and android-tests forks under it, rail, fork pills, and the "via fork" note included. The demo seeder gained the fork family (parent_id wiring plus plans, contexts with the Fork of header, and task lists) so the shot is reproducible.
- The hero is now a two-column layout: headline and copy on the left, the install terminal with the GitHub and how-it-works buttons on the right, so the whole story sits in one viewport instead of one long list. The headline wraps with balanced lines at every width, the columns top-align, the uvx note sits right under the terminal, and the secondary buttons wear a gradient ring with a hover lift. The terminal itself holds only the install command; the wizard description moved below it as a three-line mono checklist with green checkmarks and white-bold key phrases. Star on GitHub is a white filled button, See how it works is green with a nudging arrow, and the portability chips carry white brand silhouettes above the tool names (from the CC0 simple-icons set, plus OpenCode's own favicon mark).
- Cross-tool portability was promoted to a first-class differentiator, framed as what moves rather than how many tools. Its hero band grew into the page's signature: a "Your project. Every tool." gradient headline over the four tool chips seated on a gradient track, with a glowing project dot that travels from Claude Code to the other tools (CSS only, hidden on small screens and under reduced motion). The hero's third sub was replaced by this rail, with a caption carrying both the promise and the honest boundary. The Why section gained a two-frame handoff proof - the same auth-refactor project saved in a Claude Code frame and loaded in a Codex frame, echoing the amnesia replay's project and next step. The compare row "Works beyond one agent" (a tool-count contest MissionCache loses) became "Whole project moves between tools" (plan + tasks + progress vs tasks-only and memory-only, with the competitors' wider tool counts kept in the notes). The FAQ gained an "Isn't this just cross-agent memory like claude-mem?" entry drawing the shared-state vs recalled-memory line, and the meta description now carries the start-in-one-tool story.

### Added - fork families render as a tree in the dashboard projects table (missioncache-dashboard)

- A project with forks now keeps its own row, and its forks render directly beneath it, joined by a cyan rail that starts at the parent's name line and ends at the last fork. Only the fork rows carry the cyan tint - the parent stays a default row, marked by a forks count pill on its name line. The light theme's fork accent was deepened (and the via-fork note's opacity fade removed) so the 10px badges clear WCAG AA contrast on white.
- The whole family sorts as one unit: saving context on the parent or any fork lifts the group together, and when a fork caused the bump the parent's Updated cell says "via fork". The parent's Time cell shows the family total, with its own share in the tooltip.
- Before this, the table dropped a parent's own row entirely and showed only its children with a small tree glyph.

### Changed - the website describes forks as project forks with shared memory (site, docs)

- "Fork" alone reads like a Claude conversation fork or a git fork, which this is not. The landing page's fork feature block, hover panel, FAQ, and compare row, and the forks doc opener, now say it plainly: a MissionCache fork is a real second project that shares the parent's memory (its context file) and keeps its own tasks.

### Added - website and changelog links plus the running version in the dashboard sidebar (missioncache-dashboard)

- The dashboard sidebar footer now links out to missioncache.dev and to the changelog, next to the theme toggle. These are the dashboard's first outbound links.
- The sidebar shows the running dashboard version. It is read from the installed package metadata through a new `GET /api/version` endpoint, so there is no second copy of the number to fall out of step with the package. `/health` returns the version too, so one curl answers what is running. Note that under an editable install the metadata is a snapshot from the last `pip install -e`, so a maintainer working in the source tree can see an older number than `pyproject.toml` says. Published wheels carry accurate metadata, so this only affects maintainers.

### Fixed - the dashboard reported a stale version in its OpenAPI metadata (missioncache-dashboard)

- The FastAPI app was built with a hardcoded `version="2.0.0"` that had drifted away from the real package version and surfaced stale in `/docs` and `/openapi.json`. It now reads the package version, so the app no longer keeps its own copy of the number.

### Added - a documentation page for project forks (docs)

- New `docs/forks.md` covers what a fork is, when to fork instead of starting a new project or adding a subtask, how the shared context layer works, how parallel sessions stay fresh, and what happens when the parent completes. The "when to fork" section is the part that was missing: it says plainly that two separate projects is the right answer more often than a fork.
- The landing page now carries forks as a first-class feature, with a worked example of two lanes over one shared context.
- Gaps filled around the feature: the bundled rules file and the README command table were both missing `/missioncache:rename`, the rules file was also missing `/missioncache:fork`, the hooks doc did not list the shared-seen state file, and the architecture doc did not explain that `tasks.parent_id` is the fork linkage or that the `**Fork of:**` header regex is mirrored by hand in two files and must stay byte-identical.

### Changed - dark theme is the landing page default, plus a hero tagline and a friendlier uvx note (site)

- The landing page now opens in dark theme for first-time visitors instead of following the OS setting. A saved theme choice still wins, and the toggle works as before.
- A short tagline sits under the hero logo: "Plan, execute, track, and resume - without losing state."
- The install section explains how to get uvx for people who do not have it: it comes with uv, with a copyable `pip install uv` command and the curl installer for machines without Python. The hero carries a one-line pointer to the same answer.

### Fixed - links on the landing page were hard to see (site)

- Text links carried no underline and two of them had no hover state at all, so they read as plain text. Body links are now underlined at rest and strengthen on hover, nav and footer links grow a gradient underline on hover, and every link has a focus ring for keyboard users.
- The hero's kit chips signalled that they open a panel with a faint dotted underline, which was easy to miss. They are now pills with a `+` affordance, each carrying its own icon, and an open chip is filled so you can tell which one you are reading.
- The kit panel used to open fixed at the center of the page. It now opens directly under the chip you are on, with a caret pointing at it, and glides between chips as you move along the row. The panel header repeats the chip's icon so the connection reads at a glance.

## 2026-07-14

Published package versions: missioncache-db 1.0.12, mcp-missioncache 1.0.14, missioncache-dashboard 1.0.6.

### Added - project forks: a shared context layer under a parent project (missioncache-db, mcp-missioncache, missioncache-dashboard, plugin)

- A project can now be created as a fork of an existing parent (`/missioncache:fork`, or `create_missioncache_files(fork_of=...)`). The child carries a `**Fork of:** <parent>` line in its context header, and the parent's context file becomes the shared knowledge layer every child reads. Only context is shared: the parent keeps its own tasks, and each child gets its own new task list.
- The header is the durable source of truth: the repo scan links it into the task hierarchy, re-heals a lost link, and clears the link when the header is removed. Resolution refuses ambiguous name matches and cyclic links rather than guessing.
- Parallel-session freshness: each session keeps a shared-seen marker, `get_context_digest` on a fork returns a `parent_digest` block with `changed_since_seen`, `/missioncache:load` banners when a sibling session updated the shared layer, and the statusline marks the fork ("Fork of <parent>", linked to both projects) with a dot when the shared context is newer than the session's last sync.
- Completing a parent with active forks is allowed and warns; the parent's context stays readable and shared from `completed/`, and its children now surface top-level in the dashboard instead of disappearing with it.

### Added - page headers on every dashboard screen and a Structure-to-Auto link (missioncache-dashboard)

- All four dashboard screens (Projects, Activity, Auto, Settings) now open with the same title-plus-description header, so every view explains itself the way the Auto page already did. The Auto description now names its dual role: the task dependency graph for every active project, and live execution tracking when one runs with missioncache-auto.
- The task modal's Structure tab gained an "Open in Auto" link that jumps to the Auto page with that project's graph pre-selected, via the new `#auto?project=<name>` deep link.

### Added - place statusline addon rows below the Claude status line (missioncache-dashboard)

- New `addons_after_status` statusline setting, with a toggle in the dashboard Settings panel. Off by default, so Claude status stays the footer and existing statuslines are unchanged. Turn it on when your addon rows carry what you scan for first and the status line is the afterthought.
- Previously the row order was fixed: addon rows always rendered above the health line, and no combination of `mode`, `group`, or `order` could move them below it, since `order` only sorts row groups relative to each other.

## 2026-07-13

Published package versions: missioncache-db 1.0.11, missioncache-dashboard 1.0.5.

### Added - user-configurable statusline addons (missioncache-dashboard)

- The statusline can now carry your own cells. An addon names a command to run, and its output renders as a cell; addons are declared in a `statusline_addons` list in the dashboard config and managed from a Settings panel. They are off by default, so the statusline is unchanged unless you add one.
- Each addon either takes its own row (addons sharing a row become columns) or appends to a named existing row. The rendered line count comes from the config rather than from command results, so the statusline keeps a fixed height.
- Addons fail closed: a command that breaks, times out, or is slow renders a blank cell instead of taking down the statusline. Commands run without a shell, must be an existing absolute path, are subject to a per-run timeout plus a TTL cache, and their output is stripped of control characters.

### Added - delete, export, and import projects from the dashboard (missioncache-dashboard, missioncache-db)

- Projects can now be deleted, exported, and imported from the dashboard instead of only from the CLI.
- Delete removes the database record, with heartbeats, sessions, updates, and auto-run logs cascading with it. The on-disk project directory is kept unless you explicitly ask for the files too. Deleting is refused when the project has subtasks (they would be orphaned) or while a missioncache-auto run is in progress.
- Export streams a `.tgz` bundle holding the markdown tree and a manifest; the database itself never travels. Import accepts that bundle and reports whether any repo or vault paths still need mapping to the local machine.
- `TaskDB.delete_task` is new in missioncache-db. The dashboard now requires missioncache-db 1.0.11 or later.

### Changed - dashboard Settings and project assets (missioncache-dashboard)

- The Settings screen was reorganized, and the Projects view gained the delete, export, and import controls.
- The dashboard ships a favicon and a web app manifest with icons, so it can be installed as a standalone app rather than only used in a browser tab.
- Bundled logos and repository screenshots were re-compressed. Smaller download, no visual change.

## 2026-07-11.1

Published package versions: missioncache-dashboard 1.0.4, missioncache-install 1.0.5.

### Security - pinned the markdown renderer with Subresource Integrity (missioncache-dashboard)

- The dashboard now loads `marked` at a pinned version (15.0.12) with an SRI hash, matching how DOMPurify is loaded, so a compromised CDN cannot swap the markdown renderer. Behavior is unchanged from the previously floating latest.

### Fixed - missioncache-install ships a rebuildable sdist (missioncache-install)

- The bundled `rules/`, `user-commands/`, and `commands/` dirs now flow through the sdist via a build hook, so `uv build` produces a working wheel and sdist (previously the two-stage build failed and only a direct wheel could be built).

## 2026-07-11

Published package versions: missioncache-db 1.0.10, mcp-missioncache 1.0.13, missioncache-auto 1.0.2, missioncache-dashboard 1.0.3, missioncache-install 1.0.4.

### Added - missioncache-auto worktree-by-default and refusal guards (missioncache-auto)

- Parallel runs on a git repo now give each worker its own git worktree and branch by default; `--no-worktree` opts back into the shared checkout, and sequential mode is unaffected. Non-git directories warn and fall back to the shared checkout.
- Three pre-run refusals (exit code 3) prevent lost or discarded work: `--no-worktree` + auto-commit + more than one worker, worktrees + `--no-commit`, and worktrees + dirty tracked changes in the main checkout (untracked-only changes are a warning, the run proceeds).

### Changed - dirty worktrees kept on cleanup (missioncache-auto)

- A dirty worktree is left on disk with its branch and a warning so the work stays recoverable, instead of being force-removed.

### Fixed - missioncache-auto auto-commit edge cases (missioncache-auto)

- Auto-commit now detects untracked-only task output via `git status --porcelain` (was `git diff --quiet`, which missed brand-new files).
- `.env*` files are excluded from auto-commits at any nesting depth.

### Changed - statusline context percent, color, and debug handling (missioncache-dashboard)

- Removed the `SYSTEM_OVERHEAD_PERCENT` (+19) add; ctx% now equals Claude Code's `used_percentage` when present (the overhead term remains only in the no-percentage estimated fallback).
- `NO_COLOR` is honored (plain-text render), and the stdin debug log is written only when `MISSIONCACHE_STATUSLINE_DEBUG` is set (was every render).

### Changed - installer writes configs atomically (missioncache-install)

- Config writes are atomic (temp + rename) and leave a one-time `<file>.bak` next to each modified config per run; a partial component failure now exits 1, with components attempted independently.

### Fixed - dashboard hardening (missioncache-dashboard)

- Escaped remaining unescaped user-supplied values (XSS), added error and freshness states, and made interactive elements keyboard-accessible.

### Fixed - hardcoded Asia/Jerusalem timezone removed (missioncache-dashboard)

- Timestamps use the local timezone instead of a hardcoded `Asia/Jerusalem` zone.

### Fixed - process_heartbeats concurrency claim and rollback (missioncache-db)

- `process_heartbeats` rolls back on failure, and its docstring's concurrency claim is corrected to match actual behavior.

### Removed - cleanup command hardcoded migration (missioncache plugin)

- Dropped the hardcoded legacy-path migration step from the cleanup command.

### Fixed - Stop hook edit detection (missioncache plugin)

- The Stop hook now detects edited project files correctly before reminding you to run `/missioncache:save`.

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

### Fixed - version-skew startup break and fence-unaware sibling section writes (missioncache-db 1.0.9, mcp-missioncache 1.0.12)

Two follow-ups from an adversarial review of the context-file conventions work above, both caught before any release:

- **Dependency floor**: `mcp-missioncache` imports `missioncache_db.context_health` at module load, but its declared floor still allowed `missioncache-db>=1.0.6`, where that module does not exist. A user upgrading only the server (leaving `missioncache-db` at 1.0.6 or 1.0.7) would crash at startup with an ImportError before any tool could run - a hard version-skew break, not a degraded feature. The floor is now `missioncache-db>=1.0.9`, the version that carries the section helpers the server calls. The sibling packages are unaffected: the dashboard keeps its own parser copy and missioncache-auto does not import `context_health`, so only the server's floor moved.
- **Fence-awareness extended to the sibling section writers**: the `^`-anchored `_update_section` and `_append_to_section` (Next Steps, Gotchas, Key Files, Key Architectural Decisions) were anchored but not fence-aware, so a column-0 `## <name>` inside a fenced code example appearing before the real section could still be treated as the section and rewritten over on save - the same bug class fixed for Recent Changes in the entry above, left open on its siblings. Both now route through the new `context_health.replace_section_body` / `append_to_section_body`, which locate the heading on fence-masked text. Measured current exposure across all 17 active projects: zero; the fix is for arbitrary users' files that embed the convention as a fenced example.

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

# Hooks

This document covers the four lifecycle events MissionCache hooks into Claude Code: `SessionStart`, `UserPromptSubmit`, `PreCompact`, and `Stop`. Together they are what makes MissionCache's context preservation and time tracking work without the user having to think about them. When you open a Claude Code session inside a MissionCache-tracked repo, the plugin knows what project you are on, records your activity, saves your context before compaction, and reminds you to update your files before you walk away - all of that is hooks.

It assumes you have read [`architecture.md`](./architecture.md) for the shared vocabulary (`tasks.db`, heartbeats, sessions, `hooks-state.db`, `full_path`, the `find_task_for_cwd` resolution order). If a term in this doc is not defined here, it is defined there.

If you are just trying to *use* MissionCache, you already are - hooks run automatically once the plugin is installed. The rest of this doc is for when you want to understand what they do, debug one that is misbehaving, or add your own.

**Windows:** hooks run on native Windows (no WSL). They are registered in [exec form](https://code.claude.com/docs/en/hooks#exec-form-and-shell-form) - `uv` spawned directly with an argument list, no shell involved - so they work identically under Git Bash and PowerShell, and the file locking uses `msvcrt` on Windows instead of the Unix-only `fcntl` (see `missioncache_db/filelock.py`; the PreCompact hook carries its own msvcrt-aware mirror of the lock rather than importing it). **Requires Claude Code 2.1.139+**, where hook `args` (exec form) was added - an older client silently ignores `args` and runs bare `uv`, which fails. There is no plugin-manifest field to enforce a minimum client version, so this is the only place it is stated.

## The hook model

Claude Code's hook API lets a plugin register shell commands to run at specific lifecycle events. The MissionCache plugin registers four of them via `hooks/hooks.json`, in exec form (`command` + `args`):

```json
{
  "hooks": {
    "UserPromptSubmit": [{"hooks": [
      {"type": "command", "command": "uv", "args": ["run", "--no-project", "--python", ">=3.11", "python", "${CLAUDE_PLUGIN_ROOT}/hooks/activity_tracker.py"], "timeout": 5},
      {"type": "command", "command": "uv", "args": ["run", "--no-project", "--python", ">=3.11", "python", "${CLAUDE_PLUGIN_ROOT}/hooks/task_tracker.py"], "timeout": 5},
      {"type": "command", "command": "uv", "args": ["run", "--no-project", "--python", ">=3.11", "python", "${CLAUDE_PLUGIN_ROOT}/hooks/session_title.py"], "timeout": 5}
    ]}],
    "SessionStart": [{"hooks": [
      {"type": "command", "command": "uv", "args": ["run", "--no-project", "--python", ">=3.11", "python", "${CLAUDE_PLUGIN_ROOT}/hooks/session_start.py"], "timeout": 10}
    ]}],
    "PreCompact": [{"hooks": [
      {"type": "command", "command": "uv", "args": ["run", "--no-project", "--python", ">=3.11", "python", "${CLAUDE_PLUGIN_ROOT}/hooks/pre_compact.py"], "timeout": 30}
    ]}],
    "Stop": [{"hooks": [
      {"type": "command", "command": "uv", "args": ["run", "--no-project", "--python", ">=3.11", "python", "${CLAUDE_PLUGIN_ROOT}/hooks/stop.py"], "timeout": 10}
    ]}]
  }
}
```

Each hook is a standalone Python script. Claude Code spawns them as subprocesses with the specified timeout, pipes event data in on stdin as JSON, and reads stdout (for context injection) and stderr (for user-visible reminders). The scripts never persist - they start, do their one job, and exit.

The launcher is `uv run --no-project --python ">=3.11" python <script>` rather than a bare `python3` for three reasons. `uv` is already a hard dependency (the plugin spawns its MCP server via `uvx`), and it is a real `.exe` on Windows - a requirement of exec form, which cannot spawn `.cmd`/`.bat` shims. Exec form passes each argument verbatim with no shell tokenization, so a plugin cache path containing spaces (the default under `C:\Users\<name>\...` for some usernames) cannot break the command. And `--python ">=3.11"` guarantees a modern interpreter everywhere: uv resolves a suitable Python (downloading one on first use if the machine has none), so the hooks stop depending on a `python3` name that most Windows Python installs lack. The installer pre-warms this resolution (`missioncache-install` runs `uv run --no-project --python ">=3.11" python -V` at plugin-install time) so the one-time interpreter download never races a hook's 5-second timeout. The hooks find `missioncache_db` themselves: each script makes the plugin's bundled `missioncache-db/` directory importable - five insert it into `sys.path`, `activity_tracker.py` passes it as `PYTHONPATH` to the `missioncache_db heartbeat-auto` subprocess it spawns - so the interpreter uv picks needs nothing pip-installed.

Six hooks, four events: `UserPromptSubmit` runs *three* scripts (activity_tracker, task_tracker and session_title) in sequence because they have separate concerns but all trigger on the same event. The rest are one-to-one.

### Event-to-hook map

| Event | Hook script | Timeout | What it does |
|-------|-------------|---------|--------------|
| `SessionStart` | `session_start.py` | 10s | Detect active task for cwd, write session state, install bundled rules, emit context block to Claude |
| `UserPromptSubmit` | `activity_tracker.py` | 5s | Record a heartbeat in the DB for time tracking |
| `UserPromptSubmit` | `task_tracker.py` | 5s | Detect task-vs-context divergence and emit a reminder if Claude is forgetting to flip checkboxes |
| `UserPromptSubmit` | `session_title.py` | 5s | Name the session after the project it is bound to, so other sessions can address it |
| `PreCompact` | `pre_compact.py` | 30s | Update context file timestamp and add an "auto-saved before compaction" note |
| `Stop` | `stop.py` | 10s | If files were edited during the session, remind the user to run `/missioncache:save` |

### The bootstrap pattern

Every hook script begins with the same 5-line bootstrap:

```python
# Bundled missioncache-db path for marketplace installs (no system pip install).
_BUNDLED_MISSIONCACHE_DB = Path(__file__).resolve().parent.parent / "missioncache-db"
if _BUNDLED_MISSIONCACHE_DB.is_dir() and str(_BUNDLED_MISSIONCACHE_DB) not in sys.path:
    sys.path.insert(0, str(_BUNDLED_MISSIONCACHE_DB))
```

This is the glue that makes MissionCache's hooks work under a marketplace install where there is no `pip install -e ./missioncache-db` step. The plugin ships `missioncache-db/` as a sibling directory to `hooks/`, and each hook inserts it into `sys.path` before the `from missioncache_db import TaskDB` line so the import resolves against the bundled copy. If you have `missioncache-db` pip-installed for some other reason (say, you are developing it), that install takes precedence because it got there via normal imports first - the bootstrap is additive, not destructive.

This block is *inlined* in every hook rather than factored into a shared `_bootstrap.py` helper because pytest imports hooks as `hooks.session_start` (package-import mode), where relative imports from a sibling helper file do not resolve cleanly at the top of module execution. Duplicating five lines is the cost of keeping the hooks testable in the existing test harness. See the MissionCache project's gotcha notes for the full story.

The `activity_tracker.py` hook is the one exception to this pattern - it uses a *subprocess* path instead of an in-process import, for reasons that are covered in detail in its section below.

## SessionStart: `session_start.py`

**When:** Every time Claude Code starts a new session in a directory. Runs before the first user prompt.

**What it does:**

1. **Write terminal-session mapping.** Looks up `TERM_SESSION_ID` (iTerm2) or `WT_SESSION` (Windows Terminal) from the environment and, if present, writes a row into `hooks-state.db:term_sessions` mapping the terminal tab to the Claude session ID. This is the bridge that lets the statusline find the current session when it runs, because the statusline only gets its session ID from Claude Code's statusline JSON and has no direct access to the SessionStart event.
2. **Install bundled rules.** Runs `install_bundled_rules()`, which walks `${CLAUDE_PLUGIN_ROOT}/rules/*.md` and copies any file starting with `<!-- missioncache-plugin:managed` into `~/.claude/rules/`. The ownership marker is critical: files that already exist in the destination are only overwritten if they *also* start with the marker. A user who deletes the marker takes ownership of that file and the hook stops touching it on subsequent SessionStarts. This is how marketplace installs get rule-file updates without clobbering user edits.
3. **Detect the active task.** Tries `from missioncache_db import TaskDB`, instantiates a DB, calls `db.find_task_for_cwd(cwd, session_id)`. If a task is found, it:
   - Writes `projects/<session-id>.json` with the project name and task id, for the statusline and task resolution.
   - Prints a markdown context block to stdout - this is the "Active Task Detected" banner Claude sees at the top of the session - with the task name, status, time invested, JIRA key, and the path to the MissionCache files.
   - Includes a `/missioncache:load` tip and a task-tracking discipline reminder telling Claude to use `mcp__plugin_missioncache_pm__update_tasks_file` instead of the built-in TaskCreate tool for MissionCache tasks.
4. **Skip silently on failure.** If `missioncache_db` cannot be imported (minimal install, not set up yet, whatever), the hook bails out quietly. Nothing on stdout, nothing in stderr, Claude's session proceeds as if no hook ran. Rules installation still runs independently - it does not depend on `missioncache_db` at all.

**State files written:**

- `~/.claude/hooks/state/term-sessions/<TERM_SESSION_ID>` - Plain-text file containing the Claude session ID. Used by the statusline for mid-session terminal→session resolution on terminals that set `TERM_SESSION_ID`.
- `~/.claude/hooks-state.db:term_sessions` - Same mapping in the SQLite DB. Both formats exist because different readers use different stores.
- `~/.claude/hooks/state/projects/<session-id>.json` - `{projectName, taskId, updated, sessionId}`. The authoritative per-session project pointer, owned by `missioncache_db.write_session_binding` / `read_session_binding`. `taskId` is the durable identity task resolution prefers (immune to name reuse and renames); bindings written before it existed carry only the name and resolve by it as legacy. The statusline reads this, and mid-session `/missioncache:load` also writes it.

**Removed in mcp-missioncache 0.2.13:** the legacy `~/.claude/hooks/state/pending-task.json` file is no longer written by any hook or slash command. Old files left over from pre-0.2.13 installs are harmless and can be deleted by hand. `find_task_for_cwd` reads only `projects/<session-id>.json` and cwd-pattern matching now.

## UserPromptSubmit: `activity_tracker.py`

**When:** Every time the user submits a prompt. Runs *before* Claude sees the prompt - it is a pre-submit hook from the plugin's point of view.

**What it does:** Records a heartbeat in `tasks.db:heartbeats` for time tracking. Exactly one heartbeat per prompt, per session, per MissionCache task (if one is active).

**The subprocess quirk:** Unlike the other hooks, `activity_tracker.py` does not `import missioncache_db` directly. Instead it spawns a subprocess:

```python
subprocess.run(
    [sys.executable, "-m", "missioncache_db", "heartbeat-auto"],
    cwd=cwd,
    timeout=2,
    capture_output=True,
    env=env,
)
```

This is *deliberate* and was reverted to once, during an earlier refactor that tried to inline the heartbeat recording. The problem: `record_heartbeat_auto` has to acquire a SQLite write lock on `tasks.db`, and under contention (concurrent writers from other Claude sessions, MissionCache Auto, the dashboard's sync loop), a single heartbeat call can block for up to 5 seconds waiting on `busy_timeout=5000`. The UserPromptSubmit hook has a 5-second timeout of its own, so an in-process call could eat the entire budget and miss other hooks, or worse, delay the actual prompt submission.

The subprocess form solves this by imposing a hard 2-second wall on the child process. If the SQLite lock does not resolve in 2 seconds, the subprocess is killed via `subprocess.TimeoutExpired`, the exception is swallowed, and the hook returns immediately. Worst case: one heartbeat is lost. The time budget of the parent hook is unaffected.

The `PYTHONPATH` trick in the `env` dict is the subprocess's equivalent of the in-process sys.path bootstrap - it tells the child Python where to find `missioncache_db` when the plugin is marketplace-installed. The subprocess runs `python -m missioncache_db heartbeat-auto`, which is a command defined in `missioncache-db`'s CLI that dispatches to `TaskDB.record_heartbeat_auto(cwd, session_id)`.

**Skip patterns:** Not every prompt counts as "work". The hook skips:

- Slash commands (`^/\w+`)
- Shell commands (`^!\w+`)
- One-word control prompts: `exit`, `clear`, `help`, `y`, `yes`, `n`, `no`
- Empty / whitespace-only prompts

The regex list is in the `SKIP_PATTERNS` constant. It is intentionally conservative - false negatives (not recording a heartbeat when you did something substantive) are preferable to false positives (recording a heartbeat for an "ok" or a slash command), because the heartbeat-to-session aggregator can tolerate missing pings but will inflate time totals if you flood it with no-op events.

**Image attachment gotcha:** The `prompt` field in the hook payload can arrive as either a plain string or a list of content blocks (when the user attaches images). The hook flattens list-form prompts by joining the text blocks:

```python
if isinstance(raw_prompt, list):
    raw_prompt = " ".join(
        b.get("text", "") for b in raw_prompt
        if isinstance(b, dict) and b.get("type") == "text"
    )
```

Without this, `should_skip()` would crash on `prompt.strip()` and the hook would exit with an exception, silently losing the heartbeat. Both UserPromptSubmit hooks have the same flattening logic - it needs to live in both, not in a shared helper, for the pytest import reasons discussed above.

**Subagent detection:** `if data.get("agent_id"): return` - when the hook fires inside a spawned subagent's context, it skips recording. Subagents are short-lived and their activity is already attributable to the parent session; recording heartbeats for them would double-count time.

## UserPromptSubmit: `task_tracker.py`

**When:** Every user prompt, same trigger as `activity_tracker.py`. The two run in sequence but do not share any state - they are independent concerns on the same event.

**What it does:** Detects a specific failure mode: Claude has been appending findings to `<project>-context.md` under `### Task N` headings but forgetting to flip the corresponding `- [ ] N.` checkbox in `<project>-tasks.md`. When this divergence is detected, the hook prints a reminder to stdout that Claude sees as part of the prompt context.

The rationale is embedded in the module docstring and worth quoting:

> Claude instances tend to treat the context file as the live progress ledger (appending findings under `### Task N` headings) but forget to flip the corresponding checkbox in the tasks file. The statusline progress display `[X/Y]` shows the user this divergence, but Claude can't see its own statusline - so this hook injects the same signal into Claude's context.

**How it works:**

1. Parse `<project>-tasks.md` for pending `- [ ] N.` lines - these are tasks still marked as not done.
2. Parse `<project>-context.md` for `### Task N` headings - these are tasks Claude has already written findings for.
3. Intersect the two sets. Task numbers in both sets are "divergent" - the findings are there, but the checkbox is not flipped.
4. If the intersection is non-empty, print a reminder listing the divergent task numbers and an exact `mcp__plugin_missioncache_pm__update_tasks_file(...)` invocation Claude can paste.

The reminder ends with an explicit callout:

> Important: the built-in TaskCreate tool and any system reminders about "task tools" refer to Claude Code's in-conversation todo list, NOT the MissionCache tasks file.

This is there because Claude Code (the harness) periodically injects system reminders pointing at the internal `TaskCreate` tool, and Claude occasionally follows them instead of using the MissionCache MCP tool. The hook pushes back.

**Same skip patterns as activity_tracker**, same subagent guard, same list-vs-string prompt flattening. These two hooks mirror each other in structure because they run back-to-back and share the same input shape.

**Why this is not a feature of the MCP tool:** The divergence check has to run *on every prompt*, not on every `update_tasks_file` call. You can only detect "Claude forgot to flip the checkbox" by looking at the state of the two files on a schedule, and the only schedule Claude Code exposes to plugins is the hook event stream. Putting the check in an MCP tool would mean Claude has to proactively ask "am I drifting?" which is exactly the thing it forgets to do.

## UserPromptSubmit: `session_title.py`

**When:** Every user prompt, alongside the other two UserPromptSubmit hooks.

**What it does:** Sets the Claude Code session title to the name of the MissionCache project the session is bound to, by printing `{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "sessionTitle": "<project>"}}` to stdout.

**Why the title matters:** Claude Code's cross-session `SendMessage` (2.1.224+) addresses a peer session *by its title* - the title is the address. Left alone, Claude Code derives it from the session's first prompt, so a session working on `avc-in-house-testing` can be called anything at all and nothing can reliably reach the session that owns a given project. `missioncache_db.live_sessions_for_project` exists to answer "which live sessions are on this project", and its answer is only useful if those sessions carry addressable names.

**Where the binding comes from:** `project_state` in `hooks-state.db`, via `missioncache_db.bound_project_for_session`. Deliberately *not* the `projects/<session-id>.json` pointer, even though that is the more obvious source: the pointer is also written from cwd auto-resolution at SessionStart, so merely opening a project's repo would claim that project's title while the session stayed invisible to `live_sessions_for_project` (which reads `project_state`). Both ends key off the same table so that "titled", "addressable" and "notified" describe one set of sessions.

**Re-emit only when the computed address changes.** The hook records what it applied in `~/.claude/hooks/state/session-title/<session-id>.json` as `{sessionId, title, projectName, updated}`, recomputes the title every prompt, and emits only on a difference. So:

- Steady state (same project, same peers) is silent, which is what lets a user's manual `/rename` survive day to day.
- A mid-session `/missioncache:load other-project` retitles on the next prompt, because the computation changed.
- A stale suffix self-heals: a session left holding `<project>-2` after the plain-name holder died drops back to `<project>` on its next prompt. Without this, suffixes only accumulate (a real machine reached `-3` with zero live peers) and the recorded address stops matching any `ListAgents` row.
- The accepted narrow cost: a manual `/rename` is overwritten when the peer set changes (a collision appears or a suffix frees up), not only on rebind. An unaddressable session fails every notify; a clobbered rename costs one repeated `/rename`.

**Collision suffixes.** Two sessions can legitimately be on one project, and then one title cannot serve both. When another *live* session has already recorded the plain project name, this one takes the lowest free `-2` / `-3` suffix (`missioncache_db.choose_session_title`). Suffixes are computed against live sessions only, so a closed session frees its number. Two sessions taking their first prompt in the same instant can both land on the same suffix; that degrades to two identically-named `ListAgents` rows, which the caller disambiguates with the per-row ref.

**No skip patterns.** Unlike `activity_tracker.py` and `task_tracker.py`, this hook runs on *every* prompt including slash commands. (It does keep the subagent guard those hooks have: a subagent would otherwise retitle the parent session it runs under.) Those hooks skip slash commands to keep heartbeat time honest; skipping them here would mean `/missioncache:load` - the very command that creates the binding - never triggers the retitle.

**State files written:** `~/.claude/hooks/state/session-title/<session-id>.json`, whose path is owned by `missioncache_db.session_title_path`.

## PreCompact: `pre_compact.py`

**When:** Claude Code is about to auto-compact the conversation. This happens when the context window fills up, and the compaction replaces older messages with a summary. The hook fires *before* the compaction happens, giving plugins one last chance to save state.

**What it does:**

1. Find the active task via `find_task_for_cwd`. If no task, return.
2. Find the context file under `~/.missioncache/<task.full_path>/<task.name>-context.md` (or the bare `context.md` fallback for subtask layouts).
3. Update the "Last Updated" timestamp line.
4. Add an `- Auto-saved before compaction (<timestamp>)` bullet. If a `## Recent Changes` section exists, add it there; otherwise append a new section at the end.
5. Write the file back.
6. Call `db.process_heartbeats()` to flush any accumulated heartbeats into the `sessions` table, so the dashboard time totals are current before compaction.

The auto-save note is the signal: when you come back to the project later via `/missioncache:load`, you can see in the context file exactly when it was auto-saved and how many compactions have happened in the current work session. In practice you almost never look at the auto-save line - it is there for reconstructing "what happened" in pathological cases.

**The 30-second timeout** is generous because `process_heartbeats` can touch a lot of rows on a busy session, and the compaction itself does not block on the hook - Claude Code fires the hook, waits up to 30s, then compacts regardless. The hook should finish in well under a second in practice, but the budget is there for outliers.

**Why not save more aggressively:** You might expect the PreCompact hook to write a richer snapshot - a synthesized "Next Steps" section, a learnings summary, a `Recent Changes` block populated with what actually happened. It does not, because generating that content requires talking to Claude, and PreCompact is not the right moment for that: Claude is about to compact *because it is out of context*, and there is no budget for synthesizing a summary. The hook only does mechanical things that do not require the LLM.

The richer save path is `/missioncache:save`, which is a slash command the user (or Claude) invokes explicitly. PreCompact is the backstop that ensures even a forgotten `/missioncache:save` leaves *some* trace in the context file.

## Stop: `stop.py`

**When:** Claude has finished responding to the user's prompt and Claude Code is about to return control to the terminal (the "stop" event). Runs once per message exchange, after Claude's reply is already shown.

**What it does:** Checks whether Claude made any file edits during the exchange. If yes, and there is an active MissionCache task with MissionCache files on disk, prints a reminder to stderr:

```
---
**MissionCache Reminder:** You made file edits while working on **<task-name>**.
Consider running `/missioncache:save` to save context before ending your session.
---
```

**How it detects edits:** The hook reads the transcript file at `input_data["transcript_path"]` (Claude Code's per-session JSONL log) and grep-strings for `"tool_use"` co-occurring with `"Write"` or `"Edit"`. This is intentionally approximate - a proper parser would be more expensive and the goal is just "did anything get written?", not a precise edit count. A false positive (reminder fires when nothing was actually modified) is annoying but harmless; a false negative (no reminder when edits happened) misses the point of the hook.

**Why stderr:** Stop hook output goes to stderr specifically because Claude Code treats stderr from Stop hooks as *user-facing* messages - they are shown to the human in the terminal after Claude's reply, not injected back into Claude's context. This is the right channel for a "remember to save" nudge, because it is meant for the human, not Claude. Compare with UserPromptSubmit and SessionStart, which write to stdout specifically to land in Claude's context.

**The stop hook does not fail the stop event.** If anything inside the try block crashes, the bare `except Exception: pass` swallows it. Stop hooks that fail can apparently cause weird Claude Code behavior, and a reminder failing is not worth breaking the session over.

## State files: the full picture

Hooks write to a surprising number of places. Here is the complete map:

| Path | Written by | Read by | Format |
|------|-----------|---------|--------|
| `~/.missioncache/tasks.db:heartbeats` | activity_tracker (via missioncache_db subprocess) | missioncache_db aggregator, dashboard | SQLite row |
| `~/.missioncache/tasks.db:sessions` | missioncache_db `process_heartbeats` (called by pre_compact) | dashboard, MissionCache MCP `get_task_time` | SQLite row |
| `~/.claude/hooks-state.db:term_sessions` | session_start | statusline | SQLite row |
| `~/.claude/hooks-state.db:session_state` | statusline (not hooks) | statusline | SQLite row |
| `~/.claude/hooks-state.db:project_state` | `/missioncache:load`, dashboard `/api/hooks/project`, `get_task` (when called with session_id) | statusline, `bound_project_for_session` (session_title hook), `live_sessions_for_project` | SQLite row |
| `~/.claude/hooks/state/term-sessions/<term-id>` | session_start | statusline fallback path | Plain text |
| `~/.claude/hooks/state/projects/<session-id>.json` | session_start, `/missioncache:load`, `get_task` (when called with session_id) | statusline, `find_task_for_cwd` | JSON file |
| `~/.claude/hooks/state/session-pids/<session-id>.json` | session_start `write_session_pid` | `missioncache_db.session_is_alive` (parallel-session detection, live-session lookup) | JSON file |
| `~/.claude/hooks/state/session-title/<session-id>.json` | session_title | `missioncache_db.live_sessions_for_project` | JSON file |
| `~/.claude/hooks/state/shared-seen/<session-id>.json` | `/missioncache:fork` (seeds it), `/missioncache:load`, `/missioncache:save` | statusline | JSON file |
| `~/.claude/rules/*.md` | session_start `install_bundled_rules` | Claude Code (auto-loaded) | Markdown files with ownership marker |

The shared-seen marker records the parent-context mtime this session last read. That is how the statusline knows whether to show the cyan `● parent updated HH:MM` note on a fork, and how `/missioncache:load` knows whether to banner that a parallel session changed the shared layer. Besides the slash commands, `get_context_digest` restamps it when a fork session reads its parent's digest. See [`forks.md`](./forks.md).

**Invariant to be aware of:** `pending-task.json` and `pending-project.json` appear in git history but are no longer written or read by any current code path (`pending-task.json` removed in mcp-missioncache 0.2.13; `pending-project.json` writers were already removed earlier). The live per-session pointer is `projects/<session-id>.json`. Do not rely on either pending file.

## The HTTP hook path

Beyond the plugin-registered hooks in `hooks.json`, MissionCache also uses a second hook-wiring mechanism: Claude Code's native `"type": "http"` hook form in `~/.claude/settings.json`. This is a *user-level* registry - not part of the plugin manifest - where Claude Code POSTs to HTTP endpoints on every hook event without going through Python at all.

The MissionCache Dashboard exposes these endpoints:

| Endpoint | Caller | What it does |
|----------|--------|--------------|
| `POST /api/hooks/edit-count` | `PostToolUse` HTTP hook wired by `missioncache-install` when the dashboard is installed (matcher `Edit\|Write\|NotebookEdit`) | Updates `session_state.edit_count` in `hooks-state.db` for the statusline edit counter |
| `POST /api/hooks/task-created` | MissionCache MCP server (`create_task`, `create_missioncache_files`) | Triggers immediate SQLite → DuckDB sync so new projects show in the dashboard without the up-to-60s background-sync lag |
| `POST /api/hooks/heartbeat` | Optional - power-user `UserPromptSubmit` HTTP hook wiring | Records a heartbeat. Plugin already records heartbeats via `activity_tracker.py`'s subprocess path, so wiring this on top just duplicates them |

`edit-count` is wired automatically by `missioncache-install` when the dashboard is installed, so full-install users get the statusline edit counter out of the box. `task-created` is called internally by the MCP server, not by a user-level HTTP hook. `heartbeat` is only of interest if you specifically want two parallel heartbeat paths.

**If you are auditing "which endpoints are actually used by hooks", grep `~/.claude/settings.json` in addition to the plugin source.** Claude Code's `"type": "http"` hook form is wired in settings.json and isn't visible from `hooks.json` or the plugin tree.

## The `MISSIONCACHE_AUTO_MODE` signal

When MissionCache Auto spawns Claude CLI subprocesses, it sets `MISSIONCACHE_AUTO_MODE=1` in the child environment. Hooks do not currently read this variable *themselves*, but it is the signal that various user-level behaviors check to differentiate autonomous runs from interactive ones. For example:

- `~/.claude/hooks/permission-whitelist.sh` auto-approves `ExitPlanMode` transitions when `MISSIONCACHE_AUTO_MODE=1`, because autonomous runs should not block on plan approval.
- Slash commands and skills may skip clarification questions when the variable is set.

The variable is not magic - it is a plain environment variable - but it is the contract between MissionCache Auto and the rest of the plugin ecosystem. If you want your own hook or skill to behave differently under autonomous execution, check `os.environ.get("MISSIONCACHE_AUTO_MODE") == "1"`. If you are writing a new MissionCache Auto mode, set the variable in the child environment and let downstream consumers opt in.

## Adding a new hook

If you have a new event you want to hook into, the pattern is straightforward:

1. **Add the hook command in `hooks/hooks.json`.** Pick the event (`SessionStart`, `UserPromptSubmit`, `PreCompact`, `Stop`, or another event Claude Code supports). Copy an existing exec-form entry (`"command": "uv"` plus the `args` list) and change only the script filename, with a reasonable `timeout`. Hooks that already have multiple scripts (like `UserPromptSubmit`) take a list; new events need a new top-level key.
2. **Create the script under `hooks/<your_hook>.py`.**
   ```python
   #!/usr/bin/env python3
   """What this hook does - one sentence."""

   import json
   import sys
   from pathlib import Path

   # Bundled missioncache-db path for marketplace installs (no system pip install).
   _BUNDLED_MISSIONCACHE_DB = Path(__file__).resolve().parent.parent / "missioncache-db"
   if _BUNDLED_MISSIONCACHE_DB.is_dir() and str(_BUNDLED_MISSIONCACHE_DB) not in sys.path:
       sys.path.insert(0, str(_BUNDLED_MISSIONCACHE_DB))

   def main():
       try:
           data = json.load(sys.stdin)
       except (json.JSONDecodeError, EOFError):
           return
       # ... do work ...
   ```
3. **Decide stdout vs stderr.** stdout goes back into Claude's context (for SessionStart and UserPromptSubmit - Claude sees it before or with the next prompt). stderr is shown to the human in the terminal (for Stop, mainly). Pick based on who the message is for.
4. **Never raise across the hook boundary.** Wrap everything in try/except with swallowing `pass` at the outermost level. A hook crash may or may not break the parent event depending on Claude Code's tolerance for non-zero exits, but in any case it is never worth failing the session over a telemetry hook.
5. **Never block longer than the timeout.** Honor the timeout you declared in `hooks.json`. If your hook does I/O, make sure the I/O itself has a shorter timeout than the hook (e.g., `timeout=3` on a subprocess inside a 5-second hook).
6. **Reinstall the plugin.** `claude plugins install missioncache@local` and restart Claude Code. The hook registry is re-read on plugin load.
7. **Add a test.** `hooks/tests/` has fixtures for mocking `missioncache_db` via `patch.dict('sys.modules', {'missioncache_db': MagicMock()}) + importlib.reload(mod)`. Any new hook that imports `missioncache_db` at the top of the module will break mocking; keep the import lazy (inside `main()`) or stick with the in-process bootstrap the existing hooks use.

## Troubleshooting

### "No hooks fire at all, and the MCP tools are missing too"

**Cause:** the hooks launch through `uv` (exec form resolves `command` on the PATH Claude Code itself runs with), and the MCP server spawns through `uvx` from that same environment. If `uv` is not on that PATH - common when Claude Code is launched from a GUI/dock icon that does not inherit your shell's PATH - every hook silently fails to spawn and the MCP server never starts.

**Fix:** make `uv` resolvable from Claude Code's environment: install it to a location already on the system PATH, or launch Claude Code from a shell where `uv --version` works. On a machine where only some launch methods see `uv`, the tell is that hooks and MCP tools work from a terminal-launched session but not a GUI-launched one.

### "Hooks do nothing right after a plugin-only install"

**Cause:** the hooks' `uv run --python ">=3.11"` launcher downloads a managed Python on first use when the machine has no suitable interpreter. `missioncache-install` pre-warms that download, but a marketplace-only install (`claude plugins install` alone) skips the warm, and the first hook fire can lose the race against its 5-second timeout - mostly a fresh Windows box; macOS/Linux usually have a system 3.11+ that uv discovers without downloading.

**Fix:** run `uv python install 3.13` once, then hooks fire normally from the next prompt. (Running the full `uvx missioncache-install` also warms it, but only on a first install - `--update` on a marketplace-only setup finds nothing in its state file and returns without warming.)

### "SessionStart doesn't show the active task banner"

**Cause:** Either `missioncache_db` failed to import (bundled path wrong, Python version mismatch), or `find_task_for_cwd` returned `None` for the current directory, or the hook crashed in the try block and was silently swallowed.

**Fix:** Run the hook manually, with the same launcher hooks.json uses: `echo '{}' | uv run --no-project --python ">=3.11" python ${CLAUDE_PLUGIN_ROOT}/hooks/session_start.py` and watch for import errors or exceptions. (On macOS/Linux a plain `echo '{}' | python3 session_start.py` also works when your system Python is 3.11+, but the uv form reproduces exactly what Claude Code spawns.) Most commonly the answer is "your cwd is not matching any MissionCache task" - check `~/.missioncache/active/` for a project whose `full_path` corresponds to your cwd, or use `mcp__plugin_missioncache_pm__find_task_for_directory` from a live Claude session to see what it returns.

### "Heartbeats aren't being recorded"

**Cause:** Four possibilities. In order of likelihood:
1. The hook is timing out. `activity_tracker.py` has a 5-second budget and spawns a 2-second subprocess - if the SQLite lock is contended beyond 2s, the subprocess gets killed.
2. `tasks.db` is locked or corrupted. Check `sqlite3 ~/.missioncache/tasks.db "SELECT count(*) FROM heartbeats"`.
3. `find_task_for_cwd` returned no task, so there is nothing to attribute a heartbeat to.
4. The prompt matched a skip pattern (slash command, shell command, one-word control).

**Fix:** Run `activity_tracker.py` in isolation with a representative stdin payload and capture the output. Check the session transcript JSONL (`~/.claude/projects/<sanitized-cwd>/<session-id>.jsonl`) for hook stderr breadcrumbs and missioncache-db errors. For contention issues, the answer is almost always "wait and retry" - lock contention that exceeds the 2-second budget is rare enough to ignore.

### "Task tracker reminder won't go away even after I flip the checkbox"

**Cause:** The hook matches `### Task N` headings in the context file. If you flipped the checkbox but the context file still has `### Task N: ...` for that number, the hook still fires.

**Fix:** Either remove the heading from the context file (if the finding is no longer relevant), or reword it so it does not match the `### Task N` pattern. The hook's regex is `^###\s+Task\s+(\d+)` - anything that does not start with `### Task <number>` is invisible to it.

### "PreCompact never fires"

**Cause:** PreCompact only fires when Claude Code *auto*-compacts. Manual compaction via `/compact` does not fire PreCompact.

**Fix:** This is by design. If you want to save state on manual compaction, run `/missioncache:save` explicitly before `/compact`. (There is no hook for `/compact` - it is not a plugin-observable event.)

### "Stop reminder fires when I didn't actually edit anything"

**Cause:** The edit detection is a string grep on the transcript file for `"tool_use"` co-occurring with `"Write"` or `"Edit"`. If the transcript includes a tool use that *mentioned* those tool names (for instance, Claude reading a doc about the Write tool), the grep fires a false positive.

**Fix:** Ignore the reminder. It is annoying but harmless. A proper fix would require parsing the JSONL transcript and inspecting actual tool invocations, which is more work than the reminder is worth.

### "Rules in `~/.claude/rules/` aren't getting updated after a plugin upgrade"

**Cause:** The file in `~/.claude/rules/` does not start with the `<!-- missioncache-plugin:managed` marker, so `install_bundled_rules` treats it as user-owned and leaves it alone.

**Fix:** Delete the file from `~/.claude/rules/` - the next SessionStart will reinstall it from the plugin's copy (which does have the marker). Alternatively, edit your file to start with `<!-- missioncache-plugin:managed -->` as the first line - but that means your local changes will be overwritten on the next plugin update.

### "Hook test breaks because missioncache_db is imported at the top of the module"

**Cause:** The pytest mocking pattern in `hooks/tests/` is `patch.dict('sys.modules', {'missioncache_db': MagicMock()}) + importlib.reload(mod)`. This only works if the import is lazy (inside `main()`) or if the `sys.path` bootstrap is the first thing that runs.

**Fix:** Move the `missioncache_db` import inside `main()` so reload sees the mock. If you are adding a new hook, follow the lazy-import pattern used in `session_title.py`, `session_start.py`, `pre_compact.py`, `stop.py`, and `task_tracker.py`. Only `activity_tracker.py` uses a non-lazy pattern, and even that one calls `missioncache_db` via subprocess rather than `import`.

## Where to go from here

- [`architecture.md`](./architecture.md) - for the shared context on `hooks-state.db`, `tasks.db`, and how the pieces fit together.
- [`dashboard.md`](./dashboard.md) - for the HTTP hook endpoints and how they overlap with the plugin-registered path.
- [`missioncache-auto.md`](./missioncache-auto.md) - for the `MISSIONCACHE_AUTO_MODE` signal and how autonomous runs interact with hooks.
- `hooks/hooks.json` - the source-of-truth registry.
- `hooks/*.py` - the scripts themselves. Each is short (under 250 lines) and standalone.

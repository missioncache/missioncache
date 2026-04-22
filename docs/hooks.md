# Hooks

This document covers the four lifecycle hooks orbit registers with Claude Code: `SessionStart`, `UserPromptSubmit`, `PreCompact`, and `Stop`. Together they are what makes orbit's context preservation and time tracking work without the user having to think about them. When you open a Claude Code session inside an orbit-tracked repo, the plugin knows what project you are on, records your activity, saves your context before compaction, and reminds you to update your files before you walk away - all of that is hooks.

It assumes you have read [`architecture.md`](./architecture.md) for the shared vocabulary (`tasks.db`, heartbeats, sessions, `hooks-state.db`, `full_path`, the `find_task_for_cwd` resolution order). If a term in this doc is not defined here, it is defined there.

If you are just trying to *use* orbit, you already are - hooks run automatically once the plugin is installed. The rest of this doc is for when you want to understand what they do, debug one that is misbehaving, or add your own.

## The hook model

Claude Code's hook API lets a plugin register shell commands to run at specific lifecycle events. The orbit plugin registers four of them via `hooks/hooks.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [{"hooks": [
      {"type": "command", "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/activity_tracker.py", "timeout": 5},
      {"type": "command", "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/task_tracker.py", "timeout": 5}
    ]}],
    "SessionStart": [{"hooks": [
      {"type": "command", "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/session_start.py", "timeout": 10}
    ]}],
    "PreCompact": [{"hooks": [
      {"type": "command", "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/pre_compact.py", "timeout": 30}
    ]}],
    "Stop": [{"hooks": [
      {"type": "command", "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/stop.py", "timeout": 10}
    ]}]
  }
}
```

Each hook is a standalone Python script. Claude Code spawns them as subprocesses with the specified timeout, pipes event data in on stdin as JSON, and reads stdout (for context injection) and stderr (for user-visible reminders). The scripts never persist - they start, do their one job, and exit.

Five hooks, four events: `UserPromptSubmit` runs *two* scripts (activity_tracker and task_tracker) in sequence because they have separate concerns but both trigger on the same event. The rest are one-to-one.

### Event-to-hook map

| Event | Hook script | Timeout | What it does |
|-------|-------------|---------|--------------|
| `SessionStart` | `session_start.py` | 10s | Detect active task for cwd, write session state, install bundled rules, emit context block to Claude |
| `UserPromptSubmit` | `activity_tracker.py` | 5s | Record a heartbeat in the DB for time tracking |
| `UserPromptSubmit` | `task_tracker.py` | 5s | Detect task-vs-context divergence and emit a reminder if Claude is forgetting to flip checkboxes |
| `PreCompact` | `pre_compact.py` | 30s | Update context file timestamp and add an "auto-saved before compaction" note |
| `Stop` | `stop.py` | 10s | If files were edited during the session, remind the user to run `/orbit:save` |

### The bootstrap pattern

Every hook script begins with the same 5-line bootstrap:

```python
# Bundled orbit-db path for marketplace installs (no system pip install).
_BUNDLED_ORBIT_DB = Path(__file__).resolve().parent.parent / "orbit-db"
if _BUNDLED_ORBIT_DB.is_dir() and str(_BUNDLED_ORBIT_DB) not in sys.path:
    sys.path.insert(0, str(_BUNDLED_ORBIT_DB))
```

This is the glue that makes orbit's hooks work under a marketplace install where there is no `pip install -e ./orbit-db` step. The plugin ships `orbit-db/` as a sibling directory to `hooks/`, and each hook inserts it into `sys.path` before the `from orbit_db import TaskDB` line so the import resolves against the bundled copy. If you have `orbit-db` pip-installed for some other reason (say, you are developing it), that install takes precedence because it got there via normal imports first - the bootstrap is additive, not destructive.

This block is *inlined* in every hook rather than factored into a shared `_bootstrap.py` helper because pytest imports hooks as `hooks.session_start` (package-import mode), where relative imports from a sibling helper file do not resolve cleanly at the top of module execution. Duplicating five lines is the cost of keeping the hooks testable in the existing test harness. See the orbit project's gotcha notes for the full story.

The `activity_tracker.py` hook is the one exception to this pattern - it uses a *subprocess* path instead of an in-process import, for reasons that are covered in detail in its section below.

## SessionStart: `session_start.py`

**When:** Every time Claude Code starts a new session in a directory. Runs before the first user prompt.

**What it does:**

1. **Write terminal-session mapping.** Looks up `TERM_SESSION_ID` (iTerm2) or `WT_SESSION` (Windows Terminal) from the environment and, if present, writes a row into `hooks-state.db:term_sessions` mapping the terminal tab to the Claude session ID. This is the bridge that lets the statusline find the current session when it runs, because the statusline only gets its session ID from Claude Code's statusline JSON and has no direct access to the SessionStart event.
2. **Install bundled rules.** Runs `install_bundled_rules()`, which walks `${CLAUDE_PLUGIN_ROOT}/rules/*.md` and copies any file starting with `<!-- orbit-plugin:managed` into `~/.claude/rules/`. The ownership marker is critical: files that already exist in the destination are only overwritten if they *also* start with the marker. A user who deletes the marker takes ownership of that file and the hook stops touching it on subsequent SessionStarts. This is how marketplace installs get rule-file updates without clobbering user edits.
3. **Detect the active task.** Tries `from orbit_db import TaskDB`, instantiates a DB, calls `db.find_task_for_cwd(cwd, session_id)`. If a task is found, it:
   - Writes `pending-task.json` with the task name and repo path, for the activity tracker.
   - Writes `projects/<session-id>.json` with the project name, for the statusline.
   - Prints a markdown context block to stdout - this is the "Active Task Detected" banner Claude sees at the top of the session - with the task name, status, time invested, JIRA key, and the path to the orbit files.
   - Includes a `/orbit:go` tip and a task-tracking discipline reminder telling Claude to use `mcp__plugin_orbit_pm__update_tasks_file` instead of the built-in TaskCreate tool for orbit tasks.
4. **Skip silently on failure.** If `orbit_db` cannot be imported (minimal install, not set up yet, whatever), the hook bails out quietly. Nothing on stdout, nothing in stderr, Claude's session proceeds as if no hook ran. Rules installation still runs independently - it does not depend on `orbit_db` at all.

**State files written:**

- `~/.claude/hooks/state/pending-task.json` - `{taskName, cwd, timestamp}`. Used to be read by `find_task_for_cwd` but is now vestigial (the actual per-session pointer is `projects/<session-id>.json`). Still written for backwards compatibility and because nothing breaks if it exists.
- `~/.claude/hooks/state/term-sessions/<TERM_SESSION_ID>` - Plain-text file containing the Claude session ID. Used by the statusline for mid-session terminal→session resolution on terminals that set `TERM_SESSION_ID`.
- `~/.claude/hooks-state.db:term_sessions` - Same mapping in the SQLite DB. Both formats exist because different readers use different stores.
- `~/.claude/hooks/state/projects/<session-id>.json` - `{projectName, updated, sessionId}`. The authoritative per-session project pointer. The statusline reads this, and mid-session `/orbit:go` also writes it.

**Why both `pending-task.json` and `projects/<session-id>.json`:** The per-session file is per-session (obviously), so two concurrent Claude Code windows can track different projects without interfering. `pending-task.json` is shared and prone to races when you open multiple sessions. The current resolution order in `find_task_for_cwd` checks the per-session file first, then falls back to cwd-based matching - `pending-task.json` is no longer in the resolution chain at all. It is dead state.

## UserPromptSubmit: `activity_tracker.py`

**When:** Every time the user submits a prompt. Runs *before* Claude sees the prompt - it is a pre-submit hook from the plugin's point of view.

**What it does:** Records a heartbeat in `tasks.db:heartbeats` for time tracking. Exactly one heartbeat per prompt, per session, per orbit task (if one is active).

**The subprocess quirk:** Unlike the other hooks, `activity_tracker.py` does not `import orbit_db` directly. Instead it spawns a subprocess:

```python
subprocess.run(
    [sys.executable, "-m", "orbit_db", "heartbeat-auto"],
    cwd=cwd,
    timeout=2,
    capture_output=True,
    env=env,
)
```

This is *deliberate* and was reverted to once, during an earlier refactor that tried to inline the heartbeat recording. The problem: `record_heartbeat_auto` has to acquire a SQLite write lock on `tasks.db`, and under contention (concurrent writers from other Claude sessions, orbit-auto, the dashboard's sync loop), a single heartbeat call can block for up to 5 seconds waiting on `busy_timeout=5000`. The UserPromptSubmit hook has a 5-second timeout of its own, so an in-process call could eat the entire budget and miss other hooks, or worse, delay the actual prompt submission.

The subprocess form solves this by imposing a hard 2-second wall on the child process. If the SQLite lock does not resolve in 2 seconds, the subprocess is killed via `subprocess.TimeoutExpired`, the exception is swallowed, and the hook returns immediately. Worst case: one heartbeat is lost. The time budget of the parent hook is unaffected.

The `PYTHONPATH` trick in the `env` dict is the subprocess's equivalent of the in-process sys.path bootstrap - it tells the child Python where to find `orbit_db` when the plugin is marketplace-installed. The subprocess runs `python -m orbit_db heartbeat-auto`, which is a command defined in `orbit-db`'s CLI that dispatches to `TaskDB.record_heartbeat_auto(cwd, session_id)`.

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
4. If the intersection is non-empty, print a reminder listing the divergent task numbers and an exact `mcp__plugin_orbit_pm__update_tasks_file(...)` invocation Claude can paste.

The reminder ends with an explicit callout:

> Important: the built-in TaskCreate tool and any system reminders about "task tools" refer to Claude Code's in-conversation todo list, NOT the orbit tasks file.

This is there because Claude Code (the harness) periodically injects system reminders pointing at the internal `TaskCreate` tool, and Claude occasionally follows them instead of using the orbit MCP tool. The hook pushes back.

**Same skip patterns as activity_tracker**, same subagent guard, same list-vs-string prompt flattening. These two hooks mirror each other in structure because they run back-to-back and share the same input shape.

**Why this is not a feature of the MCP tool:** The divergence check has to run *on every prompt*, not on every `update_tasks_file` call. You can only detect "Claude forgot to flip the checkbox" by looking at the state of the two files on a schedule, and the only schedule Claude Code exposes to plugins is the hook event stream. Putting the check in an MCP tool would mean Claude has to proactively ask "am I drifting?" which is exactly the thing it forgets to do.

## PreCompact: `pre_compact.py`

**When:** Claude Code is about to auto-compact the conversation. This happens when the context window fills up, and the compaction replaces older messages with a summary. The hook fires *before* the compaction happens, giving plugins one last chance to save state.

**What it does:**

1. Find the active task via `find_task_for_cwd`. If no task, return.
2. Find the context file under `~/.claude/orbit/<task.full_path>/<task.name>-context.md` (or the bare `context.md` fallback for subtask layouts).
3. Update the "Last Updated" timestamp line.
4. Add an `- Auto-saved before compaction (<timestamp>)` bullet. If a `## Recent Changes` section exists, add it there; otherwise append a new section at the end.
5. Write the file back.
6. Call `db.process_heartbeats()` to flush any accumulated heartbeats into the `sessions` table, so the dashboard time totals are current before compaction.

The auto-save note is the signal: when you come back to the project later via `/orbit:go`, you can see in the context file exactly when it was auto-saved and how many compactions have happened in the current work session. In practice you almost never look at the auto-save line - it is there for reconstructing "what happened" in pathological cases.

**The 30-second timeout** is generous because `process_heartbeats` can touch a lot of rows on a busy session, and the compaction itself does not block on the hook - Claude Code fires the hook, waits up to 30s, then compacts regardless. The hook should finish in well under a second in practice, but the budget is there for outliers.

**Why not save more aggressively:** You might expect the PreCompact hook to write a richer snapshot - a synthesized "Next Steps" section, a learnings summary, a `Recent Changes` block populated with what actually happened. It does not, because generating that content requires talking to Claude, and PreCompact is not the right moment for that: Claude is about to compact *because it is out of context*, and there is no budget for synthesizing a summary. The hook only does mechanical things that do not require the LLM.

The richer save path is `/orbit:save`, which is a slash command the user (or Claude) invokes explicitly. PreCompact is the backstop that ensures even a forgotten `/orbit:save` leaves *some* trace in the context file.

## Stop: `stop.py`

**When:** Claude has finished responding to the user's prompt and Claude Code is about to return control to the terminal (the "stop" event). Runs once per message exchange, after Claude's reply is already shown.

**What it does:** Checks whether Claude made any file edits during the exchange. If yes, and there is an active orbit task with orbit files on disk, prints a reminder to stderr:

```
---
**Orbit Reminder:** You made file edits while working on **<task-name>**.
Consider running `/orbit:save` to save context before ending your session.
---
```

**How it detects edits:** The hook reads the transcript file at `input_data["transcript_path"]` (Claude Code's per-session JSONL log) and grep-strings for `"tool_use"` co-occurring with `"Write"` or `"Edit"`. This is intentionally approximate - a proper parser would be more expensive and the goal is just "did anything get written?", not a precise edit count. A false positive (reminder fires when nothing was actually modified) is annoying but harmless; a false negative (no reminder when edits happened) misses the point of the hook.

**Why stderr:** Stop hook output goes to stderr specifically because Claude Code treats stderr from Stop hooks as *user-facing* messages - they are shown to the human in the terminal after Claude's reply, not injected back into Claude's context. This is the right channel for a "remember to save" nudge, because it is meant for the human, not Claude. Compare with UserPromptSubmit and SessionStart, which write to stdout specifically to land in Claude's context.

**The stop hook does not fail the stop event.** If anything inside the try block crashes, the bare `except Exception: pass` swallows it. Stop hooks that fail can apparently cause weird Claude Code behavior, and a reminder failing is not worth breaking the session over.

## State files: the full picture

Hooks write to a surprising number of places. Here is the complete map:

| Path | Written by | Read by | Format |
|------|-----------|---------|--------|
| `~/.claude/tasks.db:heartbeats` | activity_tracker (via orbit_db subprocess) | orbit_db aggregator, dashboard | SQLite row |
| `~/.claude/tasks.db:sessions` | orbit_db `process_heartbeats` (called by pre_compact) | dashboard, orbit MCP `get_task_time` | SQLite row |
| `~/.claude/hooks-state.db:term_sessions` | session_start | statusline | SQLite row |
| `~/.claude/hooks-state.db:session_state` | statusline (not hooks) | statusline | SQLite row |
| `~/.claude/hooks-state.db:project_state` | `/orbit:go`, dashboard `/api/hooks/project` | statusline | SQLite row |
| `~/.claude/hooks/state/pending-task.json` | session_start, `/orbit:go` | nothing (vestigial) | JSON file |
| `~/.claude/hooks/state/term-sessions/<term-id>` | session_start | statusline fallback path | Plain text |
| `~/.claude/hooks/state/projects/<session-id>.json` | session_start | statusline, `find_task_for_cwd` | JSON file |
| `~/.claude/rules/*.md` | session_start `install_bundled_rules` | Claude Code (auto-loaded) | Markdown files with ownership marker |

**Invariant to be aware of:** `pending-task.json` and `pending-project.json` appear in git history and in older code, but `pending-task.json` is no longer read by anything and `pending-project.json` is *written* by nothing in the current codebase (it is read at priority 1 of `find_task_for_cwd`, but that branch is dead). The live per-session pointer is `projects/<session-id>.json`. Do not rely on either pending file.

## The HTTP hook path

Beyond the plugin-registered hooks in `hooks.json`, orbit also uses a second hook-wiring mechanism: Claude Code's native `"type": "http"` hook form in `~/.claude/settings.json`. This is a *user-level* registry - not part of the plugin manifest - where Claude Code POSTs to HTTP endpoints on every hook event without going through Python at all.

The orbit dashboard exposes these endpoints:

| Endpoint | Caller | What it does |
|----------|--------|--------------|
| `POST /api/hooks/edit-count` | `PostToolUse` HTTP hook wired by `orbit-install` when the dashboard is installed (matcher `Edit\|Write\|NotebookEdit`) | Updates `session_state.edit_count` in `hooks-state.db` for the statusline edit counter |
| `POST /api/hooks/task-created` | Orbit MCP server (`create_task`, `create_orbit_files`) | Triggers immediate SQLite → DuckDB sync so new projects show in the dashboard without the up-to-60s background-sync lag |
| `POST /api/hooks/heartbeat` | Optional - power-user `UserPromptSubmit` HTTP hook wiring | Records a heartbeat. Plugin already records heartbeats via `activity_tracker.py`'s subprocess path, so wiring this on top just duplicates them |

`edit-count` is wired automatically by `orbit-install` when the dashboard is installed, so full-install users get the statusline edit counter out of the box. `task-created` is called internally by the MCP server, not by a user-level HTTP hook. `heartbeat` is only of interest if you specifically want two parallel heartbeat paths.

**If you are auditing "which endpoints are actually used by hooks", grep `~/.claude/settings.json` in addition to the plugin source.** Claude Code's `"type": "http"` hook form is wired in settings.json and isn't visible from `hooks.json` or the plugin tree.

## The `ORBIT_AUTO_MODE` signal

When orbit-auto spawns Claude CLI subprocesses, it sets `ORBIT_AUTO_MODE=1` in the child environment. Hooks do not currently read this variable *themselves*, but it is the signal that various user-level behaviors check to differentiate autonomous runs from interactive ones. For example:

- `~/.claude/hooks/permission-whitelist.sh` auto-approves `ExitPlanMode` transitions when `ORBIT_AUTO_MODE=1`, because autonomous runs should not block on plan approval.
- Slash commands and skills may skip clarification questions when the variable is set.

The variable is not magic - it is a plain environment variable - but it is the contract between orbit-auto and the rest of the plugin ecosystem. If you want your own hook or skill to behave differently under autonomous execution, check `os.environ.get("ORBIT_AUTO_MODE") == "1"`. If you are writing a new orbit-auto mode, set the variable in the child environment and let downstream consumers opt in.

## Adding a new hook

If you have a new event you want to hook into, the pattern is straightforward:

1. **Add the hook command in `hooks/hooks.json`.** Pick the event (`SessionStart`, `UserPromptSubmit`, `PreCompact`, `Stop`, or another event Claude Code supports). Add a command entry pointing at your new script with a reasonable `timeout`. Hooks that already have multiple scripts (like `UserPromptSubmit`) take a list; new events need a new top-level key.
2. **Create the script under `hooks/<your_hook>.py`.**
   ```python
   #!/usr/bin/env python3
   """What this hook does - one sentence."""

   import json
   import sys
   from pathlib import Path

   # Bundled orbit-db path for marketplace installs (no system pip install).
   _BUNDLED_ORBIT_DB = Path(__file__).resolve().parent.parent / "orbit-db"
   if _BUNDLED_ORBIT_DB.is_dir() and str(_BUNDLED_ORBIT_DB) not in sys.path:
       sys.path.insert(0, str(_BUNDLED_ORBIT_DB))

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
6. **Reinstall the plugin.** `claude plugins install orbit@local` and restart Claude Code. The hook registry is re-read on plugin load.
7. **Add a test.** `hooks/tests/` has fixtures for mocking `orbit_db` via `patch.dict('sys.modules', {'orbit_db': MagicMock()}) + importlib.reload(mod)`. Any new hook that imports `orbit_db` at the top of the module will break mocking; keep the import lazy (inside `main()`) or stick with the in-process bootstrap the existing hooks use.

## Troubleshooting

### "SessionStart doesn't show the active task banner"

**Cause:** Either `orbit_db` failed to import (bundled path wrong, Python version mismatch), or `find_task_for_cwd` returned `None` for the current directory, or the hook crashed in the try block and was silently swallowed.

**Fix:** Run the hook manually to see what happens: `python3 ${CLAUDE_PLUGIN_ROOT}/hooks/session_start.py`. It reads from stdin, so you can `echo '{}' | python3 session_start.py` and watch for import errors or exceptions. Most commonly the answer is "your cwd is not matching any orbit task" - check `~/.claude/orbit/active/` for a project whose `full_path` corresponds to your cwd, or use `mcp__plugin_orbit_pm__find_task_for_directory` from a live Claude session to see what it returns.

### "Heartbeats aren't being recorded"

**Cause:** Four possibilities. In order of likelihood:
1. The hook is timing out. `activity_tracker.py` has a 5-second budget and spawns a 2-second subprocess - if the SQLite lock is contended beyond 2s, the subprocess gets killed.
2. `tasks.db` is locked or corrupted. Check `sqlite3 ~/.claude/tasks.db "SELECT count(*) FROM heartbeats"`.
3. `find_task_for_cwd` returned no task, so there is nothing to attribute a heartbeat to.
4. The prompt matched a skip pattern (slash command, shell command, one-word control).

**Fix:** Run `activity_tracker.py` in isolation with a representative stdin payload and capture the output. Check `~/.claude/logs/` for any orbit-db errors. For contention issues, the answer is almost always "wait and retry" - lock contention that exceeds the 2-second budget is rare enough to ignore.

### "Task tracker reminder won't go away even after I flip the checkbox"

**Cause:** The hook matches `### Task N` headings in the context file. If you flipped the checkbox but the context file still has `### Task N: ...` for that number, the hook still fires.

**Fix:** Either remove the heading from the context file (if the finding is no longer relevant), or reword it so it does not match the `### Task N` pattern. The hook's regex is `^###\s+Task\s+(\d+)` - anything that does not start with `### Task <number>` is invisible to it.

### "PreCompact never fires"

**Cause:** PreCompact only fires when Claude Code *auto*-compacts. Manual compaction via `/compact` does not fire PreCompact.

**Fix:** This is by design. If you want to save state on manual compaction, run `/orbit:save` explicitly before `/compact`. (There is no hook for `/compact` - it is not a plugin-observable event.)

### "Stop reminder fires when I didn't actually edit anything"

**Cause:** The edit detection is a string grep on the transcript file for `"tool_use"` co-occurring with `"Write"` or `"Edit"`. If the transcript includes a tool use that *mentioned* those tool names (for instance, Claude reading a doc about the Write tool), the grep fires a false positive.

**Fix:** Ignore the reminder. It is annoying but harmless. A proper fix would require parsing the JSONL transcript and inspecting actual tool invocations, which is more work than the reminder is worth.

### "Rules in `~/.claude/rules/` aren't getting updated after a plugin upgrade"

**Cause:** The file in `~/.claude/rules/` does not start with the `<!-- orbit-plugin:managed` marker, so `install_bundled_rules` treats it as user-owned and leaves it alone.

**Fix:** Delete the file from `~/.claude/rules/` - the next SessionStart will reinstall it from the plugin's copy (which does have the marker). Alternatively, edit your file to start with `<!-- orbit-plugin:managed -->` as the first line - but that means your local changes will be overwritten on the next plugin update.

### "Hook test breaks because orbit_db is imported at the top of the module"

**Cause:** The pytest mocking pattern in `hooks/tests/` is `patch.dict('sys.modules', {'orbit_db': MagicMock()}) + importlib.reload(mod)`. This only works if the import is lazy (inside `main()`) or if the `sys.path` bootstrap is the first thing that runs.

**Fix:** Move the `orbit_db` import inside `main()` so reload sees the mock. If you are adding a new hook, follow the lazy-import pattern used in `session_start.py`, `pre_compact.py`, `stop.py`, and `task_tracker.py`. Only `activity_tracker.py` uses a non-lazy pattern, and even that one calls `orbit_db` via subprocess rather than `import`.

## Where to go from here

- [`architecture.md`](./architecture.md) - for the shared context on `hooks-state.db`, `tasks.db`, and how the pieces fit together.
- [`dashboard.md`](./dashboard.md) - for the HTTP hook endpoints and how they overlap with the plugin-registered path.
- [`orbit-auto.md`](./orbit-auto.md) - for the `ORBIT_AUTO_MODE` signal and how autonomous runs interact with hooks.
- `hooks/hooks.json` - the source-of-truth registry.
- `hooks/*.py` - the scripts themselves. Each is short (under 250 lines) and standalone.

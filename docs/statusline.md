# Statusline

This document covers orbit's statusline: a single-file Python script that Claude Code invokes on every turn to render the multi-line status block at the bottom of the terminal. It is the most user-visible piece of orbit - the part you stare at all day - and also the most performance-sensitive, because every millisecond it spends is a millisecond of latency added to every Claude Code message.

It assumes you have read [`architecture.md`](./architecture.md) for the shared vocabulary (`hooks-state.db`, `session_state`, `project_state`, `term_sessions`, orbit file layout). If a term in this doc is not defined here, it is defined there.

If you are just trying to *use* the statusline, the short version is: once installed, Claude Code runs it automatically and you do not have to do anything. Customize via environment variables in your shell profile. The rest of this doc is for when you want to understand what the lines mean, change what is shown, or debug a line that is broken.

## What the statusline shows

The statusline is a 6- or 7-line block that renders below every Claude Code prompt. All lines are always shown (even when empty) so Claude Code allocates a fixed-height status area from the first render - this prevents the layout from jumping as slow data sources (HTTP usage calls, version lookups) come in on subsequent renders.

| Line | Icon cell | What it shows |
|------|-----------|---------------|
| 1 | Project | Active orbit project name + `[completed/total]` progress bracket, with OSC 8 hyperlink to the dashboard. Also shows "Last Action" time for the session. Empty if no orbit project. |
| 2 | Dir | Current working directory, git branch + clean/dirty indicator, worktree annotation if applicable. |
| 3 | Time | Elapsed session time, current date/time, edit count for the session. |
| 4 | Metrics | Model name, tokens used, context window percentage with warning colors. Shows "Fast mode activated" if Claude Code fast mode is on. |
| 5 | K8s/Ver | Kubernetes context (if `kubectl` is installed), Claude Code version + age + "reviewed" color coding via `/whats-new`, Claude service health status with clickable link to status.claude.com. |
| 6 | Usage | Subscription type (Max/Pro/API/Bedrock/etc.), session usage percentage, weekly usage percentage, Opus usage percentage if applicable, extra credits spent. |
| 7 | Codex | Codex plan type, session and weekly usage percentages. **Only shown if the Codex CLI is installed** (`~/.codex/auth.json` exists and `STATUSLINE_CODEX` is not set to `false`). |

The lines are rendered in a specific non-numeric order in the main() function - line 2 (Project+LastAction) prints first, then line 1 (Dir+Git), then line 4 (Time), then line 3 (Metrics), then lines 5/6/7. This ordering was tuned empirically by the author and is documented only by the order of the `out.write()` calls at the bottom of `main()`.

### Column alignment

Every line is laid out as two-column or three-column "cells" separated by a pipe character (` │ `). The first column has a dynamic width computed as `max(CELL_WIDTH=24, widest_first_column_item)`, the second column the same, and subsequent columns use the fixed `CELL_WIDTH`. This gives you vertical alignment on the first two cells of every line, which is the thing your eye tracks when scanning the status block.

Width calculations use a custom `display_width()` function that handles East Asian wide characters, zero-width joiners, and ANSI escape sequences. Counting just `len(s)` would misalign any line with an emoji or a foreign character - the implementation is in `orbit-dashboard/orbit_dashboard/statusline.py:227` if you need to change it.

## How it gets invoked

Claude Code runs the statusline via the `statusLine` key in `~/.claude/settings.json`:

```json
"statusLine": {
  "command": "orbit-statusline"
}
```

`orbit-statusline` is a pip entry point shipped by the `orbit-dashboard` package. `uvx orbit-install` wires it into `settings.json` automatically during the full install; if you cloned the repo and ran `uvx orbit-install --local`, the entry point resolves to `orbit_dashboard/statusline.py` in your checkout, so edits to that file are live instantly. No reinstall is needed for statusline-source changes in `--local` mode. For end users on the PyPI path, `uvx orbit-install --update` pulls in the newest published version.

Claude Code spawns the script on every turn and sends session JSON on stdin:

```json
{
  "session_id": "fb8f08aa-...",
  "model": {"display_name": "Sonnet 4.5"},
  "context_window": {
    "used_percentage": 23,
    "context_window_size": 200000,
    "current_usage": {"input_tokens": 12000, "cache_read_input_tokens": 180000, ...}
  },
  "cost": {"total_duration_ms": 450000, "total_cost_usd": 0.37},
  "workspace": {"git_worktree": {...}},
  "rate_limits": {...}
}
```

The statusline reads stdin, parses the JSON, and writes 6-7 lines of ANSI-colored text to stdout. It must finish fast - Claude Code imposes an approximate 300ms debounce/cancel window on the first render, so anything slower risks being cut off. This is the constraint that shapes everything about the script's architecture.

### The ~300ms budget and how it fits

The script does a lot: reads two SQLite DBs, fetches Claude Code usage from a cached API, checks `git` three times, runs `kubectl` once, fetches `claude --version`, checks the Anthropic status page, probes a Codex usage API. If any of these ran serially they would blow the budget on the first render easily.

The solution is a `ThreadPoolExecutor(max_workers=6)` in `main()` that launches the slow operations concurrently. The futures are:

- `get_project_info` - read `project_state` from `hooks-state.db`, read `<project>-tasks.md` for progress
- `get_git_info` - three `git` subprocess calls
- `get_k8s_context` - one `kubectl` subprocess call
- `get_version_info` - one `claude --version` + one cached GitHub API call
- `get_health_status` - one cached Anthropic status API call
- `get_usage_data` - one cached Anthropic usage API call
- `get_codex_usage` - one cached Codex usage API call
- `get_last_action_time` - read `session_state` from `hooks-state.db`

Each future is collected with a 3-second `timeout` and wrapped in try/except. On timeout or error, the line falls back to empty, and the statusline still renders - just with missing pieces. The pool is then `shutdown(wait=False, cancel_futures=True)` so stragglers do not delay exit.

**Caching is everything.** Four of the slow operations are cached on disk:

| Cache file | TTL | What |
|------------|-----|------|
| `~/.claude/scripts/health-cache.json` | 180s | Anthropic status page incidents |
| `~/.claude/scripts/usage-cache.json` | 300s | Claude Code usage API (`api.anthropic.com/api/oauth/usage`) |
| `~/.claude/scripts/codex-usage-cache.json` | 300s | Codex usage API (`chatgpt.com/backend-api/wham/usage`) |
| `~/.claude/hooks/state/version-cache.json` | (until version bump) | GitHub release date lookup for the installed Claude Code version |

On cache hit, the future returns instantly from a file read. On cache miss, the network call happens inside the thread pool, gated by 2-3 second timeouts inside each `urllib.request.urlopen` call. The net effect is that almost every render is fast, with the occasional slow render when a cache expires.

**The version cache is persistent** rather than TTL-based - once we know when version 1.0.50 was released, that date never changes. The cache file maps `version -> iso_timestamp` and grows a few bytes per Claude Code update. No eviction.

## Environment variables

The statusline has a small but useful set of environment-variable knobs, documented in the module docstring at the top of `orbit_dashboard/statusline.py`. Set them in your shell profile (`~/.zshrc`, `~/.bashrc`, fish config, etc.) to customize what is shown.

| Variable | Default | What it does |
|----------|---------|--------------|
| `STATUSLINE_CODEX` | `true` | Show the Codex usage line. Set to `false` to hide it even if Codex is installed. |
| `STATUSLINE_HEALTH_SERVICES` | `Code,Claude API` | Comma-separated list of Anthropic services to monitor. Available values: `Code`, `Claude API`, `claude.ai`, `platform.claude.com`, `Claude for Government`, `Claude Cowork`. Only incidents affecting services in this list are shown. |
| `ORBIT_DASHBOARD_URL` | `http://localhost:8787` | Base URL used for the OSC 8 clickable hyperlinks on the project name and progress bracket. Change if your dashboard runs on a non-default port or a remote host. |

Auth-provider detection also respects Claude Code's own environment variables (`CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`, `CLAUDE_CODE_USE_FOUNDRY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY`) to display the correct subscription label on the Usage line. You do not need to set these for orbit - the statusline reads them because Claude Code already uses them.

## Clickable hyperlinks (OSC 8)

Modern terminal emulators support OSC 8 escape sequences for clickable hyperlinks. The statusline uses them in three places:

1. **Project name** - links to `{ORBIT_DASHBOARD_URL}/#projects`. Click to open the dashboard's Projects view.
2. **Progress bracket** - links to `{ORBIT_DASHBOARD_URL}/#projects?task=<name>&tab=tasks`. Click to open the task modal directly on the Tasks tab.
3. **Health status** - links to `https://status.claude.com`. Click to open the Anthropic status page.
4. **Version label** - links to the Claude Code CHANGELOG on GitHub, specifically `https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md`.

Terminals that do not support OSC 8 render the link as the bare text (no underline, no clickable behavior) - the escape sequences are ignored and the rest of the line is unaffected. No fallback flag needed, this is a free enhancement.

## State reads

The statusline reads from several state sources:

### `~/.claude/hooks-state.db`

A SQLite database with WAL mode enabled. Opened via `_get_hooks_db()`, which returns `None` if the DB does not exist (minimal install scenario). All reads are wrapped in try/except - a missing DB degrades gracefully to empty lines, not a crash.

Tables the statusline reads:

- `session_state` - per-session context percent, token count, edit count, last prompt timestamp. Statusline *writes* its own row here on every render (via `update_session_state`), and reads `edit_count` and `last_prompt_at` back out.
- `term_sessions` - terminal tab → session ID mapping. Written by `session_start.py` (the hook) and by `update_term_session()` in the statusline itself. Used by mid-session `/orbit:go` to resolve the current session ID from the terminal environment variable.
- `project_state` - per-session active project. Written by `/orbit:go` and `session_start.py` via the dashboard API. Read by the statusline to populate the Project line.

`hooks-state.db` is the main contact point between the statusline and the rest of the plugin - it is how SessionStart tells the statusline "this session is on project X", and it is how `/orbit:go` tells the statusline "switch to project Y mid-session" without restarting Claude Code.

### `~/.claude/orbit/active/<project>/<project>-tasks.md`

Read for the progress bracket. Parsing is a simple regex scan for completed (`- [x]`) and pending (`- [ ]`) lines - see `_parse_task_progress()`. The format matches the MCP server's canonical parser in `mcp-server/src/mcp_orbit/orbit.py`, counting all checklist items flatly including hierarchical subtasks.

Two special cases for display:

- **Empty file or no checklists at all:** shows `[TBD]` instead of `[0/0]`.
- **Template placeholder:** a single pending item with text exactly `TBD` also shows `[TBD]`, so freshly initialized projects do not display `[0/1]` until you write real tasks.

### `~/.claude/tasks.db`

**The statusline does not read `tasks.db` directly.** This is intentional. `tasks.db` is orbit-db's domain and reading it from the statusline would require importing `orbit_db`, which is slow and adds a fork boundary. All the time and task data the statusline needs comes through `hooks-state.db` (written by hooks and the dashboard) or directly from the filesystem (`<project>-tasks.md`).

This is also why the statusline cannot show total time invested on the project - that would require a query against `tasks.db:sessions`, which is out of scope for this component. Total time lives on the dashboard.

### `~/.claude/settings.json`

Parsed once, only for the `fastMode` flag. The statusline looks at this to show the "Fast mode activated" indicator on the Metrics line.

## What the statusline writes

Beyond rendering to stdout, the statusline has a few side effects that make it more than a display component.

### `hooks-state.db:session_state` row

Every render calls `update_session_state(session_id, ctx_percent, tokens_str)`, which inserts or updates the session's row with the current context percentage and token count. The `edit_count` column is *not* written by the statusline - it is written by a separate dashboard HTTP hook wired to `Stop` events (see [`hooks.md`](./hooks.md) and [`dashboard.md`](./dashboard.md)). The statusline reads the count back out to display it, but does not increment it itself.

### `hooks-state.db:term_sessions` row

`update_term_session()` writes the terminal→session mapping to the DB on every render. This keeps the mapping fresh, which is important because the session ID that Claude Code passes on the statusline stdin is different from the `CLAUDE_SESSION_ID` env var written by `session_start.py`. Whichever value got there first from the SessionStart hook is overwritten by the correct (statusline JSON) value on the first render.

### Terminal title bar

`set_iterm_title()` writes OSC 1 escape sequences to `/dev/tty` (not stdout!) to set the terminal title bar. The title includes the project name or directory name plus an "action" label read from `session_state`. On iTerm2 specifically, it also sets a user variable via OSC 1337 for use in iTerm's own badge/subtitle system. On cmux (a tmux variant), it invokes `cmux workspace-action --action set-description` instead.

These writes go to `/dev/tty` directly because Claude Code's statusline stdout is consumed, and writing title-bar escapes there would corrupt the rendered statusline. The title-bar writes are best-effort: if `/dev/tty` cannot be opened, the title-bar update silently fails without affecting the main render.

### Debug log

`parse_input()` writes a JSON debug dump of Claude Code's stdin to `~/.claude/hooks/state/statusline-ctx-debug.log` on every render. This overwrites on each call, so the file always contains the most recent input payload. It is there because the `context_window` shape in Claude Code's statusline JSON changed multiple times during Claude Code's development and the easiest way to debug display issues was to `cat` the log and see exactly what shape arrived. The file is also tiny and hidden, so nobody has asked to make it conditional.

## Lines one by one

Here is what each line builds from, so you can trace a display issue back to its source.

### Line 1: Project + Last Action

- **Project** - `get_project_info(session_id, duration_sec)`. Reads `project_state` from `hooks-state.db`. If the row is older than `max(duration_sec + 60, 60)` seconds, it is considered stale and ignored. Looks for the project directory under `ORBIT_ACTIVE = ~/.claude/orbit/active/`, supports nested subtask layouts (`parent/child`), and reads the tasks file to compute the progress bracket. Wraps both the name and the bracket in OSC 8 links.
- **Last Action** - `get_last_action_time(session_id)`. Reads `last_prompt_at` from `session_state` and formats it as "Apr 14 10:00".

Empty on a session with no active orbit project.

### Line 2: Dir + Git

- **Dir** - `Path.cwd().name` with a `~` collapse if cwd matches `$USER`. Appends `(wt: <name>)` if a worktree is present in the Claude Code stdin.
- **Git** - `get_git_info()` runs `git rev-parse --git-dir`, `git rev-parse --show-toplevel`, `git branch --show-current`, and `git status --porcelain`. Color is green for clean, yellow for dirty. If worktree is active, appends `(worktree)` to the branch name.

Always renders dir; git cell is only shown inside a git repo.

### Line 3: Metrics

- **Model** - from `model.display_name` in stdin.
- **Tokens** - sum of `input_tokens + cache_creation_input_tokens + cache_read_input_tokens + output_tokens` from `current_usage`, formatted as `N`, `N.NK`, or `N.NM`.
- **Ctx** - context window percentage, plus a `SYSTEM_OVERHEAD_PERCENT = 19%` baseline add when the API returns a percentage directly. Uses colors:
  - Green/gray under 65%
  - Yellow 65-79% with "Compact recommended"
  - Red 80%+ with "Compact now!"
  - Blue "(Estimated)" when stdin does not include `used_percentage` and the statusline has to compute it from token counts

### Line 4: Time

- **Elapsed** - from `cost.total_duration_ms`, formatted as `Hh MMm` or `Mm SSs`.
- **Now** - `datetime.now().strftime("%a %b %-d, %-I:%M%p")` lowercased. No timezone handling because the statusline is local-time only.
- **Edits** - `edit_count` read back from `session_state`. As noted, written by a separate hook, not by the statusline.

### Line 5: K8s + Version + Health

- **K8s** - `get_k8s_context()` runs `kubectl config current-context`. Cell is empty if `kubectl` is not installed or returns nothing.
- **Version** - `claude --version` + a GitHub releases API lookup for the release date. Shows age as `(Nd)` after the version. Color is green if the version has been reviewed (`/whats-new` ran for it) or yellow if not - the "reviewed" state lives in `~/.claude/cache/whats-new-version`.
- **Health** - incidents from `https://status.claude.com/api/v2/incidents.json`, filtered to the services named in `STATUSLINE_HEALTH_SERVICES`. Unresolved incidents render with a colored status label; resolved-within-recent-hours render with a green checkmark and muted color; otherwise shows `Claude Status: OK`.

### Line 6: Usage

This line has the most branching. It starts with the subscription label (`_detect_subscription`), then:

- **Foundry mode** - shows session cost/tokens/duration. No weekly/Opus.
- **Max plan** - shows `Session: ∞` and `Weekly: ∞`. Max is unmetered.
- **Metered plans** - shows `Session: N%`, `Weekly: N%`, and optionally `Opus: N%` and `Extra: spent/limit`. Each percentage has a reset time if the usage API returned one. Extra credits are only shown when spending is non-zero.

The usage API response shape is flattened by `_parse_usage_response()`. Anything the API returns that is not in that parser is silently dropped. Extra usage (the Claude Code add-on credits) is fetched on a separate code path because stdin's `rate_limits` field does not include it - the statusline always hits the API for that one.

### Line 7: Codex (conditional)

Only shown if `CODEX_ENABLED=true` (default) AND `~/.codex/auth.json` exists. If either check fails, the line is omitted entirely and the statusline outputs 6 lines instead of 7.

Format is parallel to Line 6: plan label, session percentage with reset, weekly percentage with reset. Data comes from `https://chatgpt.com/backend-api/wham/usage` with a 300-second cache.

## Customization

### Change what is shown

The easiest customization is the two environment variables documented above. Beyond that, the script is a single flat file - the layout is determined by the order of `out.write()` calls in `main()`, and the contents of each line are built in the `# Build items per line` section of `main()`. To add, remove, or reorder lines:

1. Find the line-building block (`line1`, `line2`, `line3`, ..., `line_codex`) in `main()`.
2. Modify or add an `_item(COLORS[...], ICONS[...], "Label", value)` call.
3. If you added a new line, include it in `all_lines`, `joined`, and `line_widths` tuples, then add an `out.write()` call in the output block at the bottom of `main()` and bump the `lines` count in `_fallback_output()` to match.

Remember that the output block intentionally emits every line unconditionally (using blank-padded strings if a line has no content) so Claude Code allocates a fixed-height status area. If you add a conditional line, either make it always present with a blank fallback, or accept that the status area height will jump.

### Change colors

All colors are defined in the `COLORS` dict near the top of the file as raw 24-bit ANSI escape sequences: `\033[38;2;R;G;Bm`. Tweak the numbers to change any color. There is no theme system - this is a single-person, single-config script.

### Change icons

The `ICONS` dict immediately below `COLORS` holds every emoji used in the statusline as Unicode escape sequences. Replace entries to change icons. Most terminals render these at 1.5-2x cell width, which is accounted for by `display_width()` - swapping an emoji for a plain ASCII character will affect column alignment, so try to keep the visual width stable if you swap.

### Add a new environment variable

If you are adding a knob that controls some aspect of the display:

1. Add the variable to the module docstring at the top of the file with a one-line description, the default, and an example.
2. Read it via `os.environ.get("STATUSLINE_YOUR_VAR", "default")` at module top.
3. Use it wherever needed in the rendering code.
4. Document it in the [environment variables](#environment-variables) section of this doc.

Keep the variable name consistent with the existing ones: `STATUSLINE_<FEATURE>` for orbit-owned knobs, existing Claude Code env vars (`CLAUDE_CODE_USE_*`, `ANTHROPIC_*`) for auth-provider detection.

## Performance notes

The statusline runs on every Claude Code turn. On a 4-turn-per-minute session, that is ~240 invocations per hour. Every millisecond of bloat adds up.

Things that are fast:
- SQLite reads from `hooks-state.db` (local, WAL, cached in memory)
- Cached HTTP responses (local file read)
- ANSI rendering (pure Python string work)

Things that are slow and gated by threads + timeouts:
- `git` subprocess calls (can be 50-200ms on a large repo)
- `claude --version` (process spawn + CLI startup)
- `kubectl config current-context` (process spawn; no network)
- Cache-miss HTTP calls to `api.anthropic.com` and `chatgpt.com` (network round-trip)

Things to avoid adding:
- Any subprocess call that is not already cached or gated
- Any DB query against `~/.claude/tasks.db` (WAL lock contention with `orbit-db` writers)
- Any synchronous HTTP call that is not cached

The stderr suppression block at the top of the file (`os.dup2(_devnull_fd, 2)`) is there because subprocess stderr can corrupt the ANSI display if it leaks. Do not remove it.

## Troubleshooting

### "The statusline is missing entirely"

**Cause:** Either the `orbit-statusline` entry point is not on `PATH` (the `orbit-dashboard` package was never pip-installed, or it was uninstalled), or `settings.json` does not have a `statusLine` key, or the Python script crashed on startup. A common legacy variant: `settings.json.statusLine.command` still points at `python3 ~/.claude/scripts/statusline.py` from a pre-M10 install, and that symlink now points at a deleted path.

**Fix:** First run `which orbit-statusline` - it should print a path. If not, re-run `uvx orbit-install --dashboard --statusline` (or `--update` if orbit is already installed) to reinstall the package and wire the entry point. Then check `~/.claude/settings.json` - the `statusLine.command` value should be the bare string `orbit-statusline`, not a `python3 ~/.claude/scripts/...` invocation. Rewrite it if needed. Finally, run the script in isolation with a dummy payload: `echo '{}' | orbit-statusline`. If it errors, read the traceback in `~/.claude/logs/statusline-errors.log`.

### "The statusline renders but some lines are blank"

**Cause:** Most likely a slow future timed out, a DB read failed, or the relevant data source is unavailable. Each line has its own timeout and try/except, so a failed line just renders empty.

**Fix:** Run the script with `-v` equivalent by printing debug info inside the future you suspect. Check `~/.claude/hooks/state/statusline-ctx-debug.log` to see what Claude Code's stdin looked like on the last render. For the Usage line specifically, check `~/.claude/scripts/usage-cache.json` - if the file is stale or corrupted, delete it and the next render will hit the API.

### "Project name doesn't appear even though I'm in an orbit project"

**Cause:** The `project_state` row is either missing, too old (older than `max(session_duration + 60s, 60s)`), or does not match the current `session_id`. The statusline uses the Claude Code stdin's `session_id`, not the environment variable - if `session_start.py` wrote the wrong session ID to `project_state`, you will see this.

**Fix:** Run `/orbit:go <project>` to force a fresh `project_state` write keyed on the current Claude Code session. Or insert a row manually: `sqlite3 ~/.claude/hooks-state.db "INSERT OR REPLACE INTO project_state VALUES ('<session-id>', '<project>', datetime('now','localtime'))"`.

### "Context percentage shows 'Estimated'"

**Cause:** Claude Code did not include `used_percentage` in the statusline JSON, so the statusline falls back to computing the percentage from raw token counts. This is expected early in a session when the context field is sparse, and it should switch to the non-estimated path on subsequent turns.

**Fix:** Not a bug. If it stays in "Estimated" mode the whole session, that is a Claude Code stdin shape issue - check the debug log to see what `context_window` looked like, and file an issue if it looks like Claude Code changed the payload shape.

### "Version label shows `v1.0.50 (3d)` in yellow even though I know about this version"

**Cause:** The yellow color means "not reviewed". The statusline checks `~/.claude/cache/whats-new-version` for the reviewed version, and only goes green if the content exactly matches the current version string.

**Fix:** Run `/whats-new` to update the reviewed marker, or manually `echo '1.0.50' > ~/.claude/cache/whats-new-version` to suppress the warning.

### "Codex line won't hide even though I set `STATUSLINE_CODEX=false`"

**Cause:** Environment variables must be set in your shell profile *before* Claude Code is launched. If you set it in a new terminal and Claude Code is already running in another, the existing statusline process will not see the change - it is read once per invocation from `os.environ`.

**Fix:** Restart Claude Code after adding the variable to your shell profile, or test with `env STATUSLINE_CODEX=false claude` from a fresh terminal.

### "Terminal title bar doesn't update"

**Cause:** The title bar writes go to `/dev/tty`, which may not be available when Claude Code is running under cmux, inside a Docker container, or inside another wrapper process that does not have a controlling terminal.

**Fix:** For cmux specifically, the statusline has a fallback that invokes `cmux workspace-action` to set the description - that should work. For other environments without a TTY, title-bar updates silently fail. There is no way to make them work without a TTY; this is a fundamental limitation of how terminal title bars work.

## Where to go from here

- [`architecture.md`](./architecture.md) - for the shared context on `hooks-state.db`, the storage model, and where the statusline sits relative to the rest of orbit.
- [`hooks.md`](./hooks.md) - for `session_start.py` and how it writes the state the statusline reads.
- [`dashboard.md`](./dashboard.md) - for the `/api/hooks/project` endpoint used by `/orbit:go` and the OSC 8 link targets.
- `orbit-dashboard/orbit_dashboard/statusline.py` - the source. Single file, flat structure, labeled with `# ============ SECTION ============` dividers. Grep for a section name to jump. The `orbit-statusline` entry point declared in `orbit-dashboard/pyproject.toml` resolves here.

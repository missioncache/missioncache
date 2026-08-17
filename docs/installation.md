# Installation

This document is the comprehensive reference for installing MissionCache. If you just want the quickstart, the [README](../README.md#install) covers the two most common paths in a few lines. This doc covers all three supported paths, what each one gives you, and how to verify and uninstall.

## Which path should I pick?

| Path | Best for | You get | You skip |
|------|----------|---------|----------|
| [`uvx missioncache-install`](#full-install-via-uvx-missioncache-install) | Most users | Plugin core + dashboard + MissionCache Auto CLI + statusline | - |
| [Plugin-only (marketplace)](#plugin-only-install-via-marketplace) | Minimal footprint, teams that don't want local services | Plugin core (commands, MCP tools, hooks, rules) | Dashboard, MissionCache Auto, statusline |
| [Manual / pip-only](#manual-install-no-installer) | Docker, CI, air-gapped environments, custom layouts, or embedding `missioncache-db` / `mcp-missioncache` in your own tooling | Full control over every step | `missioncache-install` convenience |

All three paths can coexist. The plugin-only and `missioncache-install` paths both store state in `~/.claude/`, so you can start plugin-only and add the dashboard later by running `uvx missioncache-install --dashboard --statusline --missioncache-auto`.

## Prerequisites

Required for every path:

- **Python 3.11+** (`python3 --version`)
- **Claude Code CLI** ([install guide](https://docs.claude.com/en/docs/claude-code))

Required for the plugin core (adds MCP server and hooks):

- **`uvx`** on your `PATH`. If `uvx --version` fails, install `uv` first:
  ```bash
  pip install uv
  # or
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
  `pipx` works in place of `uvx` for running the installer (`pipx run missioncache-install`), but `uvx` is the path we test.

Required only for the full install (dashboard, MissionCache Auto, statusline):

- The dashboard background service registers per platform: launchd on macOS, systemd user units on Linux (on systemd-less Linux such as default WSL, a shell-profile autostart block starts it on login instead), and a Task Scheduler ONLOGON task on native Windows (falling back to an HKCU Run-key entry when run without elevation). All of plugin, MissionCache Auto, dashboard, and statusline are supported on native Windows; the hooks require Claude Code 2.1.139+ (exec form).

On Windows, read [Windows: native or WSL2](#windows-native-or-wsl2) before installing - the two are separate installs that cannot see each other's projects, so it is a choice to make up front rather than after.

## Full install (via `uvx missioncache-install`)

One command, no clone needed. Takes a minute or two on a clean machine.

```bash
uvx missioncache-install
# or
pipx run missioncache-install
```

The interactive wizard asks which components to install (default is all) and runs:

1. **Plugin core** - installs the Claude Code plugin. In the default PyPI mode this registers `missioncache/missioncache` as a marketplace and installs `missioncache@missioncache`. In `--local` mode (from a clone) it sets up a local marketplace at `~/.claude/plugins/local-marketplace/` and installs `missioncache@local` instead.
2. **Dashboard** - pip-installs `missioncache-dashboard` (which pulls in `missioncache-db` as a dependency, giving your own tooling access to the task DB) and wires up a background service (launchd on macOS, systemd on Linux) via `missioncache-dashboard install-service`
3. **MissionCache Auto CLI** - pip-installs `missioncache-auto` (also pulls in `missioncache-db` as a dependency)
4. **Statusline** - wires `missioncache-statusline` (a console entry point shipped in `missioncache-dashboard`) into `~/.claude/settings.json`. Selecting statusline without dashboard auto-adds dashboard, since that is where the entry point ships from.
5. **Rules** - copies `rules/*.md` into `~/.claude/rules/` with an ownership marker so future updates can refresh them without overwriting user edits
6. **User-level slash commands** - copies `user-commands/*.md` (`/whats-new`, `/optimize-prompt`) into `~/.claude/commands/`

If you run a subset (no dashboard and no MissionCache Auto), `missioncache-db` is not installed. Install it standalone with `pip install missioncache-db` if you need the CLI.

Flags for non-interactive use:

```bash
uvx missioncache-install --all --yes                     # install everything, no prompts
uvx missioncache-install --dashboard --statusline --yes  # install a subset
uvx missioncache-install --all --yes --no-statusline     # install everything except the statusline
uvx missioncache-install --update                        # refresh installed components
uvx missioncache-install --uninstall                     # remove everything (preserves user data)
uvx missioncache-install --all --yes --port 9999         # dashboard on a non-default port
```

Opt-out flags (`--no-statusline`, `--no-dashboard`, etc.) only take effect alongside `--all` or explicit opt-ins. Running them on their own drops you into the interactive wizard.

State is tracked at `~/.claude/missioncache-install.state.json` so subsequent runs can reconcile what is already installed. Re-running the installer is idempotent.

### Maintainer mode (`--local`)

For developing on MissionCache from a clone, `--local` swaps the PyPI installs for editable ones and registers the plugin via the local marketplace:

```bash
git clone https://github.com/missioncache/missioncache.git
cd missioncache
uvx missioncache-install --local
```

This is the workflow described in [`CONTRIBUTING.md`](../CONTRIBUTING.md). End users do not need `--local`.

## Windows: native or WSL2

Both work, and they are separate installs rather than two views of one. **Install MissionCache where Claude Code runs.** If Claude Code is the Windows app, install natively; if you work inside WSL, install inside WSL. Do not do both and expect one set of projects: WSL has its own home directory, so `~/.missioncache/` and `~/.claude/` are different directories in each, and neither install can see the other's projects, task DB, or time tracking.

### Native Windows

The install command is the same, `uvx missioncache-install`. What differs from macOS and Linux:

| Piece | On native Windows |
|-------|-------------------|
| Plugin hooks | Launch through `uv` in exec form, so they work the same under Git Bash and PowerShell. **Requires Claude Code 2.1.139+**, where hook `args` was added. An older client silently drops `args`, runs bare `uv`, and the hooks never run. `plugin.json` has no field to express that requirement, which is why it is stated here. |
| Python | The hooks resolve their own interpreter (`uv run --no-project --python ">=3.11"`) and will download one if the machine has none, so you do not need a `python3` on `PATH`. The installer pre-warms that resolution at install time so a first-use download cannot race the hooks' 5-second timeout. A marketplace-only install skips the warm; if a hook times out once on a fresh machine, run `uv python install` and retry. |
| Dashboard service | A Task Scheduler ONLOGON task, falling back to an HKCU Run-key entry when `schtasks` refuses from a non-elevated prompt (stock Windows denies ONLOGON triggers without elevation; the Run key never needs it). Both run `serve --hidden`, which keeps a console window from parking on the desktop for the whole session. |
| Statusline | Written into `settings.json` as the absolute forward-slash path to the executable, quoted when it contains a space. Git Bash eats backslashes and word-splits on spaces, and the scripts directory is not guaranteed to be on the statusline's `PATH`. |
| Slash-command bash blocks | Probe `python3` then `python` by actually executing them, not just resolving them: the Windows Store stub resolves on `PATH` but does not run. |
| `--local` maintainer mode | Falls back from symlinks to copies when Windows refuses them without Developer Mode (WinError 1314). |
| VSCode client | Not registered yet, macOS only. Codex and OpenCode register normally. |
| Cross-machine sharing | Importing a bundle onto Windows works, including Windows-shaped path rewriting. Exporting **from** Windows does not: the embedded-path scanner only recognizes `/`-rooted absolutes. |

How well this is verified: a `windows-latest` CI job runs on every push and does more than compile. It runs all six test suites on Windows, then runs `missioncache-install --all --yes` from a scratch directory the way a user would, and checks that the dashboard actually serves rather than merely registering, that the Task Scheduler task body runs (with the Run-key fallback path checked too), that stdin JSON reaches a hook through `uv run`, that `encode-cwd` produces the native drive-letter form rather than the MSYS one, and that the MCP server spawns through uvx and answers `initialize`. The known gaps are the specific ones in the table above, not general immaturity.

### WSL2

WSL2 is the ordinary Linux path, with one difference that bites on a fresh install. **Default WSL ships without systemd**, so `missioncache-dashboard install-service` cannot register a user unit. It detects that (using the check systemd's own docs recommend) and installs a managed autostart block in your shell profile instead, which starts the dashboard when you open a shell. A fresh WSL Ubuntu used to crash here with `Failed to connect to bus` before that fallback existed. The block assumes bash; on a `~/.bash_login`-only setup you need to add the line yourself.

If you would rather have a real service, enable systemd in `/etc/wsl.conf` and re-run `missioncache-dashboard install-service` - it will then take the normal Linux user-unit path.

Everything else is unchanged from Linux, and your state lives in `~/.missioncache/` inside the WSL filesystem.

## Plugin-only install (via marketplace)

If you only need the plugin core (slash commands, MCP tools, hooks, rules) and don't want the dashboard, MissionCache Auto CLI, or statusline, install MissionCache as a pure Claude Code plugin.

In Claude Code:

```
/plugin marketplace add missioncache/missioncache
/plugin install missioncache@missioncache
```

Restart your Claude Code session. The MCP server and bundled `missioncache-db` are built on demand via `uvx`; no manual `pip install` is needed.

**What you get:** per-project plan/context/tasks files, `/missioncache:load` resume, time heartbeat tracking in `~/.missioncache/tasks.db`, all 30+ MCP tools, and all MissionCache rules.

**What you give up:** local dashboard at `localhost:8787`, `missioncache-auto` CLI for parallel execution, rich statusline.

You can always upgrade to the full install later by running `uvx missioncache-install --dashboard --statusline --missioncache-auto --yes`. In PyPI mode (the default when not running from a clone), the installer does not create a local marketplace, so your existing `missioncache@missioncache` install stays untouched.

## Manual install (no installer)

For Docker, CI, air-gapped environments, if you want full control over every step, or if you only need to embed `missioncache-db` or `mcp-missioncache` in your own tooling. This reproduces what `missioncache-install` does, minus the interactive wizard and state tracking.

### From PyPI

```bash
# Python packages (pick the ones you need)
pip install missioncache-db missioncache-auto missioncache-dashboard mcp-missioncache

# Claude Code plugin (do this inside Claude Code, not the shell)
#   /plugin marketplace add missioncache/missioncache
#   /plugin install missioncache@missioncache

# Dashboard background service (after pip install missioncache-dashboard)
missioncache-dashboard install-service    # launchd on macOS, systemd on Linux

# Statusline wiring - add to ~/.claude/settings.json under "statusLine":
#   "statusLine": {"command": "missioncache-statusline"}

# Edit-count hook (optional, feeds the statusline edit counter)
# Add a PostToolUse HTTP hook in ~/.claude/settings.json pointing at
# http://localhost:8787/api/hooks/edit-count with matcher "Edit|Write|NotebookEdit"

# Rules (copy the plugin-shipped rule files into ~/.claude/rules/)
# File a copy of the repo's rules/*.md with a leading "<!-- missioncache-plugin:managed -->"
# comment so SessionStart refreshes them correctly.

# User-level slash commands (optional)
# Copy user-commands/*.md into ~/.claude/commands/ (whats-new, optimize-prompt)
```

### From a clone (editable, without `missioncache-install --local`)

```bash
git clone https://github.com/missioncache/missioncache.git
cd missioncache

# Editable Python packages
pip install -e ./missioncache-db
pip install -e ./missioncache-auto
pip install -e ./missioncache-dashboard
pip install -e ./mcp-server       # optional, only if embedding the MCP server directly

# Register the plugin via a local marketplace
mkdir -p ~/.claude/plugins/local-marketplace/.claude-plugin
cat > ~/.claude/plugins/local-marketplace/.claude-plugin/marketplace.json <<'EOF'
{
  "name": "local",
  "owner": {"name": "local"},
  "plugins": [
    {"name": "missioncache", "source": "./missioncache", "description": "missioncache"}
  ]
}
EOF
ln -s "$PWD" ~/.claude/plugins/local-marketplace/missioncache
claude plugins marketplace add ~/.claude/plugins/local-marketplace
claude plugins install missioncache@local

# Dashboard service
missioncache-dashboard install-service

# Statusline wiring + rules copy are the same as the PyPI path above.
```

The dashboard step and the rule-copy step are optional. The plugin MCP server runs fine without the dashboard; first use will be slower while `uvx` builds the server's virtualenv.

### Just `missioncache-db` or `mcp-missioncache` for your own tooling

`missioncache-db` and `mcp-missioncache` are published on PyPI and usable independently of the plugin:

```bash
pip install missioncache-db
```

Gives you the `missioncache-db` CLI and the `missioncache_db` Python library:

```python
from missioncache_db import TaskDB

db = TaskDB()            # defaults to ~/.missioncache/tasks.db, override via TaskDB(db_path=...)
db.initialize()
repo_id = db.add_repo("/path/to/repo")
task = db.create_task(name="my-task", repo_id=repo_id)
db.record_heartbeat(task_id=task.id, directory="/path/to/repo")
```

```bash
pip install mcp-missioncache
```

Gives you the `mcp-missioncache` entry point ready to wire into any MCP client. For Claude Desktop, add to your MCP config:

```json
{
  "mcpServers": {
    "missioncache": {
      "command": "mcp-missioncache"
    }
  }
}
```

For the Claude Code plugin, the bundled `uvx --with` flow is still preferred because it pins `missioncache-db` to the copy shipped with the plugin. Use the PyPI path only when you want a globally-installed MCP server that's not tied to a plugin checkout.

## Verifying the install

### Plugin core

Inside Claude Code, type `/missioncache:` - you should see the slash commands autocomplete. Then:

```
/missioncache:new
```

Should prompt you to create a new project.

### missioncache-db

```bash
missioncache-db list-active
```

Should return either an empty result or a list of active tasks (depending on whether you have any yet).

### Dashboard

```bash
curl -s http://localhost:8787/health
```

Should return `{"status":"ok"}`. If the service isn't running:

- macOS: `launchctl load ~/Library/LaunchAgents/com.missioncache.dashboard.plist`
- Linux: `systemctl --user start missioncache-dashboard`
- Manual: `missioncache-dashboard serve`

### missioncache-auto

```bash
missioncache-auto --help
```

Should print the CLI usage.

### Statusline

```bash
which missioncache-statusline
echo '{}' | missioncache-statusline
```

The first should print a path. The second prints an ANSI status block (it may be sparse without real session state, but should not error).

### MCP server (standalone)

```bash
mcp-missioncache --help
```

Should print the help text. Inside Claude Code, the MCP server is invoked via `uvx` from the plugin; you don't typically call it directly.

## Uninstall

### Via `missioncache-install`

```bash
uvx missioncache-install --uninstall
```

Removes: plugin registration, pip packages, service units, settings.json entries, and any rule files still carrying the MissionCache ownership marker. Preserves: `~/.missioncache/` (project files), `~/.missioncache/tasks.db` (task history), rule files that you customized and edited past the marker, user-level slash commands other than the two MissionCache-shipped ones, and any `<config>.bak` backups the installer left next to a modified config file (each config write is atomic - temp file plus rename - and leaves a one-time `<config>.bak` per run, e.g. `settings.json.bak`; uninstall does not delete these).

### Plugin-only install

In Claude Code:

```
/plugin uninstall missioncache@missioncache
/plugin marketplace remove missioncache/missioncache
```

### Manual uninstall

```bash
# Plugin
claude plugins uninstall missioncache@local   # (or missioncache@missioncache)

# Dashboard service
missioncache-dashboard uninstall-service      # or remove the plist/unit manually

# Python packages
pip uninstall missioncache-db missioncache-auto missioncache-dashboard mcp-missioncache missioncache-install

# Statusline wiring: remove the statusLine block from ~/.claude/settings.json

# Rules and user slash commands (only remove what is yours to remove)
# MissionCache-shipped rule files carry a "<!-- missioncache-plugin:managed -->" marker on line 1
# and are safe to delete; rule files without the marker are user-authored.
```

MissionCache state in `~/.missioncache/tasks.db` and `~/.missioncache/` is preserved so you can reinstall without losing history. Delete those directories manually if you want a clean wipe.

## Troubleshooting

**`uvx: command not found` when running the installer**
Install `uv`: `pip install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`. Make sure the install location (often `~/.local/bin`) is on your `PATH`. `pipx run missioncache-install` works as a substitute if you have `pipx`.

**`uvx missioncache-install` gives you an old version of the installer**
`uvx` caches packages. Clear with `uvx cache prune` or force a refresh with `uvx --refresh missioncache-install`.

**PEP 668 / "externally-managed-environment" error during install**
Your system Python is protected against `pip install`. The installer detects this and prints per-platform instructions - usually the fix is to install `pipx` from your package manager (`brew install pipx`, `apt install pipx`, or `dnf install pipx`) and re-run with `pipx run missioncache-install`.

**`claude plugins install` fails with "marketplace not found"**
You probably ran the manual steps out of order. Register the local marketplace first (`claude plugins marketplace add ~/.claude/plugins/local-marketplace`) before installing the plugin, or re-run `uvx missioncache-install --local` which handles the ordering.

**Dashboard not reachable at `localhost:8787`**
Check the service is running:
- macOS: `launchctl list | grep missioncache.dashboard`
- Linux: `systemctl --user status missioncache-dashboard`

If it's crashed, check logs:
- macOS: `tail -f ~/Library/Logs/missioncache-dashboard.log`
- Linux: `journalctl --user -u missioncache-dashboard -f`

Restart with `missioncache-dashboard reinstall-service`, which rewrites the unit file and reloads it.

**Statusline missing after install**
Check `~/.claude/settings.json` - the `statusLine.command` should be the bare string `"missioncache-statusline"`, not a path to a Python file. If you see `python3 ~/.claude/scripts/statusline.py`, that's from a pre-M10 install - rewrite it by hand or re-run `uvx missioncache-install --statusline`.

**`pip install mcp-missioncache` fails resolving missioncache-db**
`mcp-missioncache` depends on `missioncache-db` from PyPI. If your environment is offline or pinned to a private index that doesn't mirror missioncache-db, use the editable manual install instead or preload missioncache-db manually.

**Plugin changes don't show up after editing files**
Claude Code caches plugin content. Refresh:

```bash
claude plugins install missioncache@local
```

Then restart your Claude Code session. Skill-only edits can use `/reload-plugins` instead of a full restart.

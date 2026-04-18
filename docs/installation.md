# Installation

This document is the comprehensive reference for installing orbit. If you just want the quickstart, the [README](../README.md#install) covers the two most common paths in a few lines. This doc covers all four supported paths, what each one gives you, and how to verify and uninstall.

## Which path should I pick?

| Path | Best for | You get | You skip |
|------|----------|---------|----------|
| [Full install](#full-install-via-setupsh) | Most users | Plugin core + dashboard + orbit-auto CLI + statusline | - |
| [Plugin-only](#plugin-only-install-via-marketplace) | Minimal footprint, teams that don't want local services | Plugin core (commands, MCP tools, hooks, rules) | Dashboard, orbit-auto, statusline |
| [pip install](#pip-install-standalone-packages) | Embedding orbit-db or mcp-orbit in your own tooling | Standalone `orbit-db` and/or `mcp-orbit` Python packages | Slash commands, hooks, dashboard |
| [Manual install](#manual-install-no-setupsh) | Docker, CI, air-gapped environments, custom layouts | Full control over every step | `setup.sh` convenience |

All four paths can coexist. The plugin-only and full install both store state in `~/.claude/`, so you can start with plugin-only and add the dashboard later by running `setup.sh`.

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

Required only for the full install (dashboard, orbit-auto, statusline):

- **`pip`** (bundled with Python)
- **macOS or Linux** (Windows is untested; the dashboard's background service targets launchd and systemd)

## Full install (via setup.sh)

The fastest way to the complete orbit experience. Takes a minute or two on a clean machine.

```bash
git clone https://github.com/tomerbr1/claude-orbit.git
cd claude-orbit
./setup.sh
```

The interactive script runs 8 steps:

1. **Core plugin** - registers orbit in a local marketplace (`~/.claude/plugins/local-marketplace/`) and installs it as `orbit@local` via the Claude Code CLI
2. **orbit-db** - `pip install -e ./orbit-db` so `orbit-auto` and your own tooling can reach the task DB
3. **Dashboard** - installs dependencies and wires up a background service (launchd on macOS, systemd on Linux)
4. **Orbit Auto CLI** - `pip install -e ./orbit-auto`
5. **MCP server** - pre-builds the uvx virtual environment so the first prompt after install doesn't pay the build cost
6. **Statusline** (optional) - prompts for health-check services to monitor
7. **Rules** (optional) - symlinks `rules/*.md` into `~/.claude/rules/`
8. **User slash commands** (optional) - symlinks `user-commands/*.md` into `~/.claude/commands/` (`/whats-new`, `/optimize-prompt`)

Re-running `./setup.sh` is idempotent - it detects existing installs, refreshes the plugin cache, and restarts the dashboard service.

## Plugin-only install (via marketplace)

If you only need the plugin core (slash commands, MCP tools, hooks, rules) and don't want the dashboard, orbit-auto CLI, or statusline, install orbit as a pure Claude Code plugin.

In Claude Code:

```
/plugin marketplace add tomerbr1/claude-orbit
/plugin install orbit@claude-orbit
```

Restart your Claude Code session. The MCP server and bundled `orbit-db` are built on demand via `uvx`; no manual `pip install` is needed.

**What you get:** per-project plan/context/tasks files, `/orbit:go` resume, time heartbeat tracking in `~/.claude/tasks.db`, all 30+ MCP tools, and all orbit rules.

**What you give up:** local dashboard at `localhost:8787`, `orbit-auto` CLI for parallel execution, rich statusline.

You can always upgrade to the full install later by running `./setup.sh` from a checkout - it detects the existing marketplace install and skips the local-marketplace step.

## pip install (standalone packages)

The orbit-db and mcp-orbit packages are published on PyPI and can be installed independently of the plugin. This is useful when you want to:

- Embed `orbit-db` in your own tooling to read or write the orbit task DB
- Run `mcp-orbit` as a standalone MCP server (wired into Claude Desktop, Cursor, or any other MCP client)
- Install into a specific Python environment (virtualenv, conda, CI)

### orbit-db only

```bash
pip install orbit-db
```

Gives you the `orbit-db` CLI and the `orbit_db` Python library:

```python
from orbit_db import TaskDB

db = TaskDB()            # defaults to ~/.claude/tasks.db, override via TASK_DB_PATH
db.initialize()
repo_id = db.add_repo("/path/to/repo")
task = db.create_task(name="my-task", repo_id=repo_id)
db.record_heartbeat(task_id=task.id, directory="/path/to/repo")
```

### mcp-orbit (pulls in orbit-db)

```bash
pip install mcp-orbit
```

Gives you the `mcp-orbit` entry point ready to wire into any MCP client. For Claude Desktop, add to your MCP config:

```json
{
  "mcpServers": {
    "orbit": {
      "command": "mcp-orbit"
    }
  }
}
```

For the Claude Code plugin, the bundled `uvx --with` flow is still preferred because it pins `orbit-db` to the copy shipped with the plugin. Use the PyPI path only when you want a globally-installed MCP server that's not tied to a plugin checkout.

## Manual install (no setup.sh)

For Docker, CI, air-gapped environments, or if you want full control over every step. This reproduces what `setup.sh` does, minus the interactive prompts.

```bash
# 1. Clone
git clone https://github.com/tomerbr1/claude-orbit.git
cd claude-orbit

# 2. Create the local marketplace and install the plugin
mkdir -p ~/.claude/plugins/local-marketplace/.claude-plugin
cat > ~/.claude/plugins/local-marketplace/.claude-plugin/marketplace.json <<'EOF'
{
  "name": "local",
  "owner": {"name": "local"},
  "plugins": [
    {"name": "orbit", "source": "./orbit", "description": "orbit"}
  ]
}
EOF
ln -s "$PWD" ~/.claude/plugins/local-marketplace/orbit
claude plugins marketplace add ~/.claude/plugins/local-marketplace
claude plugins install orbit@local

# 3. orbit-db (editable, points at this checkout)
pip install -e ./orbit-db
python3 -m orbit_db init

# 4. Dashboard (only if you want localhost:8787)
pip install -r ./orbit-dashboard/requirements.txt
# Run manually or wire a service manager of your choice:
python3 ./orbit-dashboard/server.py &

# 5. orbit-auto (editable)
pip install -e ./orbit-auto

# 6. Pre-build the MCP server venv (optional; uvx will build on first use otherwise)
uvx --from ./mcp-server --with ./orbit-db mcp-orbit --help >/dev/null 2>&1 || true

# 7. Rules (symlink into ~/.claude/rules/)
mkdir -p ~/.claude/rules
for f in rules/*.md; do ln -sf "$PWD/$f" ~/.claude/rules/; done

# 8. User-level slash commands (symlink into ~/.claude/commands/)
mkdir -p ~/.claude/commands
for f in user-commands/*.md; do ln -sf "$PWD/$f" ~/.claude/commands/; done
```

Steps 4 (dashboard) and 6 (MCP pre-build) are optional. The plugin MCP server runs fine without pre-building; first use will be slower while `uvx` builds the venv.

## Verifying the install

### Plugin core

Inside Claude Code, type `/orbit:` - you should see the slash commands autocomplete. Then:

```
/orbit:new
```

Should prompt you to create a new project.

### orbit-db

```bash
orbit-db list-active
```

Should return either an empty result or a list of active tasks (depending on whether you have any yet).

### Dashboard

```bash
curl -s http://localhost:8787/health
```

Should return `{"status":"ok"}`. If the service isn't running yet, start it:

- macOS: `launchctl load ~/Library/LaunchAgents/com.orbit.dashboard.plist`
- Linux: `systemctl --user start orbit-dashboard`

### orbit-auto

```bash
orbit-auto --help
```

Should print the CLI usage.

### MCP server (standalone)

```bash
mcp-orbit --help
```

Should print the help text. (Inside Claude Code, the MCP server is invoked via `uvx` from the plugin; you don't typically call it directly.)

## Uninstall

### Plugin-only install

In Claude Code:

```
/plugin uninstall orbit@claude-orbit
/plugin marketplace remove tomerbr1/claude-orbit
```

### Full install (setup.sh)

```bash
# Plugin
claude plugins uninstall orbit@local

# Background service (macOS)
launchctl unload ~/Library/LaunchAgents/com.orbit.dashboard.plist
rm ~/Library/LaunchAgents/com.orbit.dashboard.plist

# Background service (Linux)
systemctl --user stop orbit-dashboard
systemctl --user disable orbit-dashboard
rm ~/.config/systemd/user/orbit-dashboard.service

# Python packages
pip uninstall orbit-db orbit-auto

# Rules and user slash commands (symlinks)
find ~/.claude/rules -maxdepth 1 -type l -lname "*claude-orbit*" -delete
find ~/.claude/commands -maxdepth 1 -type l -lname "*claude-orbit*" -delete
```

Orbit state in `~/.claude/tasks.db` and `~/.claude/orbit/` is preserved so you can reinstall without losing history. Delete those directories manually if you want a clean wipe.

### pip install

```bash
pip uninstall mcp-orbit orbit-db
```

## Troubleshooting

**`uvx: command not found` during plugin install or first prompt**
Install `uv`: `pip install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`. Make sure the install location (often `~/.local/bin`) is on your `PATH`.

**`claude plugins install` fails with "marketplace not found"**
You probably cloned but didn't run `setup.sh` and are trying to install manually. Either run `./setup.sh` or follow the [manual install](#manual-install-no-setupsh) steps to register the local marketplace first.

**Dashboard not reachable at `localhost:8787`**
Check the service is running:
- macOS: `launchctl list | grep orbit.dashboard`
- Linux: `systemctl --user status orbit-dashboard`

If it's crashed, check logs:
- macOS: `tail -f ~/Library/Logs/orbit-dashboard.log`
- Linux: `journalctl --user -u orbit-dashboard -f`

**`pip install mcp-orbit` fails resolving orbit-db**
`mcp-orbit` depends on `orbit-db` from PyPI. If your environment is offline or pinned to a private index that doesn't mirror orbit-db, use the editable full install instead or preload orbit-db manually.

**Plugin changes don't show up after editing files**
Claude Code caches plugin content. Refresh:

```bash
claude plugins install orbit@local
```

Then restart your Claude Code session. Skill-only edits can use `/reload-plugins` instead of a full restart.

# Installation

This document is the comprehensive reference for installing orbit. If you just want the quickstart, the [README](../README.md#install) covers the two most common paths in a few lines. This doc covers all three supported paths, what each one gives you, and how to verify and uninstall.

## Which path should I pick?

| Path | Best for | You get | You skip |
|------|----------|---------|----------|
| [`uvx orbit-install`](#full-install-via-uvx-orbit-install) | Most users | Plugin core + dashboard + orbit-auto CLI + statusline | - |
| [Plugin-only (marketplace)](#plugin-only-install-via-marketplace) | Minimal footprint, teams that don't want local services | Plugin core (commands, MCP tools, hooks, rules) | Dashboard, orbit-auto, statusline |
| [Manual / pip-only](#manual-install-no-installer) | Docker, CI, air-gapped environments, custom layouts, or embedding `orbit-db` / `mcp-orbit` in your own tooling | Full control over every step | `orbit-install` convenience |

All three paths can coexist. The plugin-only and `orbit-install` paths both store state in `~/.claude/`, so you can start plugin-only and add the dashboard later by running `uvx orbit-install --dashboard --statusline --orbit-auto`.

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
  `pipx` works in place of `uvx` for running the installer (`pipx run orbit-install`), but `uvx` is the path we test.

Required only for the full install (dashboard, orbit-auto, statusline):

- **macOS or Linux** for the dashboard background service (launchd on macOS, systemd user units on Linux). Windows is supported for the plugin, orbit-auto, and statusline components; the dashboard prints manual run instructions instead of registering a service.

## Full install (via `uvx orbit-install`)

One command, no clone needed. Takes a minute or two on a clean machine.

```bash
uvx orbit-install
# or
pipx run orbit-install
```

The interactive wizard asks which components to install (default is all) and runs:

1. **Plugin core** - installs the Claude Code plugin. In the default PyPI mode this registers `tomerbr1/claude-orbit` as a marketplace and installs `orbit@claude-orbit`. In `--local` mode (from a clone) it sets up a local marketplace at `~/.claude/plugins/local-marketplace/` and installs `orbit@local` instead.
2. **Dashboard** - pip-installs `orbit-dashboard` (which pulls in `orbit-db` as a dependency, giving your own tooling access to the task DB) and wires up a background service (launchd on macOS, systemd on Linux) via `orbit-dashboard install-service`
3. **Orbit Auto CLI** - pip-installs `orbit-auto` (also pulls in `orbit-db` as a dependency)
4. **Statusline** - wires `orbit-statusline` (a console entry point shipped in `orbit-dashboard`) into `~/.claude/settings.json`. Selecting statusline without dashboard auto-adds dashboard, since that is where the entry point ships from.
5. **Rules** - copies `rules/*.md` into `~/.claude/rules/` with an ownership marker so future updates can refresh them without overwriting user edits
6. **User-level slash commands** - copies `user-commands/*.md` (`/whats-new`, `/optimize-prompt`) into `~/.claude/commands/`

If you run a subset (no dashboard and no orbit-auto), `orbit-db` is not installed. Install it standalone with `pip install orbit-db` if you need the CLI.

Flags for non-interactive use:

```bash
uvx orbit-install --all --yes                     # install everything, no prompts
uvx orbit-install --dashboard --statusline --yes  # install a subset
uvx orbit-install --all --yes --no-statusline     # install everything except the statusline
uvx orbit-install --update                        # refresh installed components
uvx orbit-install --uninstall                     # remove everything (preserves user data)
uvx orbit-install --all --yes --port 9999         # dashboard on a non-default port
```

Opt-out flags (`--no-statusline`, `--no-dashboard`, etc.) only take effect alongside `--all` or explicit opt-ins. Running them on their own drops you into the interactive wizard.

State is tracked at `~/.claude/orbit-install.state.json` so subsequent runs can reconcile what is already installed. Re-running the installer is idempotent.

### Maintainer mode (`--local`)

For developing on orbit from a clone, `--local` swaps the PyPI installs for editable ones and registers the plugin via the local marketplace:

```bash
git clone https://github.com/tomerbr1/claude-orbit.git
cd claude-orbit
uvx orbit-install --local
```

This is the workflow described in [`CONTRIBUTING.md`](../CONTRIBUTING.md). End users do not need `--local`.

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

You can always upgrade to the full install later by running `uvx orbit-install --dashboard --statusline --orbit-auto --yes`. In PyPI mode (the default when not running from a clone), the installer does not create a local marketplace, so your existing `orbit@claude-orbit` install stays untouched.

## Manual install (no installer)

For Docker, CI, air-gapped environments, if you want full control over every step, or if you only need to embed `orbit-db` or `mcp-orbit` in your own tooling. This reproduces what `orbit-install` does, minus the interactive wizard and state tracking.

### From PyPI

```bash
# Python packages (pick the ones you need)
pip install orbit-db orbit-auto orbit-dashboard mcp-orbit

# Claude Code plugin (do this inside Claude Code, not the shell)
#   /plugin marketplace add tomerbr1/claude-orbit
#   /plugin install orbit@claude-orbit

# Dashboard background service (after pip install orbit-dashboard)
orbit-dashboard install-service    # launchd on macOS, systemd on Linux

# Statusline wiring - add to ~/.claude/settings.json under "statusLine":
#   "statusLine": {"command": "orbit-statusline"}

# Edit-count hook (optional, feeds the statusline edit counter)
# Add a PostToolUse HTTP hook in ~/.claude/settings.json pointing at
# http://localhost:8787/api/hooks/edit-count with matcher "Edit|Write|NotebookEdit"

# Rules (copy the plugin-shipped rule files into ~/.claude/rules/)
# File a copy of the repo's rules/*.md with a leading "<!-- orbit-plugin:managed -->"
# comment so SessionStart refreshes them correctly.

# User-level slash commands (optional)
# Copy user-commands/*.md into ~/.claude/commands/ (whats-new, optimize-prompt)
```

### From a clone (editable, without `orbit-install --local`)

```bash
git clone https://github.com/tomerbr1/claude-orbit.git
cd claude-orbit

# Editable Python packages
pip install -e ./orbit-db
pip install -e ./orbit-auto
pip install -e ./orbit-dashboard
pip install -e ./mcp-server       # optional, only if embedding the MCP server directly

# Register the plugin via a local marketplace
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

# Dashboard service
orbit-dashboard install-service

# Statusline wiring + rules copy are the same as the PyPI path above.
```

The dashboard step and the rule-copy step are optional. The plugin MCP server runs fine without the dashboard; first use will be slower while `uvx` builds the server's virtualenv.

### Just `orbit-db` or `mcp-orbit` for your own tooling

`orbit-db` and `mcp-orbit` are published on PyPI and usable independently of the plugin:

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

Should return `{"status":"ok"}`. If the service isn't running:

- macOS: `launchctl load ~/Library/LaunchAgents/com.orbit.dashboard.plist`
- Linux: `systemctl --user start orbit-dashboard`
- Manual: `orbit-dashboard serve`

### orbit-auto

```bash
orbit-auto --help
```

Should print the CLI usage.

### Statusline

```bash
which orbit-statusline
echo '{}' | orbit-statusline
```

The first should print a path. The second prints an ANSI status block (it may be sparse without real session state, but should not error).

### MCP server (standalone)

```bash
mcp-orbit --help
```

Should print the help text. Inside Claude Code, the MCP server is invoked via `uvx` from the plugin; you don't typically call it directly.

## Uninstall

### Via `orbit-install`

```bash
uvx orbit-install --uninstall
```

Removes: plugin registration, pip packages, service units, settings.json entries, and any rule files still carrying the orbit ownership marker. Preserves: `~/.claude/orbit/` (project files), `~/.claude/tasks.db` (task history), rule files that you customized and edited past the marker, user-level slash commands other than the two orbit-shipped ones.

### Plugin-only install

In Claude Code:

```
/plugin uninstall orbit@claude-orbit
/plugin marketplace remove tomerbr1/claude-orbit
```

### Manual uninstall

```bash
# Plugin
claude plugins uninstall orbit@local   # (or orbit@claude-orbit)

# Dashboard service
orbit-dashboard uninstall-service      # or remove the plist/unit manually

# Python packages
pip uninstall orbit-db orbit-auto orbit-dashboard mcp-orbit orbit-install

# Statusline wiring: remove the statusLine block from ~/.claude/settings.json

# Rules and user slash commands (only remove what is yours to remove)
# Orbit-shipped rule files carry a "<!-- orbit-plugin:managed -->" marker on line 1
# and are safe to delete; rule files without the marker are user-authored.
```

Orbit state in `~/.claude/tasks.db` and `~/.claude/orbit/` is preserved so you can reinstall without losing history. Delete those directories manually if you want a clean wipe.

## Troubleshooting

**`uvx: command not found` when running the installer**
Install `uv`: `pip install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`. Make sure the install location (often `~/.local/bin`) is on your `PATH`. `pipx run orbit-install` works as a substitute if you have `pipx`.

**`uvx orbit-install` gives you an old version of the installer**
`uvx` caches packages. Clear with `uvx cache prune` or force a refresh with `uvx --refresh orbit-install`.

**PEP 668 / "externally-managed-environment" error during install**
Your system Python is protected against `pip install`. The installer detects this and prints per-platform instructions - usually the fix is to install `pipx` from your package manager (`brew install pipx`, `apt install pipx`, or `dnf install pipx`) and re-run with `pipx run orbit-install`.

**`claude plugins install` fails with "marketplace not found"**
You probably ran the manual steps out of order. Register the local marketplace first (`claude plugins marketplace add ~/.claude/plugins/local-marketplace`) before installing the plugin, or re-run `uvx orbit-install --local` which handles the ordering.

**Dashboard not reachable at `localhost:8787`**
Check the service is running:
- macOS: `launchctl list | grep orbit.dashboard`
- Linux: `systemctl --user status orbit-dashboard`

If it's crashed, check logs:
- macOS: `tail -f ~/Library/Logs/orbit-dashboard.log`
- Linux: `journalctl --user -u orbit-dashboard -f`

Restart with `orbit-dashboard reinstall-service`, which rewrites the unit file and reloads it.

**Statusline missing after install**
Check `~/.claude/settings.json` - the `statusLine.command` should be the bare string `"orbit-statusline"`, not a path to a Python file. If you see `python3 ~/.claude/scripts/statusline.py`, that's from a pre-M10 install - rewrite it by hand or re-run `uvx orbit-install --statusline`.

**`pip install mcp-orbit` fails resolving orbit-db**
`mcp-orbit` depends on `orbit-db` from PyPI. If your environment is offline or pinned to a private index that doesn't mirror orbit-db, use the editable manual install instead or preload orbit-db manually.

**Plugin changes don't show up after editing files**
Claude Code caches plugin content. Refresh:

```bash
claude plugins install orbit@local
```

Then restart your Claude Code session. Skill-only edits can use `/reload-plugins` instead of a full restart.

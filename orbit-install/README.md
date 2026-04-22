# orbit-install

Bootstrap installer for [Orbit](https://github.com/tomerbr1/claude-orbit), the project manager for Claude Code.

## Install

```bash
uvx orbit-install
# or
pipx run orbit-install
```

The interactive wizard asks which components to install. Default is all:

| Component      | What it does                                                          |
|----------------|------------------------------------------------------------------------|
| Plugin         | Registers the orbit plugin with Claude Code (slash commands, MCP, hooks) |
| Dashboard      | Installs `orbit-dashboard` pip package + launchd/systemd service on port 8787 |
| orbit-auto CLI | Installs `orbit-auto` for autonomous task execution                   |
| Statusline     | Wires `~/.claude/settings.json` to run `orbit-statusline` on every prompt |
| Rules          | Copies rule files into `~/.claude/rules/`                             |
| User commands  | Copies `/whats-new` and `/optimize-prompt` into `~/.claude/commands/` |

## Non-interactive

```bash
uvx orbit-install --all                      # install everything
uvx orbit-install --dashboard --statusline   # install a subset
uvx orbit-install --update                   # refresh everything
uvx orbit-install --uninstall                # remove everything (preserves user data)
```

## Maintainer mode

From a clone of `claude-orbit`:

```bash
git clone https://github.com/tomerbr1/claude-orbit.git
cd claude-orbit
uvx orbit-install --local
```

`--local` swaps PyPI installs for editable ones and registers the plugin via a local marketplace. Edit files in the clone and see changes live.

## Windows

Windows service registration is not yet supported. The installer will register the plugin, pip-install orbit-auto, and print manual instructions for running the dashboard.

## Uninstall

```bash
uvx orbit-install --uninstall
```

Removes: plugin registration, pip packages, service units, settings.json entries. Preserves: `~/.claude/orbit/` (projects), `~/.claude/tasks.db` (task history).

## License

MIT

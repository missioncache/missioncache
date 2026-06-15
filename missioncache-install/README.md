# missioncache-install

Bootstrap installer for [MissionCache](https://github.com/missioncache/missioncache), the project manager for Claude Code.

## Install

```bash
uvx missioncache-install
# or
pipx run missioncache-install
```

The interactive wizard asks which components to install. Default is all:

| Component      | What it does                                                          |
|----------------|------------------------------------------------------------------------|
| Plugin         | Registers the MissionCache plugin with Claude Code (slash commands, MCP, hooks) |
| Dashboard      | Installs `missioncache-dashboard` pip package + launchd/systemd service on port 8787 |
| missioncache-auto CLI | Installs `missioncache-auto` for autonomous task execution            |
| Statusline     | Wires `~/.claude/settings.json` to run `missioncache-statusline` on every prompt |
| Rules          | Copies rule files into `~/.claude/rules/`                             |
| User commands  | Copies `/whats-new` and `/optimize-prompt` into `~/.claude/commands/` |

## Non-interactive

```bash
uvx missioncache-install --all                      # install everything
uvx missioncache-install --dashboard --statusline   # install a subset
uvx missioncache-install --update                   # refresh everything
uvx missioncache-install --uninstall                # remove everything (preserves user data)
```

## Maintainer mode

From a clone of `missioncache`:

```bash
git clone https://github.com/missioncache/missioncache.git
cd missioncache
uvx missioncache-install --local
```

`--local` swaps PyPI installs for editable ones and registers the plugin via a local marketplace. Edit files in the clone and see changes live.

## Windows

Windows service registration is not yet supported. The installer will register the plugin, pip-install missioncache-auto, and print manual instructions for running the dashboard.

## Uninstall

```bash
uvx missioncache-install --uninstall
```

Removes: plugin registration, pip packages, service units, settings.json entries. Preserves: `~/.missioncache/` (projects and task history).

## License

MIT

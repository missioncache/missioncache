# Installation

MissionCache installs with one command. You need Python 3.11 or newer, the Claude Code CLI, and `uv` (for `uvx`).

```bash
uvx missioncache-install
```

No `uvx`? Install uv with `curl -LsSf https://astral.sh/uv/install.sh | sh`, or run `pipx run missioncache-install` instead.

## Which install to pick

| Install | You get | Pick it when |
|---|---|---|
| **Full** (`uvx missioncache-install`) | The Claude Code plugin, the [dashboard](dashboard.md), `missioncache-auto`, the statusline, the rules, and the `missioncache-db` CLI | You want everything. Most people. |
| **Plugin only** (from Claude Code) | Slash commands, MCP tools, hooks and rules | You do not want local services running. |

You can start with the plugin and add the rest later. To wire every piece by hand (Docker, CI, no installer), see the link under More.

## Full install

Run `uvx missioncache-install`. A wizard asks which parts to install, all of them by default. Restart Claude Code when it finishes.

To skip the questions, pass flags:

```bash
uvx missioncache-install --all --yes                  # everything
uvx missioncache-install --all --yes --no-statusline  # everything but the statusline
```

| Flag | What it does |
|---|---|
| `--all` | Install every part without the wizard. |
| `--plugin`, `--dashboard`, `--missioncache-auto`, `--statusline`, `--rules`, `--user-commands`, `--missioncache-db`, `--codex`, `--opencode`, `--vscode` | Install only the parts you name. |
| `--no-<part>` | Leave a part out. Works only together with `--all` or with named parts. |
| `--no-service` | Install the dashboard but do not start it at login. |
| `--port N` | Run the dashboard on port `N` instead of 8787. |
| `--yes`, `-y` | Do not ask before each file change. |
| `--update` | Update what you installed. See [Upgrading](#upgrading). |
| `--uninstall` | Remove parts. See [Uninstall](#uninstall). |

The statusline needs the dashboard package, so asking for the statusline adds the dashboard too. Running the installer again is safe.

## Plugin only

In Claude Code:

```
/plugin marketplace add missioncache/missioncache
/plugin install missioncache@missioncache
```

Restart the session. There is nothing to `pip install`. The first MCP call is slower while `uvx` builds the server.

To add the dashboard, `missioncache-auto` and the statusline later:

```bash
uvx missioncache-install --dashboard --statusline --missioncache-auto --yes
```

## Other tools (Codex, OpenCode, VSCode)

The full installer finds Codex, OpenCode and VSCode Copilot Chat and asks, per tool, whether to add the MCP server and the MissionCache commands there.

| Tool | How you run a command | Note |
|---|---|---|
| Codex | `$missioncache-<name>`, for example `$missioncache-load` | Restart Codex after installing. |
| OpenCode | `/missioncache-<name>`, for example `/missioncache-save` | |
| VSCode | `/missioncache-<name>` | macOS only for now. |

Every command is there except `lead`. The lead role works in Claude Code only.

Without the wizard, use `--codex`, `--opencode` or `--vscode`. Add `--no-codex-commands` (or the OpenCode or VSCode version) to skip the commands.

## Editor extension

The VSCode and Cursor extension shows the active project in the status bar and its tasks in a sidebar. It needs `missioncache-db` on your PATH, which the full install gives you. It does not need Claude Code. Install steps are in [extension.md](extension.md).

## Windows: native or WSL2

Install MissionCache where Claude Code runs. If you use the Windows app, install on Windows. If you work inside WSL, install inside WSL. The two installs have separate home folders and cannot see each other's projects.

**Native Windows.** The command is the same, `uvx missioncache-install`. Known gaps:

- The hooks need **Claude Code 2.1.139 or newer**. With an older client they do not run.
- If a hook times out once on a new machine, run `uv python install` and try again.
- The dashboard starts at login as a Task Scheduler task. Its log is `~/.claude/logs/missioncache-dashboard-windows.log`.
- VSCode is not added on Windows. Codex and OpenCode are.
- You can import a shared project on Windows, but you cannot export one from Windows.
- If `--update` says the dashboard files are in use, close the processes it names (or reboot) and run `--update` again.

**WSL2.** This is the normal Linux install with one difference. Default WSL has no systemd, so the dashboard starts from your shell profile (`~/.bash_profile` or `~/.profile`) when you open a terminal. This assumes bash. To use a real service, turn on systemd in `/etc/wsl.conf` and run `missioncache-dashboard install-service` again.

## Upgrading

**Full install:**

```bash
uvx --refresh missioncache-install --update
```

This updates the parts you installed, including the Codex, OpenCode and VSCode commands. `--refresh` stops `uvx` from running an old copy of the installer. Restart Claude Code afterwards.

**Plugin only:** in Claude Code, run `/plugin update missioncache@missioncache`, then restart the session.

## Verify it works

| Check | Run | Expect |
|---|---|---|
| Plugin | Type `/missioncache:` in Claude Code | The commands autocomplete. |
| CLI | `missioncache-db list-active` | Your projects, or nothing yet. |
| Dashboard | `curl -s http://localhost:8787/health` | `"status": "healthy"` |
| Dashboard service | `missioncache-dashboard status` | Installed and running. |
| Auto | `missioncache-auto --help` | The usage text. |
| Statusline | `which missioncache-statusline` | A path. |

## Uninstall

```bash
uvx missioncache-install --uninstall                  # pick parts in a wizard
uvx missioncache-install --uninstall --all            # remove everything
uvx missioncache-install --uninstall codex,opencode   # remove only these
```

The wizard needs a terminal. In a script, use `--all` or a list.

Your projects in `~/.missioncache/` are never deleted, so you can install again and keep your history. Rule files you edited and `.bak` backups of your config files stay too. Delete `~/.missioncache/` yourself for a clean wipe.

**Plugin only:** in Claude Code, run `/plugin uninstall missioncache@missioncache`, then `/plugin marketplace remove missioncache`.

## When something goes wrong

**`uvx: command not found`.** Install uv (see the top of this page) and check that `~/.local/bin` is on your PATH. Or run `pipx run missioncache-install`.

**`uvx` runs an old installer.** Add `--refresh`: `uvx --refresh missioncache-install`.

**"externally-managed-environment" error.** Your system Python blocks `pip install`. Install pipx from your package manager (`brew install pipx`, `sudo apt install pipx` or `sudo dnf install pipx`) and run `pipx run missioncache-install`.

**The dashboard does not load.** Run `missioncache-dashboard status`. To rebuild the service, run `missioncache-dashboard reinstall-service`. Logs:

| Platform | Log |
|---|---|
| macOS | `~/.claude/logs/missioncache-dashboard-stdout.log` and `-stderr.log` |
| Linux | `journalctl --user -u missioncache-dashboard` |
| WSL without systemd | `~/.claude/logs/missioncache-dashboard-autostart.log` |
| Windows | `~/.claude/logs/missioncache-dashboard-windows.log` |

**No statusline.** Open `~/.claude/settings.json`. `statusLine.command` must be `missioncache-statusline`. If it is not, run `uvx missioncache-install --statusline --yes`.

## More

What each installer step does, the manual install, and the full Windows details: [internals/installation.md](internals/installation.md).

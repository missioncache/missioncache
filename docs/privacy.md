# Privacy

MissionCache keeps everything on your machine. There is no account, no telemetry and no MissionCache server. Nothing you write in a project is sent anywhere by MissionCache.

## What it stores

| Where | What |
|-------|------|
| `~/.missioncache/` | Your project files (plan, context, tasks), the SQLite database with projects, time tracking and a log of the changes MissionCache's tools made, and the dashboard's settings. |
| `~/.missioncache/tasks.db` (resume lists) | For every Claude Code session: its name and generated title, folder, transcript path, process id and project. The conversation itself stays in Claude Code's own files. **Remove** in the Sessions view or `missioncache-db resume forget` deletes an entry. |
| `~/.claude/rules/` | The MissionCache rule file, copied there by the `SessionStart` hook. |
| `~/.claude/hooks-state.db`, `~/.claude/hooks/state/` | Per-session state: which project a session is bound to, context use for the statusline. |
| `~/.missioncache/iterm/` | Only with the iTerm2 panels: their program, a log, and the names and folders of your open tabs with which ones you have looked at. A LaunchAgent, `com.missioncache.iterm`, starts them at login. If you let the installer add the shortcuts, it changes iTerm2's settings and keeps a copy of them in `~/.missioncache/backups/`. |
| `~/.missioncache/wt/` | Only with the Windows Terminal panels: their program, a log, and the names and folders of your open tabs with which ones you have looked at. A Task Scheduler task, `MissionCacheWtPanels`, starts them at logon (or an entry of the same name under the HKCU Run key), and a Start menu shortcut opens them. They read session titles from Claude Code's files under `~/.claude`. If you turn the shortcuts on, a keyboard hook runs while the panels do. Windows passes it every key press. It looks only for `Ctrl+Shift+[`, `]` and `\` while Windows Terminal is in front, and stores and sends nothing. |

Uninstalling MissionCache does not delete `~/.missioncache/`, so your projects survive a reinstall. Delete the folder yourself to remove them.

The change log keeps a project's history after you complete, rename or delete the project. `missioncache-db events clear <project>` removes it, and `missioncache-db prune` drops anything older than 90 days.

## What it connects to

| Destination | When | What is sent |
|-------------|------|--------------|
| `pypi.org` | First launch of the MCP server and hooks, and the update check | Package downloads through `uv`, and a version lookup. |
| `status.claude.com` | The statusline, unless you turn it off | Nothing about you. It reads the public incident feed. |
| `api.github.com` | The statusline | Nothing about you. It reads the latest Claude Code release number. |
| `api.anthropic.com` | The statusline, while it is enabled | Your Claude Code sign-in token, read from the macOS Keychain or `~/.claude/.credentials.json`, to fetch your own usage limits. Hiding the usage line does not stop this call. |
| `chatgpt.com` | The statusline's Codex line, only if Codex is signed in. You can turn it off in the statusline settings. | Your Codex sign-in token from `~/.codex/auth.json`, to fetch your own Codex usage. |
| `fonts.googleapis.com`, `fonts.gstatic.com`, `cdn.jsdelivr.net` | When you open the dashboard in your browser | Your browser loads fonts and chart libraries, like any web page. |
| A calendar URL you configure | The daily brief, only if you add an ICS feed | A request for that feed. |

Each sign-in token goes only to the company that issued it. The dashboard listens on `127.0.0.1:8787` and never on an external interface. The terminal panels listen on `127.0.0.1:8791`. They talk to iTerm2 through its local Python API, and read Windows Terminal's tabs through Windows UI Automation. They refuse requests from web pages. Other programs running as you on the same computer can read the tab names and folders they show, as they could read the files on disk.

## Questions

Open an issue at [github.com/missioncache/missioncache/issues](https://github.com/missioncache/missioncache/issues).

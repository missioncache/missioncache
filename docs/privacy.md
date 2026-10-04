# Privacy

MissionCache keeps everything on your machine. There is no account, no telemetry and no MissionCache server. Nothing you write in a project is sent anywhere by MissionCache.

## What it stores

| Where | What |
|-------|------|
| `~/.missioncache/` | Your project files (plan, context, tasks), the SQLite database with projects and time tracking, and the dashboard's settings. |
| `~/.claude/rules/` | The MissionCache rule file, copied there by the `SessionStart` hook. |
| `~/.claude/hooks-state.db`, `~/.claude/hooks/state/` | Per-session state: which project a session is bound to, context use for the statusline. |

Uninstalling MissionCache does not delete `~/.missioncache/`, so your projects survive a reinstall. Delete the folder yourself to remove them.

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

Each sign-in token goes only to the company that issued it. The dashboard listens on `127.0.0.1:8787` and never on an external interface.

## Questions

Open an issue at [github.com/missioncache/missioncache/issues](https://github.com/missioncache/missioncache/issues).

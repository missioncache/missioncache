# Terminal panels: internals

How the panels work under the hood, for maintainers. The user doc is [terminal-panels.md](../terminal-panels.md).

## Layout

| Part | Where | What it does |
|---|---|---|
| Shared server | `missioncache_dashboard/panels/server.py` | HTTP on `127.0.0.1:8791`, the Host and `X-Panel` guards, the resume lists, the row state and `state.json`, Open. |
| Shared rules | `missioncache_dashboard/panels/logic.py` | Activity, unseen, row names and order, joining a Windows tab to its Claude Code session, a session's status. |
| iTerm2 | `missioncache_dashboard/iterm/` | Talks to iTerm2 over its Python API. Runs from a LaunchAgent. |
| Windows Terminal | `missioncache_dashboard/wt/` | Reads tabs over UI Automation (`uia.py`), on one thread of its own. Runs from a Task Scheduler task or the HKCU Run key. |
| Installer | `missioncache_install/iterm_panels.py`, `wt_panels.py`, `daemon_venv.py` | Each daemon gets its own venv under the data root, so `--update` of the dashboard cannot strip its extras while it runs. |

The panels never write to the resume lists on close. The hooks do that.

## Windows Terminal

### Joining a tab to its session

Nothing in Windows ties a process to a tab, so the title is the only join. The order is:

1. The tab titled with the session's name, its MissionCache title or its own. A tab with Claude Code's glyph wins over a plain shell that kept the name.
2. The tab titled with the session's transcript title.
3. One Claude tab and one session with no transcript title left: those two.

Every step joins only when the match is the only one. Two sessions with the same name join no tab. A wrong join would focus, star or resume the other session, which is worse than a plain row.

### Status

From the MissionCache mod's `agent_status` row (`needs_you` is waiting, `idle` is done), and Claude Code's own `~/.claude/sessions/<pid>.json`. The newer one wins. Without the mod there is only working and idle.

### Open

`wt.exe -w 0 new-tab --title <name> -d <cwd> <full path of powershell.exe> -NoExit -Command <command>`, with every `;` escaped, because wt.exe splits its command line there. The environment drops `CLAUDECODE` and `CLAUDE_CODE_*`, or the new Claude Code runs as a child and writes no session record.

A session is held for 30 seconds from the moment the terminal is asked for a tab, so a second click cannot resume the same transcript twice. The hold clears early when the poll joins the new tab, or when the terminal reports that it opened nothing. Open has its own request timeout, longer than the slowest open.

### Keys

A low-level keyboard hook (`WH_KEYBOARD_LL`), not `RegisterHotKey`, so other programs keep the keys while no Terminal window is in front. It matches scan codes, so the keys stay on the same physical keys in every layout. Windows passes the hook every key press, so it is opt-in and default no.

### Staying up

- A poll or focus read that fails every round is logged once with its traceback, and once more when it recovers.
- A UI Automation thread stuck in one call for over 30 seconds is replaced by a new one.
- A port Windows holds is asked for again every 30 seconds.

### Framing

`/panels` frames both panels, so the daemon sends `frame-ancestors 'self'` and `SAMEORIGIN`. The iTerm2 daemon keeps `DENY`.

## Live check on Windows

These need a real Windows machine. Run them on a branch install (`uvx missioncache-install --local --wt-panels`) before a release that changes `wt/` or `wt_panels.py`.

| Check | What to look for |
|---|---|
| Tabs | Every tab of every Terminal window shows, stable and Preview. |
| Focus | A click brings the tab and its window to the front, from the panel and from a key. |
| Open, Terminal running | One new tab in the window used last, running the resume command. A second click goes to it. |
| Open, Terminal closed | Same, and the request does not hang on wt.exe. |
| Folder names | Open works for a folder with `;`, one ending in `\;`, and one with `%` in its name. |
| PowerShell | The new tab runs `System32\WindowsPowerShell\v1.0\powershell.exe`. |
| Keys | All three keys in English and Hebrew layouts, and one other layout. Other programs get them while no Terminal window is in front. |
| Hook drop | After a long session and heavy typing, the keys still work. If not, check `run.log` for "keyboard hook stopped". |
| Hidden tab row | Fullscreen, focus mode, and one tab with `alwaysShowTabs` off: what the panel shows. |
| Other desktops | A Terminal window on another virtual desktop, and a minimized one. |
| Split panes | Two Claude sessions in panes of one tab. |
| Logon | The task starts the daemon at logon, and `/TR` works with a space in the profile path. |
| Run key | With schtasks refused, the Run key starts it. |
| Elevated installer | Installing from an elevated prompt, then Open: which window the tab opens in. |
| Antivirus | Defender or another scanner does not flag the hook. |
| Stuck UI Automation | A hung Terminal window: the log says a new thread started, and the panel updates again. |
| Uninstall | Removes the task, the Run key, the shortcut and the folder. With the daemon running and unkillable, removes nothing. |

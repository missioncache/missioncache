# Terminal panels

Two panels that list your Claude Code sessions, plus shortcuts to move between them. On macOS they live in iTerm2's Toolbelt. On Windows they open in a window beside Windows Terminal (an Edge app window, or a browser tab without Edge). Add them with:

```bash
uvx missioncache-install --iterm-panels   # macOS, iTerm2
uvx missioncache-install --wt-panels      # Windows, Windows Terminal
```

Then, in iTerm2, open **View > Toolbelt** and tick **Recent Sessions** and **Saved Tabs**. On Windows, open **MissionCache Panels** from the Start menu.

The panels are optional and not part of `--all`. The installer asks about them only when the terminal is installed, and the default answer is no.

## Before you start

| Need | Where |
|---|---|
| The MissionCache plugin | Fills the resume lists that Saved Tabs shows. On Windows it also tells the panels whether each session is working, waiting for you or done. |
| iTerm2's Python API (macOS) | iTerm2 Settings > General > Magic > **Enable Python API**. Without it the panels say "Waiting for iTerm2..." |
| iTerm2 3.7 or later, with its Claude Code integration (macOS) | Shows whether each session is working, waiting for you or done. Without it the panels still list sessions, but cannot tell you which one finished. |

## The two panels

| Panel | What it shows |
|---|---|
| **Recent Sessions** | Every open terminal tab, the one that changed last on top, below the lead session if one runs. A Claude session that finished or needs an answer since you last looked at it is highlighted. Click a row to go to its tab. The star saves the Claude session in that tab. |
| **Saved Tabs** | The same Saved and Closed lists as the dashboard's [Sessions view](dashboard.md#sessions). **Open** goes to the session's tab when it is running, and otherwise opens a new tab that resumes it. You can also pin, rename, keep and remove entries. |

The bar at the top of Recent Sessions has two buttons. The MissionCache logo opens the dashboard in your browser. **Lead** opens the Lead view, and is outlined in blue while a lead session is running.

A row with the MissionCache logo is a session bound to a project. Those are saved when they close, without the star.

On Windows, both panels share one window. Drag the **Saved Tabs** bar to change how much room each one gets.

## Shortcuts

| macOS | Windows | What it does |
|---|---|---|
| `Cmd+Shift+]` | `Ctrl+Shift+]` | Next session in the Recent Sessions order. |
| `Cmd+Shift+[` | `Ctrl+Shift+[` | Previous session. |
| `Cmd+Shift+\` | `Ctrl+Shift+\` | The newest session that finished or is waiting for you. |

**On macOS**, the installer offers to add these to iTerm2's settings. It shows what it will change, asks first (the default is no), and saves a copy of your iTerm2 settings to `~/.missioncache/backups/` before writing. It also turns on the iTerm2 option that keeps shortcuts on the same physical keys in every keyboard layout. To add them yourself: iTerm2 Settings > Keys > Key Bindings > **+**, pick the action **Invoke Script Function**, and enter `session_next()`, `session_prev()` or `jump_to_unseen()`.

**On Windows**, the installer asks whether to turn them on, and the default is no. They need a keyboard hook. Windows passes it every key press, and it acts only on these three keys, only while a Windows Terminal window is in front, and keeps nothing. They stay on the same physical keys in every keyboard layout. While a Windows Terminal window is in front, these three keys go to the panels, not to the program in the tab. To change your answer, run `uvx missioncache-install --wt-panels` again.

## Updating and removing

`uvx missioncache-install --update` updates the panels with everything else.

```bash
uvx missioncache-install --uninstall iterm-panels   # macOS
uvx missioncache-install --uninstall wt-panels      # Windows
```

This stops the panels, removes them from login, and deletes `~/.missioncache/iterm/` or `~/.missioncache/wt/`. On Windows it also removes the Start menu shortcut. On macOS it removes the shortcuts the installer added, unless you changed them since, and turns the keyboard-layout option off again if the installer turned it on. Your resume lists stay, because the dashboard and `missioncache-db resume` use them too. On Windows, if the panels cannot be stopped, nothing is removed: close them and run the uninstall again.

## Troubleshooting

| Problem | What to do |
|---|---|
| The panels are not in iTerm2's Toolbelt menu | Turn on the Python API (see above). Then run `launchctl kickstart -k gui/$(id -u)/com.missioncache.iterm`. |
| A panel says "Waiting for iTerm2..." | Same as above. The log is `~/.missioncache/iterm/run.log`. |
| The Windows window cannot reach the page | The panels start at logon. To start them now, run `schtasks /Run /TN MissionCacheWtPanels`. If that task does not exist, run `uvx missioncache-install --wt-panels` again, which asks about the keys again. The log is `run.log` in `%USERPROFILE%\.missioncache\wt`. |
| A Claude session shows as a plain tab on Windows | The panels match a tab to its session by the tab title, so two sessions with the same name match neither. Give the session a name with `/rename`, or bind it to a project. A tab renamed in Windows Terminal no longer matches. Sessions inside WSL and in split panes are not supported. |
| A shortcut does nothing | On macOS, check it in iTerm2 Settings > Keys > Key Bindings. On Windows, run `uvx missioncache-install --wt-panels` again and turn the keys on. Another app may use the same keys. |
| Saved Tabs is empty | Sessions show up there when they close. Start a new Claude Code session after installing the plugin. |
| Open says the directory does not exist | The session's folder was moved or deleted. Remove the entry. |
| Open says the session cannot be resumed | Claude Code deleted its conversation, or its folder name has a control character, which is never typed into a terminal. |

The panels only talk to `127.0.0.1`. See [Privacy](privacy.md).

How it works inside: [internals/terminal-panels.md](internals/terminal-panels.md).

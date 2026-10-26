# iTerm2 panels

Two panels in iTerm2's Toolbelt that list your Claude Code sessions, plus shortcuts to move between them. macOS only. Add them with:

```bash
uvx missioncache-install --iterm-panels
```

Then, in iTerm2, open **View > Toolbelt** and tick **Recent Sessions** and **Saved Tabs**.

The panels are optional and not part of `--all`. The installer asks about them only when iTerm2 is installed, and the default answer is no.

## Before you start

| Need | Where |
|---|---|
| iTerm2's Python API | iTerm2 Settings > General > Magic > **Enable Python API**. Without it the panels say "Waiting for iTerm2..." |
| iTerm2 3.7 or later, with its Claude Code integration | Shows whether each session is working, waiting for you or done. Without it the panels still list sessions, but cannot tell you which one finished. |
| The MissionCache plugin | Fills the resume lists that Saved Tabs shows. |

## The two panels

| Panel | What it shows |
|---|---|
| **Recent Sessions** | Every open iTerm2 tab, the one that changed last on top, below the lead session if one runs. A Claude session that finished or needs an answer since you last looked at it is highlighted. Click a row to go to its tab. The star saves the Claude session in that tab. |
| **Saved Tabs** | The same Saved and Closed lists as the dashboard's [Sessions view](dashboard.md#sessions). **Open** goes to the session's tab when it is running, and otherwise opens a new tab that resumes it. You can also pin, rename, keep and remove entries. |

The bar at the top of Recent Sessions has two buttons. The MissionCache logo opens the dashboard in your browser. **Lead** opens the Lead view, and is outlined in blue while a lead session is running.

A row with the MissionCache logo is a session bound to a project. Those are saved when they close, without the star.

## Shortcuts

| Keys | What it does |
|---|---|
| `Cmd+Shift+]` | Next session in the Recent Sessions order. |
| `Cmd+Shift+[` | Previous session. |
| `Cmd+Shift+\` | The newest session that finished or is waiting for you. |

The installer offers to add these to iTerm2's settings. It shows what it will change, asks first (the default is no), and saves a copy of your iTerm2 settings to `~/.missioncache/backups/` before writing. It also turns on the iTerm2 option that keeps shortcuts on the same physical keys in every keyboard layout.

To add them yourself: iTerm2 Settings > Keys > Key Bindings > **+**, pick the action **Invoke Script Function**, and enter `session_next()`, `session_prev()` or `jump_to_unseen()`.

## Updating and removing

`uvx missioncache-install --update` updates the panels with everything else.

```bash
uvx missioncache-install --uninstall iterm-panels
```

This stops the panels, removes them from login, and deletes `~/.missioncache/iterm/`. It removes the shortcuts the installer added, unless you changed them since, and turns the keyboard-layout option off again if the installer turned it on. Your resume lists stay, because the dashboard and `missioncache-db resume` use them too.

## Troubleshooting

| Problem | What to do |
|---|---|
| The panels are not in the Toolbelt menu | Turn on the Python API (see above). Then run `launchctl kickstart -k gui/$(id -u)/com.missioncache.iterm`. |
| A panel says "Waiting for iTerm2..." | Same as above. The log is `~/.missioncache/iterm/run.log`. |
| A shortcut does nothing | Check it in iTerm2 Settings > Keys > Key Bindings. Another app may use the same keys. |
| Saved Tabs is empty | Sessions show up there when they close. Start a new Claude Code session after installing the plugin. |
| Open says the directory does not exist | The session's folder was moved or deleted. Remove the entry. |
| Open says the session cannot be resumed | Claude Code deleted its conversation, or its folder name has a control character, which is never typed into a terminal. |

The panels only talk to `127.0.0.1`. See [Privacy](privacy.md).

# Editor extension

MissionCache's VSCode extension shows your active project in the status bar and opens a sidebar with its tasks, next steps and blockers. It works in VSCode and in editors built on it, such as Cursor. It does not need Claude Code: it reads project state through the `missioncache-db` CLI, so it works next to any AI tool MissionCache supports.

**Short version.** Install the extension and make sure `missioncache-db` is on your PATH (the full install puts it there). The status bar shows the project bound to the folder you have open, with its progress as a fraction. Click it for a menu. The MissionCache icon in the activity bar opens the sidebar. Click a task or a next step to open the file at that line.

## What it needs

| Requirement | Why |
|---|---|
| `missioncache-db` 1.0.28 or newer on PATH | The extension runs `missioncache-db extension-state` and `missioncache-db extension-project` to read project state. With an older CLI it shows "update needed" and tells you the update command. |
| VSCode 1.96 or newer, or a fork of it | The `engines.vscode` floor in `package.json`. |

The extension never writes to your project files. Tasks are ticked by your AI tool through MissionCache, not from the sidebar.

## Install

The extension is not on the VS Marketplace or Open VSX yet. Build it from the repo and side-load it:

```bash
cd missioncache-extension
npm ci
npm run build:prod
npm run package                      # -> missioncache-0.2.0.vsix
code --install-extension missioncache-0.2.0.vsix
```

In Cursor, install the same `.vsix` from the Extensions view (the "Install from VSIX" action in its menu). Reload the window afterwards.

## The status bar

The item on the left of the status bar reads `<project> <done>/<total>`, with an arrow badge when a MissionCache update is available. Hover for a tooltip with the last save time, the fork parent if any, and the remaining-work summary.

Which project it shows:

1. A project you picked from the menu, remembered per workspace.
2. Otherwise the active project whose registered repository is the folder you have open (walking up to the git root).
3. Otherwise the most recently worked-on active project.

Click the item for a menu: open the tasks file, open the context file, open the project in the dashboard, switch to another project, go back to automatic detection, or refresh.

## The sidebar

The MissionCache icon in the activity bar opens a view named **Project** with four groups:

| Group | What is in it |
|---|---|
| Tasks | Open tasks grouped by the `## ` section they sit under in the tasks file, then a collapsed **Completed** group. The label is the task's number and first sentence. Hover for the full text. |
| Next Steps | The items of the context file's Next Steps section. |
| Waiting on | The rows of the Waiting on table, as `what - who`. Hover for since and gates. |
| Other projects | Your other active projects with how long ago they were worked on. Click one to switch the whole extension to it. |

Clicking a task, a next step or a waiting row opens its file at that line. The refresh button in the view title re-reads everything.

The sidebar is read-only on purpose. Checking a box there would write to the tasks file outside MissionCache's file lock, which two sessions on the same project rely on.

## When it refreshes

There is no polling. The extension re-reads state when a markdown file under `~/.missioncache/` changes (debounced to half a second), when the editor window regains focus, and when you press refresh.

## What each state means

| Status bar | Meaning | Fix |
|---|---|---|
| `MissionCache` with a slash icon | `missioncache-db` is not on PATH | `uvx missioncache-install` |
| `MissionCache: update needed` | The CLI predates `extension-state` or `extension-project` | `uvx --refresh missioncache-install@latest --update` |
| `MissionCache: no active project` | No active projects in the database | `/missioncache:new` in your AI tool |
| A warning icon | The CLI failed | Hover for the last lines of its output |

The sidebar shows the same states as a single line.

## The data behind it

Two `missioncache-db` verbs feed the extension, and you can run them yourself:

```bash
missioncache-db extension-state --dir "$PWD"     # every active project, with progress and a dir_match flag
missioncache-db extension-project <task_id>      # one project's tasks, Next Steps and Waiting on, with line numbers
```

Both print JSON with a `schema` field. The extension refuses a schema it does not know and asks for an update, so a future change to the format cannot render wrong silently. See [cli.md](cli.md) for the field lists.

## Where the code is

`missioncache-extension/` in the repo: `src/extension.ts` (status bar, menu, CLI calls, refresh) and `src/tree.ts` (the sidebar). `npm run typecheck` checks it. The publish workflow in `.github/workflows/publish-extension.yml` builds the `.vsix` and publishes it to both marketplaces on an `extension-v*` tag, once the publisher tokens exist.

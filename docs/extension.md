# Editor extension

A VSCode extension that shows your active project in the status bar and opens a sidebar with its tasks, next steps and blockers.

It works in VSCode and in editors built on it, such as Cursor. It does not need Claude Code. It reads project state through the `missioncache-db` CLI, so it works next to any AI tool MissionCache supports.

## What it needs

| Requirement | Why |
|---|---|
| `missioncache-db` 1.0.28 or newer on PATH | The extension reads project state through it. The full install puts it there. With an older CLI the extension shows "update needed" and the update command. |
| VSCode 1.96 or newer, or an editor built on it | The oldest version the extension supports. |

The extension never writes to your project files. Your AI tool ticks tasks through MissionCache, not the sidebar.

## Install

The extension is not on the VS Marketplace or Open VSX yet. Build it from the repo and install the file:

```bash
cd missioncache-extension
npm ci
npm run build:prod
npm run package                      # -> missioncache-0.2.0.vsix
code --install-extension missioncache-0.2.0.vsix
```

In Cursor, open the Extensions view, choose "Install from VSIX" in its menu, and pick the same file. Reload the window afterwards.

## The status bar

The item on the left reads `<project> <done>/<total>`. An arrow badge means a MissionCache update is available. Hover for the last save time, the fork parent if there is one, and what is left.

Which project it shows:

1. The project you picked from the menu. It is remembered per workspace.
2. Otherwise, the active project whose repo is the folder you have open, or its git root.
3. Otherwise, the active project you worked on last.

Click the item for a menu. From there you can open the tasks file or the context file, open the project in the dashboard, switch project, go back to automatic detection, or refresh.

## The sidebar

The MissionCache icon in the activity bar opens a view named **Project** with four groups:

| Group | What is in it |
|---|---|
| Tasks | Open tasks, grouped by the `## ` section they sit under, then a collapsed **Completed** group. Each label is the task number and its first sentence. Hover for the full text. |
| Next Steps | The items in the context file's Next Steps section. |
| Waiting on | The rows of the Waiting on table, as `what - who`. Hover for since and gates. |
| Other projects | Your other active projects, with how long ago you worked on each. Click one to switch the extension to it. |

Click a task, a next step or a Waiting on row to open its file at that line. The refresh button in the view title reads everything again.

The sidebar is read-only on purpose. A checkbox there would write to the tasks file outside MissionCache's file lock, and two sessions on one project rely on that lock.

## When it refreshes

It does not poll. It reads state again when a markdown file under `~/.missioncache/` changes, when the editor window gets focus, and when you press refresh.

## What each state means

| Status bar | Meaning | Fix |
|---|---|---|
| `MissionCache` with a slash icon | `missioncache-db` is not on PATH | `uvx missioncache-install` |
| `MissionCache: update needed` | The CLI is too old for the extension | `uvx --refresh missioncache-install@latest --update` |
| `MissionCache: no active project` | The database has no active projects | `/missioncache:new` in your AI tool |
| A warning icon | The CLI failed | Hover for the last lines of its output |

The sidebar shows the same states as a single line.

## The data behind it

Run the two commands the extension uses to see its data as JSON. [cli.md](cli.md) lists the fields.

```bash
missioncache-db extension-state --dir "$PWD"     # every active project, with progress
missioncache-db extension-project <task_id>      # one project's tasks, Next Steps and Waiting on
```

## Where the code is

`missioncache-extension/` in the repo. `src/extension.ts` has the status bar, the menu, the CLI calls and the refresh. `src/tree.ts` has the sidebar. `npm run typecheck` checks it. The publish workflow in `.github/workflows/publish-extension.yml` builds the `.vsix` and publishes it to both marketplaces on an `extension-v*` tag, once the publisher tokens exist.

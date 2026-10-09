# CLI Reference

`missioncache-db` is the command line for your MissionCache projects and time data. Every install puts it on your PATH.

Run `missioncache-db` with no arguments to print the built-in usage.

Daily work goes through the slash commands and the [MCP tools](mcp-tools.md). Use the CLI for scripts, maintenance, and the jobs only it does: moving projects between machines, repair and cleanup.

`<task>` means a numeric task id. In the project-management commands it can also be a project name.

## Every command

| Command | What it does |
|---|---|
| **Projects** | |
| `list-active [--flat]` | Active projects as a tree, or a flat list. |
| `list-completed [days]` | Projects completed in the last `days` (default 30). |
| `list-names [active\|completed]` | Project names only, one per line. For shell completion. |
| `get-task <task>` | One project's details as JSON. |
| `get-task-by-name <name> [--status S]` | Find a project by name, as JSON. |
| `create-task [--type non-coding] [--jira KEY] [--category CAT] <name>` | Create a project. |
| `complete-task <task>` | Mark a project done. |
| `reopen-task <task>` | Reopen a done project. |
| `rename-task <old> <new>` | Rename a project. |
| `set-jira <task> <key>` | Set the project's ticket key. |
| `set-category <task> <category\|none>` | Set or clear the project's category. |
| `add-update <task> <note>` | Add a timestamped note. |
| `get-updates <task> [limit]` | Show a project's notes. |
| `today-updates [task]` | Show today's notes. |
| **Project management** | |
| `action-item add\|list\|done\|update ...` | Track who owes what, by when. |
| `stakeholder add\|remove\|list ...` | People on a project. |
| `ticket add\|remove\|list ...` | Tickets linked to a project. |
| `due-date <task> <YYYY-MM-DD\|none>` | Set or clear the project's due date. |
| `lead set\|mark\|show\|stop` | Manage the lead session behind `/missioncache:lead` and `/missioncache:unlead`. `mark` stamps the delta cursor the lead's `--delta` tick reads from. |
| `events list [--since ISO] [--after-id N] [--project NAME] [--kind K[,K]] [--limit N] [--json]` | What changed in your projects, newest first. `events prune [--days N]` deletes old rows, `events clear <project>` one project's history. |
| `agenda [--date today\|tomorrow\|YYYY-MM-DD] [--json] [--source NAME] [--no-cache]` | Print one day of your configured calendar. |
| **Time** | |
| `heartbeat <task> [session_id]` | Record activity on a project. |
| `heartbeat-auto` | Record activity on the project for this folder. |
| `process-heartbeats` | Turn recorded activity into work sessions. |
| `task-time <task> [all\|week\|today]` | Time spent on a project. |
| `current-session [task]` | Time in the current session. |
| **Repos** | |
| `init` | Create the database. |
| `add-repo <path> [name]` | Track a repo. |
| `add-repos-glob "<pattern>"` | Track every folder that matches. Quote the pattern. |
| `list-repos` | Tracked repos. |
| `scan [repo_id]` | Look for project files in tracked repos. |
| **Tags** | |
| `add-keyword <word>` | Add a word that tags projects by name. |
| `remove-keyword <word>` | Remove a word you added. |
| `list-keywords` | All tag words. |
| `backfill-tags` | Re-tag existing projects after you change the words. |
| **Moving between machines** | |
| `export <name> [...]` | Pack a project into a bundle. See [below](#export-and-import). |
| `import <bundle> [...]` | Unpack a bundle on this machine. |
| `config set-path\|list-paths\|show\|seed` | Manage this machine's path map. |
| **Maintenance** | |
| `health` | Report problems in every active project's context file. |
| `repair [name...] [--all] [--apply]` | Fix a damaged context file. |
| `prune [days]` | Archive old completed projects. |
| `prune-sessions [--days N] [--dry-run]` | Delete state left by closed sessions. |
| `cleanup [--dry-run]` | Archive orphans, fix duplicate names and paths. |
| `migrate-orbit-docs [--dry-run]` | Move old project docs into `~/.missioncache/`. |
| `encode-cwd [path]` | Print Claude Code's folder key for a path. |
| **Editor extension** | |
| `extension-state [--dir PATH]` | JSON snapshot of all active projects. |
| `extension-project <task>` | JSON outline of one project. |

## Export and import

Move a project to another machine.

```bash
missioncache-db export my-project                   # writes ./my-project.missioncache-bundle/
missioncache-db export my-project --out p.tgz       # writes a tarball
missioncache-db import p.tgz --dry-run              # show what would happen
missioncache-db import p.tgz                        # do it
```

| Flag | Command | What it does |
|---|---|---|
| `--out PATH` | export | Write somewhere else. A name ending `.tgz` or `.tar.gz` makes a tarball. |
| `--no-time` | export | Leave out the time you spent on this machine. |
| `--json` | both | Print JSON instead of the summary. |
| `--repo PATH` | import | Use this local repo for the project. |
| `--force` | import | Overwrite local project files that differ from the bundle. |
| `--rewrite-paths` | import | Change absolute paths in the docs to this machine's paths. |
| `--dry-run` | import | Report only. Write nothing. |

Import takes a folder, a tarball or a zip. It never overwrites an unrelated project with the same name, even with `--force`.

After import you get a report in three groups: **resolved**, **needs mapping** and **missing**. Each item that needs mapping comes with the exact `config set-path` command to run. The exit code is 0 when everything resolved, 2 when something needs mapping or is missing, and 1 on an error.

### The path map

The path map in `~/.missioncache/machine.json` tells import where things live on this machine.

```bash
missioncache-db config seed                                   # fill it from your repos' git remotes
missioncache-db config set-path repo:<remote> ~/work/my-repo  # add one entry
missioncache-db config list-paths [repo|vault|anchor] [--json]
missioncache-db config show                                   # print the whole file
```

`repo:` maps a git remote to a local checkout. `vault:` maps a vault name to its folder. `anchor:` maps a named path prefix.

You can import onto Windows, but you cannot export from Windows.

## Health

```bash
missioncache-db health
```

For each active project, `health` reports:

- `Last Updated` older than 14 days, or Waiting-on rows older than 7
- a context file over 100 KB, or a missing core section
- more than 12 Recent Changes entries
- an unclosed code block, a duplicate section, or Recent Changes entries outside their section
- overdue action items, a due date within 7 days, or items open over 14 days with no due date

It only reports and always exits 0. `/missioncache:load` shows the same warnings.

## Repairing a damaged context file

```bash
missioncache-db repair                  # every active project, dry run
missioncache-db repair my-project       # one project, active or completed
missioncache-db repair --all            # active and completed
missioncache-db repair --all --apply    # write the fixes
```

Nothing changes until you pass `--apply`.

`repair` merges duplicate sections into the first one. It moves stray Recent Changes entries back into their section, newest first, and moves entries past the 12-entry limit into the project's journal.

It does not fix an unclosed code block. It prints the line so you can fix it. It does not delete pasted-in sections either. Remove those with `update_context_file(sections_remove=[...])`.

## Prune and cleanup

```bash
missioncache-db prune 90                     # archive projects completed over 90 days ago (default 30)
missioncache-db prune-sessions --dry-run     # show what would go
missioncache-db cleanup --dry-run            # show what would change
```

`prune` hides old completed projects from the completed lists. They stay in the database. It also deletes events older than 90 days. Nothing runs it for you, so run it now and then.

`prune-sessions` deletes state that closed sessions leave in `~/.claude/hooks/state/`. It keeps anything newer than `--days` (default 7) and any session that still runs.

`cleanup` archives projects whose files are gone, moves project files left in repos into `~/.missioncache/`, and fixes duplicate names and odd paths.

## Editor extension commands

The [editor extension](extension.md) reads these.

```bash
missioncache-db extension-state --dir .    # all active projects, marks the one for this folder
missioncache-db extension-project 42       # tasks, Next Steps and Waiting on for project 42
```

Both print JSON. For a readable list, use `list-active`.

## More

How each command works inside, the calendar setup and the lead role: [internals/cli.md](internals/cli.md).

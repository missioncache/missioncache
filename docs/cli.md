# CLI Reference (missioncache-db)

`missioncache-db` is the command-line entry point to the task database. Every install path (`uvx missioncache-install`, marketplace, manual) puts it on your PATH; maintainer checkouts with editable installs can equivalently run `python3 -m missioncache_db`.

Day-to-day project work does not need this CLI. The slash commands (`/missioncache:new`, `/missioncache:load`, `/missioncache:save`, ...) and the [MCP tools](./mcp-tools.md) behind them cover creating, resuming, and updating projects from inside your AI tool. A handful of operations are deliberately CLI-only, though: multi-step flows like cross-machine import, and rare maintenance jobs that would make poor MCP tools. This doc covers those. Running `missioncache-db` with no arguments prints the full built-in usage, including the everyday commands not detailed here.

## Cross-machine sharing: export, import, config

Move a project between machines (say, a work laptop and a personal desktop) as a self-contained bundle. The bundle carries the project's MissionCache markdown files plus a `missioncache.json` manifest with the project's identity and its references to things that live outside the bundle: the repo it belongs to (recorded as a git remote, which is portable, rather than a local path, which is not), Obsidian-style vault links, and absolute paths embedded in the docs.

### export

```bash
missioncache-db export <name> [--out <path>] [--no-time] [--json]
```

Builds the bundle for an active project and prints where it landed, how many files it contains, and which external references the manifest recorded. Flags:

- `--out <path>` - write the bundle somewhere other than the default location.
- `--no-time` - omit the origin machine's total tracked time. Time tracking is per-machine either way; when included, the value is display-only on the importing side and never merges into local time data.
- `--json` - print the raw manifest instead of the summary.

### import

```bash
missioncache-db import <bundle> [--repo <path>] [--force] [--rewrite-paths] [--dry-run] [--json]
```

Imports a bundle: creates the project locally, or updates it when the same project (matched by its stable origin UUID) was imported or exported here before. A name collision with an unrelated local project aborts rather than overwriting it, even with `--force`. Fields the bundle does not carry keep their local values.

After placing files, import reconciles the manifest's external references against this machine and prints a three-bucket alignment report: `resolved` (found locally), `needs mapping` (exists in the manifest but this machine does not know where it lives - each entry comes with a ready-to-run `config set-path` fix line), and `missing`. The exit code is 0 only when everything resolved, so the command is scriptable. Flags:

- `--repo <path>` - bind the project to this local repo path instead of resolving the manifest's git remote.
- `--force` - overwrite the local project files when they differ from the bundle (without it, a differing re-import aborts so local edits are never silently lost).
- `--rewrite-paths` - rewrite absolute paths embedded in the docs to this machine's equivalents, using the path map below.
- `--dry-run` - print the full report without writing anything.
- `--json` - print the report as JSON.

### config (the per-machine path map)

```bash
missioncache-db config set-path <repo|vault|anchor>:<name> <localpath>
missioncache-db config list-paths [kind] [--json]
missioncache-db config show
missioncache-db config seed [--dry-run]
```

The path map (`~/.missioncache/machine.json`) tells import how portable identifiers translate to THIS machine: which local checkout a git remote lives at (`repo:`), where a named vault is (`vault:`), and what a named path prefix expands to (`anchor:`). You rarely write it by hand - `config seed` pre-fills it from your registered repos' git remotes, and the import report's fix hints print the exact `config set-path` command for anything left.

## Keyword management (tags)

```bash
missioncache-db add-keyword <keyword>
missioncache-db remove-keyword <keyword>
missioncache-db list-keywords
missioncache-db backfill-tags
```

Projects get auto-tagged by matching the parts of their name against a keyword list (a project named `kafka-consumer-fix` picks up the `kafka` tag). `add-keyword` extends the built-in list with your own stack's vocabulary; `remove-keyword` removes custom keywords (the built-in defaults cannot be removed); `backfill-tags` re-derives tags for existing projects after the list changes - new keywords only affect newly created projects until you run it.

## Maintenance: prune and cleanup

```bash
missioncache-db prune [days]
missioncache-db cleanup [--dry-run]
```

`prune` archives completed projects older than the retention period (default 30 days, or pass a number explicitly). Archived projects drop out of the completed lists but stay in the database.

`cleanup` is the broader housekeeping pass, in four phases: archive orphaned active tasks whose files no longer exist on disk, move stray repo-local MissionCache files into the centralized `~/.missioncache/` layout, resolve duplicate task names, and normalize non-standard paths. Run it with `--dry-run` first - it prints exactly what each phase would touch.

## Health / diagnostics

```bash
missioncache-db health
```

Scans every active project's context file and reports, per project: a stale `Last Updated` (older than 14 days), stale Waiting-on rows (`Since` older than 7 days), a context file over the 100KB size budget, missing core sections (`Description`, `Gotchas`, `Waiting on`, `Next Steps`, `Recent Changes`), and a Recent Changes section over its 12-entry cap (meaning a journal rollover is pending on the next save). A project directory without a context file is itself a finding.

Report-only: exit code is always 0, warnings or not. The thresholds are constants in `missioncache_db/context_health.py`, not config keys. The same warnings surface per-project in the `/missioncache:load` digest, so `health` is mainly the fleet-wide sweep.

## Bulk repo registration

```bash
missioncache-db add-repos-glob "~/work/*"
```

Registers every directory matching a glob pattern as a tracked repo in one shot (hidden directories are skipped). Quote the pattern so your shell does not expand it first. Useful on a fresh machine instead of one `add-repo` call per project.

## Everything else

The rest of the CLI (`list-active`, `create-task`, `set-jira`, `set-category`, `complete-task`, `task-time`, and friends) overlaps what the MCP tools and slash commands already do conversationally; the CLI variants exist for scripts and quick shell checks. `missioncache-db` with no arguments prints the complete usage.

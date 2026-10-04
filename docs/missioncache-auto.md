# MissionCache Auto

A command that works through a project's tasks for you. Each task runs in its own Claude Code session. Auto checks the result, retries what failed, and stops when every task is done or something needs you.

Run it from your repo:

```bash
missioncache-auto my-project
```

It comes with the full install (`uvx missioncache-install`), not the plugin-only one.

## Before you run it

Auto reads the project files in `~/.missioncache/active/<project>/`. A good run needs three things:

1. **Small tasks.** One Claude session should be able to finish and check each task in `<project>-tasks.md`.
2. **Modes.** `/missioncache:mode` marks each task `[auto]` or `[inter]`. No mark counts as `[inter]`. If nothing is runnable because open `[inter]` tasks block the rest, Auto exits before it starts.
3. **Prompts.** `/missioncache:prompts <project>` writes one prompt file per task under `prompts/`, with its dependencies. Parallel mode needs them. Sequential mode does not.

## Common commands

```bash
missioncache-auto my-project                  # run in parallel, 8 workers
missioncache-auto my-project -w 12            # 12 workers
missioncache-auto my-project --sequential     # one task at a time, in file order
missioncache-auto my-project --dry-run        # show the plan, run nothing
missioncache-auto status my-project           # progress and what is ready
missioncache-auto init my-project "summary"   # create empty project files
```

A parallel run shows its plan and asks `Proceed? [Y/n]`. A sequential run starts at once.

## Options

Put the options after the project name, except `-v` and `--no-color`, which go before it.

| Option | Default | What it does |
|---|---|---|
| `-w`, `--workers N` | 8 | Parallel workers, from 1 to 12. |
| `-r`, `--retries N` | 3 | Tries per task before giving up. |
| `--timeout N` | 1800 | Seconds a task may run. `0` means no limit. |
| `--effort LEVEL` | your setting | Claude's effort for every task: `low`, `medium`, `high`, `xhigh` or `max`. |
| `--pause N` | 3 | Seconds between tasks. Sequential only. |
| `-s`, `--sequential` | off | Run one task at a time, top to bottom. |
| `-p`, `--parallel` | on | Run in parallel, in dependency order. |
| `--dry-run` | off | Show the plan and exit. Nothing runs and nothing is written. |
| `--fail-fast` | off | Stop all workers on the first failed task. Parallel only. |
| `--worktree` | on in a git repo | Give each worker its own git worktree and branch. Parallel only. |
| `--no-worktree` | off | Run every worker in your checkout. Parallel only. |
| `--no-commit` | off | Do not commit after each task. |
| `--spec-review-only` | off | Check each change against the task's acceptance criteria. Parallel only. |
| `--enable-review` | off | The spec check plus a code quality review. Parallel only. |
| `--tdd` | off | Ask for tests first, and fail a task that adds no tests. |
| `-v`, `--visibility` | `verbose` | Tool output: `verbose`, `minimal` or `none`. |
| `--no-color` | off | Plain output, no colours. |

To set it for every run, export `MISSIONCACHE_AUTO_VISIBILITY=minimal`.

In sequential mode, commits and `--tdd` apply only when the project has a `prompts/` folder.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Every task is done. Also when you answer no at `Proceed?`, or nothing was left to run. |
| 1 | A task ran out of retries, or the run ended with tasks unfinished. Also `status` on a missing project. |
| 2 | Blocked: the next task is `[WAIT]`, or open `[inter]` tasks leave nothing runnable. Also a mistyped command line. |
| 3 | Setup problem: missing files or prompts, a bad dependency graph, or a refused option mix (see below). |

## Watching a run

- **Dashboard.** Open the **Auto** view at `http://localhost:8787/#auto` for a graph of every task and a live log.
- **Logs.** In the project folder. A parallel run writes each task's raw output to `logs/`. A sequential run records every attempt in `<project>-auto-log.md`.

## How Auto decides a task is done

Claude's reply must carry one of these tags. A reply without one is a failure, and the task is retried.

| Tag | Result |
|---|---|
| `<what_worked>...</what_worked>` | This task is done. Auto ticks it. |
| `<promise>COMPLETE</promise>` | Every task is done. A sequential run ends with code 0. Parallel counts it as this task done. |
| `<blocker>WAITING_FOR_HUMAN</blocker>` | Claude needs you. A sequential run stops with code 2. Parallel counts it as a failed try. |

Prompts from `/missioncache:prompts` already ask for them.

## Worktrees and commits

In a git repo, each parallel worker gets its own worktree under `.claude/worktrees/` and its own branch, `missioncache-auto/<project>/worker-<id>`. Auto commits after each task and merges the branches back at the end. A branch with a merge conflict is kept for you to merge by hand. Outside a git repo, workers share the folder.

Your `.env*` files are copied into each worktree and never committed.

Auto refuses to start, with code 3, when:

- worktrees are on and you pass `--no-commit`. The work would be lost.
- worktrees are on and tracked files have uncommitted changes. Workers would not see them. Commit or stash first.
- `--no-worktree` with more than one worker and commits on. Workers would clash on commits. Use `-w 1` or `--no-commit`.

## Windows

Auto runs on native Windows. A `claude` installed with npm works. A `claude.bat` in the current folder is ignored. A task that times out is stopped with everything it started.

## When something looks wrong

**`missioncache-auto: command not found`.** You have the plugin-only install. Run `uvx missioncache-install`, or `uvx missioncache-install --missioncache-auto` for this part only.

**It printed the plan and stopped with code 0.** Nothing answered `Proceed?`, as in a script or cron job. Run `echo y | missioncache-auto my-project`.

**A task did its work but was retried.** The reply had no `<what_worked>` tag. Check its output in `logs/`, or its entry in `<project>-auto-log.md` for a sequential run. If Claude skipped the tag, ask for it in the prompt file. If the reply was cut off, look for a CLI error or a rate limit.

**`Parallel mode requires prompts/ directory`.** Run `/missioncache:prompts <project>`, or use `--sequential`.

**`Dependency '1' does not exist`.** A prompt lists a task that has no prompt file. Task IDs have two digits: `"01"`, not `"1"`.

**A long task gets killed at 30 minutes.** Pass `--timeout 3600`, or `--timeout 0` for no limit.

**`--tdd` fails a task that has tests.** It counts only tests the task added or changed. Put `tdd: false` in the prompt file to skip the check.

**Worktrees are left over after a crash.** Run `git worktree list`, then `git worktree remove <path>` for each one.

## More

How the loop, the scheduler and the tags work, and how to extend them: [internals/missioncache-auto.md](internals/missioncache-auto.md).

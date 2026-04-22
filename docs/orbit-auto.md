# Orbit Auto

This document covers `orbit-auto`, the autonomous execution CLI that runs Claude Code in a loop over an orbit project's task list until every box is checked, something blocks, or the retry budget runs out. It is the component with the least amount of magic and the most amount of subprocess orchestration - once you understand the core loop, everything else is a variation on it.

It assumes you have read [`architecture.md`](./architecture.md) for the shared vocabulary (orbit file layout, `~/.claude/orbit/active/<project>/`, `tasks.db`, heartbeats, `auto_executions`, `auto_execution_logs`). If a term in this doc is not defined here, it is defined there.

If you are just trying to *use* orbit-auto, the short version is: run `orbit-auto <project>` from inside your project's git repo, answer the confirmation prompt, and watch. The rest of this doc is for when you want to understand what it is doing, debug a run, or change how it behaves.

## The mental model

Orbit Auto's philosophy fits on one line: *iteration beats perfection on the first attempt*. Claude is good enough to complete a well-specified task in one shot most of the time, but "most of the time" is not "always", and a human sitting in front of the terminal to retry failures one at a time is a waste of a human. Orbit Auto is the retry loop, the scheduler, and the scoreboard.

The loop itself is four steps:

```
PROMPT -> WORK -> CHECK -> EXIT?  (YES=done, NO=repeat)
```

- **PROMPT** is "the next uncompleted task in `<project>-tasks.md`", or "the next task whose dependencies are satisfied" in parallel mode. The prompt is either generic (built at runtime from the tasks file) or read from a pre-generated prompt file under `prompts/task-NN-prompt.md`.
- **WORK** is spawning `claude --print --output-format stream-json` as a subprocess, piping the prompt in, and parsing the streaming JSON response line by line.
- **CHECK** is looking for learning-centric XML tags in Claude's reply: `<what_worked>` means this task succeeded, `<promise>COMPLETE</promise>` means every task is done, `<blocker>WAITING_FOR_HUMAN</blocker>` means something needs human input. No tag means the task failed and will be retried up to `--retries` times.
- **EXIT?** is either "all tasks done" (exit 0), "retry budget burned" (exit 1), "blocked on a `[WAIT]` task" (exit 2), or "configuration error before the loop started" (exit 3).

Every piece of state that needs to survive an iteration lives on disk: the task list, the context file, the per-iteration auto log, the parallel-mode state file, the execution record in SQLite. Nothing is held in memory between iterations except the Python process running the loop itself, which can die and be restarted without losing progress.

### Two execution modes

| Mode | Entry point | When to use |
|------|-------------|-------------|
| Sequential | `orbit-auto <project> --sequential` | Simple linear workflows, debugging one task at a time, or tasks that must run in strict order |
| Parallel (default) | `orbit-auto <project>` or `orbit-auto <project> -w 12` | Multi-task projects with pre-generated prompts, where you want real concurrency and dependency-aware scheduling |

The difference is not just speed. Sequential mode walks the task list top to bottom and only cares about completion status. Parallel mode needs a `prompts/` directory, parses YAML frontmatter from each prompt file to build a dependency graph, computes execution waves, and spawns a worker pool that atomically claims tasks from shared state. The rest of this doc treats them separately because they have different invariants.

## Task layout and the file contract

Every orbit-auto run operates on the same directory:

```
~/.claude/orbit/active/<project>/
├── <project>-tasks.md         # Checkbox items, parsed every iteration
├── <project>-context.md       # Durable learnings, decisions, gotchas
├── <project>-plan.md          # Implementation plan (optional, read-only)
├── <project>-auto-log.md      # Iteration history (written by orbit-auto)
├── prompts/                   # Optional, required for parallel mode
│   ├── task-01-prompt.md
│   ├── task-02-prompt.md
│   └── ...
├── .orbit-parallel-state/     # Parallel-mode state, only exists mid-run
│   ├── state.json
│   ├── state.lock
│   └── adjacency.txt
└── logs/                      # Per-task stdout/stderr logs (optional)
    └── worker-<wid>-task-<tid>-<timestamp>.log
```

The location is hard-coded in `orbit_auto/models.py:TaskPaths.from_task_name()` and is not configurable. This is deliberate: every other orbit component (dashboard, hooks, MCP tools, the `/orbit:go` slash command) expects to find the files here, and having one canonical location means no path plumbing.

### Task file format

Tasks are parsed from `<project>-tasks.md` with a strict regex:

```
- [ ] 1. Task title
- [x] 2. Completed task
- [ ] [WAIT] 3. Task that needs human review
- [ ] 4. Another task `[auto]`
- [ ] 5. Review the PR `[inter]`
- [ ] 6. Depends on #4 `[auto:depends=4]`
```

The parser (`orbit_auto/task_parser.py:parse_tasks_md`) matches `^\s*- \[([ x])\] (\[WAIT\])? (\d+)[.:] (.+)$`. A task number can be flat (`1`, `2`, `10`) or hierarchical (`1.1`, `1.2`) - hierarchical tasks get resolved as subtasks of their parent when computing sequential dependencies. The trailing mode marker in backticks (`[auto]`, `[inter]`, `[auto:depends=1,3]`) is optional and only used by `/orbit:mode` and the runnable-task calculator.

A task is "completed" when its checkbox is `[x]`. Orbit-auto only writes checkboxes by calling `state.sync_to_tasks_md()` in parallel mode or `mark_task_completed()` in sequential mode, both of which use atomic file writes (temp file + `os.replace`) to avoid corruption under concurrent access. **You can edit the file while orbit-auto is running** - the loop re-parses it every iteration, so human edits are picked up within one cycle.

### Context file and auto log

`<project>-context.md` is never written by orbit-auto during a run. It is read by Claude (as part of the prompt, either embedded or referenced by path) and it is the one file you should edit manually between runs to record architectural decisions or hard-won lessons. It survives compaction and is what `/orbit:go` reads when you come back to a project.

`<project>-auto-log.md` is the opposite: orbit-auto writes to it every iteration, and you can delete it after completion without losing anything the context file should have preserved. Its role is detailed debugging history - which attempt on which task succeeded or failed, what files were modified, what learnings Claude extracted. The sequential runner writes entries with `_write_iteration_log()`, and patterns and gotchas discovered via `<pattern_discovered>` and `<gotcha>` tags get bubbled up into a "Codebase Knowledge" section at the top of the file so they are visible to future iterations without having to scroll through the history.

The three-file split - tasks, context, auto log - is the invariant that makes `/orbit:go` and orbit-auto compose. You can run `orbit-auto` for an hour, delete the auto log, and the next `/orbit:go` still has everything it needs.

## Sequential mode in detail

Sequential mode is the original orbit-auto execution path. It is what you want when there are no prompt files, when tasks must run in strict order, or when you are debugging a single task failure.

### The loop

`orbit_auto/sequential.py:SequentialRunner.run()` is the top of the loop. Each iteration:

1. **Read progress.** Call `get_task_progress()` to count completed vs total tasks in `<project>-tasks.md`.
2. **Pick the next task.** Call `get_first_uncompleted_task()`, which returns the first `- [ ]` item in file order. If it returns `None`, every task is complete and the runner exits via `_handle_completion()`.
3. **Check for `[WAIT]`.** If the next task is marked `[WAIT]`, print the blocked summary, log the block, and exit with code 2. Resuming requires a human to either complete the task by hand and remove the marker, or decide the task is unblocked and re-run orbit-auto.
4. **Build the prompt.** If a `prompts/task-NN-prompt.md` file exists for this task number, use `extract_prompt_content()` to strip the YAML frontmatter and pass the body through. Otherwise fall back to `build_generic_prompt()`, which assembles a prompt from the task number, title, and absolute paths to the tasks and context files.
5. **Run Claude.** Create a `ClaudeRunner(visibility=config.visibility, on_tool_use=display.tool_use)` and call `.run(prompt, project_root)`. This spawns `claude --print --output-format stream-json --verbose --exclude-dynamic-system-prompt-sections`, sets `ORBIT_AUTO_MODE=1` in the child environment (so hooks know to skip), pipes the prompt into stdin, and parses the streaming JSON output. The `stream-json` format gives one JSON object per line, so tool use events and text deltas can be processed as they arrive.
6. **Parse the response for tags.** `_build_result()` in `claude_runner.py` pulls the accumulated text out of the stream and searches for `<learnings>`, `<what_worked>`, `<what_failed>`, `<dont_retry>`, `<try_next>`, `<pattern_discovered>`, `<gotcha>`, `<run_summary>`, `<promise>COMPLETE</promise>`, and `<blocker>WAITING_FOR_HUMAN</blocker>`. Success is defined as: `is_complete=True` OR (`what_worked is not None` AND not blocked). This is the central completion invariant - without an explicit positive signal, the task is a failure.
7. **Handle the result.** `_handle_result()` is the big decision tree. On success, reset the retry counter, update timestamps, run `process-heartbeats` for time tracking, mark the checkbox, auto-commit changes if enabled, and loop. On failure, increment the per-task retry counter and loop without advancing. On blocked, exit with code 2. On completion (`<promise>COMPLETE</promise>`), call `_handle_completion()` and exit with code 0.
8. **Pause.** `time.sleep(config.pause_seconds)` before the next iteration. Default is 3 seconds, configurable via `--pause`.

The loop runs until `max_retries` is exhausted on a single task (exit 1), a `[WAIT]` marker is hit (exit 2), or every task is `[x]`-ed (exit 0).

### Why no iteration cap

Sequential mode does not have a global iteration limit. The retry cap is **per task**: if task 3 fails `max_retries` times, the loop exits. But if every task succeeds on attempt 2 out of 3, the loop will run `2 × num_tasks` iterations and exit happily. This is intentional - a project with 30 tasks and a 3-retry budget per task can legitimately need 90 iterations, and a single global cap would either be too small or too large to be useful.

The things that actually make the loop stop are:

- **Per-task retry exhaustion.** `current_task_attempts >= max_retries` on the current task.
- **Explicit completion signal.** `<promise>COMPLETE</promise>` from Claude, which takes precedence over uncompleted checkboxes - if Claude says it is done and the human has written their tasks file optimistically, orbit-auto will exit 0.
- **`[WAIT]` marker.** Task parser returns `is_wait=True`, runner exits 2.
- **KeyboardInterrupt.** Falls through to the `finally` block and shuts down gracefully.

### Retry context

When a task fails and the loop comes back around, orbit-auto does not just retry blindly. Sequential mode passes the previous error message back to Claude via `_build_retry_prompt()`, which prepends a `<retry-context>` block explaining what went wrong last time and listing common fixes (missing tag, CLI error, specific error). This is the "learning from failure" part of the philosophy - Claude sees its own previous output summarized and can correct course. The parallel worker uses the same mechanism (`worker.py:_build_retry_prompt`) with the same format.

## Parallel mode in detail

Parallel mode is what you want when you have a project with a `prompts/` directory and independent tasks that can run concurrently. It is the default (`orbit-auto <project>` is the same as `orbit-auto <project> --parallel`), and it is what ships in the screenshots and dashboard demos.

### Setup requirements

Parallel mode requires:

1. **A prompts directory.** `~/.claude/orbit/active/<project>/prompts/` must exist and must contain `task-NN-prompt.md` files. If it does not, `ParallelRunner.validate()` returns an error and the CLI exits with code 3.
2. **YAML frontmatter on every prompt.** Each prompt file must start with a `---` delimited block containing at minimum `task_id: "NN"`. `task_title` is strongly recommended (used for display and commits). `dependencies: ["01", "03"]` is optional but required for anything other than strict sequential order.
3. **Task numbers that match.** The prompt file's `task_id` must correspond to an uncompleted line in `<project>-tasks.md`. The plan validator (`plan_validator.py:validate_plan`) cross-references the two and warns about mismatches.

You get all of this for free if you run `/orbit:prompts <project>` after `/orbit:new` - that command generates the prompt files with proper frontmatter, lists the relevant agents and skills, and shows everything in a batch-approval flow.

### The DAG build

`dag.py:DAG.build_from_prompts()` walks `prompts/task-*-prompt.md` in sorted order and extracts three things from each file: `task_id`, `task_title`, and `dependencies`. The dependency extraction is a small state machine:

- If the prompt has an explicit `dependencies: [...]` field, use it.
- If the field is missing, compute an implicit dependency: task `NN` depends on task `NN-1` (padded to two digits). Task `01` has no dependencies.

The result is an adjacency list mapping task IDs to their prerequisites. This is stored as a simple dict and serialized to `adjacency.txt` in the state directory so worker subprocesses can reload it without re-parsing YAML.

Before the DAG is used, it runs through `detect_cycles()` (DFS with a recursion stack) and `validate_plan()` (checks for missing frontmatter, dependencies pointing at non-existent tasks, orphan prompts, orphan task lines, missing `<acceptance_criteria>` sections). Cycles are fatal. Validation errors are fatal; warnings are shown but do not block execution.

### Waves and the execution plan

Once the DAG is valid, `get_waves()` computes "waves" - groups of tasks that can execute concurrently. A task's wave is `max(wave of its deps) + 1`, with no-dep tasks in wave 1. This gives you the natural parallel structure of the project: wave 1 is everything that can start immediately, wave 2 is everything that only waits on wave 1, and so on. The display step (`display.execution_plan()`) prints this before asking the user to confirm, so you can see exactly what is going to run and in what order.

The critical path (`get_critical_path()`) is the longest chain of dependent tasks. It is the theoretical minimum number of sequential steps needed - if the critical path is 4 tasks long, no amount of parallelism can finish the project in fewer than 4 Claude invocations. The CLI uses this for the execution summary.

Waves are a display and planning concept. The workers themselves do not care about waves - they just check dependencies on every claim, which gives you strictly better scheduling than wave-locked execution would (a worker that finishes task 2 of wave 1 early can start a wave 2 task immediately without waiting for the rest of wave 1).

### The worker pool

After confirmation, `ParallelRunner._run_workers()` spawns `config.max_workers` worker processes via `multiprocessing.Process`. The default is 8, the max is 12 (capped in `Config.__post_init__`), and each worker gets the same constructor arguments - worker ID, project root, state and prompts dirs, adjacency file path, retry config, visibility settings. They all share the state file via file locking.

Each worker runs `Worker.run()`, which is a simple claim loop:

```python
while True:
    task_id = state_manager.claim_task(worker_id, dag)
    if task_id is None:
        if should_wait():
            sleep(0.5)
            continue
        else:
            break
    success, error = _execute_task(task_id, previous_error)
    if success:
        if enable_review: run_review()
        if auto_commit: git_commit_task()
        state_manager.complete_task(task_id)
    else:
        result = state_manager.release_task(task_id, max_retries, error)
        # result is "released" (retry later) or "max_retries_reached" (failed)
```

`claim_task` is the atomic primitive. It acquires an exclusive `fcntl.flock` on `.orbit-parallel-state/state.lock`, reads the current state, finds the first pending task whose dependencies are satisfied, flips it to `in_progress`, writes the state atomically (temp file + `os.replace` via `_atomic_write_text`), and releases the lock. Multiple workers hitting this concurrently will serialize on the lock and only one will get any given task.

Tasks that a worker claims but cannot finish (because the worker process died, or because `release_task` hit the retry cap) go through `release_orphaned_tasks()`, which runs from the parent runner's monitoring loop every 500ms. It scans for `in_progress` tasks owned by dead worker IDs and either flips them back to pending (if attempts left) or marks them failed. This is what makes orbit-auto robust to worker crashes - the orchestrator notices, recovers the claim, and lets another worker pick it up.

### State file structure

`.orbit-parallel-state/state.json` is the shared memory for parallel mode. It looks like this:

```json
{
  "status": "running",
  "started": "2026-04-14T02:30:17.284912+00:00",
  "tasks": {
    "01": {"status": "completed", "worker": null, "attempts": 1, "error_message": null},
    "02": {"status": "in_progress", "worker": 3, "attempts": 1, "error_message": null},
    "03": {"status": "pending", "worker": null, "attempts": 0, "error_message": null},
    "04": {"status": "failed", "worker": null, "attempts": 3, "error_message": "Missing <what_worked> tag in response"}
  },
  "workers": {}
}
```

Every transition writes the whole file atomically. There is no journal and no delta format - the file is small enough (a few KB for a 30-task project) that full rewrites are cheap, and the simpler format makes debugging trivial. Just `cat state.json` to see what is happening.

State also survives interruptions. If you `Ctrl-C` a parallel run and restart, `_get_pre_completed_tasks()` reads the tasks.md file and initializes state with those checkboxes already marked completed, and any in-progress tasks from the previous run's `state.json` get picked up via `release_orphaned_tasks()` on the next worker's claim cycle. You will sometimes see a task with `attempts > 0` from an interrupted run; that is correct and will be retried up to `max_retries`.

### Monitoring and progress display

The main `ParallelRunner` process does not execute tasks itself. It sits in a monitoring loop:

```python
while not state_manager.is_complete():
    state = state_manager.read()
    completed, in_progress, failed = _classify(state)
    display.parallel_progress(completed, total, in_progress, failed)
    logger.update_progress(completed, failed)
    if config.fail_fast and failed:
        terminate_all_workers()
        break
    release_orphaned_tasks(dead_workers, max_retries)
    if no_workers_alive and not complete:
        break
    time.sleep(0.5)
```

The 500ms sleep is the progress-bar refresh rate. It is also how often the dashboard sees updated progress - `logger.update_progress()` writes `completed_subtasks` and `failed_subtasks` to the `auto_executions` SQLite row, and the dashboard's `/api/auto/*` endpoints read from there.

`fail-fast` (`--fail-fast`) terminates every worker on the first failure. This is useful when you want to catch a problem early, but the default is to let every worker run to completion so you can see the full failure picture.

## Prompts and the YAML contract

Prompt files are the bridge between `/orbit:prompts` and orbit-auto. They are what makes parallel mode possible, and they are also a reproducible way to re-run a single task without the whole loop.

### Structure

```markdown
---
task_id: "03"
task_title: "Wire up the /api/users endpoint"
dependencies: ["01", "02"]
agents:
  - python-pro
  - code-reviewer
skills:
  - pytest-patterns
tdd: true
---

# Task 03: Wire up the /api/users endpoint

<context>
The `/api/users` endpoint is currently returning a 501. You need to connect it
to the `User.list_all()` ORM method and return results as JSON.
</context>

<instructions>
1. Read src/routes/users.py to understand the existing route stub.
2. Read src/models/user.py for the ORM.
3. Implement the handler with pagination support.
4. Add tests in tests/test_users.py covering empty, single, and multiple users.
</instructions>

<constraints>
- Preserve existing route registration order.
- Default page size is 50, max is 500.
</constraints>

<agents>
## Available Agents
Use the Task tool with the specified subagent_type:
| Agent | Invoke With | Use For |
|-------|-------------|---------|
| python-pro | subagent_type="python-pro" | Type hints, async, pytest |
| code-reviewer | subagent_type="code-reviewer" | Pre-completion review |
</agents>

<validation>
Run `pytest tests/test_users.py -v` and confirm all tests pass.
</validation>

<acceptance_criteria>
- GET /api/users returns 200 with JSON list
- Pagination via ?page=N&per_page=M works
- Tests added and pass
- Typecheck passes
</acceptance_criteria>
```

Only `task_id` is strictly required by the parser (`task_parser.py:parse_prompt_yaml`). Everything else is advisory:

- `task_title` is used in auto-commit messages and display output.
- `dependencies` drives the DAG; without it, the task gets an implicit dep on `task_id - 1`.
- `agents` and `skills` are metadata for `/orbit:prompts`; they are not read by orbit-auto at runtime.
- `tdd` is a per-task override for the TDD enforcement flag - `true` forces TDD wrapping on, `false` forces it off, absent means use the global `--tdd` setting.

### How prompts are picked up

In parallel mode, the worker extracts `prompts/task-NN-prompt.md` for the task it claimed, strips the YAML frontmatter, and pipes the body into Claude. In sequential mode, the runner checks for a prompt file and uses it if one exists, falling back to `build_generic_prompt()` otherwise. The fallback is a minimal prompt template that references the task file and context file by path and includes the mandatory learning-tag instructions - it works fine for projects that do not bother with pre-generated prompts, at the cost of less structured guidance for Claude.

Progress is tracked entirely through checkboxes in `<project>-tasks.md`. Prompts do not have their own "completed" state. When `tasks.md` has `- [x] 3.` and the corresponding `task-03-prompt.md` still exists on disk, orbit-auto skips the prompt - the checkbox is the source of truth.

## Learning tags: the completion contract

The learning-tag system is the most subtle thing about orbit-auto, and getting it wrong produces the single most common failure mode: a task that looks like it succeeded (Claude edited files, ran tests, wrote a correct implementation) but gets retried anyway because the required tag was missing.

### The rule

A task is complete if and only if Claude's response contains at least one of:

1. `<promise>COMPLETE</promise>` - signals that **all tasks in the project** are done. The loop stops entirely.
2. `<what_worked>...</what_worked>` - signals that **this specific task** succeeded. The loop marks the task completed and moves on.

The detection happens in `claude_runner.py:_build_result()`:

```python
success = is_complete or (what_worked is not None and not is_blocked)
```

Everything else - `<learnings>`, `<what_failed>`, `<dont_retry>`, `<try_next>`, `<pattern_discovered>`, `<gotcha>` - is purely informational. It is written to the auto log, surfaced in the dashboard, and may be used to enrich retry prompts, but it does not affect the pass/fail decision.

### Why an explicit tag

An earlier version of orbit-auto inferred success from the absence of errors, and it produced a lot of false positives: Claude would crash, the CLI would rate-limit, the prompt would fail to parse, and the loop would count the empty response as "nothing went wrong, must have worked". Moving to an explicit positive signal is a forcing function. Claude has to commit to "this is done" in a way that shows up in the output, and orbit-auto simply counts tags.

The failure mode you will actually hit is not "Claude forgot the tag" (the prompt template is explicit about it), but "Claude crashed mid-response" or "the CLI errored before Claude saw the prompt". Both cases produce no `<what_worked>` and both are correctly counted as failures, which is exactly what you want.

### The full tag set

| Tag | Required? | Effect |
|-----|-----------|--------|
| `<learnings>` | Always | Written to auto log, shown in console output |
| `<what_worked>` | On success | **Marks task complete**; written to auto log |
| `<what_failed>` | On failure | Written to auto log; fed into next retry's error context |
| `<dont_retry>` | On failure | Written to auto log; suggests approaches to avoid |
| `<try_next>` | On failure | Written to auto log; prioritized list of next attempts |
| `<pattern_discovered>` | Optional | Bubbled into Codebase Knowledge section at top of auto log |
| `<gotcha>` | Optional | Bubbled into Codebase Knowledge section at top of auto log |
| `<run_summary>` | On final task | Written to auto log completion entry |
| `<promise>COMPLETE</promise>` | On final task | **Exits the loop with code 0** |
| `<blocker>WAITING_FOR_HUMAN</blocker>` | On blocked task | **Exits the loop with code 2** |

Patterns and gotchas deserve special mention because they are the only tags that compound across iterations. When Claude emits `<pattern_discovered>Temp file cleanup: always use trap to clean temp files on EXIT</pattern_discovered>`, orbit-auto inserts it into the "Codebase Knowledge > Patterns Discovered" section of the auto log, which is at the top of the file and therefore visible to every subsequent iteration. Over a long run, the auto log accumulates a small local knowledge base about the codebase, and later tasks can benefit from earlier lessons without the human ever touching it.

## Execution logging and the dashboard feedback loop

Everything orbit-auto does is logged to `tasks.db` so the dashboard can show you what happened. This integration has two halves: the execution record (one row in `auto_executions` per `orbit-auto` invocation) and the streaming logs (many rows in `auto_execution_logs` per execution).

### The execution record

`db_logger.py:ExecutionLogger.start()` creates the row when the runner starts. It looks up the task by name in the `tasks` table, calls `create_auto_execution(task_id, mode, worker_count, total_subtasks)`, and stores the returned execution ID on the logger instance. From that point on, every log line and every progress update carries this ID.

The record tracks:

- `task_id` - foreign key into `tasks`
- `mode` - `sequential` or `parallel`
- `worker_count` - how many workers were configured (null for sequential)
- `total_subtasks` / `completed_subtasks` / `failed_subtasks` - updated live during the run
- `started_at` / `completed_at` - timestamps
- `status` - `running`, `completed`, `failed`, `cancelled`
- `error_message` - populated on failure

`update_progress()` is called from the parallel runner's monitoring loop every 500ms and from the sequential runner's `_handle_result()` after every task completion. `finish()` is called at the end, setting the final status and writing the completion timestamp. If the runner dies without calling `finish()`, the row stays in `running` state - the dashboard handles this gracefully (shows "running" indefinitely until a new row for the same task_id replaces it) but you can also clean up by starting a new run, since the retention policy kicks in on every `start()`.

### Retention

`ExecutionLogger._cleanup_old_executions()` runs automatically every time a new execution starts. It keeps the last 10 executions per task and deletes anything older than 30 days. Log entries tied to deleted executions go with them. This keeps the DB from growing unbounded on projects that get rerun frequently without requiring any manual maintenance.

The constants are class attributes (`KEEP_EXECUTIONS_PER_TASK = 10`, `DELETE_OLDER_THAN_DAYS = 30`) and changing them requires editing `db_logger.py`. They are not exposed as CLI flags because nobody has asked for different values yet.

### Streaming logs

`auto_execution_logs` is the rolling stream of log lines. Every worker and the orchestrator emit rows here via `add_auto_execution_log(execution_id, message, level, worker_id, subtask_id)`. The dashboard streams these to the frontend over SSE (`GET /api/auto/output/{execution_id}/stream`), which is what gives the Auto tab its live-log feel.

Log levels are `debug`, `info`, `warn`, `error`, `success`. There is no central enforcement - workers choose their own level per message. The conventions in the codebase are:

- `info` for "worker claimed task" and "worker started"
- `success` for "task completed" with duration
- `warn` for "retrying task" and "merge conflict in worktree"
- `error` for "task failed permanently" and "validation error"
- `debug` for "cleaned up N old executions"

The dashboard's log viewer has a per-level filter, so sprinkling `debug` liberally for internal state is fine - users can hide it.

### Workers that cannot log

`_WorkerDBLogger` in `worker.py` is a lightweight wrapper around `TaskDB` that each worker process instantiates on startup. If `orbit_db` cannot be imported (which happens in some install configurations) or `~/.claude/tasks.db` does not exist, the logger silently becomes a no-op. Every `log()` call is wrapped in try/except with a bare `pass`, so logging failures never crash a worker. This is deliberate: the dashboard is a nice-to-have, not a dependency, and an orbit-auto run must succeed in minimal-install environments too.

## Advanced features

Parallel and sequential are the two core modes, but orbit-auto has a handful of opt-in features that layer on top. They are all off by default.

### Worktree isolation (`--worktree`)

By default, every worker executes Claude in the same directory - the project root from which you ran `orbit-auto`. This is fine for tasks that touch non-overlapping files, but it breaks down when two workers edit the same file concurrently: whichever worker writes last wins, and you get lost changes.

`--worktree` fixes this by creating one git worktree per worker under `.claude/worktrees/orbit-auto-<project>-w<worker_id>`. Each worker runs in its own worktree with its own branch (`orbit-auto/<project>/worker-<id>`), does its work in isolation, and when the run finishes, `WorktreeManager.merge_all()` merges every worker branch back into the original branch sequentially, in worker ID order.

Merge conflicts are reported but not auto-resolved. If a conflict happens, the worktree branch is preserved (not deleted) so you can resolve it manually with `git merge orbit-auto/<project>/worker-3` and inspect the conflict in-repo. Successful merges clean up both the worktree and the branch.

The worktree manager also copies `.env*` files from the project root into each worktree on creation (`_copy_env_files`), because git worktrees do not inherit gitignored files and environment variables often live in `.env.local`. If your project has other gitignored config that workers need, they will not be there automatically - you would need to add them to the copy list in `worktree.py`.

Worktrees are cleaned up via `cleanup_with_results()`, which respects conflict branches. A conflicted worktree is left on disk (with its branch) so you can resolve it manually; cleaned worktrees have their branch deleted with `git branch -d` (safe delete) and a warning is printed if the branch has unmerged commits.

### Auto-commit

By default, orbit-auto commits after each successful task with a message like `feat(03): Wire up the /api/users endpoint`. The title comes from the prompt's `task_title` frontmatter or, if missing, from the first markdown heading in the prompt body. `git add -A` is used, so every change in the working tree gets committed - this is fine inside a worktree, but without `--worktree` it means two workers may fight over the same commit. **If you are not using worktrees, consider `--no-commit` for parallel runs** and commit manually at the end.

Sequential mode also auto-commits, but with less fighting - there is only one worker. The commit happens in `_handle_result()` after `update_timestamps()` and `_process_heartbeats()`.

### Spec, quality, and TDD review (`--enable-review`, `--spec-review-only`, `--tdd`)

These flags run an additional Claude invocation after each successful task to review the work:

- `--spec-review-only` runs only the spec compliance check (`code_reviewer.py:run_spec_review`). Cheap, roughly $0.15 per task. It diffs `HEAD~1` against the original prompt and checks whether the implementation matches the acceptance criteria.
- `--enable-review` runs both spec compliance **and** code quality (`run_quality_review`). More thorough but more expensive.
- `--tdd` wraps the task prompt with RED-GREEN-REFACTOR instructions before execution, then runs a **blocking** TDD review after (`run_tdd_review`). If no test files were added or modified, the task is treated as failed and retried.

Spec and quality reviews are advisory - their results are logged to the auto log and the dashboard but do not block task completion. TDD review is blocking: a TDD failure counts as a task failure and consumes a retry.

All three reviews use `ClaudeRunner(visibility=Visibility.NONE)` and write their output to `logs/review-<stage>-task-<id>-<timestamp>.log` if `logs_dir` is set. The review prompts are in `code_reviewer.py` and are short enough to read in one sitting if you need to understand what they are asking.

Per-task TDD can be overridden with `tdd: true` or `tdd: false` in the prompt's YAML frontmatter (`worker.py:_check_tdd_override`). This lets you enable TDD globally for the run but skip it on tasks where it does not make sense (docs, config, non-code changes).

### Task timeout (`--timeout`)

Each task has a 30-minute default timeout (`task_timeout=1800`), applied to the Claude CLI subprocess via `process.communicate(input=prompt, timeout=timeout)`. On timeout, the process is killed, pipes are drained, and the result is marked as a CLI error (`"Task timed out after 1800s"`). Set `--timeout 0` to disable entirely.

The timeout is per task invocation, not per orbit-auto run. A project with 30 tasks and a 30-minute timeout per task can legitimately run for 15 hours if every task runs to the max. In practice most tasks finish in 1-3 minutes, but the default is high to avoid killing long-running refactors in the middle.

### Dry run (`--dry-run`)

`--dry-run` runs the validation and planning phases (DAG build, cycle detection, plan validation, execution plan display) and exits without spawning any workers. It is what you want when you are not sure whether the dependency graph is correct or whether all the prompts are in place. The execution plan output shows waves, the critical path, and any validation warnings, so you can fix problems before the run starts.

## The CLI, end to end

### Commands

```bash
orbit-auto <project>                       # run (parallel, default)
orbit-auto <project> --sequential          # run (sequential)
orbit-auto <project> --dry-run             # show plan, do not run
orbit-auto init <project> "description"    # create task files from templates
orbit-auto status <project>                # show progress and blocking status
```

`orbit-auto run` is the implicit default when the first positional argument is not a known command. `orbit-auto my-project` and `orbit-auto run my-project` are equivalent, and `orbit-auto status my-project` and `orbit-auto init my-project "..."` use their named subcommands.

`status` is the one command that does not invoke Claude at all. It reads `<project>-tasks.md` via `parse_tasks_md()`, runs `get_runnable_tasks()` to compute blocking status, and prints a per-task list with `[ready]`, `[waiting on #3]`, `[blocked by #2]`, or `[WAIT]` annotations. It is the fastest way to see "what is actually runnable right now" without starting the worker pool.

### Options reference

| Option | Default | Used by | Description |
|--------|---------|---------|-------------|
| `-w, --workers N` | 8 | parallel | Number of parallel workers (capped at 12) |
| `-r, --retries N` | 3 | both | Max retries per task before failure |
| `--pause N` | 3 | sequential | Seconds to sleep between iterations |
| `--timeout N` | 1800 | both | Per-task Claude subprocess timeout (0 = none) |
| `--sequential, -s` | off | - | Force sequential mode |
| `--parallel, -p` | on | - | Force parallel mode (default) |
| `--fail-fast` | off | parallel | Terminate all workers on first failure |
| `--dry-run` | off | both | Plan only, do not execute |
| `--worktree` | off | parallel | Per-worker git worktree isolation |
| `--no-commit` | off | both | Disable auto-commit after tasks |
| `--enable-review` | off | both | Run spec + quality review after each task |
| `--spec-review-only` | off | both | Run only spec compliance review |
| `--tdd` | off | both | Enforce RED-GREEN-REFACTOR + blocking TDD review |
| `-v, --visibility` | verbose | both | Tool output detail: verbose, minimal, none |
| `--no-color` | off | both | Disable ANSI colors in output |

### Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `ORBIT_AUTO_VISIBILITY` | `verbose` | Sets `-v` without a flag; useful in `.envrc` |
| `ORBIT_AUTO_MODE` | set to `1` by orbit-auto | Set in the child Claude CLI environment so hooks and skills can detect autonomous mode and skip interactive prompts |

`ORBIT_AUTO_MODE=1` is the signal that every hook in the orbit plugin looks for. When it is set, `permission-whitelist.sh` auto-approves certain plan-exit transitions, `activity_tracker.py` tags heartbeats as autonomous, and various skills skip clarification steps. **If you are running orbit-auto on a machine where hooks check this variable, changing its value manually will produce surprising results** - leave it to the CLI.

### Exit codes

| Code | Meaning | When |
|------|---------|------|
| 0 | All tasks completed successfully | `<promise>COMPLETE</promise>` or every checkbox is `[x]` |
| 1 | One or more tasks failed | Retry budget exhausted on at least one task |
| 2 | Blocked on `[WAIT]` task | Human input required to proceed |
| 3 | Configuration / setup error | Missing files, invalid YAML, DAG validation failure |

These are documented in the CLI `--help` epilog too, so you do not need to open this doc when writing a wrapper script.

## Extending orbit-auto

### Adding a new execution mode

If you want a mode that does not fit sequential or parallel - say, a "one-task-at-a-time-with-human-approval" mode or a "replay" mode that re-executes a completed task without checkpointing - create a new file alongside `sequential.py` and `parallel.py` in `orbit_auto/`. Implement a runner class that takes the same constructor arguments (`task_name`, `project_root`, `config`, `display`), exposes a `run()` method that returns an exit code, and uses the shared components: `TaskPaths` for file resolution, `ClaudeRunner` for subprocess invocation, `StateManager` if you need concurrent state, `ExecutionLogger` for dashboard integration.

Wire the new mode into `cli.py:cmd_run()` by adding a flag to `_add_run_arguments()` and a branch in `cmd_run`. Make sure to validate flag conflicts in the mutex group if your mode is mutually exclusive with the existing ones.

Do not fork `worker.py`. The completion-detection logic (tag parsing, retry budget, failure classification) is subtle and shared across modes intentionally. If you need to change how "success" is determined, change it in one place - `ClaudeRunner._build_result()` - and the change propagates everywhere.

### Adding a new learning tag

Learning tags are extracted in `claude_runner.py:_build_result()` by calling `_extract_tag(text, name)` for each known tag. Adding a new one is three steps:

1. Add a field to `ExecutionResult` in `models.py`.
2. Call `_extract_tag(text, "your_tag")` in `_build_result()` and assign to the field.
3. Decide what to do with it in the runners. `SequentialRunner._write_iteration_log()` writes learning tags to the auto log; update it if the new tag should be visible there. If the tag should affect the pass/fail decision (rare - most tags are advisory), update the `success = ...` line at the top of `_build_result()`.

Do not add a new success-condition tag without really thinking about it. The current two (`<what_worked>` and `<promise>COMPLETE</promise>`) are the result of iterating through several false-positive failure modes, and adding a third is the kind of change that can regress completion detection in non-obvious ways.

### Adding a new review stage

Reviews live in `code_reviewer.py` as module-level functions: `run_tdd_review`, `run_spec_review`, `run_quality_review`. Each one builds a review prompt, spawns Claude via `ClaudeRunner(visibility=Visibility.NONE)`, parses the response for `<passed>true</passed>` and `<summary>...</summary>` tags, and returns `(passed: bool, summary: str)`.

To add a new review:

1. Write a new `run_<stage>_review()` function following the existing signature.
2. Add a corresponding CLI flag in `cli.py:_add_run_arguments()` and a field in `Config`.
3. Call it from `worker.py:_run_review()` (parallel) or `sequential.py:_handle_result()` (sequential). Decide if it is blocking (TDD) or advisory (spec, quality).
4. If you want the review visible in the dashboard, log it through `self._log()` with appropriate level.

Review prompts must include `<what_worked>...</what_worked>` at the end, otherwise the Claude subprocess will appear to fail and retry forever. This is the same completion-detection invariant as regular tasks, and the review runners use the same parser.

### Adding a new display output

`display.py` is the ANSI output layer. Every user-facing string in orbit-auto goes through it: iteration headers, tool visibility, progress bars, completion summaries. It uses no third-party dependencies - just ANSI escape codes wrapped in simple print functions.

If you want to add a new display method, add it to the `Display` class and call it from wherever in the runners you want. If you are adding structured output for a new CLI feature, prefer a new method over inlining `print()` statements - it keeps the runners clean and gives you a single place to add things like `--no-color` handling or alternate output formats (e.g., a hypothetical JSON mode).

## Troubleshooting

### "Task completed successfully but orbit-auto retried it"

**Cause:** Claude's response did not contain `<what_worked>...</what_worked>`. This is the #1 issue and it happens because a prompt got out of sync with the template, or Claude was distracted by something mid-response and forgot to close the tag.

**Fix:** Look at the auto log entry for that task - it will show the raw Claude output. If `<what_worked>` is missing, either the prompt's instructions are not clear enough (edit `prompts/task-NN-prompt.md` to re-emphasize) or Claude's response was truncated (check for CLI errors or rate limits in the log). The generic prompt template (`build_generic_prompt`) always includes the instructions, so the issue is almost always a pre-generated prompt that drifted from the template.

### "Parallel mode says 'Missing prompt' for a task in tasks.md"

**Cause:** A line in `<project>-tasks.md` does not have a matching `prompts/task-NN-prompt.md` file. `plan_validator.py:validate_plan()` catches this during the pre-run check.

**Fix:** Either create the missing prompt file (copy an existing one as a template, or re-run `/orbit:prompts <project>` to regenerate), or remove the unwanted task from the tasks file. Parallel mode will refuse to run until every uncompleted task in `tasks.md` has a prompt.

### "Parallel mode says 'Dependency 01 does not exist'"

**Cause:** A prompt's YAML frontmatter lists a dependency on a task ID that has no prompt file. Often this is a typo (`"1"` instead of `"01"`) or a leftover from a deleted task.

**Fix:** Edit the offending prompt file's `dependencies:` field. Task IDs must be padded to two digits (`01`, `02`, ..., `10`, `11`). The validation error message tells you which task and which bad dependency.

### "Worker process died and its task is stuck in_progress"

**Cause:** A worker crashed before releasing its task. The parent process should catch this via `release_orphaned_tasks()` in the monitoring loop, but if the parent itself died, no cleanup happened.

**Fix:** Start a fresh `orbit-auto` run. The new run's `init()` re-initializes the state file with tasks from tasks.md, and any old `in_progress` rows get discarded. If the old run left behind worktrees (`--worktree` was used), you may need to `git worktree list` and `git worktree remove` them manually - `WorktreeManager.create_worktrees()` does clean up stale worktrees for the same worker IDs, but only if you run with the same `--workers N` count.

### "Dashboard shows 'running' forever for an execution that finished"

**Cause:** The orbit-auto process was killed (SIGKILL, OS crash) before `ExecutionLogger.finish()` ran. The `auto_executions` row still has `status=running`.

**Fix:** Start a new run. The next `ExecutionLogger.start()` on the same task does not touch the old row, but `_cleanup_old_executions()` will eventually delete it via the retention policy. If you want to clean it up immediately, `UPDATE auto_executions SET status='cancelled' WHERE id=<id>` via `sqlite3 ~/.claude/tasks.db` is fine - the dashboard will refresh on its next poll.

### "Task times out at 1800s but I want it to keep going"

**Cause:** The 30-minute per-task timeout is the default, and long-running tasks (large refactors, big test suites) can legitimately need more.

**Fix:** Pass `--timeout 3600` for a 1-hour budget, or `--timeout 0` to disable the per-task timeout entirely. Keep in mind that `--timeout 0` also disables the safety valve - a genuinely stuck Claude subprocess will hang forever without manual intervention.

### "TDD review fails even though tests exist"

**Cause:** `run_tdd_review` runs `git diff HEAD~1 --name-only` to detect test changes, so tests that were committed before the task started do not count. It wants to see test files added or modified *as part of the task*.

**Fix:** Either explicitly modify or add tests in the task (which is the point of TDD), or set `tdd: false` in the task's prompt frontmatter to opt out.

### "orbit-auto command not found"

**Cause:** You are on the quick (marketplace) install path, which does not include the `orbit-auto` CLI - it ships only the plugin core.

**Fix:** Do the full install: `uvx orbit-install` (or `uvx orbit-install --orbit-auto` if you only want this component). That pip-installs `orbit-auto` from PyPI and puts `orbit-auto` on your `PATH`. From a clone you can run `uvx orbit-install --local`, or `pip install -e ./orbit-auto` by hand if you would rather skip the installer. The CLI binary lands in whatever Python environment you installed into - if `which orbit-auto` is empty after install, check that that environment's `bin/` directory is on `PATH`.

## Where to go from here

- [`architecture.md`](./architecture.md) - if you need the big picture on orbit's storage model, hooks, and how the pieces fit together.
- [`dashboard.md`](./dashboard.md) - for the other end of the pipeline: how the Auto view, the execution record, and the streaming logs actually get rendered.
- `orbit-auto/CLAUDE.md` - the in-repo maintainer guide, which has more details on the PRD builder skill and task-writing heuristics that did not fit in this doc.
- `orbit-auto/orbit_auto/` - the source. Start with `cli.py` to see the entry points, then `sequential.py` or `parallel.py` depending on which mode you care about. Everything else is called from one of those two.

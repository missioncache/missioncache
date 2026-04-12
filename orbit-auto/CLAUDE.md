# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Orbit Auto is an autonomous AI development tool that enables Claude to work continuously on programming tasks until completion. It uses iterative loops where AI reads its previous work via the file system.

**This repository contains a custom implementation integrated with our orbit task management system.** The scripts here work with the `~/.claude/orbit/active/<task-name>/` directory structure and support features like `/orbit:go`, task DB time tracking, dashboard visualization, and the hybrid 3-file approach (`*-tasks.md`, `*-context.md`, `*-auto-log.md`).

**CLI Command:** `orbit-auto`
**Core Philosophy:** "Iteration beats perfection on the first attempt"

## Architecture

```
+-------------------------------------------------------------+
|                       ORBIT AUTO                             |
+-------------------------------------------------------------+
|   PROMPT -> WORK -> CHECK -> EXIT? (YES=done, NO=repeat)    |
+-------------------------------------------------------------+
```

Key principles:
- **Self-referential feedback** - Same prompt repeated, AI sees file changes
- **File persistence** - All work persists via filesystem between iterations
- **Git visibility** - Each iteration sees git history for progress tracking
- **Deterministic exit** - `<promise>COMPLETE</promise>` signals completion

## Repository Structure

```
orbit-auto/
+-- orbit_auto/                # Python package
|   +-- __init__.py
|   +-- __main__.py            # Entry point: python -m orbit_auto
|   +-- cli.py                 # Argument parsing, commands
|   +-- models.py              # Data models (Task, State, Config)
|   +-- dag.py                 # Dependency graph builder
|   +-- state.py               # State management with file locking
|   +-- task_parser.py         # Parse tasks.md and prompts
|   +-- claude_runner.py       # Claude CLI integration
|   +-- display.py             # Terminal output and colors
|   +-- db_logger.py           # Database logging for dashboard
|   +-- sequential.py          # Sequential execution
|   +-- parallel.py            # Parallel orchestration
|   +-- worker.py              # Worker process
|   +-- init_task.py           # Task initialization
|   +-- templates/             # Embedded templates
+-- pyproject.toml             # Package configuration (name: orbit-auto)
+-- README.md                  # Quick reference
+-- SKILL.md                   # PRD Builder skill documentation
+-- CLAUDE.md                  # This file
```

## Orbit Files Integration

### Task Directory Structure
When running orbit-auto, tasks are organized in the centralized orbit directory:
```
~/.claude/orbit/
+-- active/
|   +-- <task-name>/
|       +-- <task-name>-tasks.md      # Checkbox items
|       +-- <task-name>-context.md    # KEY learnings only
|       +-- <task-name>-plan.md       # Implementation plan (optional)
|       +-- <task-name>-auto-log.md   # Detailed iteration history (auto-created)
|       +-- prompts/                  # Optimized prompts (optional)
|           +-- README.md             # Index with status tracking
|           +-- task-01-prompt.md     # Optimized prompt for task 1
|           +-- task-02-prompt.md     # etc.
|           +-- ...
+-- completed/
    +-- <task-name>/                  # Moved here on completion
```

### Hybrid 3-File Approach

| File | What Goes Here | Written By |
|------|----------------|------------|
| `*-tasks.md` | Checkbox items, acceptance criteria | Human initially, orbit-auto marks `[x]` |
| `*-context.md` | KEY learnings, blockers, decisions | Human + orbit-auto (important stuff only) |
| `*-auto-log.md` | Detailed iteration history | Orbit-auto only (delete after completion) |

**Why this works:**
- Context file stays clean and useful for `/orbit:go`
- Auto log has full debugging history if needed
- Log can be deleted after task completion

### Task DB Integration
Scripts integrate with `~/.claude/scripts/orbit_db.py` when available:
- Time tracking via heartbeat processing
- Progress updates with `[PROGRESS] X/Y (Z%)` format
- Task completion marking

### Dashboard Integration

Orbit Auto logs execution runs to the task database for visualization in the Orbit Dashboard (`http://localhost:8787`).

**What's logged:**
- Execution start/end with status (completed, failed, cancelled)
- Per-worker task claims, completions, and failures
- Progress updates (completed/failed subtask counts)
- Log entries with levels (debug, info, warn, error, success)

**Retention Policy:**
- Keep last 10 executions per task
- Delete executions older than 30 days
- Cleanup runs automatically when starting new executions

**Dashboard Features:**
- **Auto tab**: View active projects with D3.js dependency graphs
- **Output viewer**: Browse execution logs with filtering by level, worker, and subtask
- **SSE streaming**: Live log updates during running executions

### Optimized Prompts (Orbit Feature)

The orbit plugin can generate optimized prompts for each subtask with agent/skill references. Prompts can be executed manually or via orbit-auto. This enables:

- **Parallel agent execution** - Each prompt specifies which agents to use
- **Better task context** - Prompts include specific instructions, constraints, and validation steps
- **Status tracking** - Prompts have statuses: Pending -> Approved -> In Progress -> Completed

#### How It Works

1. **Discovery** - `/orbit:prompts` analyzes subtasks, lists relevant agents/skills, identifies gaps
2. **Gap Resolution** - If gaps found, suggests creating new agents/skills (user must approve)
3. **Generation** - Uses `/optimize-prompt` to create structured prompts with XML tags
4. **Batch Approval** - All prompts shown together for batch approval (approve once for all)
5. **Execution** - Orbit-auto uses prompts in order, following checkboxes in tasks.md
6. **Tracking** - Progress tracked via checkboxes in tasks.md (single source of truth)

#### Prompt File Structure

**Note:** Prompts do NOT have status fields. Progress is tracked by checkboxes in the tasks file.

```markdown
---
task_id: "01"
task_title: "Add priority field to Task model"
agents:
  - python-pro
skills:
  - pytest-patterns
dependencies: []
---

# Task 01: Add priority field to Task model

<context>...</context>
<instructions>...</instructions>
<constraints>...</constraints>
<agents>
## Available Agents
Use the **Task tool** with the specified `subagent_type`:
| Agent | Invoke With | Use For |
|-------|-------------|---------|
| python-pro | `subagent_type="python-pro"` | Python best practices |
</agents>
<skills>
## Available Skills
Invoke skills directly using `/skill-name`:
| Skill | Invoke | Auto-triggers on |
|-------|--------|------------------|
| pytest-patterns | `/pytest-patterns` | pytest, fixture |
</skills>
<validation>...</validation>
<acceptance_criteria>...</acceptance_criteria>
```

#### Creating Prompts

Use the orbit plugin commands:

1. **Create the task** (if not already done):
   ```bash
   /orbit:new my-feature
   ```

2. **Generate optimized prompts** with agent/skill discovery:
   ```bash
   /orbit:prompts my-feature
   ```

This workflow:
- Analyzes subtasks and identifies relevant agents/skills
- Shows gaps and suggests new agents/skills if needed (user approval required)
- Uses `/optimize-prompt` internally for each subtask
- **Shows all prompts together for batch approval**
- Creates `prompts/` directory with indexed prompt files

#### Using Prompts

**Manual execution** - work through prompts one at a time:
```bash
cat prompts/task-01-prompt.md  # Read the prompt
# Then paste into a new Claude session or continue in current session
```

**Orbit-auto execution** - autonomous batch processing:
```bash
orbit-auto my-feature
# Output:
# Found 5 prompt file(s) in prompts/
# Prompts: Using optimized prompts (tracking via tasks.md)
```

Orbit-auto determines which prompt to use by checking which tasks are still uncompleted (marked `[ ]`) in the tasks.md file. When a task is marked `[x]`, it moves to the next prompt.

## Usage

### Installation

```bash
# From the orbit-auto directory
pip install -e .

# Or run directly with Python
python -m orbit_auto <task-name>
```

### Commands

```bash
# Initialize a new task
orbit-auto init <task-name> "description"

# Run in parallel mode (default, 8 workers)
orbit-auto <task-name>

# Run with more workers
orbit-auto <task-name> -w 12

# Run in sequential mode
orbit-auto <task-name> --sequential

# Show execution plan without running
orbit-auto <task-name> --dry-run

# Check task status
orbit-auto status <task-name>
```

### Options

| Option | Default | Description |
|--------|---------|-------------|
| `-w, --workers N` | 8 | Number of parallel workers (max: 12) |
| `-r, --retries N` | 3 | Max retries per task |
| `--pause N` | 3 | Pause between iterations (sequential mode) |
| `--sequential, -s` | | Run in sequential mode |
| `--parallel, -p` | + | Run in parallel mode (default) |
| `--fail-fast` | | Stop all workers on first failure |
| `--dry-run` | | Show execution plan without running |
| `-v, --visibility` | verbose | Output level: verbose, minimal, none |
| `--no-color` | | Disable colored output |

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ORBIT_AUTO_VISIBILITY` | `verbose` | Controls tool call output during iterations |

**Visibility Modes:**
- `verbose` - Timestamps + full paths + command args (default)
- `minimal` - Timestamps + filenames only
- `none` - Original behavior (no tool visibility)

Example output with `verbose`:
```
-------------------------------------------------------------------
  * ITERATION 1/20  |  Task 2/9  |  #........... 11%
  > Add priority field to Task model
-------------------------------------------------------------------

  * Working...
  14:32:05 Read ~/.claude/orbit/active/my-task/my-task-tasks.md
  14:32:06 Read ~/.claude/orbit/active/my-task/my-task-context.md
  14:32:08 Edit src/components/Button.tsx
  14:32:15 Bash npm run typecheck
  14:32:28 Done (23s, 5 tools)

  + SUCCESS  |  23s  |  5 tools
  |_ Added priority field with enum type and default value
```

Usage examples:
```bash
# Default (verbose)
orbit-auto my-task

# Minimal output
ORBIT_AUTO_VISIBILITY=minimal orbit-auto my-task

# Disable tool visibility
ORBIT_AUTO_VISIBILITY=none orbit-auto my-task
```

### Parallel Mode

Parallel mode runs multiple tasks concurrently while respecting dependencies.

**Requirements:**
- Task must have `prompts/` directory with `task-XX-prompt.md` files
- Each prompt needs YAML frontmatter with `task_id` and `dependencies` fields

**How it works:**
1. Parses dependencies from prompt YAML frontmatter
2. Builds DAG and computes parallel execution "waves"
3. Shows execution plan for user approval
4. Spawns worker processes (up to 12)
5. Workers claim tasks atomically, respecting dependencies
6. State synced to tasks.md periodically

**Dependency format in prompts:**
```yaml
dependencies: ["01", "03"]   # Waits for tasks 01 and 03
dependencies: []             # No dependencies (Wave 1)
# (missing field)            # Implicit dependency on previous task
```

### Sequential Mode

Sequential mode runs tasks one at a time, in order. Use this for:
- Simple linear workflows
- Tasks that need careful human oversight
- Debugging specific task failures

## Task Writing Rules

### Story Size (Critical)
Every task must fit in one context window (~10 min):
- Add a database field and migration
- Create one UI component
- Modify a single backend action

**Rule:** If you can't describe it in 2-3 sentences, split it.

| Too Broad | Break Down Into |
|-----------|-----------------|
| Build the dashboard | Schema -> Queries -> UI |
| Add authentication | Schema -> Middleware -> UI -> Sessions |

### Dependency-First Ordering
1. Database/schema updates
2. Backend logic
3. UI elements consuming backend
4. Aggregated or summary views

### Acceptance Criteria
Must be objectively verifiable:
- + "Add `status` column with default `pending`"
- + "Dropdown includes All / Active / Completed"
- x "Works as expected" / "Good UX"

**Mandatory criteria:**
- "Typecheck passes" (all code changes)
- "Verify changes work in browser" (UI changes)

## File Update Rules

### Tasks File (`*-tasks.md`)
```markdown
**Status:** In Progress
**Last Updated:** YYYY-MM-DD HH:MM
**Remaining:** Configure webhook endpoint, add integration tests

## Tasks
- [x] Completed task
- [ ] Next task with clear acceptance criteria
- [ ] Typecheck passes
```
Update `**Remaining:**` field with natural language summary (max 15 words).

### Context File (`*-context.md`)
**Only add KEY learnings:**
- Architectural decisions made
- Important gotchas discovered
- Patterns to follow/avoid

**DO NOT add:** routine completion notes, iteration-by-iteration updates.

### Auto Log (`*-auto-log.md`)
Detailed iteration history (auto-generated):
```markdown
## Iteration N - [Task Title]
**Status:** SUCCESS/FAILED/PROGRESS
**Time:** [timestamp]
**Duration:** Xs | **Tools:** N

### Files Modified
- `path/to/file.ts`

### Summary
Added priority field to Task model using SQLAlchemy Enum. Created migration with default 'medium'.
```

On completion, adds:
```markdown
# COMPLETED
**Finished:** YYYY-MM-DD HH:MM
**Total iterations:** N
**Duration:** Xs

## Run Summary
Implemented complete task priority system across 5 subtasks. Main challenge was circular imports, resolved with lazy loading.
```

## Learning Tags

Orbit-auto extracts learning-centric information from Claude's responses using XML tags. These tags help future iterations learn from past attempts.

> **CRITICAL: Without `<what_worked>` tag, orbit-auto cannot detect task success and will retry indefinitely!**
>
> Every successful task completion MUST include:
> ```xml
> <what_worked>Brief description of approach that succeeded</what_worked>
> ```
>
> This is the ONLY way orbit-auto knows a task succeeded. The prompt template (`templates/prompt-template.md`) includes instructions for this tag.

### Required Tags (every response)

**Always include:**
```xml
<learnings>Key insights discovered during this attempt</learnings>
```

### Success Tags (REQUIRED for task completion)

**CRITICAL:** Orbit-auto only marks a task complete if it sees one of:
1. `<promise>COMPLETE</promise>` - signals ALL tasks done
2. `<what_worked>` tag - signals THIS task succeeded

Without these tags, the task is considered **failed** and will be retried.

**On SUCCESS, you MUST include:**
```xml
<what_worked>The specific approach that succeeded</what_worked>
```

### Failure Tags

**On FAILURE, include ALL of these:**
```xml
<what_failed>Exact error/symptom observed</what_failed>
<dont_retry>Approaches that didn't work - prevents repeating mistakes</dont_retry>
<try_next>Prioritized list of what to try next</try_next>
```

### Pattern Discovery (optional)

**When discovering a reusable pattern:**
```xml
<pattern_discovered>Pattern name: Description</pattern_discovered>
```

**Good pattern examples:**
- "Temp file cleanup: Always use trap to clean temp files on EXIT"
- "Safe grep count: Use || true after grep -c to handle zero matches"

Patterns are automatically bubbled up to the Codebase Knowledge section at the top of the auto log.

### Gotcha Discovery (optional)

**When discovering a surprising behavior to avoid:**
```xml
<gotcha>Issue: What went wrong and how to avoid it</gotcha>
```

**Good gotcha examples:**
- "grep -c exit code: Returns exit 1 when count is 0 - breaks set -e"
- "sed -i on macOS: Requires '' as first argument unlike Linux"
- "JSON parsing: Use jq instead of grep for text fields - handles escaped quotes"

Gotchas are automatically bubbled up to the Codebase Knowledge section at the top of the auto log.

### Run Completion

**When ALL tasks are complete:**
```xml
<run_summary>Overall summary of work done</run_summary>
<promise>COMPLETE</promise>
```

### How Tags Are Used

| Tag | Written To | Purpose |
|-----|-----------|---------|
| `<learnings>` | Auto log, Console | Shows what was learned |
| `<what_worked>` | Auto log, Console | Documents successful approach |
| `<what_failed>` | Auto log | Records failure details |
| `<dont_retry>` | Auto log | Prevents repeating mistakes |
| `<try_next>` | Auto log | Guides next attempt |
| `<pattern_discovered>` | Codebase Knowledge section | Reusable patterns |
| `<gotcha>` | Codebase Knowledge section | Surprising behaviors to avoid |
| `<run_summary>` | Auto log, Console | Final summary |

### Log Compaction

When a task completes successfully, all its attempts are compacted to a single line:
```markdown
## Task 2: Create config.sh - SUCCESS (2 attempts)
**Learning:** Config vars must be exported for child scripts
```

This keeps the log focused on actionable information for future iterations.

## Completion Signal

Output `<promise>COMPLETE</promise>` when ALL tasks are marked `[x]`. Include `<run_summary>` before it for best results.

## Blocker Syntax

Mark tasks that require human review before proceeding:
```markdown
- [ ] [WAIT] Task that needs human approval before proceeding
```

When orbit-auto encounters `[WAIT]`:
1. Outputs `<blocker>WAITING_FOR_HUMAN</blocker>`
2. Loop exits with code 2 (blocked, not failed)
3. Human reviews and either completes task or removes marker
4. Rerun orbit-auto to continue

## Workflow

### Quick Start
1. `orbit-auto init my-feature "Description"`
2. Edit `~/.claude/orbit/active/my-feature/my-feature-tasks.md` with tasks
3. Add context to `~/.claude/orbit/active/my-feature/my-feature-context.md`
4. `orbit-auto my-feature`  # or `orbit-auto my-feature --sequential`

### Integration with /orbit:go
The context file is designed to survive compaction and work with `/orbit:go`:
- Keep it clean with only significant learnings
- Next Steps section helps resume work
- Blockers section surfaces issues

## PRD Builder Skill

The `SKILL.md` file contains a PRD Builder skill for creating Product Requirements Documents optimized for orbit-auto loops.

Key PRD rules:
1. Each user story must fit in one context window (~10 min)
2. Stories ordered by dependency (schema -> backend -> UI)
3. All acceptance criteria must be objectively verifiable
4. Always include "Typecheck passes" criterion
5. UI stories must include "Verify changes work in browser"

## When to Use Orbit Auto

**Good use cases:**
- Large refactors with test coverage
- Batch operations across codebase
- Test coverage expansion
- Tasks with automatic verification (tests, typecheck)

**Bad use cases:**
- Tasks requiring human judgment/design decisions
- One-shot operations
- Unclear success criteria
- Production debugging

## Safety Best Practices

1. **Always set max-iterations** as a safety net (default: 20)
2. **Monitor token usage** - loops can be expensive
3. **Clear completion criteria** - vague goals = infinite loops
4. **Use git** - you can always revert bad iterations
5. **Start small** - test with 5-10 iterations first

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | All tasks completed successfully |
| 1 | Max iterations reached (timeout) |
| 2 | Blocked on `[WAIT]` task (human input needed) |

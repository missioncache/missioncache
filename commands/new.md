---
description: "Create a new orbit project with plan, context, and tasks files"
argument-hint: "[project-name] [--jira TICKET]"
---

# Create New Project

Create development documentation for a new feature or project. This command creates the plan, context, and tasks files - everything you need to start working interactively.

`/orbit:prompts` is a separate, optional step that generates per-subtask prompts optimized for autonomous parallel execution via `orbit-auto`. Most interactive workflows do not need it.

## Workflow

### Step 1: Gather Information

Ask the user for:
- Project name (suggest kebab-case based on description)
- Short description (max 12 words)
- Optional JIRA ticket
- Initial subtasks (or generate from discussion)

**Duplicate check:** Once you have a name, call
`mcp__plugin_orbit_pm__get_task(project_name="<name>")` before going further.

- If the response indicates the task is not found, the name is free - proceed.
- If a task is returned (status `active` or `completed`), ask the user how to
  proceed and wait for their reply. Present three options:
  1. **Resume the existing project** - run `/orbit:go <name>` instead of recreating.
  2. **Use a different name** - pick a new project name and re-run the duplicate check.
  3. **Recreate from scratch (destructive)** - confirms with the user that the existing
     plan/context/tasks files will be overwritten, then in Step 4 pass `force=True`.
  If your tool supports a structured option picker (Claude Code's `AskUserQuestion`),
  use it; otherwise present the options as a numbered prose list.

### Step 2: Research Phase

Ask the user what level of research they want before creating the project and wait for their reply. Present these three options:

> How much codebase research should I do before creating the project plan?
>
> 1. **Skip (Recommended)** - Proceed directly to project creation. Best when you already know what needs to be done.
> 2. **Quick** - Fast codebase scan: existing patterns, similar implementations, affected dependencies. ~30 seconds.
> 3. **Deep** - Thorough analysis with 4 parallel agents: stack, features, architecture, pitfalls. ~2 minutes.

If your tool supports a structured option picker (Claude Code's `AskUserQuestion`), use it; otherwise present the options as prose and wait for the user to reply.

**If Skip:** Set `research_findings = ""` and continue to Step 3.

**If Quick:** Spawn 1 Explore agent to scan the codebase:

```
Agent(
  subagent_type="Explore",
  description="Quick codebase research",
  prompt="Research the codebase at <repo_root> for a project: <description>.
Find and summarize:
1. **Existing patterns**: How does this codebase handle similar features? What conventions are used?
2. **Reusable code**: Functions, utilities, or modules that could be reused or extended
3. **Affected dependencies**: What existing code will this project need to integrate with or modify?

Return a structured summary with these 3 sections. Be concise - bullet points, not paragraphs."
)
```

Set `research_findings` to the agent's result.

**If Deep:** Spawn 4 parallel Explore agents in a single message:

```
# Agent 1: Stack
Agent(
  subagent_type="Explore",
  description="Stack research",
  prompt="Analyze the technology stack at <repo_root> relevant to: <description>.
Report: dependencies and versions, framework patterns, compatibility constraints, build/test tooling."
)

# Agent 2: Features
Agent(
  subagent_type="Explore",
  description="Feature research",
  prompt="Search <repo_root> for existing implementations related to: <description>.
Report: similar features already built, reusable utilities and helpers, shared patterns and abstractions."
)

# Agent 3: Architecture
Agent(
  subagent_type="Explore",
  description="Architecture research",
  prompt="Analyze the architecture at <repo_root> relevant to: <description>.
Report: module structure and boundaries, data flow and state management, integration points and APIs."
)

# Agent 4: Pitfalls
Agent(
  subagent_type="Explore",
  description="Pitfalls research",
  prompt="Identify potential pitfalls at <repo_root> for: <description>.
Report: failure modes and edge cases, known issues in related code, testing gaps, performance concerns."
)
```

Merge all 4 results into a single structured `research_findings` with sections: Stack, Features, Architecture, Pitfalls.

### Step 3: Determine Project Location

Pass the current working directory as `repo_path`. The MCP tool walks
parents to the git root server-side, so any cwd inside a git repo
resolves to the same registered path regardless of which subdirectory
the user invoked `/orbit:new` from. Non-git directories pass through
unchanged - orbit projects can be started anywhere.

```bash
pwd
```

Use the output as `repo_path`. The tool's response includes the
registered `repo_path` so you can report what was actually stored.

**Monorepo opt-out:** if the user is starting an orbit project for a
sub-package within a monorepo (e.g., `~/repo/packages/auth-service`)
and the sub-package itself is the project boundary, pass
`resolve_git_root=False` so the tool registers the subdir verbatim
rather than rebasing to the monorepo root. Default is `True`.

### Step 4: Create Orbit Files

First resolve the current Claude session ID so the new project binds to this session for the statusline. Run:

```bash
CWD_KEY=$(pwd | sed 's|/|-|g')
POINTER_FILE="$HOME/.claude/hooks/state/cwd-session/${CWD_KEY}.json"
SESSION_ID=""
if [ -r "$POINTER_FILE" ]; then
  SESSION_ID=$(python3 -c "import json,sys; print(json.load(sys.stdin)['sessionId'])" < "$POINTER_FILE" 2>/dev/null)
fi
echo "$SESSION_ID"
```

Capture the printed `SESSION_ID`. If the output is empty, the SessionStart hook has not fired yet (rare); call `create_orbit_files` without the `session_id` argument and tell the user the statusline can be populated by running `/orbit:go` once the project exists.

Now create the orbit files. Pass `research_findings` from Step 2 via the `plan` dict. Pass the resolved `session_id` so the binding is atomic with task creation. Pass `force=True` ONLY if Step 1's duplicate check confirmed the user wants to recreate destructively - the tool returns `ALREADY_EXISTS` by default to prevent silent overwrite.

**Flat tasks (simple):**
```
mcp__plugin_orbit_pm__create_orbit_files(
  repo_path="<git repository root from step 3>",
  project_name="<kebab-case-name>",
  description="<short description>",
  session_id="<SESSION_ID from bash above; omit if empty>",
  jira_key="<optional JIRA ticket>",
  tasks=["subtask 1", "subtask 2", ...],
  plan={"research_findings": "<research results from step 2>"}
)
```

**Hierarchical tasks (with parent groupings):**
```
mcp__plugin_orbit_pm__create_orbit_files(
  repo_path="<git repository root from step 3>",
  project_name="<kebab-case-name>",
  description="<short description>",
  session_id="<SESSION_ID from bash above; omit if empty>",
  tasks=[
    {"title": "Authentication", "subtasks": ["Create user model", "Add login endpoint"]},
    {"title": "Dashboard", "subtasks": ["Create component", "Add data fetching"]}
  ],
  plan={"research_findings": "<research results from step 2>"}
)
```

This generates numbered tasks:
```markdown
- [ ] 1. Authentication
  - [ ] 1.1. Create user model
  - [ ] 1.2. Add login endpoint
- [ ] 2. Dashboard
  - [ ] 2.1. Create component
  - [ ] 2.2. Add data fetching
```

The response includes `session_bound: true|false`. If `session_bound` is `false` and you DID pass a session_id, the binding helper rejected it (invalid shape or DB error); the user can recover via `/orbit:go`.

### Step 5: Probe Dashboard (optional)

Check whether the dashboard is reachable so the confirmation output can surface a deep link to the newly-created project. Skip silently when the dashboard is not installed or not running - dead links teach users to ignore the hint.

Replace `<project-name>` with the kebab-case project name, then run:

```bash
PROJECT_NAME='<project-name>'
DASHBOARD_URL="${ORBIT_DASHBOARD_URL:-http://localhost:8787}"
if curl -sf -o /dev/null --max-time 1 "${DASHBOARD_URL}/health" 2>/dev/null; then
  echo "Dashboard: ${DASHBOARD_URL}/#projects?task=$PROJECT_NAME"
fi
```

If the probe emits a line, include it as a **Dashboard** entry in the confirmation below. If nothing is emitted, omit the entry.

### Step 6: Show Plan and Confirm

```markdown
## Plan for: my-feature

**Description:** Short description here
**JIRA:** PROJ-12345 (if provided)
**Research:** Quick/Deep/Skipped

**Subtasks:**
1. First subtask
2. Second subtask
3. Third subtask

**Files created:**
- ~/.orbit/active/my-feature/my-feature-plan.md
- ~/.orbit/active/my-feature/my-feature-context.md
- ~/.orbit/active/my-feature/my-feature-tasks.md

**Dashboard:** http://localhost:8787/#projects?task=my-feature *(only if Step 5 emitted a line)*

**Next step:** Start working on task 1. The plan, context, and tasks files have everything you need.

**Optional - only for autonomous execution:** If you'll run this project via `orbit-auto` (parallel workers), run `/orbit:prompts my-feature` to generate per-subtask prompts with agent/skill recommendations. Skip this step for interactive work - it generates prompt files you won't read.
```

---

## For Non-Coding Projects

Non-coding projects don't need prompts:

1. Ask for project name and optional JIRA ticket

2. Resolve the current session ID (same bash as Step 4 above):
   ```bash
   CWD_KEY=$(pwd | sed 's|/|-|g')
   POINTER_FILE="$HOME/.claude/hooks/state/cwd-session/${CWD_KEY}.json"
   SESSION_ID=""
   if [ -r "$POINTER_FILE" ]; then
     SESSION_ID=$(python3 -c "import json,sys; print(json.load(sys.stdin)['sessionId'])" < "$POINTER_FILE" 2>/dev/null)
   fi
   echo "$SESSION_ID"
   ```

3. Create project, passing `session_id` so the statusline binds atomically:
   ```
   mcp__plugin_orbit_pm__create_task(
     name="<project-name>",
     task_type="non-coding",
     session_id="<SESSION_ID from bash above; omit if empty>",
     jira_key="<optional>"
   )
   ```

4. Explain how to track progress:
   ```
   mcp__plugin_orbit_pm__add_task_update(task_id=<id>, note="...")
   ```

---

## MCP Tools Used

| Tool | Purpose |
|------|---------|
| `mcp__plugin_orbit_pm__create_orbit_files` | Create plan/context/tasks files (also registers task in DB) |
| `mcp__plugin_orbit_pm__create_task` | Create project in database (non-coding) |
| `mcp__plugin_orbit_pm__get_task` | Pre-flight duplicate check before creating |
| `mcp__plugin_orbit_pm__add_repo` | Register repo if not already tracked |

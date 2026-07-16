---
description: "Create a new MissionCache project with plan, context, and tasks files"
argument-hint: "[project-name] [--jira TICKET]"
---

# Create New Project

Create development documentation for a new feature or project. This command creates the plan, context, and tasks files - everything you need to start working interactively.

`/missioncache:prompts` is a separate, optional step that generates per-subtask prompts optimized for autonomous parallel execution via `missioncache-auto`. Most interactive workflows do not need it.

To create a project UNDER an existing one - sharing the parent's context as a common knowledge layer while keeping its own tasks - use `/missioncache:fork` instead.

## Workflow

### Step 1: Gather Information

Ask the user for:
- Project name (suggest kebab-case based on description)
- Short description (max 12 words)
- Optional JIRA ticket
- Initial subtasks (or generate from discussion)

**Duplicate check:** Once you have a name, call
`mcp__plugin_missioncache_pm__get_task(project_name="<name>")` before going further.

- If the response indicates the task is not found, the name is free - proceed.
- If a task is returned (status `active` or `completed`), ask the user how to
  proceed and wait for their reply. Present three options:
  1. **Resume the existing project** - run `/missioncache:load <name>` instead of recreating.
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
the user invoked `/missioncache:new` from. Non-git directories pass through
unchanged - MissionCache projects can be started anywhere.

```bash
pwd
```

Use the output as `repo_path`. The tool's response includes the
registered `repo_path` so you can report what was actually stored.

**Monorepo opt-out:** if the user is starting a MissionCache project for a
sub-package within a monorepo (e.g., `~/repo/packages/auth-service`)
and the sub-package itself is the project boundary, pass
`resolve_git_root=False` so the tool registers the subdir verbatim
rather than rebasing to the monorepo root. Default is `True`.

### Step 4: Create MissionCache Files

<!-- claude-code-only -->
First resolve the current Claude session ID so the new project binds to this session for the statusline. Run:

```bash
CWD_KEY=$(pwd | sed 's|/|-|g')
# Authoritative current-session id (Claude Code 2.1.132+). Fall back to the
# cwd-session pointer, then a transcript-mtime walk for older versions. Env
# var FIRST is critical: the cwd-pointer is last-writer-wins and goes stale
# when a session is resumed or two sessions share a cwd. Binding off a stale
# pointer writes project_state under the wrong session_id, so the statusline
# (which keys on the real session id) never shows the new project.
SESSION_ID="$CLAUDE_CODE_SESSION_ID"
if [ -z "$SESSION_ID" ]; then
  POINTER_FILE="$HOME/.claude/hooks/state/cwd-session/${CWD_KEY}.json"
  if [ -r "$POINTER_FILE" ]; then
    SESSION_ID=$(python3 -c "import json,sys; print(json.load(sys.stdin)['sessionId'])" < "$POINTER_FILE" 2>/dev/null)
  fi
  [ -z "$SESSION_ID" ] && SESSION_ID=$(ls -t "$HOME/.claude/projects/${CWD_KEY}"/*.jsonl 2>/dev/null | head -1 | xargs -I{} basename {} .jsonl)
fi
echo "$SESSION_ID"
```

Capture the printed `SESSION_ID`. With `$CLAUDE_CODE_SESSION_ID` available it is essentially always populated; only if the output is empty (older Claude Code with no transcript yet) call `create_missioncache_files` without the `session_id` argument and tell the user the statusline can be populated by running `/missioncache:load` once the project exists.
<!-- /claude-code-only -->

Now create the MissionCache files. Pass `research_findings` from Step 2 via the `plan` dict.<!-- claude-code-only --> Pass the resolved `session_id` so the binding is atomic with task creation.<!-- /claude-code-only --> Pass `force=True` ONLY if Step 1's duplicate check confirmed the user wants to recreate destructively - the tool returns `ALREADY_EXISTS` by default to prevent silent overwrite.

**Derive the project category** from the description (and the conversation context), and pass it as `category`. Pick the single best fit from this taxonomy - judge by what the project DOES, not by keywords in its name:

| Category | Use for |
|----------|---------|
| `bug` | Fixing a defect, crash, or regression |
| `feature` | Building new user-facing functionality |
| `refactor` | Restructuring code without behavior change |
| `test` | Test coverage, test infrastructure, QA suites |
| `docs` | Documentation, guides, READMEs |
| `infra` | CI/CD, deployment, K8s, build systems, tooling setup |
| `ui` | Frontend, dashboards, design, styling |
| `api` | Backend endpoints, services, MCP servers, integrations |
| `database` | Schemas, migrations, queries, data layers |
| `security` | Auth, permissions, credential handling, hardening |
| `perf` | Speed, memory, caching, optimization work |
| `coding` | General coding work that fits none of the above |
| `noncoding` | Non-coding projects (planning, research, writing) |

If genuinely ambiguous between two, prefer the more specific one (e.g. a dashboard feature is `ui`, not `feature`). The coding branch never picks `noncoding`; the non-coding branch never picks `coding`. Echo the chosen category in the Step 6 confirmation so the user can correct it.

**Flat tasks (simple):**
```
mcp__plugin_missioncache_pm__create_missioncache_files(
  repo_path="<git repository root from step 3>",
  project_name="<kebab-case-name>",
  description="<short description>",
  category="<derived category>",
  session_id="<SESSION_ID from bash above; omit if empty>",
  jira_key="<optional JIRA ticket>",
  tasks=["subtask 1", "subtask 2", ...],
  plan={"research_findings": "<research results from step 2>"}
)
```

**Hierarchical tasks (with parent groupings):**
```
mcp__plugin_missioncache_pm__create_missioncache_files(
  repo_path="<git repository root from step 3>",
  project_name="<kebab-case-name>",
  description="<short description>",
  category="<derived category>",
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

The response includes `session_bound: true|false`. If `session_bound` is `false` and you DID pass a session_id, the binding helper rejected it (invalid shape or DB error); the user can recover via `/missioncache:load`.

### Step 4b: Fill Definition of Done (and Key People when relevant)

The generated context file carries a `## Definition of Done` section with a TBD placeholder. Right after creation, fill it via a direct Edit when the conversation gave you acceptance criteria - concrete, verifiable exit conditions, not restated goals. If no criteria exist yet, leave the TBD and say so in the confirmation output ("Definition of Done: not defined yet - estimates stay gated until it is"), per the estimation discipline: no estimate without acceptance criteria.

If the project involves specific colleagues (reviewers, SMEs, external owners), add a `## Key People` section between Definition of Done and Gotchas listing who owns what. Skip it entirely for solo/personal-tool projects - do not add an empty section.

Check whether the dashboard is reachable so the confirmation output can surface a deep link to the newly-created project. Skip silently when the dashboard is not installed or not running - dead links teach users to ignore the hint.

Replace `<project-name>` with the kebab-case project name, then run:

```bash
PROJECT_NAME='<project-name>'
DASHBOARD_URL="${MISSIONCACHE_DASHBOARD_URL:-http://localhost:8787}"
if curl -sf -o /dev/null --max-time 1 "${DASHBOARD_URL}/health" 2>/dev/null; then
  echo "Dashboard: ${DASHBOARD_URL}/#projects?task=$PROJECT_NAME"
fi
```

If the probe emits a line, include it as a **Dashboard** entry in the confirmation below. If nothing is emitted, omit the entry.

### Step 6: Show Plan and Confirm

```markdown
## Plan for: my-feature

**Description:** Short description here
**Category:** ui (say "change category" to correct)
**JIRA:** PROJ-12345 (if provided)
**Research:** Quick/Deep/Skipped

**Subtasks:**
1. First subtask
2. Second subtask
3. Third subtask

**Files created:**
- ~/.missioncache/active/my-feature/my-feature-plan.md
- ~/.missioncache/active/my-feature/my-feature-context.md
- ~/.missioncache/active/my-feature/my-feature-tasks.md

**Dashboard:** http://localhost:8787/#projects?task=my-feature *(only if Step 5 emitted a line)*

**Next step:** Start working on task 1. The plan, context, and tasks files have everything you need.

**Optional - only for autonomous execution:** If you'll run this project via `missioncache-auto` (parallel workers), run `/missioncache:prompts my-feature` to generate per-subtask prompts with agent/skill recommendations. Skip this step for interactive work - it generates prompt files you won't read.
```

---

## For Non-Coding Projects

Non-coding projects don't need prompts:

1. Ask for project name and optional JIRA ticket

2. <!-- claude-code-only -->Resolve the current session ID using the same bash as Step 4 above, and capture the printed `SESSION_ID`.<!-- /claude-code-only -->

3. Create the project.<!-- claude-code-only --> Pass `session_id` so the statusline binds atomically.<!-- /claude-code-only --> Non-coding projects default to `category="noncoding"` unless the description clearly fits another taxonomy value (e.g. a docs-writing project is `docs`):
   ```
   mcp__plugin_missioncache_pm__create_task(
     name="<project-name>",
     task_type="non-coding",
     category="noncoding",
     session_id="<SESSION_ID from bash above; omit if empty>",
     jira_key="<optional>"
   )
   ```

4. Explain how to track progress:
   ```
   mcp__plugin_missioncache_pm__add_task_update(task_id=<id>, note="...")
   ```

---

## MCP Tools Used

| Tool | Purpose |
|------|---------|
| `mcp__plugin_missioncache_pm__create_missioncache_files` | Create plan/context/tasks files (also registers task in DB) |
| `mcp__plugin_missioncache_pm__create_task` | Create project in database (non-coding) |
| `mcp__plugin_missioncache_pm__get_task` | Pre-flight duplicate check before creating |
| `mcp__plugin_missioncache_pm__add_repo` | Register repo if not already tracked |

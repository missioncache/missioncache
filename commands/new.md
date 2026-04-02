---
description: "Create a new orbit project with plan, context, and tasks files"
argument-hint: "[project-name] [--jira TICKET]"
---

# Create New Project

Create development documentation for a new feature or project. This command creates the plan, context, and tasks files. Run `/orbit:prompts` afterwards to create optimized prompts for each subtask.

## Workflow

### Step 1: Gather Information

Ask the user for:
- Project name (suggest kebab-case based on description)
- Short description (max 12 words)
- Optional JIRA ticket
- Initial subtasks (or generate from discussion)

### Step 2: Research Phase

Ask the user what level of research they want before creating the project:

```
AskUserQuestion(questions=[{
    "question": "How much codebase research should I do before creating the project plan?",
    "header": "Research",
    "multiSelect": false,
    "options": [
        {
            "label": "Skip (Recommended)",
            "description": "Proceed directly to project creation. Best when you already know what needs to be done."
        },
        {
            "label": "Quick",
            "description": "Fast codebase scan: existing patterns, similar implementations, affected dependencies. ~30 seconds."
        },
        {
            "label": "Deep",
            "description": "Thorough analysis with 4 parallel agents: stack, features, architecture, pitfalls. ~2 minutes."
        }
    ]
}])
```

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

### Step 3: Determine Repository Root

**IMPORTANT:** The `repo_path` must be the **git repository root**, not the current working directory.

Run this command to get the repo root:
```bash
git rev-parse --show-toplevel
```

Use the output as `repo_path`.

### Step 4: Create Orbit Files

Pass `research_findings` from Step 2 via the `plan_content` dict.

**Flat tasks (simple):**
```
mcp__plugin_orbit_pm__create_orbit_files(
  repo_path="<git repository root from step 3>",
  project_name="<kebab-case-name>",
  description="<short description>",
  jira_key="<optional JIRA ticket>",
  tasks=["subtask 1", "subtask 2", ...],
  plan_content={"research_findings": "<research results from step 2>"}
)
```

**Hierarchical tasks (with parent groupings):**
```
mcp__plugin_orbit_pm__create_orbit_files(
  repo_path="<git repository root from step 3>",
  project_name="<kebab-case-name>",
  description="<short description>",
  tasks=[
    {"title": "Authentication", "subtasks": ["Create user model", "Add login endpoint"]},
    {"title": "Dashboard", "subtasks": ["Create component", "Add data fetching"]}
  ],
  plan_content={"research_findings": "<research results from step 2>"}
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

### Step 5: Register Project

```
mcp__plugin_orbit_pm__scan_repos()
```

### Step 6: Register Project in Statusline

So statusline shows correct project:
```bash
SESSION_ID=$(curl -s "http://localhost:8787/api/hooks/term-session/${TERM_SESSION_ID:-$WT_SESSION}" --connect-timeout 1 --max-time 2 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null); [ -z "$SESSION_ID" ] && SESSION_ID=$(sqlite3 ~/.claude/hooks-state.db "SELECT session_id FROM term_sessions WHERE term_session_id = '${TERM_SESSION_ID:-$WT_SESSION}'" 2>/dev/null); [ -n "$SESSION_ID" ] && curl -s -X POST http://localhost:8787/api/hooks/project -H "Content-Type: application/json" -d "{\"session_id\":\"$SESSION_ID\",\"project_name\":\"<project-name>\"}" --connect-timeout 1 --max-time 2 >/dev/null 2>&1
```

### Step 7: Show Plan and Confirm

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
- ~/.claude/orbit/active/my-feature/my-feature-plan.md
- ~/.claude/orbit/active/my-feature/my-feature-context.md
- ~/.claude/orbit/active/my-feature/my-feature-tasks.md

**Next step:** Run `/orbit:prompts my-feature` to create optimized prompts with agent/skill recommendations for each subtask.
```

---

## For Non-Coding Projects

Non-coding projects don't need prompts:

1. Ask for project name and optional JIRA ticket

2. Create project:
   ```
   mcp__plugin_orbit_pm__create_task(
     name="<project-name>",
     task_type="non-coding",
     jira_key="<optional>"
   )
   ```

3. Explain how to track progress:
   ```
   mcp__plugin_orbit_pm__add_task_update(task_id=<id>, note="...")
   ```

---

## MCP Tools Used

| Tool | Purpose |
|------|---------|
| `mcp__plugin_orbit_pm__create_orbit_files` | Create plan/context/tasks files |
| `mcp__plugin_orbit_pm__create_task` | Create project in database (non-coding) |
| `mcp__plugin_orbit_pm__scan_repos` | Register project in database |
| `mcp__plugin_orbit_pm__add_repo` | Register repo if not already tracked |

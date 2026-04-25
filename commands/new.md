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

**Duplicate check:** Once you have a name, call
`mcp__plugin_orbit_pm__get_task(project_name="<name>")` before going further.

- If the response indicates the task is not found, the name is free - proceed.
- If a task is returned (status `active` or `completed`), use `AskUserQuestion`
  to choose between: resume via `/orbit:go <name>`, pick a different name, or
  recreate from scratch (destructive - confirm with the user, then in Step 4
  pass `force=True`).

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

### Step 3: Determine Project Location

`repo_path` is a location marker used to group tasks in the dashboard. It does NOT need to be a git repo - orbit projects can be started anywhere. Prefer the git repo root when available so all tasks in the same repo group together; otherwise fall back to the current working directory.

```bash
git rev-parse --show-toplevel 2>/dev/null || pwd
```

Use the output as `repo_path`.

### Step 4: Create Orbit Files

Pass `research_findings` from Step 2 via the `plan` dict. Pass `force=True`
ONLY if Step 1's duplicate check confirmed the user wants to recreate
destructively - the tool returns `ALREADY_EXISTS` by default to prevent
silent overwrite.

**Flat tasks (simple):**
```
mcp__plugin_orbit_pm__create_orbit_files(
  repo_path="<git repository root from step 3>",
  project_name="<kebab-case-name>",
  description="<short description>",
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

### Step 5: Register Project in Statusline

Register the project name against the current Claude session so the statusline picks it up. Uses the filesystem resolver (works on any terminal, including Ghostty and cmux) with a legacy term-session fallback. Silently no-ops if the dashboard and `hooks-state.db` aren't present - quick-install users don't have a statusline to update.

Replace `<project-name>` with the actual kebab-case project name, then run:

```bash
PROJECT_NAME='<project-name>'

# Primary: SessionStart hook writes the authoritative current-session pointer
# at ~/.claude/hooks/state/cwd-session/<sanitized-cwd>.json. Falls back to
# transcript mtime for sessions that started before the pointer mechanism landed.
CWD_KEY=$(pwd | sed 's|/|-|g')
POINTER_FILE="$HOME/.claude/hooks/state/cwd-session/${CWD_KEY}.json"
SESSION_ID=""
if [ -r "$POINTER_FILE" ]; then
  SESSION_ID=$(python3 -c "import json,sys; print(json.load(sys.stdin)['sessionId'])" < "$POINTER_FILE" 2>/dev/null)
fi
[ -z "$SESSION_ID" ] && SESSION_ID=$(ls -t "$HOME/.claude/projects/${CWD_KEY}"/*.jsonl 2>/dev/null | head -1 | xargs -I{} basename {} .jsonl)

# Fallback: legacy terminal-env-var lookup (iTerm2, Windows Terminal only).
if [ -z "$SESSION_ID" ]; then
  TERM_KEY="${TERM_SESSION_ID:-$WT_SESSION}"
  if [ -n "$TERM_KEY" ]; then
    SESSION_ID=$(curl -s "http://localhost:8787/api/hooks/term-session/${TERM_KEY}" --connect-timeout 1 --max-time 2 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null)
    [ -z "$SESSION_ID" ] && SESSION_ID=$(TERM_KEY="$TERM_KEY" python3 -c '
import os, sqlite3
conn = sqlite3.connect(os.path.expanduser("~/.claude/hooks-state.db"))
row = conn.execute("SELECT session_id FROM term_sessions WHERE term_session_id = ?", (os.environ["TERM_KEY"],)).fetchone()
print(row[0] if row else "")
' 2>/dev/null)
  fi
fi

# Write project_state. Dashboard API first, direct SQL fallback with parameter binding.
if [ -n "$SESSION_ID" ]; then
  PROJECT_JSON=$(python3 -c 'import json,sys; print(json.dumps({"session_id":sys.argv[1],"project_name":sys.argv[2]}))' "$SESSION_ID" "$PROJECT_NAME")
  curl -s -X POST http://localhost:8787/api/hooks/project \
    -H "Content-Type: application/json" \
    -d "$PROJECT_JSON" \
    --connect-timeout 1 --max-time 2 >/dev/null 2>&1 \
  || SESSION_ID="$SESSION_ID" PROJECT_NAME="$PROJECT_NAME" python3 -c '
import os, sqlite3
conn = sqlite3.connect(os.path.expanduser("~/.claude/hooks-state.db"))
conn.execute(
    "INSERT INTO project_state (session_id, project_name, updated_at) "
    "VALUES (?, ?, datetime(\"now\", \"localtime\")) "
    "ON CONFLICT(session_id) DO UPDATE SET project_name = excluded.project_name, "
    "updated_at = datetime(\"now\", \"localtime\")",
    (os.environ["SESSION_ID"], os.environ["PROJECT_NAME"]),
)
conn.commit()
' 2>/dev/null

  # Write per-session project pointer read by find_task_for_cwd (orbit-db/__init__.py:1270).
  # Without this, /orbit:save cannot find the task when cwd is the repo root. Format matches
  # session_start.py's write_session_project() so either writer is interchangeable.
  SESSION_ID="$SESSION_ID" PROJECT_NAME="$PROJECT_NAME" python3 -c '
import os, json, datetime, pathlib
projects_dir = pathlib.Path.home() / ".claude" / "hooks" / "state" / "projects"
projects_dir.mkdir(parents=True, exist_ok=True)
(projects_dir / (os.environ["SESSION_ID"] + ".json")).write_text(json.dumps({
    "projectName": os.environ["PROJECT_NAME"],
    "updated": datetime.datetime.now().astimezone().isoformat(),
    "sessionId": os.environ["SESSION_ID"],
}))
' 2>/dev/null
fi
```

### Step 6: Probe Dashboard (optional)

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

**Dashboard:** http://localhost:8787/#projects?task=my-feature *(only if Step 6 emitted a line)*

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
| `mcp__plugin_orbit_pm__create_orbit_files` | Create plan/context/tasks files (also registers task in DB) |
| `mcp__plugin_orbit_pm__create_task` | Create project in database (non-coding) |
| `mcp__plugin_orbit_pm__get_task` | Pre-flight duplicate check before creating |
| `mcp__plugin_orbit_pm__add_repo` | Register repo if not already tracked |

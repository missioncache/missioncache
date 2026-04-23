---
description: "Regenerate optimized prompts for an existing project (after subtask changes)"
argument-hint: "<project-name>"
---

# Regenerate Optimized Prompts

Regenerate or add optimized prompts for an existing orbit project. Use this when:
- Subtasks have been added or modified after initial project creation
- You want to regenerate all prompts
- Prompts were skipped during initial `/orbit:new` and you want to add them now

**Note:** For new projects, prompt generation is built into `/orbit:new` workflow.

## Prerequisites

- Project must exist in `~/.claude/orbit/active/<project-name>/`
- Project must have a `<project-name>-tasks.md` file with subtasks

## Workflow

### Phase 1: Agent & Skill Discovery

**BEFORE generating any prompts:**

1. **List available agents:**
   - Read the agent descriptions from Task tool documentation

2. **List available skills:**
   - Check `/optimize-prompt` skill for the full list

3. **Analyze each subtask** and identify:
   - Which existing agents are relevant
   - Which existing skills should be triggered
   - **GAPS**: Are there agents/skills that SHOULD exist but don't?

4. **Present gap analysis to user:**
   ```markdown
   ## Agent/Skill Analysis for: <project-name>

   ### Subtasks and Coverage

   | Subtask | Agents | Skills | Gap? |
   |---------|--------|--------|------|
   | <subtask 1> | <agent-A> | <skill-X> | No |
   | <subtask 2> | <agent-B> | - | Missing: <describe gap> |

   ### Recommended New Agents/Skills

   | Type | Name | Scope | Reason |
   |------|------|-------|--------|
   | Agent | calculator-validator | Project | Validates math operations |
   | Skill | api-validation | Global | Useful across projects |

   **Do you want me to create any of these before proceeding?**
   ```

5. **Wait for user decision** on creating new agents/skills

### Phase 2: Prompt Generation (Batch Mode)

**Generate ALL prompts at once, show them together for approval:**

1. **Announce batch generation:**
   ```
   Generating 4 prompts for project: my-feature
   ```

2. **For EACH subtask**, use `/optimize-prompt` internally to structure the prompt:
   - Include project context from `<project-name>-context.md`
   - Add specific instructions for the subtask
   - Reference relevant agents with clear invocation instructions
   - Include skill triggers with clear invocation instructions
   - Add validation criteria

3. **Show ALL prompts together in a single response:**

   ````markdown
   ## Generated Prompts (4 total)

   ### Prompt 1: <subtask 1 title>

   **File:** `prompts/task-01-prompt.md`
   **Agents:** <agent-A>
   **Skills:** <skill-X>

   <details>
   <summary>View full prompt</summary>

   ```markdown
   ---
   task_id: "01"
   task_title: "<subtask 1 title>"
   agents:
     - <agent-A>
   skills:
     - <skill-X>
   dependencies: []
   ---

   # Task 01: <subtask 1 title>

   <context>
   ...
   </context>

   <instructions>
   ...
   </instructions>

   ...rest of prompt...
   ```

   </details>

   ---

   ### Prompt 2: <subtask 2 title>

   **File:** `prompts/task-02-prompt.md`
   **Agents:** <agent-A>
   **Skills:** <skill-X>

   <details>
   <summary>View full prompt</summary>

   ```markdown
   ...full prompt content...
   ```

   </details>

   ---

   ... (continue for all prompts)

   ## Summary

   | # | Task | Agents | Skills |
   |---|------|--------|--------|
   | 1 | <subtask 1 title> | <agent-A> | <skill-X> |
   | 2 | <subtask 2 title> | <agent-A> | <skill-X> |
   | 3 | <subtask 3 title> | <agent-A> | <skill-X> |
   | 4 | <subtask 4 title> | <agent-A>, <agent-B> | <skill-X> |

   **Do you approve all these prompts?** (yes/edit prompt N/regenerate)
   ````

4. **Wait for batch approval:**
   - `yes` -> Write all prompts to files
   - `edit prompt N` -> Let user specify edits, regenerate that prompt
   - `regenerate` -> Start over from Phase 1

5. **On approval, write all prompts:**
   - Create `prompts/` directory if needed
   - Write each prompt to `prompts/task-NN-prompt.md`
   - For hierarchical tasks: `prompts/task-NN-MM-prompt.md`

### Phase 3: Create Prompts Index

After all prompts are written:

1. **Create `prompts/README.md`** with index table:
   ```markdown
   # Prompts Index - <project-name>

   **Generated:** 2025-01-20
   **Total Prompts:** 4

   | # | Task | File | Agents | Skills |
   |---|------|------|--------|--------|
   | 1 | <subtask 1 title> | task-01-prompt.md | <agent-A> | <skill-X> |
   | 2 | <subtask 2 title> | task-02-prompt.md | <agent-A> | <skill-X> |
   | 3 | <subtask 3 title> | task-03-prompt.md | <agent-A> | <skill-X> |
   | 4 | <subtask 4 title> | task-04-prompt.md | <agent-A>, <agent-B> | <skill-X> |

   ## Execution

   Orbit-auto will execute prompts in order, following the checkboxes in the tasks file.
   A task is considered complete when its checkbox is marked `[x]` in the tasks file.
   ```

2. **Summary output:**
   ```
   Generated 4 prompts for my-feature:
   - prompts/task-01-prompt.md
   - prompts/task-02-prompt.md
   - prompts/task-03-prompt.md
   - prompts/task-04-prompt.md

   Execution options:
   - Manual: Read prompts/task-01-prompt.md and work through it
   - Orbit-Auto:  orbit-auto my-feature
   ```

## Prompt Template Structure

Each prompt file follows this structure:

**Filename convention:**
- Flat task 1: `task-01-prompt.md`
- Hierarchical task 1.2: `task-01-02-prompt.md`
- Hierarchical task 2.1: `task-02-01-prompt.md`

```markdown
---
task_id: "{NN}" or "{NN-MM}" for hierarchical
task_title: "{subtask description}"
agents:
  - agent-name-1
  - agent-name-2
skills:
  - skill-name-1
dependencies: [] | ["01", "01-01"]
---

# Task {N} or {N.M}: {subtask description}

<context>
## Project Context
- **Working in:** {project_dir}
- **Project:** {project_name}

## Specific Context
{from context.md}

## Key Files
{relevant files for this subtask}
</context>

<instructions>
{numbered steps to complete the subtask}
</instructions>

<constraints>
{limitations, requirements, patterns to follow}
</constraints>

<agents>
## Available Agents

Use the **Task tool** with the specified `subagent_type` when you need specialized help:

| Agent | Invoke With | Use For |
|-------|-------------|---------|
| <agent-name> | `subagent_type="<agent-name>"` | <what this agent is good for> |
| <agent-name-2> | `subagent_type="<agent-name-2>"` | <what this agent is good for> |

Example invocation:
```
Task tool with:
  subagent_type: "<agent-name>"
  prompt: "<task-specific request>"
```
</agents>

<skills>
## Available Skills

Invoke skills directly using `/skill-name` or by including trigger keywords:

| Skill | Invoke | Auto-triggers on |
|-------|--------|------------------|
| <skill-name> | `/<skill-name>` | <trigger keywords> |
| <skill-name-2> | `/<skill-name-2>` | <trigger keywords> |

Skills provide specialized guidance and patterns for specific domains.
</skills>

<validation>
{how to verify the subtask is complete}
</validation>

<acceptance_criteria>
{specific, measurable criteria - checklist format}
- [ ] Criterion 1
- [ ] Criterion 2
</acceptance_criteria>

<on_success>
1. Mark task `[x]` in `{project_name}-tasks.md`
2. Update `**Remaining:**` field with natural language summary of what's left
3. Add KEY learnings to context.md (only if significant)
</on_success>

<on_failure>
1. DO NOT mark task complete
2. Add blocker to context.md if significant
3. Document error in iteration-log.md
</on_failure>

<response_format>
## REQUIRED: Signal Task Completion to Orbit-Auto

When task **succeeds**, you MUST include at the end of your response:
```
<what_worked>Brief description of the approach that succeeded</what_worked>
```

When task **fails** or is blocked, include:
```
<what_failed>Brief description of what failed and why</what_failed>
```

If blocked waiting for human input:
```
<blocker>WAITING_FOR_HUMAN</blocker>
```

**IMPORTANT:** Without `<what_worked>`, orbit-auto cannot detect success and will retry the task.
</response_format>
```

## Example Usage

```
User: /orbit:prompts auth-feature

Claude: Let me analyze the subtasks and check agent/skill coverage...

## Agent/Skill Analysis for: auth-feature

### Subtasks (4 total - hierarchical)
1. Authentication
   1.1. Create user model
   1.2. Add login endpoint
2. Dashboard
   2.1. Create dashboard component
   2.2. Add data fetching

### Coverage Analysis

| Subtask | Agents | Skills | Complete? |
|---------|--------|--------|-----------|
| 1.1. Create user model | <agent-A> | - | Yes |
| 1.2. Add login endpoint | <agent-B> | - | Yes |
| 2.1. Create dashboard | <agent-A> | - | Yes |
| 2.2. Add data fetching | <agent-A> | - | Yes |

### No gaps identified

All subtasks are covered by existing agents and skills.

**Ready to generate prompts. Proceed?**

Prompts will be named:
- task-01-01-prompt.md (for 1.1)
- task-01-02-prompt.md (for 1.2)
- task-02-01-prompt.md (for 2.1)
- task-02-02-prompt.md (for 2.2)

User: yes

Claude: *Generates all 4 prompts and shows them in a single batch for approval*
```

## Important Notes

- **Always ask before creating new agents/skills** - user must approve
- **Batch approval** - show all prompts together, approve once
- **Use `/optimize-prompt`** - don't manually structure prompts
- **Include all XML sections** - context, instructions, constraints, validation
- **Clear agent/skill invocation** - tell Claude exactly how to invoke them
- **No status field** - orbit-auto tracks progress via tasks.md checkboxes

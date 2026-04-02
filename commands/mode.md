---
description: "Assign workflow mode to specific tasks"
argument-hint: "<project> <range:mode,...>"
---

# Set Task Mode

Assign workflow modes (autonomous or interactive) to specific tasks in an orbit project.

## Quick Start

```
/orbit:mode my-project 1-4:auto,5-6:inter,7-10:auto
```

## Workflow

### Step 1: Parse Arguments

Extract project name and mode assignments from the command arguments.

**Format:** `<project-name> <range:mode,range:mode,...>`

**Mode values:**
- `auto` - Autonomous mode (prompt-driven, orbit-auto compatible)
- `inter` - Interactive mode (conversation-driven)

**Range formats:**
- `1-4:auto` - Tasks 1, 2, 3, 4 set to autonomous
- `5,6:inter` - Tasks 5 and 6 set to interactive
- `all:auto` - All tasks set to autonomous
- `1-4:auto,5:inter,6-10:auto` - Mixed assignment

### Step 2: Apply Mode Changes

Call the MCP tool:
```
mcp__plugin_orbit_pm__set_task_modes(
    task_name="<project-name>",
    task_ranges="1-4:auto,5-6:inter,7-10:auto"
)
```

### Step 3: Display Results

**Output format:**

```markdown
## Task Mode Assignment: my-project

Updated 10 tasks:

| # | Task | Mode | Needs Prompt |
|---|------|------|--------------|
| 1 | Create user model | Auto | Yes |
| 2 | Add login endpoint | Auto | Yes |
| 3 | Design database | Auto | Yes |
| 4 | Implement validation | Auto | Yes |
| 5 | Manual review | Inter | No |
| 6 | Design meeting | Inter | No |
| 7 | Unit tests | Auto | Yes |
| 8 | Integration tests | Auto | Yes |
| 9 | Performance tests | Auto | Yes |
| 10 | Final validation | Auto | Yes |

**Project classification:** Hybrid (8 auto, 2 inter)

### Next Steps

Autonomous tasks need prompts. Run:
```
/orbit:prompts my-project
```

This will generate prompts only for `[auto]` tasks.
```

### Step 4: Suggest Prompt Generation

If any tasks were set to `auto` and don't have prompts yet, suggest running `/orbit:prompts`.

## Mode Markers in tasks.md

Tasks are marked with mode tags:

```markdown
## Phase 1: Implementation

- [ ] 1. Create user model `[auto]`
- [ ] 2. Add login endpoint `[auto]`
- [ ] 3. Design database schema `[auto]`
- [ ] 4. Implement validation `[auto]`
- [ ] 5. Manual code review `[inter]`
- [ ] 6. Design meeting prep `[inter]`
- [ ] 7. Write unit tests `[auto]`
```

Tasks without mode markers default to interactive.

## Project Classification

| Classification | Meaning | Display |
|----------------|---------|---------|
| Fully Interactive | All tasks interactive (or unset) | Interactive |
| Fully Autonomous | All tasks autonomous | Autonomous |
| Hybrid | Mix of interactive and autonomous | Hybrid |

## Dependencies

Tasks can have explicit dependencies:

```markdown
- [ ] 7. Integration tests `[auto:depends=3,5]`
```

This means task 7 can only run after tasks 3 and 5 are complete.

To set dependencies:
```
mcp__plugin_orbit_pm__set_task_dependencies(
    task_name="my-project",
    task_id="7",
    depends_on=["3", "5"]
)
```

## Example Usage

```
# Set all tasks to autonomous
/orbit:mode my-project all:auto

# Set specific ranges
/orbit:mode my-project 1-3:auto,4:inter,5-8:auto

# Change a task from auto to interactive
/orbit:mode my-project 5:inter
```

## MCP Tools Used

| Tool | Purpose |
|------|---------|
| `mcp__plugin_orbit_pm__set_task_modes` | Apply mode changes to tasks.md |
| `mcp__plugin_orbit_pm__get_project_mode_info` | Get current mode classification |
| `mcp__plugin_orbit_pm__set_task_dependencies` | Set explicit dependencies |

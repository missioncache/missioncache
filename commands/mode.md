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

### Step 0: Handle No-Args Invocation

If invoked bare (`/orbit:mode` with no arguments), switch to interactive mode before running Step 1.

**Step 0a: Detect project.** Resolve the current Claude session and look up the active project, using the same SESSION_ID resolver pattern as `/orbit:save` (pointer-first, mtime fallback). If no project is found, print `Usage: /orbit:mode <project> <range:mode,...>` and stop.

**Step 0b: Show tasks.** Call `mcp__plugin_orbit_pm__get_orbit_files(project_name="<detected>")` and read tasks.md. Parse task numbers, titles, and any existing `[auto]` / `[inter]` markers. Display:

```markdown
## Tasks in: <project-name>

| # | Task | Current Mode |
|---|------|--------------|
| 1 | <task 1 title> | (unset) |
| 2 | <task 2 title> | auto |
| 3 | <task 3 title> | inter |
| 4 | <task 4 title> | (unset) |
```

**Step 0c: Ask how to assign.** Ask the user and wait for their reply:

> How do you want to assign modes?
>
> 1. **Set all tasks to one mode**
> 2. **Change only specific tasks**
> 3. **Cancel**

If your tool supports a structured option picker (Claude Code's `AskUserQuestion`), use it; otherwise present the options as prose.

**Step 0c-A (option 1, all tasks):** follow up with:

> Which mode for all tasks?
>
> 1. **Autonomous (`auto`)**
> 2. **Interactive (`inter`)**

Build the range spec `all:<selected>` and proceed to Step 2.

**Step 0c-B (option 2, specific tasks):** ask the user to type the range:mode spec directly. Include the expected format in the question: `e.g. 2,4:inter or 1-3:auto,5:inter`. Take the user's input as the range spec and proceed to Step 2. Tasks not listed in the spec keep their existing mode (or stay unset).

**Step 0c-C (option 3, cancel):** stop, do nothing.

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

### Step 2: Read Current Tasks

1. Get the project's orbit files:
   ```
   mcp__plugin_orbit_pm__get_orbit_files(project_name="<project-name>")
   ```

2. Read the tasks file to see current task list and any existing mode markers.

### Step 3: Edit tasks.md Directly

Parse the range:mode assignments and append mode tags to each matching task line.

**Mode tag format:** `` `[auto]` `` or `` `[inter]` ``

For each task line matching the range, append or replace the mode tag:
- Before: `- [ ] 3. Design database schema`
- After:  `- [ ] 3. Design database schema \`[auto]\``

If a task already has a mode tag, replace it.

### Step 4: Display Results

**Output format:**

```markdown
## Task Mode Assignment: my-project

Updated 10 tasks:

| # | Task | Mode |
|---|------|------|
| 1 | Create user model | Auto |
| 2 | Add login endpoint | Auto |
| 3 | Design database | Auto |
| 4 | Implement validation | Auto |
| 5 | Manual review | Inter |
| 6 | Design meeting | Inter |
| 7 | Unit tests | Auto |
| 8 | Integration tests | Auto |
| 9 | Performance tests | Auto |
| 10 | Final validation | Auto |

**Project classification:** Hybrid (8 auto, 2 inter)

### Next Steps

Autonomous tasks need prompts. Run:
```
/orbit:prompts my-project
```
```

### Step 5: Suggest Prompt Generation

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

| Classification | Meaning |
|----------------|---------|
| Fully Interactive | All tasks interactive (or unset) |
| Fully Autonomous | All tasks autonomous |
| Hybrid | Mix of interactive and autonomous |

## Dependencies

Tasks can have explicit dependencies:

```markdown
- [ ] 7. Integration tests `[auto:depends=3,5]`
```

This means task 7 can only run after tasks 3 and 5 are complete. Set dependencies by editing the mode tag directly in tasks.md.

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
| `mcp__plugin_orbit_pm__get_orbit_files` | Get paths to project's orbit files |

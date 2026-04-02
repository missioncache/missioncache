---
description: "Mark an active project as completed and archive files"
argument-hint: "[project-name]"
---

# Complete Project

Mark a project as completed and optionally move orbit files to the completed folder.

## Quick Start

1. **If project name provided:**
   ```
   mcp__plugin_orbit_pm__complete_task(project_name="<name>", move_files=true)
   ```

2. **If no project name, list active projects:**
   ```
   mcp__plugin_orbit_pm__list_active_tasks()
   ```
   Then ask user to select one.

## Workflow

### Step 1: Confirm Project

If project name not provided, list active projects and ask user to select.

### Step 2: Show Summary

Before completing, show the user:
- Total time invested
- Progress (should be 100%)
- What will happen (files moved, status changed)

### Step 3: Complete

Call `mcp__plugin_orbit_pm__complete_task` which:
1. Updates project status to "completed" in database
2. Moves files from `dev/active/<name>/` to `dev/completed/<name>/`
3. Records completion timestamp

### Step 4: Process Time Tracking

Call `mcp__plugin_orbit_pm__process_heartbeats()` to finalize time tracking.

## Example Output

```
## Completing Project: kafka-consumer-fix

**Time Invested:** 4h 30m
**Progress:** 8/8 tasks (100%)
**Status:** active -> completed

Moving files:
  dev/active/kafka-consumer-fix/ -> dev/completed/kafka-consumer-fix/

Project completed successfully!

Summary:
- Total time: 4h 30m
- Sessions: 12
- Completed at: 2026-01-20 15:30
```

## Options

- `move_files=true` (default): Move orbit files to completed/
- `move_files=false`: Keep files in active/ (useful for reference)

## MCP Tools Used

| Tool | Purpose |
|------|---------|
| `mcp__plugin_orbit_pm__list_active_tasks` | List projects if none specified |
| `mcp__plugin_orbit_pm__get_task` | Get project details for summary |
| `mcp__plugin_orbit_pm__complete_task` | Mark complete and move files |
| `mcp__plugin_orbit_pm__process_heartbeats` | Finalize time tracking |

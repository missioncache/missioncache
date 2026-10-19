---
description: "Send one instruction from the lead to a project's live session, which runs it after you confirm there"
argument-hint: "<project> <instruction>"
---

# Relay an Instruction

> Claude Code only, from the lead session (`/missioncache:lead`).

Send one instruction to the live session of a project. Nothing runs on arrival: the session there shows the instruction in a dialog, and it runs only when you pick **Run** in that session. **Drop** discards it. Either way the lead's feed shows what happened.

With the MissionCache mod loaded, Claude Code sends the relay itself and this page is never read. You see one line saying where it went.

## Workflow (without the mod)

### Step 1: Parse the Arguments

The first word of `$ARGUMENTS` is the project, the rest is the instruction. Either one missing: show `Usage: /missioncache:relay <project> <instruction>` and stop.

### Step 2: Build the Relay

```
mcp__plugin_missioncache_pm__prepare_relay(project_name="<project>", text="<instruction>")
```

`success: false` carries a `reason` (not the lead, no live session), and an error refuses a text over 500 characters or one that starts with `!`. Show it and stop.

### Step 3: Send It Unchanged

`SendMessage` to the returned `title` with the returned `envelope` as the whole message, unchanged. Report in one line: sent to `<title>`, and it runs there once the person picks Run.

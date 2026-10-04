---
description: "Stop being the lead and end its update loop"
---

# Stop Leading the Portfolio

> Claude Code only, like `/missioncache:lead-start`.

End the lead role that `/missioncache:lead-start` gave a session. Working sessions stop sending it change notices, and the session gives up the `missioncache-lead` title on its next prompt: it takes its project's name back, or a `session-<id>` name when it has no project.

## Workflow

### Step 1: Clear the Role

```bash
missioncache-db lead stop
```

### Step 2: End the Loop

If this session scheduled a delta-brief loop, end it. A loop that is still running elsewhere stops by itself on its next tick, because every tick checks that the role is still designated, and `lead stop` just cleared it.

### Step 3: Report

Tell the user in one line that the role ended. Nothing else: no brief, no snapshot.

---
description: "Create a project as a fork of an existing parent, sharing the parent's context as a common knowledge layer"
argument-hint: "[parent-name] [child-name] [--jira TICKET]"
---

# Fork a Project

Create a child project under an existing parent. The parent's context file becomes the **shared knowledge layer**: every child (and the parent's own sessions) reads it, any session may update it, and sibling sessions are told when it changed. The child gets its own plan, context, and tasks files - **tasks are never shared or copied**; only context is.

Use a fork when one effort has a shared spine (architecture, access patterns, environment facts, gotchas) plus parallel work lanes that each need their own task list - e.g. one pipeline project with two product-specific test layers.

## Workflow

### Step 1: Resolve the Parent

Call `mcp__plugin_missioncache_pm__get_task(project_name="<parent>")`.

- Not found: stop and tell the user - a fork needs an existing parent (active or completed both work).
- Found: also fetch `mcp__plugin_missioncache_pm__get_context_digest(project_name="<parent>")` and skim it so you can describe to the user what shared knowledge the child inherits (the parent's Description, decisions, gotchas).

### Step 2: Gather the Child

Same conversation as `/missioncache:new` Step 1, plus the fork rules:

- Child name (kebab-case; run the same duplicate check as `/missioncache:new`).
- Short description - describe the child's OWN lane, not the parent's mandate.
- Initial subtasks: the child's own new tasks ONLY. Never copy tasks from the parent - parent-owned work stays in the parent's tasks file.

### Step 3: Resolve Session ID

Reuse the exact bash block from `/missioncache:new` (Step 4 there): resolve `$CLAUDE_CODE_SESSION_ID` with the cwd-pointer and transcript-mtime fallbacks, capture `SESSION_ID`.

### Step 4: Create the Fork

```
mcp__plugin_missioncache_pm__create_missioncache_files(
    repo_path="<repo>",
    project_name="<child>",
    description="...",
    fork_of="<parent>",
    tasks=[...child-only tasks...],
    session_id="<SESSION_ID>",
)
```

This writes the child's files with a `**Fork of:** <parent>` line in the context header, links `parent_id` in the database (the scan reconciles the header), and binds the session. Check the response:

- `fork_linked: true` - the link is live.
- `fork_linked: false` + `fork_warning` - the header was written but the parent did not resolve unambiguously; surface the warning. The link self-heals on the next scan once resolvable.

### Step 5: Seed the Shared-Seen Marker

Baseline this session's view of the shared layer so freshness tracking starts clean. Call `mcp__plugin_missioncache_pm__get_context_digest(project_name="<child>")` - its `parent_digest` block carries the exact `context_file` path (works for prefixed AND legacy context filenames, active or completed) and a `context_mtime` coupled to the bytes the digest read. You just presented the parent's knowledge in Step 1, so stamping this snapshot is honest. Then run:

```bash
PARENT='<parent-name>'
PARENT_CTX='<parent_digest.context_file>'
SEEN_MTIME='<parent_digest.context_mtime, verbatim>'
SESSION_ID='<SESSION_ID from Step 3>'
if [ -n "$SESSION_ID" ] && [ -n "$SEEN_MTIME" ]; then
  mkdir -p "$HOME/.claude/hooks/state/shared-seen"
  PARENT="$PARENT" PARENT_CTX="$PARENT_CTX" SEEN_MTIME="$SEEN_MTIME" SESSION_ID="$SESSION_ID" python3 -c '
import json, os, pathlib, datetime
marker = pathlib.Path.home() / ".claude" / "hooks" / "state" / "shared-seen" / (os.environ["SESSION_ID"] + ".json")
marker.write_text(json.dumps({
    "parent": os.environ["PARENT"],
    "parent_context_path": os.environ["PARENT_CTX"],
    "seen_mtime": float(os.environ["SEEN_MTIME"]),
    "seen_at": datetime.datetime.now().astimezone().isoformat(),
}))
'
fi
```

### Step 6 (optional): Migrate Shared Content Out of a Monolith Parent

Only when the user says the parent is a pre-fork monolith they want to split:

1. Read the parent's full context file.
2. Propose a bullet-level split: content specific to the new child MOVES down into the child's context; shared infra/architecture/access/gotchas content STAYS in the parent. Sections like Gotchas, Key Architectural Decisions, Who's who, and Next Steps usually mix lanes bullet-by-bullet - split within sections, not whole sections.
3. Show the user the proposed moves as a summary and get an explicit yes BEFORE writing.
4. Apply the split with DIRECT file edits (Read + Edit on both context files) - `update_context_file` can only append to sections, it cannot remove bullets, so it cannot express a move. Two cautions that replace the tool's guarantees: (a) do the split only when no sibling session is actively writing these files (the file locks serialize the MCP tools, not direct edits); (b) moves are MOVES, not copies - after editing, verify each moved bullet appears exactly once, in the child. Then use `update_context_file` on the parent to add a one-line Recent Changes note saying what moved to which child.
5. Tasks are never migrated. Ever.

### Step 7: Confirm

Show the user:
- The child's files and its `Fork of: <parent>` relationship.
- Both dashboard links: `http://localhost:8787/#projects?task=<child>` and `...?task=<parent>`.
- How sharing works from now on (one paragraph): any session updates the shared layer by calling `update_context_file` on the PARENT's context path (resolve it via `get_missioncache_files(<parent>)`); `/missioncache:load` on any sibling shows a banner when the shared layer changed since that session last synced, and the statusline marks the fork with a staleness dot.

## Rules That Keep Forks Sane

- **Only context is shared.** Tasks, plans, prompts, and time tracking stay per-project.
- **The parent's context file is the single shared layer.** Do not duplicate its content into children - link and reference.
- **Completing the parent is allowed** while children are active; its context stays readable and shared from `completed/`. `/missioncache:done` warns so nobody is surprised.
- **Renaming a parent breaks the children's `Fork of:` headers** - they keep their last link but stop re-healing. If you rename a parent, update each child's header line to the new name in the same breath.
- **Fork chains** (grandparent -> parent -> child) are stored faithfully, but tooling resolves ONE level: a child sees its immediate parent's context as the shared layer.

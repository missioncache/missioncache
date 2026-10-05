# Forks

A fork is a project with a parent. It has its own plan, tasks and time, and it reads the parent's context file as knowledge shared by every fork.

You write a shared fact once, in the parent, and every lane reads it there.

## Fork, new project, or subtask

| The work is... | Use |
|---|---|
| A step inside one effort | A subtask in that project's tasks file |
| Its own effort, but it needs a large body of knowledge another effort also needs | A fork |
| Its own effort, sharing nothing but a repo | A new project |

Two plain projects is the right answer more often than a fork. Fork only when the shared knowledge is real, large, and stable: the same architecture, access, environment facts and gotchas. Sharing a repo folder is not enough.

A subtask is wrong when the lane needs its own task list. Two lanes in one tasks file give you one progress number that means nothing, one dashboard row and one clock for two efforts, and one `missioncache-auto` run that schedules both lanes together.

## Make a fork

| Client | Command |
|---|---|
| Claude Code | `/missioncache:fork <parent> <child>` |
| Codex | `$missioncache-fork <parent> <child>` |
| OpenCode, VSCode | `/missioncache-fork <parent> <child>` |

The parent must already exist, active or completed. The command asks for the child's description and first tasks. Describe the child's own lane, not the parent's.

The child's context file gets one header line that makes it a fork:

```markdown
**Fork of:** my-parent
```

That line is the link. It must sit in the header, above the first `##` heading. Delete it and the project becomes a plain project on the next scan.

## What is shared

Only the parent's context file. Nothing else crosses the link.

| | Parent | Fork |
|---|---|---|
| Plan | its own | its own |
| Tasks | its own | its own. Never copied from the parent. |
| Context | its own, and the shared layer for every fork | its own, for facts true only in this lane |
| Time tracking | its own | its own |
| Dashboard row | its own | its own |
| `/missioncache:done` | closes the parent, forks keep running | closes this fork only |

Finishing a task in the parent ticks nothing in the fork.

**Where a fact goes.** Ask one question: is this true for every lane? Yes goes in the parent's context. No goes in the fork's.

To write to the shared layer, ask Claude to update the parent's context. Do not copy the parent's content down into a fork.

## When a sibling changes the shared layer

Two sessions on two forks can both write the parent's context. MissionCache does not block that. In Claude Code it tells you:

- **The statusline** shows `⤵ Fork of: <parent>` on the project line. When the parent is itself a fork, its own parent follows in gray: `⤵ Fork of: <parent> ← <grandparent>`, and `← …` marks a longer chain. When the parent's context changed since this session last read it, a cyan `● parent updated <time>` note follows.
- **`/missioncache:load`** says either "shared context up to date" or "UPDATED by a parallel session since your last sync". On the second, it reads the parent before you continue.

To clear the note, do any of these:

- Run `/missioncache:load <fork>`.
- Run `/missioncache:save` in a session that wrote to the parent.
- Ask Claude to re-read the shared context.

In Codex, OpenCode and VSCode there is no note. `/missioncache-load` reads the parent's context every time instead.

## Moving content between parent and fork

When a section, a bullet, a task or a Waiting-on row is in the wrong project, ask Claude to move it with `move_to_project`. It moves the content in one step and notes the move in both files' Recent Changes. It will not move a section when the target already has one with that heading.

A moved task belongs to the other project. It is still not shared.

## An example

You have one data pipeline and two product test suites built on it.

- **Parent: `ingest-pipeline`.** Its context holds the schema, the auth setup, the staging endpoints, and the three gotchas that each took a day to find. All of it is true for both suites.
- **Forks: `product-a-tests` and `product-b-tests`.** Each has its own suite, fixtures and task list.

A session on `product-a-tests` finds that staging rotates its token every hour. That is true for both suites, so it goes in the parent. The next `product-b-tests` session sees `● parent updated` and reads it first.

## Completing, renaming and deleting a parent

**Completing.** `/missioncache:done` on a parent with active forks warns you and completes it. The forks keep reading its context from the completed folder.

**Renaming.** `/missioncache:rename` does not update the forks. Each fork still says `**Fork of:** <old-name>` and stops linking. Edit that line in every fork to the new name right after you rename.

**Deleting.** A parent that still has forks cannot be deleted. Complete or delete the forks first, or remove the `**Fork of:**` line from each one so they unlink on the next scan.

**Chains.** You can fork a fork. It sees only its direct parent's context, never the grandparent's. The dashboard and `missioncache-db list-active` show it nested under its parent.

## When something looks wrong

**The fork command warned that the link did not resolve.** The header is written. Two projects may share the parent's name, or the parent may not exist yet. Fix the name and the link heals on the next scan.

**No fork cell on the statusline.** Check that the `**Fork of:**` line sits above the first `##` heading and that the parent name matches exactly. A line lower in the file is ignored.

**A sibling changed the shared context and I saw no note.** This session has not read the parent yet, so it has nothing to compare against. Run `/missioncache:load <fork>` once. From then on the note works.

## More

How the link is stored, the freshness marker and the header rules: [internals/forks.md](internals/forks.md).

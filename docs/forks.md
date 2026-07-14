# Forks

This document covers MissionCache's forks: a way to run two or more projects that lean on the same body of knowledge, without keeping a separate copy of that knowledge in each one. A fork is a full project with a parent, and the parent's context file is the shared layer they all read and write.

It assumes you have read [`architecture.md`](./architecture.md) for the shared vocabulary (MissionCache file layout, the `tasks` table, `~/.claude/hooks/state/`, the MCP tools). If a term in this doc is not defined here, it is defined there.

If you are just trying to *use* forks, the short version is: run `/missioncache:fork <parent> <child>`, the child gets its own task list, and shared knowledge goes in the parent's context file. The rest of this doc is for deciding *whether* to fork, which is the harder question and the one this doc mostly exists to answer.

## What a fork is

A fork is a full project. It has its own plan, its own context, its own tasks, its own time tracking, and its own `/missioncache:done`. You work in it exactly the way you work in any other project. The one thing it does not have of its own is the shared spine: the parent's context file plays that role for every fork under it.

**Tasks are never shared and never copied.** This is the number-one thing people get wrong. Forking does not clone the parent's task list, and finishing a task in the parent does not tick anything in the child. Only context is shared.

| | Parent | Fork |
|---|---|---|
| Plan file | its own | its own |
| Tasks file | its own | its own |
| Context file | its own, and it is the shared layer every fork reads | its own, for facts true only in this lane |
| Time tracking | its own clock | its own clock |
| Dashboard row | its own | its own |
| `/missioncache:done` | closes the parent, forks keep running | closes the fork only |
| Fork link | none | a `**Fork of:** <parent>` line in its context header |

The link is that one header line. It is a plain line of text in a plain markdown file, which means you can add it, change it, or delete it by hand, and MissionCache will follow you.

## When to fork

### Fork vs. two separate projects

Two plain projects each hold their own copy of the shared facts. Copies drift.

You learn a gotcha while working lane A. You write it in lane A's context, because that is where you were. Lane B never sees it. A week later, a session in lane B rediscovers the same thing the slow way. Worse: you fix a stale fact in one copy, and the other copy keeps lying to whoever reads it next.

A fork has one copy. You write the fact once, in the parent, and every lane reads it there.

**Be honest with yourself: two separate projects is the right answer more often than a fork.** Fork only when the shared knowledge is real, substantial, and stable enough that keeping two copies of it would be a mistake.

Sharing "the same repo" is not a spine. Two efforts in one repo that share nothing but the folder should be two projects. Sharing "the same architecture, the same access patterns, the same environment facts, the same gotchas" is a spine. That is when a fork pays for itself.

### Fork vs. a subtask

A subtask is a checkbox line inside one project's tasks file. It shares that project's plan, its context, and its task list, and it closes when you tick the box. That is exactly right when the work is one lane.

A fork exists precisely when the work needs its own task list. Run two parallel lanes inside one tasks file and three things break:

1. **Progress stops meaning anything.** One "60% complete" across two independent lanes answers no question anybody actually has.
2. **MissionCache Auto schedules across lanes that have nothing to do with each other.** Dependencies are per-project, so two lanes in one tasks file are one graph.
3. **The dashboard shows one row when you are running two efforts.** One clock, one finish line, for work that has two of each.

A quick test:

| The work is... | Use |
|---|---|
| A step inside one effort | A subtask |
| Its own effort, but it leans on a large body of knowledge the other effort also needs | A fork |
| Its own effort, sharing nothing but a repo | A new project |

### The shared spine

Here is the whole mental model.

The parent holds the facts that are true **no matter which lane you are in**. The children hold the facts that are true **only for their lane**. One question decides where anything goes: *is this true for every lane?* Yes goes in the parent. No goes in the child.

A worked example. One pipeline project, two product-specific test layers on top of it. The pipeline's schema, its auth, its staging endpoints, and the three gotchas that each cost you a day to find are true for both test layers, so they live in the parent's context. Each test layer's own suite, its fixtures, and its task list are true only for that layer, so they live in that fork.

When you find yourself about to write the same fact into both children, that is the signal: it belongs in the parent.

## How the shared layer works

### The header is the source of truth

The `**Fork of:** <parent>` line in the child's context header is what makes a fork a fork. The `tasks.parent_id` column in the database is derived from it, not the other way round.

The repo scan reconciles the two on every run (`_reconcile_fork_link` in `missioncache-db/missioncache_db/__init__.py`):

- Header present and the parent resolves: the link is set.
- Header present but the link was lost (a deleted parent nulled it, or an import dropped it): the link is re-healed.
- Header removed: the link is cleared.
- Header present but the name does not resolve to exactly one parent: **the scan refuses to guess.** It logs a warning and preserves whatever link is already there. Ambiguous names and cyclic links are both refused rather than resolved by guessing.

Only the **header region** counts. That is everything before the first `##` heading, and the parse is fence-aware, so a `##` inside a fenced code block does not end the region early. A "Fork of" mention further down in the body, or inside a code fence, never links anything. The parse is `parse_fork_parent` at `missioncache-db/missioncache_db/context_health.py:499`. It accepts a plain name (`**Fork of:** my-parent`) or a balanced wikilink (`**Fork of:** [[my-parent]]`), and rejects a malformed half-link.

The database column is the index, not the truth. It is what makes "find every child of this parent" a fast lookup, backed by `idx_tasks_parent`.

### Reading and writing the shared layer

Reading it is automatic, but it is not unconditional. `get_context_digest` on a fork always returns a small `parent_digest` block next to the child's digest: the parent's name, its context path, when it was last updated, and whether it changed since this session last synced. `/missioncache:load` then reads the parent's **full** digest only when the shared layer actually moved since your last sync, or when this session has no marker yet. When the shared layer is already up to date, there is nothing new to read and the resume skips it. The parent resolves from `active/` first and then `completed/`, so a completed parent's shared layer stays reachable.

Writing it is a deliberate act. To update the shared layer, call `update_context_file` on the **parent's** context path, which you resolve with `get_missioncache_files(<parent>)`. Do not duplicate the parent's content down into a child. Link to it and reference it.

Any session may write the shared layer, which means two sessions can be writing near each other. The parallel-session discipline is the same as everywhere else in MissionCache, and it matters more here:

- Re-read the digest (`get_context_digest`) before you write, so you are not writing over something you have not read.
- Write only through the locked MCP tools (`update_context_file`, `update_tasks_file`). They serialize on a sidecar lock. Direct file edits do not.
- Treat Recent Changes as prepend-only. Never rewrite an older subsection.

## Parallel sessions and freshness

Two sessions on two forks are two people editing one shared file. MissionCache does not lock you out of that. It tells you when it happened.

| Piece | What it is |
|---|---|
| The shared-seen marker | `~/.claude/hooks/state/shared-seen/<session-id>.json`. Records the parent-context mtime this session last read. Shape: `{parent, parent_context_path, seen_mtime, seen_at}`. Seeded by `/missioncache:fork`, stamped by `/missioncache:load`, restamped by `/missioncache:save` when that save wrote the parent. |
| `parent_digest.changed_since_seen` | `get_context_digest` takes a `seen_mtime` and returns this flag on a fork. True means a sibling session updated the shared layer since this session last synced. |
| The `/missioncache:load` banner | On resume, the fork line reads either "shared context up to date" or "UPDATED by a parallel session since your last sync". On the second, `/missioncache:load` reads the parent's digest before continuing. |
| The statusline cell | A `⤵ Fork of <parent>` cell, OSC 8-linked to the parent's dashboard modal, with a `● shared updated` dot when the parent's context is newer than this session's marker. |

The marker means "this session consumed the shared layer at this version". It is only stamped after the shared layer was actually read, never before.

## Completing the parent

You can complete a parent that still has active forks. `/missioncache:done` allows it and warns you, it does not refuse. The warning names the active forks so nobody is surprised.

Nothing breaks. The parent's context file moves to `completed/`, and the forks keep reading it from there. On the dashboard, a fork whose parent has left the active set surfaces top-level instead of disappearing under an absent parent.

This is deliberate. The spine outlives the effort that grew it. The pipeline project finishes, and the test layers built on top of it keep running against the knowledge it left behind.

Deleting a parent is a different matter. `delete_task` refuses while forks still point at it, and tells you to complete or delete them first, or remove the `**Fork of:**` line from each fork's context and re-scan to unlink.

## Rules and limits

**Only context is shared.** Tasks, plans, prompts, and time tracking stay per-project. Nothing else crosses the link.

**The parent's context file is the single shared layer.** Do not copy its content into the children. Link and reference.

**Renaming a parent breaks the children's `Fork of:` headers.** This is a real sharp edge, and `/missioncache:rename` does not warn you about it. The children keep their last link, but they stop re-healing, because the name in their header now points at nothing. If you rename a parent, update every child's `**Fork of:**` line to the new name in the same breath.

**Fork chains resolve one level.** Grandparent to parent to child is stored faithfully, but the tooling reads one hop. A child sees its immediate parent's context as the shared layer. Shared layers do not stack.

**The header parse grammar is a security control, not a style rule.** The name in a `**Fork of:**` line carries no slashes and must lead with an alphanumeric. That is what blocks path traversal through a hand-edited context header, and the header is a plain text file any user can write. The same pattern lives in two places and must stay byte-identical (see the invariant in [`architecture.md`](./architecture.md)). Do not loosen it.

Note this is the grammar for **parsing the header line**, not the rule for naming a project. Project names are validated more strictly at creation, by `_TASK_NAME_RE` in `missioncache-db/missioncache_db/__init__.py:234`: lowercase letters, digits, and hyphens only. The header grammar is deliberately the looser of the two, so it can still parse a name written by an older version or by hand.

## Troubleshooting

### "`fork_linked: false` came back from the fork"

**Cause:** The `**Fork of:**` header was written to the child's context, but the parent name did not resolve to exactly one project. Usually that means two projects share the name, or the parent does not exist yet.

**Fix:** The header is already correct on disk, so this self-heals. The next repo scan links it as soon as the name resolves unambiguously. If it does not, check for a duplicate project with the parent's name - the resolver refuses to guess between two candidates rather than pick the wrong one.

### "I renamed the parent and the fork went plain"

**Cause:** Renaming a parent does not rewrite its children's headers. Each child still says `**Fork of:** <old-name>`, which now resolves to nothing, so the statusline drops the fork cell and the child renders as a plain project.

**Fix:** Edit each child's context header and put the new parent name in the `**Fork of:**` line. The next scan re-links it. Do this in the same breath as the rename, not later.

### "A sibling changed the shared context and I never saw it"

**Cause:** The shared-seen marker is **per session**. A session that has never run `/missioncache:load` or `/missioncache:fork` has no marker, so it has no baseline to compare against and nothing to warn you with. This also happens right after a session switches to a fork of a different parent, because the marker it holds belongs to the other parent.

**Fix:** Run `/missioncache:load <fork>`. It reads the shared layer and stamps a fresh marker, and from then on the banner and the statusline dot both work for that session.

### "The statusline shows no fork cell even though the header is there"

**Cause:** Almost always the header line is not in the header region. Only the text before the first `##` heading counts. A `**Fork of:**` line below a section heading is ignored, by design, and the database ignores it too, so the fork is not linked either. The other cause is an unresolvable parent, which the statusline renders as a plain project on purpose.

**Fix:** Move the `**Fork of:** <parent>` line up into the context header, above the first `##`. Confirm the parent project exists and its name matches exactly.

## Where to go from here

- [`architecture.md`](./architecture.md) - the `tasks` schema, `parent_id`, and the invariant about the two hand-mirrored `Fork of:` regexes.
- [`mcp-tools.md`](./mcp-tools.md) - `create_missioncache_files(fork_of=...)`, `get_context_digest`, `update_context_file`, `complete_task`.
- [`statusline.md`](./statusline.md) - the fork cell, the shared-updated dot, and the OSC 8 links.
- [`dashboard.md`](./dashboard.md) - how a fork renders when its parent leaves the active set.
- [`commands/fork.md`](../commands/fork.md) - the command itself, step by step, including migrating shared content out of a monolith parent.
- `missioncache-db/missioncache_db/context_health.py:499` - `parse_fork_parent`, the header-region-only parse that every other piece depends on.

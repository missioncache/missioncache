---
name: whats-new
description: Scan Claude Code changelog since installed version and suggest workflow improvements based on your setup.
argument-hint: "[--since VERSION]"
allowed-tools: ["Bash", "Glob", "Read", "Write", "Edit", "AskUserQuestion", "Grep"]
effort: high
---

# What's New in Claude Code

Scan the local changelog and cross-reference with the user's setup to surface relevant changes, tips, and **concrete improvement proposals**.

**Arguments:** "$ARGUMENTS"

## Workflow

### Step 1: Get Current Version and Last Reviewed Version

```bash
claude --version
cat ~/.claude/cache/whats-new-version 2>/dev/null || echo "none"
```

Save both values:
- `CURRENT_VERSION` - the installed Claude Code version
- `LAST_REVIEWED` - the version from the tracking file (or "none" if first run)

### Step 2: Read Changelog

Read the local changelog file at `~/.claude/cache/changelog.md`.

If it doesn't exist, tell the user: "No local changelog found at ~/.claude/cache/changelog.md. This file is typically maintained by the update process."

### Step 3: Parse Version Range

Determine the version range to scan:

- **If `--since X.Y.Z` is provided:** Use X.Y.Z as range start (explicit override).
- **If `LAST_REVIEWED` exists and differs from `CURRENT_VERSION`:** Scan from `LAST_REVIEWED` (exclusive) to `CURRENT_VERSION` (inclusive). This covers all versions the user hasn't seen yet.
- **If `LAST_REVIEWED` equals `CURRENT_VERSION`:** Tell the user "You're up to date - changelog already reviewed for vX.Y.Z." and stop (skip remaining steps).
- **If no `LAST_REVIEWED` (first run):** Show only the current version's section.

Changelog sections are typically headed by version numbers (e.g., `## 2.3.0` or `# v2.3.0`).

### Step 4: Gather User Setup Context

Read these files to understand the user's current configuration. Read them in parallel:

- `~/.claude/settings.json` - global settings (hooks, permissions, plugins, statusline)
- `~/.claude/settings.local.json` - local settings overrides
- List files in `~/.claude/commands/` - existing slash commands
- List files in `~/.claude/rules/` - rule files
- List files in `~/.claude/hooks/` - hook scripts
- `~/.claude/skills/skill-rules.json` - skill activation rules
- `~/.claude/plugins/installed_plugins.json` - installed plugins

Don't read the full contents of every file - just list what exists and skim structure. The goal is to know what the user HAS so you can identify what's relevant.

### Step 5: Deep Setup Analysis (for proposals)

For changelog entries that look like they could improve the user's setup, **read the relevant hook/script files** to understand what they currently do. This enables concrete proposals.

For example:
- If a new hook field like `agent_id` was added, read the user's hook scripts to see which ones could benefit from it
- If a new setting was added, check if the user's settings.json could use it
- If a command was improved, check if the user has custom commands that overlap

Only read files that are directly relevant to a potential proposal. Don't read everything.

### Step 6: Categorize and Generate Proposals

For each changelog entry in the version range:

#### A. Changelog Summary (brief)

Categorize each entry as one of:
1. **Action Items** - Changes that directly affect the user's existing setup
2. **New Features** - Capabilities not yet used that would benefit this setup
3. **Bug Fixes** - Fixes for issues the user might have encountered
4. **Tips** - Non-obvious power-user features

#### B. Improvement Proposals (the key addition)

For entries where the user's setup could concretely benefit, generate **Improvement Proposals**. Each proposal should include:

1. **Title** - Short name for the improvement
2. **Why** - What problem it solves or what it improves (1-2 sentences)
3. **What to change** - The specific file(s) to edit and what to add/modify
4. **Code example** - Show the exact change (before/after or new code to add)
5. **Effort** - Low / Medium / High
6. **Risk** - None / Low / Medium (would it break anything?)

**Proposal quality rules:**
- Only propose changes you're confident about - don't guess at APIs you haven't verified
- Show exact file paths and code, not vague instructions
- If a proposal requires reading a file you haven't read yet, read it first
- Don't propose changes the user already has
- Prioritize high-impact, low-effort proposals

**Examples of good proposals:**

```
### Proposal: Use `agent_id` in tab-title hook
**Why:** Your tab-title.sh currently can't distinguish subagent runs from main sessions.
The new `agent_id` field in hook events lets you show which agent is active.
**File:** ~/.claude/hooks/tab-title.sh
**Change:** Add agent_id extraction from the hook event JSON:
  ```bash
  AGENT_ID=$(echo "$CLAUDE_HOOK_EVENT" | jq -r '.agent_id // empty')
  if [ -n "$AGENT_ID" ]; then
    TITLE="[$AGENT_ID] $TITLE"
  fi
  ```
**Effort:** Low | **Risk:** None
```

```
### Proposal: Add `/reload-plugins` to workflow
**Why:** You currently need to restart Claude Code after plugin changes.
`/reload-plugins` activates changes mid-session.
**Change:** No config change needed - just use `/reload-plugins` after editing plugins.
**Effort:** None | **Risk:** None
```

**Anti-patterns to avoid:**
- Don't propose adding features that have no clear use case for the user
- Don't propose complex refactors for minor gains
- Don't propose changes to files you haven't read
- Don't propose things that are purely informational (put those in Tips instead)

### Step 7: Present Findings

Format output in two parts:

#### Part 1: Changelog Summary

```
## Claude Code vX.Y.Z

### Action Items
- [item]: [brief description]

### New Features
- [feature]: [what it does]

### Bug Fixes
- [fix]: [what was broken]

### Tips
- [tip]: [how to use it]

### Skipped
X entries skipped (SDK-only, irrelevant to CLI setup)
```

#### Part 2: Improvement Proposals

```
---

## Improvement Proposals

Based on your setup, here are concrete improvements you can adopt:

### 1. [Title]
**Why:** [motivation]
**File:** [path]
**Change:**
[code block with the exact change]
**Effort:** Low | **Risk:** None

### 2. [Title]
...
```

**Presentation rules:**
- Order proposals by impact (highest first)
- Skip entries irrelevant to the user's CLI setup (SDK-only, API-only, wrong platform)
- Be specific - exact file paths, exact code, exact commands
- If no proposals are warranted, say "No setup improvements needed for this release"
- For `--since` ranges with many versions, group the most important proposals at the top

#### Part 3: Interactive Application (optional)

After presenting proposals, ask the user:

```
Would you like me to apply any of these proposals? You can say:
- "Apply all" - apply everything
- "Apply 1, 3" - apply specific proposals by number
- "Skip" - just reviewing, don't change anything
```

If the user selects proposals to apply, make the changes using Edit/Write tools. After applying, briefly confirm what was changed.

### Step 8: Mark Version as Reviewed

After presenting findings, write the current installed version to the tracking file so the statusline shows the version in green:

```bash
echo -n "X.Y.Z" > ~/.claude/cache/whats-new-version
```

Replace `X.Y.Z` with the actual current version from Step 1. This tells the statusline that you've reviewed the changelog for this version.

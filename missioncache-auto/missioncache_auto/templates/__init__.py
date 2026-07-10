"""
Templates for missioncache-auto task initialization.

These templates are used when creating new tasks with `missioncache-auto init`.
"""

TASKS_TEMPLATE = """# {task_name} - Tasks

**Status:** In Progress
**Started:** {date}
**Last Updated:** {date}
**Remaining:** All tasks pending

---

## Overview

{description}

---

## Tasks

### Phase 1: Setup
- [ ] 1. First task - Define acceptance criteria
- [ ] 2. Second task - Define acceptance criteria

### Phase 2: Implementation
- [ ] 3. Implementation task - Define acceptance criteria
- [ ] 4. Another task - Define acceptance criteria

### Phase 3: Validation
- [ ] 5. Typecheck passes
- [ ] 6. Tests pass
- [ ] 7. Manual verification (if applicable)

---

## Notes

- Add any blockers or decisions needed here
"""

CONTEXT_TEMPLATE = """# {task_name} - Context

**Last Updated:** {date}

## Description

{description}

## Definition of Done

- TBD

## Constraints

- Any limitations or requirements
- Tech stack constraints
- Performance requirements

## Key Learnings

(missioncache-auto will add important discoveries here)

## Blockers

(missioncache-auto will add blocking issues here if encountered)

## Gotchas

(Things to watch out for - missioncache-auto will read this)

## Waiting on

External replies/events that gate work. Check on every resume; when one resolves, act on what it gates and move the row into Recent Changes.

| What | Who | Since | Gates |
|------|-----|-------|-------|

## Next Steps

1. TBD

## Recent Changes

### {date}

- Created MissionCache files

## Key Architectural Decisions

- TBD

## Key Files

| File | Purpose |
|------|---------|
| `path/to/file.py` | Description |
"""

PLAN_TEMPLATE = """# {task_name} - Plan

**Created:** {date}
**Last Updated:** {date}

## Overview

{description}

## Implementation Approach

(Describe the high-level approach)

## Technical Design

(Add technical details, diagrams, API designs, etc.)

## Phases

### Phase 1: Foundation
- Goals
- Key decisions
- Dependencies

### Phase 2: Implementation
- Goals
- Key decisions
- Dependencies

### Phase 3: Validation
- Testing strategy
- Acceptance criteria

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Risk 1 | How to mitigate |

## Open Questions

- Question 1?
- Question 2?
"""

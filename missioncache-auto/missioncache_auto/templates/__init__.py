"""
Templates for orbit-auto task initialization.

These templates are used when creating new tasks with `orbit-auto init`.
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

## Key Files

| File | Purpose |
|------|---------|
| `path/to/file.py` | Description |

## Architecture Decisions

(Add important decisions about how to approach this)

## Constraints

- Any limitations or requirements
- Tech stack constraints
- Performance requirements

## Gotchas

(Things to watch out for - orbit-auto will read this)

---

## Key Learnings

(orbit-auto will add important discoveries here)

## Blockers

(orbit-auto will add blocking issues here if encountered)
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

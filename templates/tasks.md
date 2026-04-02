# {{task_name}} - Tasks

**Status:** In Progress
**Started:** {{timestamp}}
**Last Updated:** {{timestamp}}
**Remaining:** {{remaining}}

---

## Task Numbering Format (Orbit-Auto Compatible)

Tasks use numbered format for prompt alignment:
- Flat: `- [ ] 1. Task description`
- Hierarchical: `- [ ] 1.1. Subtask description`

Prompts match by number:
- `task-01-prompt.md` → `- [ ] 1. ...`
- `task-01-02-prompt.md` → `- [ ] 1.2. ...`

---

## Phase 1: Implementation

{{tasks}}

## Phase 2: Validation

- [ ] Typecheck passes
- [ ] Tests pass

## Notes

- TBD

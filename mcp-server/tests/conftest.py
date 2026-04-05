"""Shared fixtures for orbit MCP server tests."""

import pytest


@pytest.fixture
def sample_tasks_md():
    """Sample tasks.md content with mixed completion states."""
    return """\
# Tasks - My Project

**Last Updated:** 2026-04-01 10:00
**Remaining:** 3 tasks pending

## Tasks

- [x] 1. Set up project structure
- [x] 2. Add configuration module
- [ ] 3. Implement core logic
- [ ] 4. Write tests
- [ ] 5. Add documentation
"""


@pytest.fixture
def sample_tasks_md_all_done():
    """Tasks.md content with all tasks completed."""
    return """\
# Tasks - My Project

**Last Updated:** 2026-04-01 10:00
**Remaining:** 0 tasks pending

## Tasks

- [x] 1. Set up project structure
- [x] 2. Add configuration module
- [x] 3. Implement core logic
"""


@pytest.fixture
def sample_tasks_md_hierarchical():
    """Tasks.md content with hierarchical (parent.child) task numbering."""
    return """\
# Tasks - My Project

## Tasks

- [x] 1. Infrastructure
  - [x] 1.1. Set up CI
  - [ ] 1.2. Add linting
- [ ] 2. Features
  - [ ] 2.1. User auth
  - [ ] 2.2. API endpoints
"""


@pytest.fixture
def sample_context_md():
    """Sample context.md content."""
    return """\
# Context - My Project

**Last Updated:** 2026-04-01 10:00
**Description:** A sample project

## Next Steps

1. Implement core logic
2. Write tests

## Key Architectural Decisions

- Use pydantic for models

## Gotchas

- Config requires env vars
"""


@pytest.fixture
def sample_iteration_log():
    """Sample iteration log content."""
    return """\
# Iteration Log - my-task

**Started:** 2026-04-01
**Max Iterations:** 20

---

## Iteration 1 - Set up structure
**Status:** SUCCESS
**Time:** 2026-04-01 10:00:00

### What was done
- Created project scaffolding

## Iteration 2 - Add config
**Status:** FAILED
**Time:** 2026-04-01 10:15:00

### What was attempted
- Tried to add config module

### Error details
Import error in config.py

## Iteration 3 - Fix config
**Status:** SUCCESS
**Time:** 2026-04-01 10:30:00

### What was done
- Fixed import issue
"""


@pytest.fixture
def sample_iteration_log_completed():
    """Iteration log with completion marker."""
    return """\
# Iteration Log - my-task

**Started:** 2026-04-01
**Max Iterations:** 20

---

## Iteration 1 - Task A
**Status:** SUCCESS
**Time:** 2026-04-01 10:00:00

---

# COMPLETED
**Finished:** 2026-04-01 11:00
**Total iterations:** 1
**Duration:** 3600s
"""


@pytest.fixture
def sample_prompt_content():
    """Sample prompt file with YAML frontmatter."""
    return """\
---
task_id: "01"
title: Set up project
---

# Task 1: Set up project

Create the initial project structure.
"""


@pytest.fixture
def sample_prompt_content_subtask():
    """Sample prompt file for a subtask."""
    return """\
---
task_id: "01-02"
title: Add linting
---

# Task 1.2: Add linting

Configure linting tools.
"""

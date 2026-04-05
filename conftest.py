"""Shared fixtures for orbit project tests."""

import pytest


@pytest.fixture
def sample_tasks_md_content():
    """Standard 5-task markdown for cross-component parser tests."""
    return """\
# Tasks

## Phase 1: Foundation

- [x] 01 - Set up project structure [auto]
- [ ] 02 - Implement core database [auto]
- [ ] 03 - Add API endpoints [auto:depends=02]

## Phase 2: Features

- [ ] 04 - Build dashboard UI [inter]
- [ ] 05 - Add monitoring [auto:depends=03,04]
"""


@pytest.fixture
def sample_prompt_content():
    """Prompt YAML frontmatter fixture."""
    return """\
---
task_id: "03"
depends:
  - "02"
acceptance_criteria:
  - API returns 200 for valid requests
  - Error handling for invalid input
---

Implement the API endpoints for the core service.

## Requirements
- REST API with CRUD operations
- Input validation
- Error responses with proper status codes
"""

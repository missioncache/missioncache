"""Shared fixtures for MissionCache project tests."""

import pytest


@pytest.fixture(autouse=True)
def _neutralize_legacy_data_paths(tmp_path, monkeypatch):
    """Point every legacy data-path constant at a non-existent tmp location so
    the migration guard (``check_legacy_paths``, run in ``TaskDB.__init__``)
    never fires against a developer's real ``~/.orbit`` or ``~/.claude`` data.

    The guard reads the module-level ``_LEGACY_*`` constants at call time;
    without this, every test that constructs a ``TaskDB`` on a machine that
    still has ``~/.orbit/tasks.db`` would raise ``MissionCacheMigrationRequired``.
    Guard-behavior tests (test_legacy_guard.py, test_db_logger_init.py) request
    their own fixtures that override these with controlled paths.
    """
    try:
        import missioncache_db
    except ImportError:
        return
    for name in (
        "_LEGACY_CLAUDE_DB",
        "_LEGACY_CLAUDE_ORBIT_ROOT",
        "_LEGACY_ORBIT_DB",
        "_LEGACY_ORBIT_ROOT",
    ):
        monkeypatch.setattr(missioncache_db, name, tmp_path / f"no-{name}")


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

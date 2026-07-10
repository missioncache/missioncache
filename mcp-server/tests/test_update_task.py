"""Integration tests for the update_task MCP tool.

update_task closes the post-creation set-jira/set-category gap
conversationally: optional jira_key + category fields, 'none' sentinel
to clear, category validated against CATEGORIES BEFORE any write so an
invalid value never leaves a half-applied update.

Uses the shared isolated_orbit fixture from conftest.py.
"""

from __future__ import annotations

import asyncio

from mcp_missioncache import db as db_module
from mcp_missioncache import tools_tasks


def _create(name: str, **kwargs) -> int:
    result = asyncio.run(tools_tasks.create_task(name=name, task_type="non-coding", **kwargs))
    assert result.get("error") is None, result
    return result["task_id"]


def _update(**kwargs) -> dict:
    return asyncio.run(tools_tasks.update_task(**kwargs))


class TestUpdateTaskFields:
    def test_set_jira_only(self, isolated_orbit):
        tid = _create("jira-only")

        result = _update(task_id=tid, jira_key="PROJ-123")

        assert result.get("error") is None
        assert result["jira_key"] == "PROJ-123"
        assert result["updated"] == ["jira_key"]
        assert db_module.get_db().get_task(tid).jira_key == "PROJ-123"

    def test_set_category_only(self, isolated_orbit):
        tid = _create("category-only")

        result = _update(task_id=tid, category="infra")

        assert result.get("error") is None
        assert result["category"] == "infra"
        assert result["updated"] == ["category"]
        assert db_module.get_db().get_task(tid).category == "infra"

    def test_set_both_fields(self, isolated_orbit):
        tid = _create("both-fields")

        result = _update(task_id=tid, jira_key="PROJ-9", category="ui")

        assert result.get("error") is None
        assert result["jira_key"] == "PROJ-9"
        assert result["category"] == "ui"
        assert sorted(result["updated"]) == ["category", "jira_key"]

    def test_none_sentinel_clears_case_insensitively(self, isolated_orbit):
        tid = _create("clear-me", jira_key="PROJ-1", category="bug")

        result = _update(task_id=tid, jira_key="None", category="NONE")

        assert result.get("error") is None
        assert result["jira_key"] is None
        assert result["category"] is None
        task = db_module.get_db().get_task(tid)
        assert task.jira_key is None
        assert task.category is None

    def test_resolve_by_project_name(self, isolated_orbit):
        _create("named-target")

        result = _update(project_name="named-target", category="docs")

        assert result.get("error") is None
        assert result["task_name"] == "named-target"
        assert result["category"] == "docs"

    def test_clear_jira_only_leaves_category_untouched(self, isolated_orbit):
        """Clearing one field must not touch the other, and `updated`
        names only the field this call changed."""
        tid = _create("partial-clear", jira_key="PROJ-7", category="docs")

        result = _update(task_id=tid, jira_key="none")

        assert result.get("error") is None
        assert result["jira_key"] is None
        assert result["category"] == "docs"
        assert result["updated"] == ["jira_key"]
        assert db_module.get_db().get_task(tid).category == "docs"


class TestUpdateTaskValidation:
    def test_no_fields_rejected(self, isolated_orbit):
        tid = _create("no-fields")

        result = _update(task_id=tid)

        assert result.get("error") is True
        assert result.get("code") == "VALIDATION_ERROR"

    def test_no_identifier_rejected(self, isolated_orbit):
        result = _update(jira_key="PROJ-1")

        assert result.get("error") is True
        assert result.get("code") == "VALIDATION_ERROR"

    def test_unknown_task_rejected(self, isolated_orbit):
        _create("exists")  # ensure DB is initialized

        result = _update(task_id=99999, category="ui")

        assert result.get("error") is True
        assert result.get("code") == "TASK_NOT_FOUND"

    def test_unknown_project_name_rejected(self, isolated_orbit):
        _create("exists-too")  # ensure DB is initialized

        result = _update(project_name="no-such-project", category="ui")

        assert result.get("error") is True
        assert result.get("code") == "TASK_NOT_FOUND"

    def test_empty_jira_key_rejected(self, isolated_orbit):
        """An empty/whitespace jira_key must not persist as a ghost ""
        value (nor silently clear) - the explicit 'none' sentinel is the
        only clear path."""
        tid = _create("no-ghost", jira_key="PROJ-KEEP")

        result = _update(task_id=tid, jira_key="   ")

        assert result.get("error") is True
        assert result.get("code") == "VALIDATION_ERROR"
        assert db_module.get_db().get_task(tid).jira_key == "PROJ-KEEP"

    def test_custom_category_accepted(self, isolated_orbit):
        """A category defined in custom_categories passes update_task's
        pre-flight. (The creation tools carry their own copies of this
        check, each tested in its own file - the checks are NOT shared.)"""
        db_module.get_db().add_custom_category("research", "🔬", "#4dabf7")
        tid = _create("custom-cat-target")

        result = _update(task_id=tid, category="research")

        assert result.get("error") is None
        assert result["category"] == "research"
        assert db_module.get_db().get_task(tid).category == "research"

    def test_invalid_category_rejected_before_jira_write(self, isolated_orbit):
        """Atomicity: a valid jira_key paired with an invalid category
        must NOT half-apply - the jira_key stays untouched."""
        tid = _create("atomic", jira_key="PROJ-OLD")

        result = _update(task_id=tid, jira_key="PROJ-NEW", category="sparkles")

        assert result.get("error") is True
        assert result.get("code") == "VALIDATION_ERROR"
        task = db_module.get_db().get_task(tid)
        assert task.jira_key == "PROJ-OLD"
        assert task.category is None

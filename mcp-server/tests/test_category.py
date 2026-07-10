"""Integration tests for the category param on create_task / create_missioncache_files.

Category is assigned at creation time (the LLM derives it from the project
description in /missioncache:new) and validated against missioncache_db.CATEGORIES.
The two creation paths store it differently:

  - ``create_task`` passes it straight into ``TaskDB.create_task``.
  - ``create_missioncache_files`` registers the task via ``scan_all_repos`` (which
    knows nothing about category), so the tool sets it post-scan via
    ``TaskDB.set_task_category``.
"""

from __future__ import annotations

import asyncio

from missioncache_db import CATEGORIES

from mcp_missioncache import db as db_module
from mcp_missioncache import tools_docs, tools_tasks

# The isolated_orbit fixture lives in conftest.py (shared with
# test_update_task.py).


# ── create_task ───────────────────────────────────────────────────────────


class TestCreateTaskCategory:
    def test_category_stored_and_echoed(self, isolated_orbit):
        """A valid category is stored on the row and echoed in the result."""
        tmp_path, _root_dir, _home = isolated_orbit
        repo_path = tmp_path / "repo"
        repo_path.mkdir()

        result = asyncio.run(
            tools_tasks.create_task(
                name="dashboard-filter-bar",
                repo_path=str(repo_path),
                category="ui",
            )
        )

        assert result.get("error") is None
        assert result["category"] == "ui"
        assert db_module.get_db().get_task(result["task_id"]).category == "ui"

    def test_omitted_category_is_null(self, isolated_orbit):
        """No category -> stored NULL, result echoes None."""
        tmp_path, _root_dir, _home = isolated_orbit
        repo_path = tmp_path / "repo"
        repo_path.mkdir()

        result = asyncio.run(
            tools_tasks.create_task(name="uncategorized", repo_path=str(repo_path))
        )

        assert result.get("error") is None
        assert result["category"] is None

    def test_invalid_category_rejected(self, isolated_orbit):
        """A category outside the taxonomy fails validation before any write."""
        tmp_path, _root_dir, _home = isolated_orbit
        repo_path = tmp_path / "repo"
        repo_path.mkdir()

        result = asyncio.run(
            tools_tasks.create_task(
                name="bad-category",
                repo_path=str(repo_path),
                category="sparkles",
            )
        )

        assert result.get("error") is True
        assert result.get("code") == "VALIDATION_ERROR"
        assert db_module.get_db().get_task_by_name("bad-category") is None


# ── create_missioncache_files ─────────────────────────────────────────────


class TestCreateMissionCacheFilesCategory:
    def test_category_set_on_scanned_task(self, isolated_orbit):
        """The scan-registered task row carries the category post-creation."""
        tmp_path, _root_dir, _home = isolated_orbit
        repo_path = tmp_path / "repo"
        repo_path.mkdir()

        result = asyncio.run(
            tools_docs.create_missioncache_files(
                repo_path=str(repo_path),
                project_name="categorized-project",
                category="infra",
                resolve_git_root=False,
            )
        )

        assert result.get("success") is True
        assert result["category"] == "infra"
        assert (
            db_module.get_db().get_task_by_name("categorized-project").category
            == "infra"
        )

    def test_omitted_category_stays_null(self, isolated_orbit):
        """No category -> the scanned row stays NULL (heuristic fallback territory)."""
        tmp_path, _root_dir, _home = isolated_orbit
        repo_path = tmp_path / "repo"
        repo_path.mkdir()

        result = asyncio.run(
            tools_docs.create_missioncache_files(
                repo_path=str(repo_path),
                project_name="null-category-project",
                resolve_git_root=False,
            )
        )

        assert result.get("success") is True
        assert result["category"] is None
        assert (
            db_module.get_db().get_task_by_name("null-category-project").category
            is None
        )

    def test_invalid_category_rejected_before_files_created(self, isolated_orbit):
        """Invalid category fails fast - no files land on disk."""
        tmp_path, root_dir, _home = isolated_orbit
        repo_path = tmp_path / "repo"
        repo_path.mkdir()

        result = asyncio.run(
            tools_docs.create_missioncache_files(
                repo_path=str(repo_path),
                project_name="rejected-project",
                category="not-real",
                resolve_git_root=False,
            )
        )

        assert result.get("error") is True
        assert result.get("code") == "VALIDATION_ERROR"
        assert not (root_dir / "active" / "rejected-project").exists()


# ── read-path exposure ────────────────────────────────────────────────────


def test_task_summary_exposes_category(isolated_orbit):
    """_task_to_summary carries category into TaskSummary - the read path
    that list_active_tasks / get_task responses surface."""
    from mcp_missioncache.helpers import _task_to_summary

    tmp_path, _root_dir, _home = isolated_orbit
    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    result = asyncio.run(
        tools_tasks.create_task(
            name="summary-exposure",
            repo_path=str(repo_path),
            category="perf",
        )
    )
    task = db_module.get_db().get_task(result["task_id"])

    summary = _task_to_summary(task)

    assert summary.category == "perf"


# ── taxonomy sanity ───────────────────────────────────────────────────────


def test_every_category_accepted_by_create_task(isolated_orbit):
    """All 13 taxonomy values pass validation end-to-end."""
    tmp_path, _root_dir, _home = isolated_orbit
    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    for cat in CATEGORIES:
        result = asyncio.run(
            tools_tasks.create_task(
                name=f"taxonomy-{cat}",
                repo_path=str(repo_path),
                category=cat,
            )
        )
        assert result.get("error") is None, f"category {cat!r} rejected"
        assert result["category"] == cat

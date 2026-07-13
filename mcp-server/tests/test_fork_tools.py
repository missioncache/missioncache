"""Integration tests for the project-fork surface of the MCP tools.

Spec source: the fork feature contract - a child project carries a
``**Fork of:** <parent>`` header in its context file, the parent's context
file is the child's shared knowledge layer (readable even after the parent
completes), ``get_context_digest`` on a child returns a ``parent_digest``
block with a ``changed_since_seen`` freshness flag, and ``complete_task``
on a parent with active children warns instead of blocking.

Async tools called via ``asyncio.run`` (test_context_digest.py pattern).
"""

import asyncio
import os
import time

import pytest

from mcp_missioncache import tools_docs, tools_tasks
from mcp_missioncache.project_files import _inject_fork_header


# ── _inject_fork_header (pure) ────────────────────────────────────────────


class TestInjectForkHeader:
    def test_placed_right_after_last_updated(self):
        content = "# Child - Context\n\n**Last Updated:** now\n\n## Description\n"
        out = _inject_fork_header(content, "parent-proj")
        lines = out.splitlines()
        idx = lines.index("**Last Updated:** now")
        assert lines[idx + 1] == "**Fork of:** parent-proj"
        # Still in the header region: before the first section.
        assert out.index("**Fork of:**") < out.index("## Description")

    def test_fallback_after_h1_without_last_updated(self):
        content = "# Child - Context\n\n## Description\n"
        out = _inject_fork_header(content, "parent-proj")
        assert out.index("**Fork of:**") < out.index("## Description")


# ── fixtures ──────────────────────────────────────────────────────────────


def _create(repo_path, name, **kwargs):
    return asyncio.run(
        tools_docs.create_missioncache_files(
            repo_path=str(repo_path),
            project_name=name,
            resolve_git_root=False,
            **kwargs,
        )
    )


@pytest.fixture()
def fork_pair(isolated_orbit):
    """A parent project and a child forked from it, via the real tool."""
    tmp_path, root_dir, _home = isolated_orbit
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    parent = _create(repo_path, "parent-proj")
    child = _create(repo_path, "child-proj", fork_of="parent-proj")
    return repo_path, root_dir, parent, child


# ── create_missioncache_files(fork_of=...) ────────────────────────────────


class TestCreateFork:
    def test_header_written_and_linked(self, fork_pair):
        _repo, root_dir, parent, child = fork_pair
        assert child["success"] is True
        ctx = (root_dir / "active" / "child-proj" / "child-proj-context.md").read_text()
        assert "**Fork of:** parent-proj" in ctx
        # The header sits in the header region, before the first section.
        assert ctx.index("**Fork of:**") < ctx.index("## ")
        assert child["fork_of"] == "parent-proj"
        assert child["fork_linked"] is True

    def test_missing_parent_warns_but_creates(self, isolated_orbit):
        tmp_path, root_dir, _home = isolated_orbit
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        child = _create(repo_path, "orphan-child", fork_of="no-such-parent")
        assert child["success"] is True
        assert child["fork_linked"] is False
        assert "fork_warning" in child
        ctx = (root_dir / "active" / "orphan-child" / "orphan-child-context.md").read_text()
        assert "**Fork of:** no-such-parent" in ctx

    def test_self_fork_rejected(self, isolated_orbit):
        tmp_path, root_dir, _home = isolated_orbit
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        result = _create(repo_path, "selfie", fork_of="selfie")
        assert result.get("error") is True
        assert not (root_dir / "active" / "selfie").exists()


# ── get_context_digest parent_digest ──────────────────────────────────────


class TestParentDigest:
    def test_child_gets_parent_digest(self, fork_pair):
        _repo, _root, _parent, _child = fork_pair
        result = asyncio.run(tools_docs.get_context_digest(project_name="child-proj"))
        assert result["success"] is True
        pd = result["parent_digest"]
        assert pd is not None
        assert pd["name"] == "parent-proj"
        assert pd["context_file"].endswith("parent-proj-context.md")
        assert pd["context_mtime"] > 0
        assert pd["changed_since_seen"] is None  # no seen_mtime passed

    def test_non_fork_has_no_parent_digest(self, fork_pair):
        result = asyncio.run(tools_docs.get_context_digest(project_name="parent-proj"))
        assert result["success"] is True
        assert result["parent_digest"] is None

    def test_changed_since_seen_flags(self, fork_pair):
        _repo, root_dir, _parent, _child = fork_pair
        parent_ctx = root_dir / "active" / "parent-proj" / "parent-proj-context.md"
        mtime = parent_ctx.stat().st_mtime

        fresh = asyncio.run(
            tools_docs.get_context_digest(project_name="child-proj", seen_mtime=mtime)
        )
        assert fresh["parent_digest"]["changed_since_seen"] is False

        stale = asyncio.run(
            tools_docs.get_context_digest(
                project_name="child-proj", seen_mtime=mtime - 100.0
            )
        )
        assert stale["parent_digest"]["changed_since_seen"] is True

    def test_parallel_session_update_flips_flag(self, fork_pair):
        """The requirement itself: child B sees that the shared layer changed
        after its last sync."""
        _repo, root_dir, _parent, _child = fork_pair
        parent_ctx = root_dir / "active" / "parent-proj" / "parent-proj-context.md"
        seen = parent_ctx.stat().st_mtime

        parent_ctx.write_text(parent_ctx.read_text() + "\n- shared update\n")
        # Deterministic strictly-newer mtime (no sleep/granularity dependency).
        os.utime(parent_ctx, (seen + 10, seen + 10))

        result = asyncio.run(
            tools_docs.get_context_digest(project_name="child-proj", seen_mtime=seen)
        )
        assert result["parent_digest"]["changed_since_seen"] is True

    def test_unresolvable_parent_yields_null_not_error(self, isolated_orbit):
        tmp_path, _root, _home = isolated_orbit
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        _create(repo_path, "orphan-child", fork_of="no-such-parent")
        result = asyncio.run(tools_docs.get_context_digest(project_name="orphan-child"))
        assert result["success"] is True
        assert result["parent_digest"] is None

    def test_parent_resolves_from_completed(self, fork_pair):
        """Completing the parent moves its files to completed/ - the shared
        layer must stay reachable for the child (core requirement)."""
        _repo, root_dir, parent, _child = fork_pair
        done = asyncio.run(
            tools_tasks.complete_task(task_id=parent["task_id"], move_files=True)
        )
        assert done.get("error") is None
        assert (root_dir / "completed" / "parent-proj").exists()

        result = asyncio.run(tools_docs.get_context_digest(project_name="child-proj"))
        assert result["success"] is True
        pd = result["parent_digest"]
        assert pd is not None
        assert "completed" in pd["context_file"]


# ── complete_task fork warning ────────────────────────────────────────────


class TestCompleteParentWarning:
    def test_parent_with_active_child_warns(self, fork_pair):
        _repo, _root, parent, _child = fork_pair
        result = asyncio.run(
            tools_tasks.complete_task(task_id=parent["task_id"], move_files=True)
        )
        assert result["active_children_count"] == 1
        assert "child-proj" in result["warning"]

    def test_childless_completion_has_no_warning(self, fork_pair):
        _repo, _root, _parent, child = fork_pair
        result = asyncio.run(
            tools_tasks.complete_task(task_id=child["task_id"], move_files=True)
        )
        assert result["active_children_count"] == 0
        assert "warning" not in result


# ── adversarial: advisory failures + stale-link certification ────────────


class TestCompleteWarningNonFatal:
    def test_children_lookup_failure_keeps_completion_success(
        self, fork_pair, monkeypatch
    ):
        """The children query is advisory: if it raises AFTER completion
        committed, the tool must still report the completion, not an error."""
        from mcp_missioncache import db as db_module

        _repo, root_dir, parent, _child = fork_pair
        real_db = db_module.get_db()

        def boom():
            raise RuntimeError("sqlite contention")

        monkeypatch.setattr(real_db, "get_active_tasks", boom)
        result = asyncio.run(
            tools_tasks.complete_task(task_id=parent["task_id"], move_files=True)
        )
        assert result.get("error") is None
        assert result["new_status"] == "completed"
        assert result["active_children_count"] is None
        # And the files really moved - completion committed.
        assert (root_dir / "completed" / "parent-proj").exists()


class TestForkLinkedCertifiesRequestedParent:
    def test_force_recreate_to_missing_parent_not_certified(self, fork_pair):
        """force re-create pointing at an unresolvable parent must NOT report
        fork_linked=true off the surviving old link."""
        repo_path, root_dir, _parent, _child = fork_pair
        result = _create(
            repo_path, "child-proj", fork_of="ghost-parent", force=True
        )
        assert result["success"] is True
        assert result["fork_linked"] is False
        assert "fork_warning" in result
        # Header now names the new (unresolvable) parent.
        ctx = (root_dir / "active" / "child-proj" / "child-proj-context.md").read_text()
        assert "**Fork of:** ghost-parent" in ctx


# ── review follow-ups: multi-child warning, is_fork/fork_parent_error fields ─


class TestCompleteMultiChildWarning:
    def test_warning_lists_all_active_children(self, isolated_orbit):
        """The plural join path (", ".join child names) must run - a single
        child never exercises it."""
        tmp_path, root_dir, _home = isolated_orbit
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        parent = _create(repo_path, "parent-proj")
        _create(repo_path, "child-a", fork_of="parent-proj")
        _create(repo_path, "child-b", fork_of="parent-proj")

        result = asyncio.run(
            tools_tasks.complete_task(task_id=parent["task_id"], move_files=True)
        )
        assert result["active_children_count"] == 2
        assert "child-a" in result["warning"]
        assert "child-b" in result["warning"]


class TestDigestForkFields:
    def test_non_fork_reports_is_fork_false_no_error(self, fork_pair):
        result = asyncio.run(tools_docs.get_context_digest(project_name="parent-proj"))
        assert result["is_fork"] is False
        assert result["parent_digest"] is None
        assert result["fork_parent_error"] is None

    def test_fork_reports_is_fork_true(self, fork_pair):
        result = asyncio.run(tools_docs.get_context_digest(project_name="child-proj"))
        assert result["is_fork"] is True
        assert result["parent_digest"] is not None
        assert result["fork_parent_error"] is None

    def test_unreadable_parent_is_fork_true_with_error(self, isolated_orbit):
        """A fork whose parent has no readable context is distinguishable from
        a plain project: is_fork True, parent_digest null, error set."""
        tmp_path, root_dir, _home = isolated_orbit
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        # Parent project dir exists but its context file is missing.
        _create(repo_path, "child-proj", fork_of="ghost-parent")
        result = asyncio.run(tools_docs.get_context_digest(project_name="child-proj"))
        assert result["is_fork"] is True
        assert result["parent_digest"] is None
        assert result["fork_parent_error"] is not None

    def test_non_fork_digest_shape_intact(self, fork_pair):
        """Adding the fork fields must not drop the existing digest keys that
        /missioncache:load consumers depend on."""
        result = asyncio.run(tools_docs.get_context_digest(project_name="parent-proj"))
        for key in (
            "last_updated", "hub", "fork_of", "related_projects", "waiting_on",
            "next_steps", "recent_changes_last3", "section_index",
            "file_size_bytes", "health_warnings",
        ):
            assert key in result

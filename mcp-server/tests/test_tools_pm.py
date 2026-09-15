"""Tests for the PM MCP tools (action items / stakeholders / tickets / due date).

Spec source: the missioncache-pm-layer design (DoD bullets 1-2, 5): tools are
thin wrappers over pm_items - a mutation lands in SQLite AND re-renders the
context-file mirror; list works project-scoped and cross-project; unknown
projects yield structured errors, never exceptions; ticket URL falls back to
the user's JIRA prefix map only when omitted.

Async wrappers called via ``asyncio.run`` (same pattern as
test_context_digest.py).
"""

import asyncio

import pytest

from mcp_missioncache import db as db_module, tools_pm


CONTEXT = """# Pm Proj - Context

**Last Updated:** 2026-07-10 12:00

## Description

PM tools test project.

## Gotchas

- TBD

## Waiting on

| What | Who | Since | Gates |
|------|-----|-------|-------|

## Next Steps

1. TBD

## Recent Changes

### 2026-07-10 11:00

- created
"""


@pytest.fixture
def pm_project(isolated_orbit):
    """A registered coding task with a context file under the tmp root."""
    _, root_dir, _ = isolated_orbit
    project_dir = root_dir / "active" / "pm-proj"
    project_dir.mkdir(parents=True)
    ctx = project_dir / "pm-proj-context.md"
    ctx.write_text(CONTEXT)
    db = db_module.get_db()
    task = db.create_task("pm-proj", task_type="coding", repo_id=None)
    return task, ctx


class TestActionItemTools:
    def test_add_and_list_project_scope(self, pm_project):
        task, ctx = pm_project
        result = asyncio.run(tools_pm.add_action_item(
            project_name="pm-proj", what="send the coverage numbers",
            requested_by="Lior", due_date="2099-01-01", source="weekly 24/07",
        ))
        assert result["success"] is True
        assert result["item"]["id"] == 1

        listed = asyncio.run(tools_pm.list_action_items(project_name="pm-proj"))
        assert listed["count"] == 1
        assert listed["items"][0]["label"] == "AI-1"
        assert listed["items"][0]["overdue"] is False
        # The mirror rendered through the MCP layer (DoD round-trip, session side).
        content = ctx.read_text()
        assert "## Action Items" in content
        assert "send the coverage numbers" in content

    def test_cross_project_scope_carries_project_name(self, pm_project, isolated_orbit):
        _, root_dir, _ = isolated_orbit
        db = db_module.get_db()
        other_dir = root_dir / "active" / "other-proj"
        other_dir.mkdir(parents=True)
        (other_dir / "other-proj-context.md").write_text(CONTEXT)
        db.create_task("other-proj", task_type="coding", repo_id=None)

        asyncio.run(tools_pm.add_action_item(project_name="pm-proj", what="one"))
        asyncio.run(tools_pm.add_action_item(project_name="other-proj", what="two"))
        listed = asyncio.run(tools_pm.list_action_items())
        assert listed["count"] == 2
        assert {i["project_name"] for i in listed["items"]} == {"pm-proj", "other-proj"}

    def test_complete_with_outcome(self, pm_project):
        task, ctx = pm_project
        asyncio.run(tools_pm.add_action_item(project_name="pm-proj", what="do it"))
        result = asyncio.run(tools_pm.update_action_item(
            item_id=1, status="done", notes="sent by mail"
        ))
        assert result["success"] is True
        assert result["item"]["status"] == "done"
        assert result["item"]["completed_at"] is not None
        assert "Action item done (AI-1)" in ctx.read_text()

    def test_due_date_clear_sentinel(self, pm_project):
        asyncio.run(tools_pm.add_action_item(
            project_name="pm-proj", what="x", due_date="2099-01-01"
        ))
        result = asyncio.run(tools_pm.update_action_item(item_id=1, due_date="none"))
        assert result["item"]["due_date"] is None

    def test_unknown_project_structured_error(self, isolated_orbit):
        result = asyncio.run(tools_pm.add_action_item(
            project_name="no-such-proj", what="x"
        ))
        assert result.get("error") is True
        assert result.get("code") == "TASK_NOT_FOUND"

    def test_bad_due_date_validation_error(self, pm_project):
        result = asyncio.run(tools_pm.add_action_item(
            project_name="pm-proj", what="x", due_date="next tuesday"
        ))
        assert result.get("error") is True
        assert result.get("code") == "VALIDATION_ERROR"


class TestStakeholderAndTicketTools:
    def test_stakeholder_upsert_and_remove(self, pm_project):
        task, ctx = pm_project
        asyncio.run(tools_pm.set_stakeholder(
            project_name="pm-proj", name="Keren", role="SIA lead"
        ))
        result = asyncio.run(tools_pm.set_stakeholder(
            project_name="pm-proj", name="Keren", role="SIA manager"
        ))
        assert result["stakeholder"]["role"] == "SIA manager"
        assert "## Stakeholders" in ctx.read_text()

        removed = asyncio.run(tools_pm.set_stakeholder(
            project_name="pm-proj", name="Keren", remove=True
        ))
        assert removed == {"success": True, "removed": True}

    def test_ticket_url_fallback_only_when_omitted(self, pm_project, monkeypatch):
        from missioncache_db import pm_items

        monkeypatch.setattr(pm_items, "jira_url_for", lambda label: f"https://map/{label}")
        with_url = asyncio.run(tools_pm.set_ticket(
            project_name="pm-proj", label="MON-7", url="https://monday.com/7"
        ))
        assert with_url["ticket"]["url"] == "https://monday.com/7"

        mapped = asyncio.run(tools_pm.set_ticket(project_name="pm-proj", label="GC-1"))
        assert mapped["ticket"]["url"] == "https://map/GC-1"

    def test_ticket_remove(self, pm_project):
        asyncio.run(tools_pm.set_ticket(project_name="pm-proj", label="GC-2"))
        result = asyncio.run(tools_pm.set_ticket(
            project_name="pm-proj", label="GC-2", remove=True
        ))
        assert result == {"success": True, "removed": True}


class TestProjectDueDateTool:
    def test_set_and_clear(self, pm_project):
        task, ctx = pm_project
        result = asyncio.run(tools_pm.set_project_due_date(
            project_name="pm-proj", due_date="2099-06-30"
        ))
        assert result == {"success": True, "task_id": task.id, "due_date": "2099-06-30"}
        assert "**Due:** 2099-06-30" in ctx.read_text()

        cleared = asyncio.run(tools_pm.set_project_due_date(
            project_name="pm-proj", due_date="none"
        ))
        assert cleared["due_date"] is None
        assert "**Due:**" not in ctx.read_text()


class TestPmWritersNotifyPeers:
    """Spec source: the cross-session notifications rule - a session whose
    context file just changed under it gets told. Every PM writer re-renders
    the project's context mirror, so the notification contract cannot hold
    only for update_context_file.
    """

    @pytest.fixture
    def one_peer(self, monkeypatch):
        """Fake exactly one live peer on any project; capture the lookups."""
        import missioncache_db

        calls = []

        def _fake(project_name, exclude_session_id=None):
            calls.append(project_name)
            return [{"session_id": "sid-peer", "title": project_name,
                     "last_active": "now"}]

        monkeypatch.setattr(missioncache_db, "live_sessions_for_project", _fake)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sid-mine")
        return calls

    def test_add_action_item_returns_live_sessions(self, pm_project, one_peer):
        result = asyncio.run(
            tools_pm.add_action_item(project_name="pm-proj", what="do the thing")
        )
        assert result["success"] is True
        assert [p["session_id"] for p in result["live_sessions"]] == ["sid-peer"]
        assert one_peer == ["pm-proj"]

    def test_update_action_item_resolves_project_from_the_item(
        self, pm_project, one_peer
    ):
        """This tool addresses by item id, and the peer lookup must still hit
        the item's project."""
        added = asyncio.run(
            tools_pm.add_action_item(project_name="pm-proj", what="do the thing")
        )
        one_peer.clear()
        result = asyncio.run(
            tools_pm.update_action_item(item_id=added["item"]["id"], status="done")
        )
        assert result["success"] is True
        assert one_peer == ["pm-proj"]
        assert "live_sessions" in result

    def test_set_stakeholder_and_ticket_and_due_date_notify(
        self, pm_project, one_peer
    ):
        for coro in (
            tools_pm.set_stakeholder(project_name="pm-proj", name="Dana"),
            tools_pm.set_ticket(project_name="pm-proj", label="GC-1",
                                url="https://x/GC-1"),
            tools_pm.set_project_due_date(project_name="pm-proj",
                                          due_date="2026-09-01"),
        ):
            result = asyncio.run(coro)
            assert result["success"] is True
            assert "live_sessions" in result

    def test_no_peers_means_no_key(self, pm_project, monkeypatch):
        """The common case stays byte-identical to the pre-feature response."""
        import missioncache_db

        monkeypatch.setattr(
            missioncache_db, "live_sessions_for_project", lambda *a, **k: []
        )
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sid-mine")
        result = asyncio.run(
            tools_pm.add_action_item(project_name="pm-proj", what="x")
        )
        assert result["success"] is True
        assert "live_sessions" not in result

    def test_unresolvable_session_id_means_no_key(self, pm_project, monkeypatch):
        """Without its own id the caller cannot exclude itself, so the key is
        omitted rather than returned unfiltered."""
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        result = asyncio.run(
            tools_pm.add_action_item(project_name="pm-proj", what="x")
        )
        assert result["success"] is True
        assert "live_sessions" not in result

    def test_lookup_failure_does_not_fail_the_write(self, pm_project, monkeypatch):
        """The DB write already happened; a notification failure must not turn
        it into a failed tool call."""
        import missioncache_db

        def _boom(*_a, **_k):
            raise RuntimeError("lookup exploded")

        monkeypatch.setattr(missioncache_db, "live_sessions_for_project", _boom)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sid-mine")
        result = asyncio.run(
            tools_pm.add_action_item(project_name="pm-proj", what="x")
        )
        assert result["success"] is True
        assert "live_sessions" not in result

    def test_real_remove_notifies_noop_remove_does_not(self, pm_project, one_peer):
        """A remove that deleted a row rewrote the mirror; one that matched
        nothing wrote nothing, and a notify hint for it would be noise."""
        asyncio.run(tools_pm.set_stakeholder(project_name="pm-proj", name="Dana"))
        real = asyncio.run(
            tools_pm.set_stakeholder(project_name="pm-proj", name="Dana", remove=True)
        )
        assert real["removed"] is True
        assert [p["session_id"] for p in real["live_sessions"]] == ["sid-peer"]

        noop = asyncio.run(
            tools_pm.set_ticket(project_name="pm-proj", label="GC-404", remove=True)
        )
        assert noop["removed"] is False
        assert "live_sessions" not in noop

    def test_update_action_item_survives_a_raising_task_lookup(
        self, pm_project, one_peer, monkeypatch
    ):
        """A lookup that RAISES must not turn the committed update into an
        error response - it degrades to no hint, like a None return.

        The break is applied to the notification seam only: pm_items completes
        (the write committed), and the task lookup that follows it raises. A
        blanket get_task patch would also break the mirror refresh inside
        pm_items, which is a different failure than the one under test.
        """
        created = asyncio.run(
            tools_pm.add_action_item(project_name="pm-proj", what="x")
        )
        real_update = tools_pm.pm_items.update_action_item

        def _update_then_arm(db, item_id, **kwargs):
            item = real_update(db, item_id, **kwargs)

            def _boom(_task_id):
                raise RuntimeError("db exploded mid-lookup")

            monkeypatch.setattr(db, "get_task", _boom)
            return item

        monkeypatch.setattr(tools_pm.pm_items, "update_action_item", _update_then_arm)
        result = asyncio.run(
            tools_pm.update_action_item(item_id=created["item"]["id"], status="done")
        )
        assert result["success"] is True
        assert "live_sessions" not in result


class TestGetPortfolio:
    """Spec: the cross-project brief's data source. Same ranking and counts as
    the dashboard's /api/today (both come from missioncache_db.portfolio),
    clipped to a chat-sized payload, scoped to what is live or recent, and
    never binding the calling session to a project."""

    def _two_projects(self, isolated_orbit):
        import missioncache_db
        from missioncache_db import pm_items

        _, root_dir, _ = isolated_orbit
        db = db_module.get_db()
        made = {}
        for name, worked in (("fresh-proj", "2099-01-01 10:00:00"), ("stale-proj", "2020-01-01 10:00:00")):
            task = db.create_task(name, task_type="coding", repo_id=None)
            d = root_dir / "active" / name
            d.mkdir(parents=True)
            (d / f"{name}-context.md").write_text(CONTEXT.replace("Pm Proj", name))
            pm_items.add_action_item(db, task.id, f"late thing in {name}",
                                     due_date="2000-01-01")
            with db.connection() as conn:
                conn.execute("UPDATE tasks SET last_worked_on = ? WHERE id = ?", (worked, task.id))
                conn.commit()
            made[name] = task
        return made

    def test_focus_scope_keeps_recent_and_drops_stale(self, isolated_orbit):
        self._two_projects(isolated_orbit)
        result = asyncio.run(tools_pm.get_portfolio(scope="focus", recent_days=7))
        assert result["success"] is True
        assert [p["name"] for p in result["projects"]] == ["fresh-proj"]
        # counts re-derive from what survived, so the dropped project's overdue
        # item must not leak into the header numbers.
        assert result["counts"]["overdue"] == 1

    def test_all_scope_keeps_everything(self, isolated_orbit):
        self._two_projects(isolated_orbit)
        result = asyncio.run(tools_pm.get_portfolio(scope="all"))
        assert sorted(p["name"] for p in result["projects"]) == ["fresh-proj", "stale-proj"]
        assert result["counts"]["overdue"] == 2

    def test_bad_scope_is_a_structured_error(self, isolated_orbit):
        result = asyncio.run(tools_pm.get_portfolio(scope="everything"))
        assert result["error"] is True

    def test_payload_is_clipped_and_counts_are_not(self, isolated_orbit):
        from missioncache_db import pm_items

        made = self._two_projects(isolated_orbit)
        db = db_module.get_db()
        long_text = "x" * 500
        for _ in range(8):
            pm_items.add_action_item(db, made["fresh-proj"].id, long_text, due_date="2000-01-01")
        result = asyncio.run(tools_pm.get_portfolio(scope="all"))
        overdue_rows = result["on_me"]["overdue"]
        assert len(overdue_rows) == 5, "on_me buckets are capped per bucket"
        assert all(len(r["what"]) <= 120 for r in overdue_rows)
        # 9 overdue on fresh + 1 on stale: the cap trims the LIST, never the count.
        assert result["counts"]["overdue"] == 10

    def test_never_binds_the_calling_session(self, isolated_orbit, monkeypatch):
        """A project-manager session is not a project. get_task binds as a side
        effect; this tool must not, or the lead would take a project_state row
        and a live badge on whichever project it read last."""
        import missioncache_db
        import sqlite3

        self._two_projects(isolated_orbit)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "lead-sid")
        asyncio.run(tools_pm.get_portfolio())
        state_db = missioncache_db.HOOKS_STATE_DB_PATH
        if not state_db.exists():
            return  # nothing was written at all, which is the point
        conn = sqlite3.connect(state_db)
        try:
            rows = conn.execute(
                "SELECT COUNT(*) FROM project_state WHERE session_id = ?", ("lead-sid",)
            ).fetchone()[0]
        finally:
            conn.close()
        assert rows == 0

    def test_reports_the_live_lead_session(self, isolated_orbit, monkeypatch):
        import os
        import missioncache_db

        self._two_projects(isolated_orbit)
        # A lead whose pid is this test process: proven alive.
        pid_path = missioncache_db.session_pid_path("lead-sid")
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(
            '{"sessionId": "lead-sid", "pid": %d, "startTime": null}' % os.getpid()
        )
        missioncache_db.set_lead_session("lead-sid")
        result = asyncio.run(tools_pm.get_portfolio())
        assert result["lead_session"]["title"] == missioncache_db.LEAD_SESSION_TITLE

    def test_no_lead_is_none_not_missing(self, isolated_orbit):
        self._two_projects(isolated_orbit)
        result = asyncio.run(tools_pm.get_portfolio())
        assert "lead_session" in result and result["lead_session"] is None

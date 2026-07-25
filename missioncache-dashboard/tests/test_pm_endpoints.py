"""Tests for the PM endpoints (action items / stakeholders / tickets / due date / today).

Spec source: the missioncache-pm-layer design (DoD bullets 1, 6): dashboard
writes go through pm_items (the same path as the MCP tools), a UI mutation
re-renders the context-file mirror, and /api/today aggregates open items
across projects into overdue / due-soon buckets with a DERIVED at_risk flag.

Calls the endpoint functions directly (no TestClient / lifespan boot) with a
sandboxed missioncache-db, mirroring test_category_endpoint.py.
"""

from __future__ import annotations

import asyncio
import pathlib
from datetime import date, timedelta

import pytest
from fastapi import HTTPException

import missioncache_db
from missioncache_dashboard import server


CONTEXT = """# Pm Proj - Context

**Last Updated:** 2026-07-10 12:00

## Description

PM endpoint test project.

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
def sandboxed(tmp_path, monkeypatch):
    """Sandbox missioncache-db's filesystem layout (test_category_endpoint pattern)."""
    mc_root = tmp_path / ".missioncache"
    mc_root.mkdir()
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    db_path = tmp_path / "tasks.db"

    monkeypatch.setattr(missioncache_db, "MISSIONCACHE_ROOT", mc_root)
    monkeypatch.setattr(missioncache_db, "DB_PATH", db_path)
    monkeypatch.setattr(missioncache_db, "_LEGACY_CLAUDE_DB", tmp_path / "no-legacy-db")
    monkeypatch.setattr(missioncache_db, "_LEGACY_CLAUDE_ORBIT_ROOT", tmp_path / "no-legacy-orbit")
    monkeypatch.setattr(missioncache_db, "_LEGACY_ORBIT_DB", tmp_path / "no-legacy-orbit-db")
    monkeypatch.setattr(missioncache_db, "_LEGACY_ORBIT_ROOT", tmp_path / "no-legacy-orbit-root")
    monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: fake_home))
    return mc_root


@pytest.fixture
def project(sandboxed):
    """A registered coding task with a context file on disk."""
    db = missioncache_db.TaskDB()
    task = db.create_task("pm-proj", task_type="coding", repo_id=None)
    project_dir = sandboxed / "active" / "pm-proj"
    project_dir.mkdir(parents=True)
    ctx = project_dir / "pm-proj-context.md"
    ctx.write_text(CONTEXT)
    db.close()
    return task, ctx


class TestPmCrudEndpoints:
    def test_pm_bundle_empty(self, project):
        task, _ = project
        result = asyncio.run(server.get_task_pm(task.id))
        assert result == {
            "task_id": task.id,
            "due_date": None,
            "action_items": [],
            "stakeholders": [],
            "tickets": [],
        }

    def test_unknown_task_404(self, sandboxed):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(server.get_task_pm(999))
        assert exc.value.status_code == 404

    def test_action_item_roundtrip_renders_mirror(self, project):
        task, ctx = project
        created = asyncio.run(server.create_action_item(
            task.id,
            server.ActionItemPayload(
                what="send the numbers", requested_by="Lior", due_date="2099-01-01"
            ),
        ))
        assert created["success"] is True
        item_id = created["item"]["id"]
        assert created["item"]["label"] == f"AI-{item_id}"

        done = asyncio.run(server.update_action_item_endpoint(
            item_id, server.ActionItemUpdatePayload(status="done", notes="sent")
        ))
        assert done["item"]["status"] == "done"
        assert done["item"]["completed_at"] is not None

        content = ctx.read_text()
        assert "## Action Items" in content
        assert "send the numbers" in content
        assert "Action item done (AI-1)" in content

    def test_clear_due_date_flag(self, project):
        task, _ = project
        asyncio.run(server.create_action_item(
            task.id, server.ActionItemPayload(what="x", due_date="2099-01-01")
        ))
        result = asyncio.run(server.update_action_item_endpoint(
            1, server.ActionItemUpdatePayload(clear_due_date=True)
        ))
        assert result["item"]["due_date"] is None

    def test_bad_due_date_422(self, project):
        task, _ = project
        with pytest.raises(HTTPException) as exc:
            asyncio.run(server.create_action_item(
                task.id, server.ActionItemPayload(what="x", due_date="soon")
            ))
        assert exc.value.status_code == 422

    def test_stakeholder_upsert_and_delete(self, project):
        task, ctx = project
        asyncio.run(server.upsert_stakeholder(
            task.id, server.StakeholderPayload(name="Keren", role="SIA lead")
        ))
        updated = asyncio.run(server.upsert_stakeholder(
            task.id, server.StakeholderPayload(name="Keren", role="SIA manager")
        ))
        assert updated["stakeholder"]["role"] == "SIA manager"
        assert "## Stakeholders" in ctx.read_text()

        removed = asyncio.run(server.delete_stakeholder(task.id, "Keren"))
        assert removed == {"success": True, "removed": True}

    def test_ticket_upsert_and_delete(self, project):
        task, ctx = project
        created = asyncio.run(server.upsert_ticket(
            task.id, server.TicketPayload(label="MON-7", url="https://m/7", system="monday")
        ))
        assert created["ticket"]["label"] == "MON-7"
        assert "## Tickets" in ctx.read_text()
        removed = asyncio.run(server.delete_ticket(task.id, "MON-7"))
        assert removed == {"success": True, "removed": True}

    def test_due_date_set_and_clear(self, project):
        task, ctx = project
        result = asyncio.run(server.set_due_date(
            task.id, server.DueDatePayload(due_date="2099-06-30")
        ))
        assert result["due_date"] == "2099-06-30"
        assert "**Due:** 2099-06-30" in ctx.read_text()

        cleared = asyncio.run(server.set_due_date(
            task.id, server.DueDatePayload(due_date=None)
        ))
        assert cleared["due_date"] is None
        assert "**Due:**" not in ctx.read_text()


def _with_waiting_rows(*rows: str) -> str:
    """CONTEXT with extra Waiting-on table rows (`What | Who | Since | Gates`)."""
    return CONTEXT.replace(
        "|------|-----|-------|-------|",
        "|------|-----|-------|-------|\n" + "\n".join(rows),
    )


class TestWaitingOnResolveEndpoint:
    """Spec: blockers became resolvable from the UI (the 3b follow-on). The
    row is identified positionally and verified by its own text, so a table
    that shifted under the user yields 409 and no write."""

    @pytest.fixture
    def waiting_project(self, sandboxed):
        db = missioncache_db.TaskDB()
        task = db.create_task("wait-proj", task_type="coding", repo_id=None)
        d = sandboxed / "active" / "wait-proj"
        d.mkdir(parents=True)
        ctx = d / "wait-proj-context.md"
        ctx.write_text(_with_waiting_rows(
            "| Egress check | Dana | 2026-07-01 | Retry rollout |",
            "| Schema signoff | Ori | 2026-07-10 | The migration |",
        ))
        db.close()
        return task, ctx

    def test_resolves_the_row_and_records_it(self, waiting_project):
        task, ctx = waiting_project
        result = asyncio.run(server.resolve_waiting_on(
            task.id,
            server.WaitingOnResolvePayload(
                row_index=0, what="Egress check", who="Dana",
                since="2026-07-01", outcome="confirmed open",
            ),
        ))
        assert result["success"] is True
        assert result["row"]["who"] == "Dana"
        content = ctx.read_text()
        assert "Schema signoff" in content
        assert "Resolved (was waiting on Dana): Egress check - confirmed open" in content
        # And it disappears from the Today feed.
        groups = asyncio.run(server.get_today())["on_others"]
        assert [r["what"] for r in groups[0]["rows"]] == ["Schema signoff"]

    def test_stale_view_is_409_and_writes_nothing(self, waiting_project):
        task, ctx = waiting_project
        before = ctx.read_text()
        with pytest.raises(HTTPException) as exc:
            asyncio.run(server.resolve_waiting_on(
                task.id,
                server.WaitingOnResolvePayload(
                    row_index=0, what="Schema signoff", who="Ori", since="2026-07-10"
                ),
            ))
        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "STALE_VIEW"
        assert ctx.read_text() == before

    def test_unknown_task_is_404(self, sandboxed):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(server.resolve_waiting_on(
                999, server.WaitingOnResolvePayload(row_index=0, what="x", who="y", since="2026-07-01")
            ))
        assert exc.value.status_code == 404


class TestTodayEndpoint:
    """Spec: the unified-view design agreed 2026-07-24 - Today splits by WHO
    OWES the work, not by record type. `on_me` keeps the overdue/due-soon/other
    bucketing; `on_others` merges non-me action items (kind=commitment) with
    Waiting-on rows (kind=blocker) onto one "days past the line" scale, lined
    rows first. A commitment's line is its due date, a blocker's is the 7-day
    staleness threshold. Blocker rows are read-only (no id to act on)."""

    def test_on_me_bucketing_excludes_others_items(self, sandboxed):
        from missioncache_db import pm_items

        db = missioncache_db.TaskDB()
        task = db.create_task("proj", task_type="coding", repo_id=None)
        d = sandboxed / "active" / "proj"
        d.mkdir(parents=True)
        (d / "proj-context.md").write_text(CONTEXT)

        yesterday = (date.today() - timedelta(days=1)).isoformat()
        in_three = (date.today() + timedelta(days=3)).isoformat()
        far = (date.today() + timedelta(days=60)).isoformat()
        pm_items.add_action_item(db, task.id, "late one", due_date=yesterday)
        pm_items.add_action_item(db, task.id, "soon one", due_date=in_three)
        pm_items.add_action_item(db, task.id, "far one", due_date=far)
        pm_items.add_action_item(db, task.id, "no date one")
        pm_items.add_action_item(db, task.id, "not mine", assignee="Yuval")
        db.close()

        result = asyncio.run(server.get_today())
        on_me = result["on_me"]
        assert [i["what"] for i in on_me["overdue"]] == ["late one"]
        assert [i["what"] for i in on_me["due_soon"]] == ["soon one"]
        assert sorted(i["what"] for i in on_me["other_open"]) == ["far one", "no date one"]
        # The colleague's item never appears in on_me.
        assert all(
            i["what"] != "not mine"
            for bucket in on_me.values() for i in bucket
        )
        assert result["counts"] == {
            "on_me": 4, "overdue": 1, "on_others": 1,
            "on_others_projects": 1, "stale": 0,
        }

    def test_on_others_merges_commitments_and_blockers_by_line(self, sandboxed):
        from missioncache_db import pm_items

        db = missioncache_db.TaskDB()
        task = db.create_task("proj", task_type="coding", repo_id=None)
        d = sandboxed / "active" / "proj"
        d.mkdir(parents=True)
        # 10 days old = 3 past the 7-day line; 2 days old = not yet lined.
        old = (date.today() - timedelta(days=10)).isoformat()
        fresh = (date.today() - timedelta(days=2)).isoformat()
        (d / "proj-context.md").write_text(_with_waiting_rows(
            f"| Stale ask | Bob | {old} | the rollout |",
            f"| Fresh ask | Ann | {fresh} | the review |",
        ))
        # 5 days overdue = 5 past the line; a future due date is not lined.
        pm_items.add_action_item(
            db, task.id, "very late", assignee="Yuval",
            due_date=(date.today() - timedelta(days=5)).isoformat(),
            source="weekly sync",
        )
        pm_items.add_action_item(
            db, task.id, "not due yet", assignee="Mor",
            due_date=(date.today() + timedelta(days=9)).isoformat(),
        )
        db.close()

        groups = asyncio.run(server.get_today())["on_others"]
        assert len(groups) == 1
        rows = groups[0]["rows"]
        assert [r["what"] for r in rows] == [
            "very late",    # 5 past the line
            "Stale ask",    # 3 past the line
            "Fresh ask",    # unlined, 2 days old
            "not due yet",  # unlined, created today
        ]
        assert [r["days_past_line"] for r in rows] == [5, 3, None, None]
        assert [r["kind"] for r in rows] == [
            "commitment", "blocker", "blocker", "commitment"
        ]
        assert groups[0]["count"] == 4
        assert groups[0]["stale_count"] == 2
        assert groups[0]["newest_age_days"] == 0  # both commitments made today

    def test_row_shape_is_uniform_across_kinds(self, sandboxed):
        from missioncache_db import pm_items

        db = missioncache_db.TaskDB()
        task = db.create_task("proj", task_type="coding", repo_id=None)
        d = sandboxed / "active" / "proj"
        d.mkdir(parents=True)
        (d / "proj-context.md").write_text(_with_waiting_rows(
            "| Broker config review | Dana | 2020-01-01 | Retry rollout |"
        ))
        pm_items.add_action_item(
            db, task.id, "update the test plan", assignee="Yuval",
            source="AIP weekly 2026-07-24",
        )
        db.close()

        groups = asyncio.run(server.get_today())["on_others"]
        rows = {r["kind"]: r for r in groups[0]["rows"]}
        # Both kinds fill What / Who / why-it-matters / project identically.
        blocker = rows["blocker"]
        assert (blocker["what"], blocker["who"]) == ("Broker config review", "Dana")
        assert blocker["why"] == "Retry rollout"
        assert blocker["since"] == "2020-01-01"
        # No action-item id, but a verified positional handle to resolve by.
        assert blocker["id"] is None and blocker["label"] is None
        assert blocker["row_index"] == 0

        commitment = rows["commitment"]
        assert (commitment["what"], commitment["who"]) == ("update the test plan", "Yuval")
        assert commitment["why"] == "AIP weekly 2026-07-24"
        assert commitment["label"] == "AI-1" and commitment["id"] == 1
        assert commitment["project_name"] == "proj"

    def test_non_blocking_commitment_on_others_still_listed(self, sandboxed):
        """An item owned by a colleague that gates nothing still belongs -
        it is tracked because you own the meeting, not because you are blocked."""
        from missioncache_db import pm_items

        db = missioncache_db.TaskDB()
        task = db.create_task("proj", task_type="coding", repo_id=None)
        d = sandboxed / "active" / "proj"
        d.mkdir(parents=True)
        (d / "proj-context.md").write_text(CONTEXT)
        pm_items.add_action_item(db, task.id, "nice to have", assignee="Oren")
        db.close()

        result = asyncio.run(server.get_today())
        assert [r["what"] for r in result["on_others"][0]["rows"]] == ["nice to have"]
        assert result["counts"]["on_others"] == 1
        assert result["counts"]["stale"] == 0

    def test_groups_sort_by_newest_row_not_by_staleness(self, sandboxed):
        """Grouping spec: a project asked about yesterday is live; one whose
        asks have all sat for months is archaeology, even though its rows are
        further past the line."""
        from missioncache_db import pm_items

        db = missioncache_db.TaskDB()
        ancient = db.create_task("ancient-proj", task_type="coding", repo_id=None)
        fresh = db.create_task("fresh-proj", task_type="coding", repo_id=None)
        for name in ("ancient-proj", "fresh-proj"):
            (sandboxed / "active" / name).mkdir(parents=True)
        (sandboxed / "active" / "ancient-proj" / "ancient-proj-context.md").write_text(
            _with_waiting_rows(
                f"| Very old ask | Bob | {(date.today() - timedelta(days=90)).isoformat()} | x |"
            )
        )
        (sandboxed / "active" / "fresh-proj" / "fresh-proj-context.md").write_text(
            _with_waiting_rows(
                f"| Asked today | Ann | {date.today().isoformat()} | y |"
            )
        )
        db.close()

        groups = asyncio.run(server.get_today())["on_others"]
        assert [g["project_name"] for g in groups] == ["fresh-proj", "ancient-proj"]
        assert groups[0]["newest_age_days"] == 0
        assert groups[1]["newest_age_days"] == 90
        # The ancient one is still the stale one - order is recency, not urgency.
        assert groups[1]["stale_count"] == 1
        assert groups[0]["stale_count"] == 0

    def test_project_risk_from_overdue_due_date_or_stale_waiting(self, sandboxed):
        from missioncache_db import pm_items

        db = missioncache_db.TaskDB()
        overdue_p = db.create_task("overdue-proj", task_type="coding", repo_id=None)
        due_p = db.create_task("due-proj", task_type="coding", repo_id=None)
        waiting_p = db.create_task("waiting-proj", task_type="coding", repo_id=None)
        calm_p = db.create_task("calm-proj", task_type="coding", repo_id=None)
        for name in ("overdue-proj", "due-proj", "waiting-proj", "calm-proj"):
            (sandboxed / "active" / name).mkdir(parents=True)
            (sandboxed / "active" / name / f"{name}-context.md").write_text(CONTEXT)
        (sandboxed / "active" / "waiting-proj" / "waiting-proj-context.md").write_text(
            _with_waiting_rows("| Old ask | Bob | 2020-01-01 | thing |")
        )

        pm_items.add_action_item(
            db, overdue_p.id, "late",
            due_date=(date.today() - timedelta(days=1)).isoformat(),
        )
        pm_items.set_project_due_date(
            db, due_p.id, (date.today() + timedelta(days=2)).isoformat()
        )
        pm_items.add_action_item(db, calm_p.id, "relaxed", due_date="2099-01-01")
        db.close()

        by_name = {p["name"]: p for p in asyncio.run(server.get_today())["projects"]}
        assert by_name["overdue-proj"]["at_risk"] is True
        assert by_name["overdue-proj"]["overdue_count"] == 1
        assert by_name["due-proj"]["at_risk"] is True
        assert by_name["due-proj"]["days_to_due"] == 2
        assert by_name["waiting-proj"]["at_risk"] is True
        assert by_name["waiting-proj"]["stale_on_others_count"] == 1
        assert by_name["waiting-proj"]["on_others_count"] == 1
        assert by_name["calm-proj"]["at_risk"] is False

    def test_overdue_item_owned_by_someone_else_flags_the_project(self, sandboxed):
        """A colleague being late is a project needing attention too - it counts
        as past the line, the same as a stale Waiting-on row."""
        from missioncache_db import pm_items

        db = missioncache_db.TaskDB()
        task = db.create_task("proj", task_type="coding", repo_id=None)
        d = sandboxed / "active" / "proj"
        d.mkdir(parents=True)
        (d / "proj-context.md").write_text(CONTEXT)
        pm_items.add_action_item(
            db, task.id, "colleague is late", assignee="Yuval",
            due_date=(date.today() - timedelta(days=4)).isoformat(),
        )
        db.close()

        result = asyncio.run(server.get_today())
        project = result["projects"][0]
        assert project["at_risk"] is True
        assert project["overdue_count"] == 0        # not on my plate
        assert project["stale_on_others_count"] == 1
        assert result["counts"]["stale"] == 1

    def test_at_risk_projects_sort_first(self, sandboxed):
        from missioncache_db import pm_items

        db = missioncache_db.TaskDB()
        calm = db.create_task("calm-proj", task_type="coding", repo_id=None)
        risky = db.create_task("risky-proj", task_type="coding", repo_id=None)
        for name in ("calm-proj", "risky-proj"):
            (sandboxed / "active" / name).mkdir(parents=True)
            (sandboxed / "active" / name / f"{name}-context.md").write_text(CONTEXT)
        pm_items.add_action_item(db, calm.id, "relaxed", due_date="2099-01-01")
        pm_items.add_action_item(
            db, risky.id, "late",
            due_date=(date.today() - timedelta(days=1)).isoformat(),
        )
        db.close()

        assert asyncio.run(server.get_today())["projects"][0]["name"] == "risky-proj"

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
import sqlite3
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
    # server.py binds its OWN MISSIONCACHE_ROOT at import time from the real
    # home, so patching Path.home and missioncache_db's copy leaves it pointing
    # at the developer's actual ~/.missioncache. The endpoint re-imports the
    # patched value locally, but parse_missioncache_progress reads this global -
    # without this line the progress assertions below silently measure real
    # projects instead of the sandbox.
    monkeypatch.setattr(server, "MISSIONCACHE_ROOT", mc_root)
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
        """Grouping spec (user instruction, 2026-07-24): "sort them from the
        project with the newest action items". A project someone was asked
        about yesterday is live; one whose asks have all sat for months is
        archaeology, even though its rows are further past the line. Ordering
        is recency; urgency is carried by the per-row stale marks instead.
        """
        db = missioncache_db.TaskDB()
        db.create_task("ancient-proj", task_type="coding", repo_id=None)
        db.create_task("fresh-proj", task_type="coding", repo_id=None)
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


class TestSelfOwnedWaitingRows:
    """Spec: the split is by WHO OWES THE WORK, not by which table stored the
    row. A Waiting-on row whose `who` cell names the owner is work he owes, so
    it counts on his side.

    Before this, every row in the table counted against "on other people"
    regardless of the cell, which under-reported his own plate roughly 4x on
    live data and filed his own asks under someone else's label.
    """

    def _project_with(self, sandboxed, name, *rows):
        db = missioncache_db.TaskDB()
        task = db.create_task(name, task_type="coding", repo_id=None)
        (sandboxed / "active" / name).mkdir(parents=True)
        (sandboxed / "active" / name / f"{name}-context.md").write_text(
            _with_waiting_rows(*rows)
        )
        db.close()
        return task

    def test_row_naming_the_owner_counts_on_his_side(self, sandboxed):
        self._project_with(
            sandboxed, "selfown",
            "| Merge the PRs | Tomer | 2026-07-01 | the release |",
            "| Access approval | Mor | 2026-07-01 | the cluster |",
        )
        result = asyncio.run(server.get_today())
        rows = result["on_others"][0]["rows"]
        mine = [r for r in rows if r["mine"]]
        theirs = [r for r in rows if not r["mine"]]

        assert [r["what"] for r in mine] == ["Merge the PRs"]
        assert [r["what"] for r in theirs] == ["Access approval"]
        # His row counts once, on his side only.
        assert result["counts"]["on_me"] == 1
        assert result["counts"]["on_others"] == 1
        project = result["projects"][0]
        assert project["open_count"] == 1
        assert project["on_others_count"] == 1

    def test_first_person_who_also_counts_as_his(self, sandboxed):
        """"Me (Jose is blocked on it)" is a real live value."""
        self._project_with(
            sandboxed, "selfown2",
            "| Deliver the YAMLs | Me (Jose is blocked on it) | 2026-07-01 | Jose |",
        )
        rows = asyncio.run(server.get_today())["on_others"][0]["rows"]
        assert rows[0]["mine"] is True

    def test_trailing_note_does_not_make_someone_elses_row_his(self, sandboxed):
        """"Itai Sela (IT) - I said I would handle it" names Itai in the who
        cell. The note is ambiguous, so the cell wins and the row stays theirs
        rather than being guessed onto his plate."""
        self._project_with(
            sandboxed, "selfown3",
            "| Confluence access | Itai Sela (IT) - I said I would handle it | 2026-07-01 | the map |",
        )
        rows = asyncio.run(server.get_today())["on_others"][0]["rows"]
        assert rows[0]["mine"] is False
        assert rows[0]["who_primary"] == "Itai"

    def test_his_stale_row_does_not_inflate_the_idle_meter(self, sandboxed):
        """The meter reads as "someone else is slow", so his own overdue row
        must not fill it."""
        old = (date.today() - timedelta(days=30)).isoformat()
        self._project_with(
            sandboxed, "selfown4",
            f"| My own late thing | Tomer | {old} | the release |",
        )
        result = asyncio.run(server.get_today())
        group = result["on_others"][0]
        assert group["count"] == 0
        assert group["stale_count"] == 0
        assert group["mine_count"] == 1
        assert result["counts"]["stale"] == 0
        assert result["projects"][0]["stale_on_others_count"] == 0


class TestWhoPrimary:
    """Spec: the by-person rollup needs a grouping key, and the `who` cell is
    hand-typed prose. Counting distinct exact strings says the field cannot
    group; that measures string equality, not owners.
    """

    def test_variants_of_one_person_share_a_key(self, sandboxed):
        db = missioncache_db.TaskDB()
        db.create_task("whoproj", task_type="coding", repo_id=None)
        (sandboxed / "active" / "whoproj").mkdir(parents=True)
        (sandboxed / "active" / "whoproj" / "whoproj-context.md").write_text(
            _with_waiting_rows(
                "| First | Lior | 2026-07-01 | a |",
                "| Second | Lior Ben Naon | 2026-07-01 | b |",
                "| Third | Lior (with Adam) | 2026-07-01 | c |",
            )
        )
        db.close()
        rows = asyncio.run(server.get_today())["on_others"][0]["rows"]
        assert {r["who_primary"] for r in rows} == {"Lior"}
        # The raw cell survives untouched, so a merge stays inspectable.
        assert sorted(r["who"] for r in rows) == [
            "Lior", "Lior (with Adam)", "Lior Ben Naon",
        ]

    def test_multi_owner_cell_keys_on_the_first_named(self, sandboxed):
        db = missioncache_db.TaskDB()
        db.create_task("multi", task_type="coding", repo_id=None)
        (sandboxed / "active" / "multi").mkdir(parents=True)
        (sandboxed / "active" / "multi" / "multi-context.md").write_text(
            _with_waiting_rows("| Shared | Dima / Lior | 2026-07-01 | a |")
        )
        db.close()
        rows = asyncio.run(server.get_today())["on_others"][0]["rows"]
        assert rows[0]["who_primary"] == "Dima"

    def test_unparseable_owner_falls_back_to_the_raw_cell(self, sandboxed):
        """A non-person owner is shown as written rather than dropped or
        merged into somebody."""
        db = missioncache_db.TaskDB()
        db.create_task("cron", task_type="coding", repo_id=None)
        (sandboxed / "active" / "cron").mkdir(parents=True)
        (sandboxed / "active" / "cron" / "cron-context.md").write_text(
            _with_waiting_rows("| Nightly | (nobody) | 2026-07-01 | a |")
        )
        db.close()
        rows = asyncio.run(server.get_today())["on_others"][0]["rows"]
        assert rows[0]["who_primary"] == "(nobody)"


class TestProjectRecency:
    """Spec: the view distinguishes a project whose asks rot while he works in
    it (chase them) from one whose asks rot because the project stopped (close
    them). Tracked minutes cannot tell those apart - only 9% of a measured day
    attributes to a project at all - so recency carries it.
    """

    def test_days_since_worked_is_exposed_and_none_when_never_worked(self, sandboxed):
        db = missioncache_db.TaskDB()
        worked = db.create_task("worked-proj", task_type="coding", repo_id=None)
        db.create_task("never-proj", task_type="coding", repo_id=None)
        for name in ("worked-proj", "never-proj"):
            (sandboxed / "active" / name).mkdir(parents=True)
            (sandboxed / "active" / name / f"{name}-context.md").write_text(CONTEXT)
        from missioncache_db import pm_items
        pm_items.add_action_item(db, worked.id, "a", due_date="2099-01-01")
        pm_items.add_action_item(
            db, [t for t in db.get_active_tasks() if t.name == "never-proj"][0].id,
            "b", due_date="2099-01-01",
        )
        db.close()
        # last_worked_on is stamped by heartbeat recording, so set it directly
        # rather than simulating a session just to age one row.
        four_days_ago = (date.today() - timedelta(days=4)).isoformat()
        raw = sqlite3.connect(missioncache_db.DB_PATH)
        raw.execute(
            "UPDATE tasks SET last_worked_on = ? WHERE id = ?",
            (f"{four_days_ago} 09:00:00", worked.id),
        )
        raw.commit()
        raw.close()

        by_name = {p["name"]: p for p in asyncio.run(server.get_today())["projects"]}
        assert by_name["worked-proj"]["days_since_worked"] == 4
        assert by_name["worked-proj"]["last_worked_on"].startswith(four_days_ago)
        # Never worked must be None, not 0 - a project nobody has opened is the
        # opposite of one worked today, and 0 would read as "today".
        assert by_name["never-proj"]["days_since_worked"] is None
        assert by_name["never-proj"]["last_worked_on"] is None


class TestProjectProgressAndNextUp:
    """Spec: a row says who owes what, but "3 asks outstanding" reads the same on
    a project at 86% as on one at 0%, and the view showed nothing about what the
    user should pick up next. The payload carries checklist progress and the next
    unchecked item - derived server-side, because the parsed checklist runs to
    hundreds of items across the active projects and a row needs one line of it.
    """

    TASKS_FILE = """# Prog Proj - Tasks

**Last Updated:** 2026-07-10 12:00

## Tasks

- [x] 1. Write the parser
- [x] 2. Wire it to the endpoint
- [ ] 3. Cover it with tests
- [ ] 4. Document the payload
"""

    def _project(self, sandboxed, name, tasks_body):
        db = missioncache_db.TaskDB()
        task = db.create_task(name, task_type="coding", repo_id=None)
        d = sandboxed / "active" / name
        d.mkdir(parents=True)
        (d / f"{name}-context.md").write_text(CONTEXT)
        if tasks_body is not None:
            (d / f"{name}-tasks.md").write_text(tasks_body)
        from missioncache_db import pm_items
        pm_items.add_action_item(db, task.id, "keep the project in the payload",
                                 due_date="2099-01-01")
        db.close()
        return task

    def test_counts_and_next_up_come_from_the_checklist(self, sandboxed):
        self._project(sandboxed, "prog-proj", self.TASKS_FILE)
        p = asyncio.run(server.get_today())["projects"][0]
        assert p["completed_count"] == 2
        assert p["total_count"] == 4
        assert p["completion_pct"] == 50
        # The FIRST unchecked item, not the last completed one and not a join of
        # all of them: this is what the user picks up next.
        assert p["next_up"] == "Cover it with tests"

    def test_task_modes_is_never_shipped(self, sandboxed):
        """The parsed checklist is the input to next_up, not part of the payload.

        Forwarding it would put every item of every project on the wire so the
        client could pick one line, which is the whole reason the pick happens
        server-side.
        """
        self._project(sandboxed, "prog-proj", self.TASKS_FILE)
        p = asyncio.run(server.get_today())["projects"][0]
        assert "task_modes" not in p
        assert "project_mode" not in p

    def test_all_items_done_reports_no_next_up(self, sandboxed):
        """100% complete has to be distinguishable from "no checklist at all",
        so next_up is None while the counts still show the work.
        """
        self._project(sandboxed, "done-proj", """# Done Proj - Tasks

## Tasks

- [x] 1. Only item
""")
        p = asyncio.run(server.get_today())["projects"][0]
        assert p["completed_count"] == p["total_count"] == 1
        assert p["completion_pct"] == 100
        assert p["next_up"] is None

    def test_project_with_no_tasks_file_reports_zeroes_not_errors(self, sandboxed):
        """A project can exist with action items and no tasks file; the row still
        has to render.
        """
        self._project(sandboxed, "bare-proj", None)
        p = asyncio.run(server.get_today())["projects"][0]
        assert p["total_count"] == 0
        assert p["completed_count"] == 0
        assert p["completion_pct"] == 0
        assert p["next_up"] is None


class TestProjectTicketReference:
    """Spec: tickets are system-agnostic - label + url is the whole interface.
    The tickets table is the current home; tasks.jira_key is the legacy column
    that migrates into a row on the first PM mutation and stays readable.
    """

    def _project(self, sandboxed, name):
        db = missioncache_db.TaskDB()
        task = db.create_task(name, task_type="coding", repo_id=None)
        d = sandboxed / "active" / name
        d.mkdir(parents=True)
        (d / f"{name}-context.md").write_text(CONTEXT)
        from missioncache_db import pm_items
        pm_items.add_action_item(db, task.id, "keep it in the payload",
                                 due_date="2099-01-01")
        db.close()
        return task

    def test_tickets_row_wins_over_the_legacy_column(self, sandboxed):
        task = self._project(sandboxed, "tkt-proj")
        raw = sqlite3.connect(missioncache_db.DB_PATH)
        raw.execute("UPDATE tasks SET jira_key = ? WHERE id = ?", ("OLD-1", task.id))
        raw.commit()
        raw.close()
        db = missioncache_db.TaskDB()
        from missioncache_db import pm_items
        pm_items.add_ticket(db, task.id, "NEW-2", url="https://tracker/NEW-2")
        db.close()

        p = asyncio.run(server.get_today())["projects"][0]
        assert p["ticket_label"] == "NEW-2"
        assert p["ticket_url"] == "https://tracker/NEW-2"

    def test_legacy_column_is_the_fallback(self, sandboxed):
        task = self._project(sandboxed, "legacy-proj")
        raw = sqlite3.connect(missioncache_db.DB_PATH)
        raw.execute("UPDATE tasks SET jira_key = ? WHERE id = ?", ("OLD-1", task.id))
        raw.commit()
        raw.close()

        p = asyncio.run(server.get_today())["projects"][0]
        assert p["ticket_label"] == "OLD-1"

    def test_no_ticket_anywhere_is_none_not_empty_string(self, sandboxed):
        """The UI branches on falsiness to decide whether to render the link at
        all, so an absent reference must not arrive as a renderable value.
        """
        self._project(sandboxed, "no-tkt-proj")
        p = asyncio.run(server.get_today())["projects"][0]
        assert p["ticket_label"] is None
        assert p["ticket_url"] is None


class TestOwnLateWorkCountsAsOverdue:
    """Spec: /api/today's own contract states that "days past the line" is ONE
    scale across both record kinds - a commitment's line is its due date, a
    Waiting-on row's line is the 7-day threshold. So anything of his that has
    crossed its line is late, whichever table it came from.

    The bug this pins: a Waiting-on row whose `who` names him incremented
    open_count only. overdue_count skipped it, and counts.overdue is built from
    the action items, which are the only rows carrying a due_date. Measured on
    live data, the header reported "0 overdue" while a row of his sat 2 days
    past its line with a named colleague blocked on it. A count that cannot
    represent the case is worse than a wrong number, because the page asserts
    the good news in a headline.
    """

    def _project_with(self, sandboxed, name, *rows):
        db = missioncache_db.TaskDB()
        task = db.create_task(name, task_type="coding", repo_id=None)
        (sandboxed / "active" / name).mkdir(parents=True)
        (sandboxed / "active" / name / f"{name}-context.md").write_text(
            _with_waiting_rows(*rows)
        )
        db.close()
        return task

    def test_his_row_past_the_line_is_counted_overdue(self, sandboxed):
        since = (date.today() - timedelta(days=9)).isoformat()
        self._project_with(
            sandboxed, "latemine",
            f"| Deliver the preset YAMLs | Tomer | {since} | Jose is blocked |",
        )
        result = asyncio.run(server.get_today())
        assert result["counts"]["overdue"] == 1
        assert result["projects"][0]["overdue_count"] == 1

    def test_his_row_inside_the_line_is_not_overdue(self, sandboxed):
        """The threshold has to bite in both directions, or the count is just
        'anything of his', which is open_count under another name.
        """
        since = (date.today() - timedelta(days=2)).isoformat()
        self._project_with(
            sandboxed, "freshmine",
            f"| Reply to the thread | Tomer | {since} | nothing yet |",
        )
        result = asyncio.run(server.get_today())
        assert result["counts"]["overdue"] == 0
        assert result["projects"][0]["overdue_count"] == 0
        assert result["counts"]["on_me"] == 1      # still on his plate

    def test_someone_elses_late_row_is_not_his_overdue(self, sandboxed):
        """A colleague sitting on an ask is THEIR latency, not his lateness. It
        belongs to stale, and must not leak into overdue - red is reserved for
        "you are late" and nothing else.
        """
        since = (date.today() - timedelta(days=20)).isoformat()
        self._project_with(
            sandboxed, "theirlate",
            f"| Access approval | Mor | {since} | the cluster |",
        )
        result = asyncio.run(server.get_today())
        assert result["counts"]["overdue"] == 0
        assert result["projects"][0]["overdue_count"] == 0
        assert result["counts"]["stale"] == 1

    def test_both_kinds_of_his_late_work_add_up(self, sandboxed):
        """The two live in different lists (action items in on_me, waiting rows
        in on_others), so the count has to reach across both without
        double-counting either.
        """
        since = (date.today() - timedelta(days=9)).isoformat()
        task = self._project_with(
            sandboxed, "bothkinds",
            f"| Deliver the YAMLs | Tomer | {since} | Jose is blocked |",
        )
        db = missioncache_db.TaskDB()
        from missioncache_db import pm_items
        pm_items.add_action_item(
            db, task.id, "ship the fix", assignee="me",
            due_date=(date.today() - timedelta(days=3)).isoformat(),
        )
        db.close()

        result = asyncio.run(server.get_today())
        assert result["counts"]["overdue"] == 2
        assert result["projects"][0]["overdue_count"] == 2
        # and neither row is counted twice into on_me
        assert result["counts"]["on_me"] == 2

    def test_a_late_row_of_his_makes_the_project_at_risk(self, sandboxed):
        """at_risk is derived from overdue_count, so fixing the count has to fix
        the flag with it - otherwise a project where he is the blocker still
        reads as healthy.
        """
        since = (date.today() - timedelta(days=11)).isoformat()
        self._project_with(
            sandboxed, "riskmine",
            f"| Sign off the plan | Tomer | {since} | the whole suite |",
        )
        result = asyncio.run(server.get_today())
        assert result["projects"][0]["at_risk"] is True


class TestGroupOrderIsTotal:
    """Spec: the sibling projects.sort in the same function documents the
    convention - "Name last so the order is stable across requests when
    everything else ties." The group list is the same kind of list rendered on
    the same page, so it owes the reader the same guarantee.

    Ordering on newest_age_days alone left ties resolved by dict insertion
    order. That is deterministic within one process but it is not a property of
    the data, so the same day's list can come back in a different order after a
    rename or a new task shifts the row order underneath it.
    """

    def _project_with(self, sandboxed, name, *rows):
        db = missioncache_db.TaskDB()
        db.create_task(name, task_type="coding", repo_id=None)
        (sandboxed / "active" / name).mkdir(parents=True)
        (sandboxed / "active" / name / f"{name}-context.md").write_text(
            _with_waiting_rows(*rows)
        )
        db.close()

    def test_projects_tied_on_age_are_ordered_by_quiet_volume(self, sandboxed):
        """Same freshest-ask age, different amounts gone quiet. The one holding
        more quiet asks is the more useful one to look at, so it comes first."""
        old = (date.today() - timedelta(days=40)).isoformat()
        same = (date.today() - timedelta(days=40)).isoformat()
        self._project_with(
            sandboxed, "zzz-few",
            f"| One thing | Mor | {same} | a |",
        )
        self._project_with(
            sandboxed, "aaa-many",
            f"| First thing | Mor | {same} | a |",
            f"| Second thing | Gal | {old} | b |",
            f"| Third thing | Yuval | {old} | c |",
        )
        groups = asyncio.run(server.get_today())["on_others"]
        names = [g["project_name"] for g in groups]
        # Both tie at the same newest age, so volume decides - NOT the
        # alphabetical or insertion order, either of which would put zzz-few
        # somewhere else.
        assert names == ["aaa-many", "zzz-few"]

    def test_projects_tied_on_age_and_volume_are_ordered_by_name(self, sandboxed):
        """Fully tied, so the name makes the order total and repeatable."""
        same = (date.today() - timedelta(days=40)).isoformat()
        for name in ("charlie", "alpha", "bravo"):
            self._project_with(
                sandboxed, name, f"| A thing | Mor | {same} | gates |",
            )
        groups = asyncio.run(server.get_today())["on_others"]
        assert [g["project_name"] for g in groups] == ["alpha", "bravo", "charlie"]

    def test_freshest_still_wins_over_volume(self, sandboxed):
        """The tiebreaks must not outrank the primary key: a project answered
        recently stays ahead of a busier one that has gone quiet longer."""
        fresh = (date.today() - timedelta(days=1)).isoformat()
        old = (date.today() - timedelta(days=40)).isoformat()
        self._project_with(
            sandboxed, "busy-but-old",
            f"| One | Mor | {old} | a |",
            f"| Two | Gal | {old} | b |",
            f"| Three | Yuval | {old} | c |",
        )
        self._project_with(
            sandboxed, "quiet-but-fresh",
            f"| Only one | Mor | {fresh} | a |",
        )
        groups = asyncio.run(server.get_today())["on_others"]
        assert [g["project_name"] for g in groups] == ["quiet-but-fresh", "busy-but-old"]

    def test_projects_with_no_answered_ask_sort_last(self, sandboxed):
        """None means nothing has come back at all, which must not read as
        "age zero" and jump the queue."""
        fresh = (date.today() - timedelta(days=2)).isoformat()
        self._project_with(
            sandboxed, "has-ages", f"| A thing | Mor | {fresh} | gates |",
        )
        # A row that names HIM has no other-people age, so the group's
        # newest_age_days is None.
        self._project_with(
            sandboxed, "no-ages", f"| My own thing | Tomer | {fresh} | gates |",
        )
        groups = asyncio.run(server.get_today())["on_others"]
        assert [g["project_name"] for g in groups] == ["has-ages", "no-ages"]


class TestLeftOff:
    """Spec: the Attention view proposes one project to sit in, and a proposal
    needs orientation before it is actionable. "last worked 2 days ago" does not
    supply that; the newest Recent Changes bullet is the last session's headline
    and does.

    Bullets are hand-written and save-flow-written, so they carry assorted
    leading timestamps. Those are noise in a card about the last few days.
    """

    def _with_recent(self, sandboxed, name, body):
        db = missioncache_db.TaskDB()
        db.create_task(name, task_type="coding", repo_id=None)
        (sandboxed / "active" / name).mkdir(parents=True)
        ctx = CONTEXT.replace("- created", body)
        # a waiting row so the project reaches projects[] at all
        ctx = ctx.replace(
            "|------|-----|-------|-------|",
            "|------|-----|-------|-------|\n| A thing | Mor | 2026-07-01 | gates |",
        )
        (sandboxed / "active" / name / f"{name}-context.md").write_text(ctx)
        db.close()

    def _left_off(self, sandboxed, name, body):
        self._with_recent(sandboxed, name, body)
        result = asyncio.run(server.get_today())
        return result["projects"][0]["left_off"]

    def test_newest_bullet_is_returned(self, sandboxed):
        assert self._left_off(
            sandboxed, "lo1", "- Wired the export path end to end"
        ) == "Wired the export path end to end"

    def test_date_and_time_prefix_is_stripped(self, sandboxed):
        assert self._left_off(
            sandboxed, "lo2", "- 2026-07-10 11:00 Wired the export path end to end"
        ) == "Wired the export path end to end"

    def test_bare_date_prefix_is_stripped(self, sandboxed):
        """`2026-07-25 - did the thing` is a real shape in the live files, and an
        earlier version stripped only the with-time form, so the date showed."""
        assert self._left_off(
            sandboxed, "lo3", "- 2026-07-25 - Wired the export path end to end"
        ) == "Wired the export path end to end"

    def test_heading_nested_in_the_bullet_is_stripped(self, sandboxed):
        """`- ### 2026-07-24 - did the thing` is real in the live files. The
        hashes have to come off before the date strip can fire, or the card
        shows the raw markdown."""
        assert self._left_off(
            sandboxed, "lo6", "- ### 2026-07-24 - Wired the export path end to end"
        ) == "Wired the export path end to end"

    def test_bold_markers_are_stripped(self, sandboxed):
        """The card renders escaped plain text, so `**x**` would show its
        asterisks. Real live shape: `- **26PI03 ... Review** - summary`."""
        assert self._left_off(
            sandboxed, "lo7", "- **26PI03 Plan Review** - wired the export path"
        ) == "26PI03 Plan Review - wired the export path"

    def test_short_fragments_are_skipped(self, sandboxed):
        """A bullet that orients nobody is worse than none: it fills the line."""
        assert self._left_off(
            sandboxed, "lo4", "- done\n- Wired the export path end to end"
        ) == "Wired the export path end to end"

    def test_no_recent_changes_gives_none(self, sandboxed):
        db = missioncache_db.TaskDB()
        db.create_task("lo5", task_type="coding", repo_id=None)
        (sandboxed / "active" / "lo5").mkdir(parents=True)
        (sandboxed / "active" / "lo5" / "lo5-context.md").write_text(
            _with_waiting_rows("| A thing | Mor | 2026-07-01 | gates |")
            .split("## Recent Changes")[0]
        )
        db.close()
        assert asyncio.run(server.get_today())["projects"][0]["left_off"] is None


class TestDisplayName:
    """Spec: the greeting addresses the reader by name, and MissionCache is
    installed by people who are not its author. The name therefore has to come
    from the machine, and "no name" has to be a supported state rather than a
    blank in the sentence.

    Git's global user.name is the source because it is the one place a
    developer has already written their own name down.
    """

    def _run(self, monkeypatch, returncode, stdout, raises=None):
        import subprocess as sp

        def fake_run(*a, **kw):
            if raises:
                raise raises
            return sp.CompletedProcess(a[0] if a else [], returncode, stdout, "")

        monkeypatch.setattr(server.subprocess, "run", fake_run)
        server._DISPLAY_NAME_CACHE = None
        try:
            return server._display_name()
        finally:
            server._DISPLAY_NAME_CACHE = None

    def test_first_token_only(self, monkeypatch):
        """A full name in a greeting reads like a form letter."""
        assert self._run(monkeypatch, 0, "Tomer Brami\n") == "Tomer"

    def test_single_word_name(self, monkeypatch):
        assert self._run(monkeypatch, 0, "ada\n") == "ada"

    def test_unset_git_name_gives_none(self, monkeypatch):
        """git exits non-zero when the key is unset. None is the answer, and
        the view has nameless wording for it."""
        assert self._run(monkeypatch, 1, "") is None

    def test_blank_git_name_gives_none(self, monkeypatch):
        assert self._run(monkeypatch, 0, "   \n") is None

    def test_missing_git_binary_gives_none(self, monkeypatch):
        """No git on the box is a normal state for a dashboard-only install,
        not an error worth failing the board over."""
        assert self._run(monkeypatch, 0, "", raises=FileNotFoundError("git")) is None

    def test_timeout_gives_none(self, monkeypatch):
        import subprocess as sp

        assert self._run(
            monkeypatch, 0, "", raises=sp.TimeoutExpired("git", 2)
        ) is None

    def test_undecodable_git_output_does_not_escape(self, monkeypatch):
        """Regression: text=True decodes with the preferred encoding and
        errors='strict', so a non-ASCII user.name on a non-UTF-8 machine raised
        UnicodeDecodeError. That is a ValueError, not a SubprocessError, so it
        escaped the helper and 500'd the whole /api/today endpoint - blanking
        the board over a greeting. None is the contract here, always."""
        boom = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        assert self._run(monkeypatch, 0, "", raises=boom) is None

    def test_a_failed_lookup_is_not_cached_forever(self, monkeypatch):
        """A transient failure used to be pinned by lru_cache, leaving every
        later render nameless until the process restarted."""
        server._DISPLAY_NAME_CACHE = None
        try:
            monkeypatch.setattr(
                server.subprocess, "run",
                lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("git")),
            )
            assert server._display_name() is None
            import subprocess as sp
            monkeypatch.setattr(
                server.subprocess, "run",
                lambda *a, **k: sp.CompletedProcess([], 0, "Ada Lovelace\n", ""),
            )
            assert server._display_name() == "Ada"
        finally:
            server._DISPLAY_NAME_CACHE = None

    def test_today_payload_carries_the_name(self, sandboxed, monkeypatch):
        monkeypatch.setattr(server, "_display_name", lambda: "Ada")
        assert asyncio.run(server.get_today())["user_name"] == "Ada"

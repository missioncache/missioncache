"""Tests for missioncache_db.portfolio that the dashboard suite does not cover.

The rollup itself is pinned by missioncache-dashboard/tests/test_pm_endpoints.py
through ``get_today`` (48 tests, zero edits across the extraction). What lives
here is the db-side layer on top: ``scope_portfolio`` must keep ``counts`` equal
to the rows that survive, and ``_checklist_progress`` must keep the two quirks
its four fields depend on.
"""

from datetime import date

import copy

import pytest

import missioncache_db
from missioncache_db import portfolio


CONTEXT = """# {name} - Context

**Last Updated:** 2026-07-10 12:00

## Description

x

## Gotchas

- none

## Waiting on

| What | Who | Since | Gates |
|------|-----|-------|-------|
{rows}

## Next Steps

1. TBD

## Recent Changes

### 2026-07-10 11:00

- wired the thing up end to end
"""


@pytest.fixture
def rooted(tmp_path, monkeypatch):
    root = tmp_path / ".missioncache"
    root.mkdir()
    monkeypatch.setattr(missioncache_db, "MISSIONCACHE_ROOT", root)
    monkeypatch.setattr(missioncache_db, "DB_PATH", tmp_path / "tasks.db")
    return root


def _project(root, db, name, waiting_rows="", tasks_body=None):
    task = db.create_task(name, task_type="coding", repo_id=None)
    d = root / "active" / name
    d.mkdir(parents=True)
    (d / f"{name}-context.md").write_text(CONTEXT.format(name=name, rows=waiting_rows))
    if tasks_body is not None:
        (d / f"{name}-tasks.md").write_text(tasks_body)
    return task


class TestScopePortfolio:
    def test_counts_equal_the_surviving_rows(self, rooted):
        from missioncache_db import pm_items

        db = missioncache_db.TaskDB(); db.initialize()
        keep = _project(rooted, db, "keep",
                        waiting_rows="| answer from Keren | Keren | 2026-01-01 | merge |")
        drop = _project(rooted, db, "drop",
                        waiting_rows="| answer from Tamir | Tamir | 2026-01-01 | deploy |")
        pm_items.add_action_item(db, keep.id, "mine, late", due_date="2000-01-01")
        pm_items.add_action_item(db, drop.id, "also mine, late", due_date="2000-01-01")
        full = portfolio.build_portfolio(db, today=date(2026, 9, 8), user_name="Tomer")
        db.close()

        assert full["counts"]["overdue"] == 2
        assert full["counts"]["on_others"] == 2

        scoped = portfolio.scope_portfolio(full, {"keep"})
        assert [p["name"] for p in scoped["projects"]] == ["keep"]
        assert [g["project_name"] for g in scoped["on_others"]] == ["keep"]
        assert all(r["project_name"] == "keep" for r in scoped["on_me"]["overdue"])
        # The whole point: every count re-derives from what survived.
        assert scoped["counts"] == {
            "on_me": 1, "overdue": 1, "on_others": 1,
            "on_others_projects": 1, "stale": 1,
        }

    def test_scope_does_not_mutate_the_full_result(self, rooted):
        db = missioncache_db.TaskDB(); db.initialize()
        _project(rooted, db, "a", waiting_rows="| x | Dana | 2026-01-01 | y |")
        full = portfolio.build_portfolio(db, today=date(2026, 9, 8), user_name="Tomer")
        db.close()
        # Deep snapshot, not a length. `scope_portfolio` returns a SHALLOW
        # copy whose groups and rows are the caller's own objects, so a length
        # check is the one thing an in-place write could never disturb.
        snapshot = copy.deepcopy(full)
        portfolio.scope_portfolio(full, set())
        assert full == snapshot


class TestChecklistProgress:
    # The unnumbered item sits ABOVE the numbered one deliberately. Below it,
    # item 2 wins next_up whatever the regex does, and the "never become
    # next_up" half of the test name is unfalsifiable: making the number
    # optional in _NUMBERED_ITEM_RE keeps every assertion green.
    TASKS = """# P - Tasks

## Tasks

- [x] 1. Done one `[auto]`
- [ ] unnumbered item that counts but has no identity
- [ ] 2. Next one `[inter:depends=1]`
- [ ] 3. Third
"""

    def test_unnumbered_items_count_but_never_become_next_up(self, rooted):
        (rooted / "active" / "p").mkdir(parents=True)
        (rooted / "active" / "p" / "p-tasks.md").write_text(self.TASKS)
        got = portfolio._checklist_progress(rooted, "", "manual/p")
        assert got["total_count"] == 4
        assert got["completed_count"] == 1
        assert got["completion_pct"] == 25
        assert got["next_up"] == "Next one"  # mode marker stripped

    def test_missing_file_is_zeros_not_an_error(self, rooted):
        assert portfolio._checklist_progress(rooted, "", "manual/nope") == {
            "completed_count": 0, "total_count": 0, "completion_pct": 0, "next_up": None,
        }

    def test_empty_full_path_is_zeros(self, rooted):
        assert portfolio._checklist_progress(rooted, "", "")["total_count"] == 0


class TestOwnerNames:
    @pytest.mark.parametrize("raw, expected", [
        ("Ilya (on vacation til Wed)", ["ilya"]),
        ("Itai Sela (IT) - I said I would handle it", ["itai"]),
        ("Keren / Tamir + Dana, Oren", ["keren", "tamir", "dana", "oren"]),
        ("", []),
        (None, []),
    ])
    def test_normalizes_prose_cells(self, raw, expected):
        assert portfolio.owner_names(raw) == expected


class TestSelfNames:
    def test_includes_the_lowercased_first_name(self):
        assert portfolio.self_names("Tomer") == frozenset({"me", "myself", "tomer"})

    def test_base_only_when_unknown(self):
        assert portfolio.self_names(None) == frozenset({"me", "myself"})


class TestWatermark:
    def test_stable_when_nothing_changed(self, rooted, monkeypatch):
        monkeypatch.setattr(missioncache_db, "HOOKS_STATE_DB_PATH", rooted / "no-hooks.db")
        db = missioncache_db.TaskDB(); db.initialize()
        _project(rooted, db, "a")
        first = portfolio.portfolio_watermark(db)
        second = portfolio.portfolio_watermark(db)
        db.close()
        assert first == second

    def test_moves_on_an_action_item_write(self, rooted, monkeypatch):
        from missioncache_db import pm_items
        monkeypatch.setattr(missioncache_db, "HOOKS_STATE_DB_PATH", rooted / "no-hooks.db")
        db = missioncache_db.TaskDB(); db.initialize()
        task = _project(rooted, db, "a")
        before = portfolio.portfolio_watermark(db)
        pm_items.add_action_item(db, task.id, "something new", due_date="2099-01-01")
        after = portfolio.portfolio_watermark(db)
        db.close()
        assert before != after

    def test_moves_on_a_context_file_edit(self, rooted, monkeypatch):
        import os, time
        monkeypatch.setattr(missioncache_db, "HOOKS_STATE_DB_PATH", rooted / "no-hooks.db")
        db = missioncache_db.TaskDB(); db.initialize()
        _project(rooted, db, "a")
        before = portfolio.portfolio_watermark(db)
        ctx = rooted / "active" / "a" / "a-context.md"
        # Force a distinct mtime rather than relying on filesystem resolution.
        future = time.time() + 120
        os.utime(ctx, (future, future))
        after = portfolio.portfolio_watermark(db)
        db.close()
        assert before != after

    def test_never_raises_without_stores(self, rooted, monkeypatch):
        monkeypatch.setattr(missioncache_db, "HOOKS_STATE_DB_PATH", rooted / "no-hooks.db")
        monkeypatch.setattr(missioncache_db, "MISSIONCACHE_ROOT", rooted / "nope")
        assert isinstance(portfolio.portfolio_watermark(object()), str)

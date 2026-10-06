"""Tests for the event log (missioncache_db.events).

Spec source: docs/lead-dashboard-plan.md, Phase 1a. Every write tool
records one event per item it changed; rows are read newest first with
since / project / kind filters; the ticket is the project's own key when
set, else the first key in the text, else null; events older than the
retention window are pruned; the CLI lists them.
"""

import json
import os
import subprocess
import sys

import pytest

from missioncache_db import TaskDB, events


@pytest.fixture
def db(tmp_path):
    db = TaskDB(db_path=tmp_path / "test.db")
    db.initialize()
    yield db
    db.close()


class TestRecordAndList:
    def test_round_trip_newest_first(self, db):
        events.record_events(db, "alpha", [
            ("recent_change", "first change", None),
            ("waiting_added", "answer from Robin (Robin)", "Waiting on"),
        ])
        rows = events.list_events(db)
        assert [(r["kind"], r["what"]) for r in rows] == [
            ("waiting_added", "answer from Robin (Robin)"),
            ("recent_change", "first change"),
        ]
        assert rows[0]["project"] == "alpha"
        assert rows[0]["section"] == "Waiting on"

    def test_filters_by_project_kind_and_since(self, db):
        events.record_events(db, "alpha", [("recent_change", "a1", None)])
        events.record_events(db, "beta", [("task_done", "b1", None), ("recent_change", "b2", None)])
        with db.connection() as conn:
            conn.execute("UPDATE events SET created_at = '2026-01-01 09:00:00' WHERE what = 'a1'")
            conn.commit()

        assert {r["what"] for r in events.list_events(db, project="beta")} == {"b1", "b2"}
        assert [r["what"] for r in events.list_events(db, kinds=["task_done"])] == ["b1"]
        assert [r["what"] for r in events.list_events(db, since="2026-06-01")] == ["b2", "b1"]

    def test_limit_keeps_the_newest(self, db):
        events.record_events(db, "alpha", [("recent_change", f"c{i}", None) for i in range(5)])
        assert [r["what"] for r in events.list_events(db, limit=2)] == ["c4", "c3"]

    def test_unknown_kind_is_refused(self, db):
        with pytest.raises(ValueError, match="unknown event kind"):
            events.record_events(db, "alpha", [("recnet_change", "typo", None)])
        assert events.list_events(db) == []

    def test_multi_line_text_becomes_one_line(self, db):
        events.record_events(db, "alpha", [("recent_change", "line one\n\n## heading\nline two", None)])
        assert events.list_events(db)[0]["what"] == "line one ## heading line two"

    def test_empty_text_records_nothing(self, db):
        assert events.record_events(db, "alpha", [("recent_change", "   ", None)]) == 0
        assert events.list_events(db) == []

    def test_an_iso_since_with_a_t_matches_the_same_rows(self, db):
        """isoformat() output is what a model passes. Compared as raw text,
        the 'T' sorts after the stored space and drops the whole day."""
        events.record_events(db, "alpha", [("recent_change", "old", None), ("recent_change", "today", None)])
        with db.connection() as conn:
            conn.execute("UPDATE events SET created_at = '2020-01-01 09:00:00' WHERE what = 'old'")
            conn.commit()
        day = events.list_events(db, limit=1)[0]["created_at"][:10]
        assert [r["what"] for r in events.list_events(db, since=f"{day}T00:00:00")] == ["today"]
        assert [r["what"] for r in events.list_events(db, since=f"{day}T00:00:00.123+03:00")] == ["today"]

    def test_an_offset_is_converted_to_local_time_not_dropped(self):
        from datetime import datetime, timezone

        expected = (
            datetime(2026, 10, 6, 7, 0, tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        )
        assert events.normalize_since("2026-10-06T07:00:00Z") == expected
        assert events.normalize_since("2026-10-06T07:00:00+00:00") == expected
        assert events.normalize_since("2026-10-06T09:30:00.5") == "2026-10-06 09:30:00"

    def test_a_since_that_is_not_a_date_is_refused(self, db):
        with pytest.raises(ValueError, match="since must look like"):
            events.list_events(db, since="yesterday")

    def test_after_id_returns_exactly_the_newer_rows(self, db):
        events.record_events(db, "alpha", [("recent_change", f"c{i}", None) for i in range(3)])
        cursor = events.list_events(db, limit=3)[1]["id"]
        assert [r["what"] for r in events.list_events(db, after_id=cursor)] == ["c2"]

    def test_limit_is_clamped(self, db):
        events.record_events(db, "alpha", [("recent_change", f"c{i}", None) for i in range(3)])
        assert len(events.list_events(db, limit=0)) == 1
        assert len(events.list_events(db, limit=10**9)) == 3

    def test_an_unknown_kind_filter_is_refused(self, db):
        with pytest.raises(ValueError, match="unknown event kind"):
            events.list_events(db, kinds=["recnet_change"])

    def test_task_id_follows_a_rename(self, db):
        task = db.create_task("old-name")
        db.create_task("decoy")
        events.record_events(db, "old-name", [("recent_change", "before", None)])
        events.record_events(db, "decoy", [("recent_change", "someone else", None)])
        db.rename_task(task.id, "new-name")
        events.record_events(db, "new-name", [("recent_change", "after", None)])
        assert [r["what"] for r in events.list_events(db, task_id=task.id)] == [
            "after", "old-name -> new-name", "before",
        ]


class TestTicket:
    def test_project_key_wins(self, db):
        db.create_task("alpha", jira_key="PROJ-1")
        events.record_events(db, "alpha", [("recent_change", "fixed OTHER-9 too", None)])
        assert events.list_events(db)[0]["ticket"] == "PROJ-1"

    def test_a_ticket_row_wins_over_the_legacy_key(self, db):
        from missioncache_db import pm_items

        task = db.create_task("alpha", jira_key="OLD-1")
        pm_items.add_ticket(db, task.id, "PROJ-7", refresh_mirror=False)
        events.record_events(db, "alpha", [("recent_change", "note", None)])
        assert events.list_events(db)[0]["ticket"] == "PROJ-7"

    def test_key_in_text_when_project_has_none(self, db):
        db.create_task("alpha")
        events.record_events(db, "alpha", [("recent_change", "merged PROJ-42 after review", None)])
        assert events.list_events(db)[0]["ticket"] == "PROJ-42"

    def test_no_ticket_anywhere(self, db):
        events.record_events(db, "alpha", [("recent_change", "plain note", None)])
        assert events.list_events(db)[0]["ticket"] is None


class TestRecordedByTheDatabaseWrites:
    """The dashboard and the CLI write through pm_items and TaskDB, so those
    writes must record without any MCP code involved."""

    def test_action_items_and_due_date(self, db):
        from missioncache_db import pm_items

        task = db.create_task("alpha")
        item = pm_items.add_action_item(db, task.id, "send the numbers", refresh_mirror=False)
        pm_items.update_action_item(db, item.id, status="done", refresh_mirror=False)
        pm_items.set_project_due_date(db, task.id, "2099-01-01", refresh_mirror=False)
        assert [(r["kind"], r["what"]) for r in reversed(events.list_events(db))] == [
            ("action_item", f"Action item added ({item.label}): send the numbers (owner: me)"),
            ("action_item", f"Action item done ({item.label}): send the numbers"),
            ("due_date", "Project due date set: 2099-01-01"),
        ]
        assert {r["task_id"] for r in events.list_events(db)} == {task.id}

    def test_an_update_that_changes_nothing_records_nothing(self, db):
        from missioncache_db import pm_items

        task = db.create_task("alpha")
        item = pm_items.add_action_item(db, task.id, "x", refresh_mirror=False)
        pm_items.update_action_item(db, item.id, refresh_mirror=False)
        assert len(events.list_events(db)) == 1

    def test_complete_reopen_and_rename(self, db, tmp_path, monkeypatch):
        import missioncache_db

        monkeypatch.setattr(missioncache_db, "MISSIONCACHE_ROOT", tmp_path / "root")
        task = db.create_task("alpha", task_type="non-coding")
        db.complete_project(task.id, move_files=False)
        db.reopen_project(task.id, move_files=False)
        db.rename_task(task.id, "beta")
        assert [r["kind"] for r in reversed(events.list_events(db))] == [
            "completed", "reopened", "renamed",
        ]
        assert events.list_events(db)[0]["what"] == "alpha -> beta"


class TestPrune:
    def test_drops_only_rows_past_the_window(self, db):
        events.record_events(db, "alpha", [("recent_change", "old", None), ("recent_change", "new", None)])
        with db.connection() as conn:
            conn.execute(
                "UPDATE events SET created_at = datetime('now', 'localtime', '-91 days') WHERE what = 'old'"
            )
            conn.commit()
        assert events.prune_events(db, days=90) == 1
        assert [r["what"] for r in events.list_events(db)] == ["new"]

    def test_a_window_below_one_day_is_refused(self, db):
        events.record_events(db, "alpha", [("recent_change", "keep me", None)])
        for days in (0, -1):
            with pytest.raises(ValueError, match="at least 1"):
                events.prune_events(db, days=days)
        assert len(events.list_events(db)) == 1

    def test_clearing_one_project_leaves_the_others(self, db):
        events.record_events(db, "alpha", [("recent_change", "a", None)])
        events.record_events(db, "beta", [("recent_change", "b", None)])
        assert events.delete_project_events(db, "alpha") == 1
        assert [r["project"] for r in events.list_events(db)] == ["beta"]


class TestEventsCLI:
    def test_list_json_filters_by_project(self, tmp_path):
        root = tmp_path / ".missioncache"
        (root / "active").mkdir(parents=True)
        db = TaskDB(db_path=root / "tasks.db")
        db.initialize()
        events.record_events(db, "alpha", [("recent_change", "from alpha", None)])
        events.record_events(db, "beta", [("recent_change", "from beta", None)])
        db.close()

        env = {**os.environ, "MISSIONCACHE_ROOT": str(root)}
        r = subprocess.run(
            [sys.executable, "-c", "from missioncache_db import main; main()",
             "events", "list", "--project", "alpha", "--json"],
            capture_output=True, text=True, env=env,
        )
        assert r.returncode == 0, r.stderr
        assert [row["what"] for row in json.loads(r.stdout)] == ["from alpha"]

        # The CLI's own complete-task / reopen-task record too.
        db = TaskDB(db_path=root / "tasks.db")
        task = db.create_task("gamma", task_type="non-coding")
        db.close()
        for cmd in ("complete-task", "reopen-task"):
            r = subprocess.run(
                [sys.executable, "-c", "from missioncache_db import main; main()", cmd, str(task.id)],
                capture_output=True, text=True, env=env,
            )
            assert r.returncode == 0, r.stdout + r.stderr
        r = subprocess.run(
            [sys.executable, "-c", "from missioncache_db import main; main()",
             "events", "list", "--project", "gamma", "--json"],
            capture_output=True, text=True, env=env,
        )
        assert [row["kind"] for row in json.loads(r.stdout)] == ["reopened", "completed"]

        # An unknown flag on prune is refused, not ignored: --dry-run must
        # never delete for real.
        r = subprocess.run(
            [sys.executable, "-c", "from missioncache_db import main; main()",
             "events", "prune", "--dry-run"],
            capture_output=True, text=True, env=env,
        )
        assert r.returncode == 1
        assert "unknown argument" in r.stdout

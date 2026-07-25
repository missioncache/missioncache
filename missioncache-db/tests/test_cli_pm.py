"""Tests for the PM CLI command groups (action-item / stakeholder / ticket / due-date).

These 185 lines of `main()` dispatch shipped with zero automated tests, which a
2026-07-24 review pass flagged as the largest coverage gap in the PM layer. The
harness is the one `test_cli_health.py` established: run `main()` in-process with
`sys.argv` monkeypatched and `MISSIONCACHE_ROOT` / `DB_PATH` redirected to tmp.

Spec source: docs/cli.md "Project management" plus the usage block at the top of
missioncache_db/__init__.py. Every documented flag is exercised, the documented
clear sentinel (`none`) is exercised on both surfaces that accept it, and the
contract that a CLI mutation renders the context mirror is asserted rather than
assumed - that is the whole reason these commands route through pm_items.
"""

import json
import sys

import pytest

import missioncache_db
from missioncache_db import pm_items


CONTEXT = """# Cli Proj - Context

**Last Updated:** 2026-07-01 10:00

## Description

CLI test project.

## Gotchas

- TBD

## Waiting on

| What | Who | Since | Gates |
|------|-----|-------|-------|

## Next Steps

1. TBD

## Recent Changes

### 2026-07-01 10:00

- created
"""


@pytest.fixture
def cli(tmp_path, monkeypatch, capsys):
    """A tmp root with one registered coding project and a context file.

    Returns a ``run(*argv)`` helper that invokes ``main()`` and returns parsed
    JSON stdout (the PM commands all print JSON), plus the context path.
    """
    root = tmp_path / ".missioncache"
    (root / "active").mkdir(parents=True)
    monkeypatch.setattr(missioncache_db, "MISSIONCACHE_ROOT", root)
    monkeypatch.setattr(missioncache_db, "DB_PATH", root / "tasks.db")
    monkeypatch.setattr(missioncache_db, "_LEGACY_CLAUDE_DB", tmp_path / "no-legacy")
    monkeypatch.setattr(missioncache_db, "_LEGACY_CLAUDE_ORBIT_ROOT", tmp_path / "no-orbit")
    monkeypatch.setattr(missioncache_db, "_LEGACY_ORBIT_DB", tmp_path / "no-orbit-db")
    monkeypatch.setattr(missioncache_db, "_LEGACY_ORBIT_ROOT", tmp_path / "no-orbit-root")
    monkeypatch.setattr(
        pm_items, "_dashboard_config_file", lambda: tmp_path / "no-config.json"
    )

    db = missioncache_db.TaskDB(db_path=root / "tasks.db")
    db.initialize()
    db.create_task("cli-proj", task_type="coding", repo_id=None)
    db.close()

    proj_dir = root / "active" / "cli-proj"
    proj_dir.mkdir()
    ctx = proj_dir / "cli-proj-context.md"
    ctx.write_text(CONTEXT)

    def run(*argv):
        monkeypatch.setattr(sys, "argv", ["missioncache-db", *argv])
        missioncache_db.main()
        out = capsys.readouterr().out
        return json.loads(out) if out.strip() else out

    return run, ctx


class TestActionItemCli:
    def test_add_with_every_documented_flag(self, cli):
        run, ctx = cli
        item = run(
            "action-item", "add", "cli-proj", "send", "the", "numbers",
            "--from", "Lior", "--owner", "Yuval", "--due", "2099-08-01",
            "--source", "weekly sync", "--notes", "context here",
        )
        assert item["what"] == "send the numbers"
        assert item["requested_by"] == "Lior"
        assert item["assignee"] == "Yuval"
        assert item["due_date"] == "2099-08-01"
        assert item["source"] == "weekly sync"
        assert item["notes"] == "context here"
        assert item["status"] == "open"

    def test_add_renders_the_context_mirror(self, cli):
        """The reason the CLI routes through pm_items at all."""
        run, ctx = cli
        run("action-item", "add", "cli-proj", "mirror me")
        content = ctx.read_text()
        assert "## Action Items" in content
        assert "| AI-1 | mirror me |" in content
        assert "Action item added (AI-1): mirror me" in content

    def test_resolves_project_by_name_or_id(self, cli):
        run, _ = cli
        by_name = run("action-item", "add", "cli-proj", "by name")
        by_id = run("action-item", "add", str(by_name["task_id"]), "by id")
        assert by_id["task_id"] == by_name["task_id"]

    def test_list_scoped_and_filtered(self, cli):
        run, _ = cli
        run("action-item", "add", "cli-proj", "mine")
        run("action-item", "add", "cli-proj", "theirs", "--owner", "Yuval")
        assert len(run("action-item", "list", "cli-proj")) == 2
        assert [i["what"] for i in run(
            "action-item", "list", "cli-proj", "--owner", "Yuval"
        )] == ["theirs"]
        assert [i["what"] for i in run(
            "action-item", "list", "cli-proj", "--status", "open"
        )] == ["mine", "theirs"]

    def test_list_overdue_and_due_within(self, cli):
        from datetime import date, timedelta

        run, _ = cli
        past = (date.today() - timedelta(days=2)).isoformat()
        soon = (date.today() + timedelta(days=3)).isoformat()
        run("action-item", "add", "cli-proj", "late", "--due", past)
        run("action-item", "add", "cli-proj", "soon", "--due", soon)
        run("action-item", "add", "cli-proj", "later", "--due", "2099-01-01")
        assert [i["what"] for i in run(
            "action-item", "list", "cli-proj", "--overdue"
        )] == ["late"]
        assert [i["what"] for i in run(
            "action-item", "list", "cli-proj", "--due-within", "7"
        )] == ["late", "soon"]

    def test_list_without_a_task_spans_projects(self, cli):
        run, _ = cli
        run("action-item", "add", "cli-proj", "somewhere")
        assert [i["project_name"] for i in run("action-item", "list")] == ["cli-proj"]

    def test_done_records_the_outcome(self, cli):
        run, ctx = cli
        run("action-item", "add", "cli-proj", "do it")
        done = run("action-item", "done", "1", "sent", "by", "mail")
        assert done["status"] == "done"
        assert done["notes"] == "sent by mail"
        assert done["completed_at"]
        assert "Action item done (AI-1)" in ctx.read_text()

    def test_update_fields_and_the_clear_sentinel(self, cli):
        run, _ = cli
        run("action-item", "add", "cli-proj", "x", "--due", "2099-01-01")
        assert run(
            "action-item", "update", "1", "--what", "renamed", "--owner", "Mor",
            "--from", "Keren", "--notes", "n", "--source", "s",
        )["what"] == "renamed"
        assert run("action-item", "update", "1", "--due", "none")["due_date"] is None
        assert run("action-item", "update", "1", "--status", "dropped")["status"] == "dropped"

    def test_bad_due_date_prints_the_message_not_a_traceback(self, cli):
        run, _ = cli
        with pytest.raises(SystemExit) as exc:
            run("action-item", "add", "cli-proj", "x", "--due", "tomorrow")
        assert exc.value.code == 1

    def test_non_numeric_item_id_exits_cleanly(self, cli):
        run, _ = cli
        with pytest.raises(SystemExit) as exc:
            run("action-item", "done", "not-a-number")
        assert exc.value.code == 1

    def test_unknown_project_exits_cleanly(self, cli):
        run, _ = cli
        with pytest.raises(SystemExit) as exc:
            run("action-item", "add", "no-such-project", "x")
        assert exc.value.code == 1


class TestStakeholderCli:
    def test_add_list_remove_and_mirror(self, cli):
        run, ctx = cli
        added = run(
            "stakeholder", "add", "cli-proj", "Lior", "Ben", "Naon",
            "--role", "Manager", "--notes", "direct",
        )
        assert added["name"] == "Lior Ben Naon"
        assert added["role"] == "Manager"
        assert "## Stakeholders" in ctx.read_text()

        assert [s["name"] for s in run("stakeholder", "list", "cli-proj")] == [
            "Lior Ben Naon"
        ]
        assert run("stakeholder", "remove", "cli-proj", "Lior", "Ben", "Naon") == {
            "removed": True
        }
        assert run("stakeholder", "remove", "cli-proj", "Nobody") == {"removed": False}

    def test_add_is_an_upsert(self, cli):
        run, _ = cli
        run("stakeholder", "add", "cli-proj", "Keren", "--role", "SIA lead")
        assert run(
            "stakeholder", "add", "cli-proj", "Keren", "--role", "SIA manager"
        )["role"] == "SIA manager"
        assert len(run("stakeholder", "list", "cli-proj")) == 1


class TestTicketCli:
    def test_add_list_remove_and_mirror(self, cli):
        run, ctx = cli
        added = run(
            "ticket", "add", "cli-proj", "GC-1234",
            "--url", "https://jira/GC-1234", "--system", "jira",
            "--status", "In Progress", "--notes", "parent",
        )
        assert added["label"] == "GC-1234"
        assert added["url"] == "https://jira/GC-1234"
        assert "## Tickets" in ctx.read_text()

        assert [t["label"] for t in run("ticket", "list", "cli-proj")] == ["GC-1234"]
        assert run("ticket", "remove", "cli-proj", "GC-1234") == {"removed": True}

    def test_javascript_url_is_rejected_cleanly(self, cli):
        run, _ = cli
        with pytest.raises(SystemExit) as exc:
            run("ticket", "add", "cli-proj", "GC-9", "--url", "javascript:alert(1)")
        assert exc.value.code == 1


class TestDueDateCli:
    def test_set_and_clear(self, cli):
        run, ctx = cli
        assert run("due-date", "cli-proj", "2099-06-30")["due_date"] == "2099-06-30"
        assert "**Due:** 2099-06-30" in ctx.read_text()
        assert run("due-date", "cli-proj", "none")["due_date"] is None
        assert "**Due:**" not in ctx.read_text()

    def test_malformed_date_exits_cleanly(self, cli):
        run, _ = cli
        with pytest.raises(SystemExit) as exc:
            run("due-date", "cli-proj", "30/06/2099")
        assert exc.value.code == 1

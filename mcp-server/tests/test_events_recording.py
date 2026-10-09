"""The write tools record what they changed in the event log.

Spec source: docs/lead-dashboard-plan.md, Phase 1a. One event per item a
write changed, with the kind of change; a resolve that matched no row
records nothing; a failure to record never fails the write; get_events
reads the rows back.
"""

import asyncio
import logging

import pytest

from mcp_missioncache import db as db_module, helpers, tools_docs, tools_pm, tools_tasks

CONTEXT = """# Event Proj - Context

**Last Updated:** 2026-07-10 12:00

## Description

Event recording test project.

## Gotchas

- TBD

## Waiting on

| What | Who | Since | Gates |
|------|-----|-------|-------|
| Schema signoff | Dana | 2026-07-01 | Rollout |

## Next Steps

1. TBD

## Recent Changes

### 2026-07-10 11:00

- created
"""

TASKS = """# Event Proj - Tasks

## Tasks

- [ ] 1. Write the parser
- [ ] 2. Add the tests
"""


@pytest.fixture
def project(isolated_orbit):
    _, root, _ = isolated_orbit
    d = root / "active" / "event-proj"
    d.mkdir(parents=True)
    ctx = d / "event-proj-context.md"
    ctx.write_text(CONTEXT)
    tasks = d / "event-proj-tasks.md"
    tasks.write_text(TASKS)
    db_module.get_db().create_task("event-proj", task_type="coding", repo_id=None)
    return ctx, tasks


@pytest.fixture(autouse=True)
def _no_swallowed_recording_errors(caplog):
    """The recorder is best-effort, so a call-site bug (a typo in a kind)
    would otherwise only reach a log nobody reads. Fail the test instead."""
    caplog.set_level(logging.ERROR)
    yield
    swallowed = [r for r in caplog.records if "recording" in r.getMessage().lower()]
    assert not swallowed, [r.getMessage() for r in swallowed]


def _events(**kw):
    result = asyncio.run(tools_pm.get_events(**kw))
    assert result["success"] is True, result
    return [(e["kind"], e["what"]) for e in reversed(result["events"])]


def _sections(**kw):
    result = asyncio.run(tools_pm.get_events(**kw))
    return [e["section"] for e in reversed(result["events"])]


def test_a_mixed_context_write_records_one_event_per_item(project):
    ctx, _ = project
    result = asyncio.run(tools_docs.update_context_file(
        context_file=str(ctx),
        recent_changes=["wired the parser", "fixed the flaky test", "bumped the floor"],
        waiting_on_add=[{"what": "Broker config review", "who": "Robin", "gates": "Retry rollout"}],
        waiting_on_resolve=[
            {"match": "Schema signoff", "outcome": "approved as-is"},
            {"match": "No such row", "outcome": "ignored"},
        ],
    ))
    assert result["success"] is True
    assert result["waiting_on_unmatched"] == ["No such row"]
    assert _events(project="event-proj") == [
        ("recent_change", "wired the parser"),
        ("recent_change", "fixed the flaky test"),
        ("recent_change", "bumped the floor"),
        ("waiting_added", "Broker config review (Robin)"),
        ("waiting_resolved", "Resolved (was waiting on Dana): Schema signoff - approved as-is"),
    ]
    assert _sections(project="event-proj") == (
        ["Recent Changes"] * 3 + ["Waiting on", "Waiting on"]
    )


def test_the_same_match_twice_still_records_the_one_row_it_removed(project):
    ctx, _ = project
    asyncio.run(tools_docs.update_context_file(
        context_file=str(ctx),
        waiting_on_resolve=[
            {"match": "Schema signoff", "outcome": "approved"},
            {"match": "Schema signoff", "outcome": "again"},
        ],
    ))
    assert _events(kinds=["waiting_resolved"]) == [
        ("waiting_resolved", "Resolved (was waiting on Dana): Schema signoff - approved"),
    ]


def test_an_imported_event_records_its_heading(project):
    ctx, _ = project
    asyncio.run(tools_docs.update_context_file(
        context_file=str(ctx),
        imported_event={
            "heading": "Broker moved to the new cluster",
            "body": "The other project changed the broker.",
            "related_project": "other-proj",
            "related_note": "shared broker",
        },
    ))
    events = asyncio.run(tools_pm.get_events(project="event-proj"))["events"]
    assert [e["kind"] for e in events] == ["recent_change"]
    assert events[0]["what"].startswith("Broker moved to the new cluster")

    # The same event again is a duplicate the write skips, so no second row.
    again = asyncio.run(tools_docs.update_context_file(
        context_file=str(ctx),
        imported_event={
            "heading": events[0]["what"],
            "body": "The other project changed the broker.",
            "related_project": "other-proj",
            "related_note": "shared broker",
        },
    ))
    assert again.get("imported_event_duplicate") is True, again
    assert len(asyncio.run(tools_pm.get_events(project="event-proj"))["events"]) == 1


def test_ticking_a_task_records_its_full_text(project):
    _, tasks = project
    asyncio.run(tools_docs.update_tasks_file(tasks_file=str(tasks), completed_tasks=["2"]))
    assert _events(kinds=["task_done"]) == [("task_done", "2. Add the tests")]


def test_action_items_and_a_due_date_are_recorded_once_each(project):
    """Recorded by the database layer: the tools must not add a second row."""
    asyncio.run(tools_pm.add_action_item(project_name="event-proj", what="send the numbers"))
    asyncio.run(tools_pm.update_action_item(item_id=1, status="done"))
    asyncio.run(tools_pm.update_action_item(item_id=1))
    asyncio.run(tools_pm.set_project_due_date(project_name="event-proj", due_date="2099-01-01"))
    assert [k for k, _ in _events(project="event-proj")] == ["action_item", "action_item", "due_date"]


def test_a_move_records_on_both_projects_and_a_miss_records_nothing(project, isolated_orbit):
    _, root, _ = isolated_orbit
    d = root / "active" / "other-proj"
    d.mkdir(parents=True)
    (d / "other-proj-context.md").write_text(CONTEXT.replace("Event Proj", "Other Proj"))
    db_module.get_db().create_task("other-proj", task_type="coding", repo_id=None)

    miss = asyncio.run(tools_docs.move_to_project(
        source_project="event-proj", target_project="other-proj",
        waiting_on=["No such row"],
    ))
    assert miss["success"] is True, miss
    assert miss["summary"] == "nothing matched - no files were written"
    assert _events(kinds=["moved"]) == []

    asyncio.run(tools_docs.move_to_project(
        source_project="event-proj", target_project="other-proj",
        waiting_on=["Schema signoff"],
    ))
    moved = asyncio.run(tools_pm.get_events(kinds=["moved"]))["events"]
    assert sorted(e["project"] for e in moved) == ["event-proj", "other-proj"]


def test_reopen_and_rename_are_recorded(project):
    asyncio.run(tools_tasks.complete_task(project_name="event-proj", move_files=False))
    asyncio.run(tools_tasks.reopen_task(project_name="event-proj", move_files=False))
    asyncio.run(tools_tasks.rename_task(project_name="event-proj", new_name="event-renamed"))
    recorded = _events()
    assert [k for k, _ in recorded] == ["completed", "reopened", "renamed"]
    assert recorded[1:] == [("reopened", "reopened"), ("renamed", "event-proj -> event-renamed")]


def test_completing_a_project_is_recorded(project):
    result = asyncio.run(tools_tasks.complete_task(project_name="event-proj"))
    assert result.get("error") is not True, result
    assert len(_events(project="event-proj")) == 1
    kind, what = _events(project="event-proj")[0]
    assert kind == "completed" and what.startswith("completed after")


def test_a_failed_record_never_fails_the_write(project, monkeypatch, caplog):
    from missioncache_db import events

    calls = []

    def boom(*a, **k):
        calls.append(a)
        raise RuntimeError("disk full")

    monkeypatch.setattr(events, "record_events", boom)
    ctx, _ = project
    result = asyncio.run(tools_docs.update_context_file(
        context_file=str(ctx), recent_changes=["still saved"],
    ))
    assert result["success"] is True
    assert "still saved" in ctx.read_text()
    assert len(calls) == 1, "the recorder was never reached"
    # This one failure is the point of the test; clear it so the autouse
    # guard only catches failures nobody expected.
    assert any("Error recording events" in r.getMessage() for r in caplog.records)
    caplog.clear()


def test_get_events_pages_from_the_cursor_and_flags_more(project):
    ctx, _ = project
    asyncio.run(tools_docs.update_context_file(context_file=str(ctx), recent_changes=["a", "b", "c"]))
    first = asyncio.run(tools_pm.get_events(limit=3))["events"][-1]["id"]
    page = asyncio.run(tools_pm.get_events(after_id=first, limit=1))
    assert [e["what"] for e in page["events"]] == ["b"] and page["has_more"] is True
    rest = asyncio.run(tools_pm.get_events(after_id=page["events"][0]["id"], limit=1))
    assert [e["what"] for e in rest["events"]] == ["c"] and rest["has_more"] is False


def test_get_events_refuses_bad_input(project):
    result = asyncio.run(tools_pm.get_events(kinds=["recnet_change"]))
    assert result["error"] is True and "recnet_change" in result["message"]
    result = asyncio.run(tools_pm.get_events(since="yesterday"))
    assert result["error"] is True and "since" in result["message"]


def test_no_project_name_records_nothing(isolated_orbit):
    helpers.record_tool_events(None, [("recent_change", "orphan", None)])
    assert _events() == []

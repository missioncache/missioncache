"""Tests for the live-peer lookup behind update_context_file's live_sessions.

Spec source: "Cross-session notifications" in rules/missioncache.md. The
contract the caller depends on:

* The project is taken from the file being written, so a write into ANOTHER
  project's context reports that project's sessions - the case the whole
  feature exists for.
* The caller is never in its own results.
* When the caller's own session id cannot be resolved, the key is omitted
  rather than returned unfiltered: ListAgents never lists the calling session,
  so its own row could never match and the caller would be sent into the
  ask-the-user fallback for nothing.
* Identity comes from this MCP subprocess's own CLAUDE_CODE_SESSION_ID.
  It must NOT be derived from the process tree: one claude process hosts many
  sessions, so pid to session is one-to-many.
"""

import asyncio

import pytest

import missioncache_db

from mcp_missioncache import helpers, tools_docs


@pytest.fixture
def peers(monkeypatch):
    """Capture the arguments the lookup is called with, return one fake peer."""
    calls = {}

    def _fake(project_name, exclude_session_id=None):
        calls["project_name"] = project_name
        calls["exclude_session_id"] = exclude_session_id
        return [{"session_id": "sid-peer", "title": project_name, "last_active": "now"}]

    monkeypatch.setattr(missioncache_db, "live_sessions_for_project", _fake)
    return calls


def _resolve_to(monkeypatch, session_id):
    monkeypatch.setattr(helpers, "_resolve_session_id", lambda _=None: session_id)


def test_project_comes_from_the_context_filename(monkeypatch, peers, tmp_path):
    _resolve_to(monkeypatch, "sid-mine")
    helpers.live_peer_sessions_for_context_file(tmp_path / "other-proj-context.md")
    assert peers["project_name"] == "other-proj"


def test_caller_is_excluded(monkeypatch, peers, tmp_path):
    _resolve_to(monkeypatch, "sid-mine")
    helpers.live_peer_sessions_for_context_file(tmp_path / "demo-context.md")
    assert peers["exclude_session_id"] == "sid-mine"


def test_returns_the_peers(monkeypatch, peers, tmp_path):
    _resolve_to(monkeypatch, "sid-mine")
    found = helpers.live_peer_sessions_for_context_file(tmp_path / "demo-context.md")
    assert [p["session_id"] for p in found] == ["sid-peer"]


def test_empty_when_the_session_id_is_unresolvable(monkeypatch, peers, tmp_path):
    _resolve_to(monkeypatch, None)
    assert helpers.live_peer_sessions_for_context_file(tmp_path / "demo-context.md") == []
    assert peers == {}, "must not query at all without a caller id to exclude"


def test_empty_for_a_bare_context_filename(monkeypatch, peers, tmp_path):
    """The subtask layout writes `context.md`, which carries no project name.
    Those are nested under a parent and nobody addresses them as projects."""
    _resolve_to(monkeypatch, "sid-mine")
    assert helpers.live_peer_sessions_for_context_file(tmp_path / "context.md") == []


def test_a_lookup_failure_does_not_propagate(monkeypatch, tmp_path):
    """A successful write must not be reported as a failed tool call because
    the notification lookup blew up."""
    _resolve_to(monkeypatch, "sid-mine")

    def _boom(*_args, **_kwargs):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(missioncache_db, "live_sessions_for_project", _boom)
    assert helpers.live_peer_sessions_for_context_file(tmp_path / "demo-context.md") == []


# ── the MCP wrapper's imported_event guard ────────────────────────────────


def test_half_an_imported_event_is_rejected_not_dropped(tmp_path, monkeypatch):
    """A heading with no body writes nothing, so reporting success would claim
    a section that does not exist. Same never-silently-drop contract as
    waiting_on_unmatched."""
    import asyncio

    from mcp_missioncache.config import Settings

    monkeypatch.setattr(tools_docs, "settings", Settings(root=tmp_path))
    monkeypatch.setattr(
        "mcp_missioncache.project_files.settings", Settings(root=tmp_path)
    )
    ctx = tmp_path / "demo-context.md"
    ctx.write_text("# demo - Context\n**Last Updated:** 2026-04-01 10:00\n")

    result = asyncio.run(
        tools_docs.update_context_file(
            context_file=str(ctx),
            imported_event={"heading": "Steering call", "body": ""},
        )
    )
    assert result["error"] is True
    assert result["code"] == "VALIDATION_ERROR"
    assert "Steering call" not in ctx.read_text()


# ── the MCP layer's imported_event validation and response wiring ─────────


def _settings_at(monkeypatch, tmp_path):
    from mcp_missioncache.config import Settings

    monkeypatch.setattr(tools_docs, "settings", Settings(root=tmp_path))
    monkeypatch.setattr("mcp_missioncache.project_files.settings", Settings(root=tmp_path))


def _ctx(tmp_path, name="demo-context.md"):
    ctx = tmp_path / name
    ctx.write_text(
        "# demo - Context\n**Last Updated:** 2026-04-01 10:00\n"
        "\n## Waiting on\n\n| What | Who | Since | Gates |\n|---|---|---|---|\n"
        "| real row | Dana | 2026-08-01 | rollout |\n"
        "\n## Next Steps\n\n1. real step\n"
    )
    return ctx


def _call(ctx, **kwargs):
    import asyncio

    return asyncio.run(tools_docs.update_context_file(context_file=str(ctx), **kwargs))


@pytest.mark.parametrize(
    "event, field",
    [
        ({"heading": "a\n\n## Waiting on", "body": "- x"}, "heading"),
        ({"heading": "a\r## Waiting on", "body": "- x"}, "heading"),
        ({"heading": "a", "body": "- x", "related_project": "x]]\n**Fork of:** [[victim"},
         "related_project"),
        ({"heading": "a", "body": "- x", "related_project": "../../etc/passwd"},
         "related_project"),
    ],
)
def test_structure_forging_input_is_rejected(tmp_path, monkeypatch, event, field):
    """A heading spanning lines forges sections; a related_project with a newline
    or a slash forges a header line the resume flow follows. Both reproduced
    against the pre-fix writer, so these are regression guards."""
    _settings_at(monkeypatch, tmp_path)
    ctx = _ctx(tmp_path)
    before = ctx.read_text()
    result = _call(ctx, imported_event=event)
    assert result["error"] is True
    assert result["code"] == "VALIDATION_ERROR"
    assert field in result.get("details", {}).get("field", field)
    assert ctx.read_text() == before, "a rejected event must not touch the file"


def test_live_sessions_key_present_with_peers(tmp_path, monkeypatch, peers):
    """The response key IS the public contract - commands/save.md and the rules
    both branch on its presence - so it needs asserting through the tool."""
    _settings_at(monkeypatch, tmp_path)
    _resolve_to(monkeypatch, "sid-mine")
    result = _call(_ctx(tmp_path))
    assert result["live_sessions"][0]["session_id"] == "sid-peer"


def test_live_sessions_key_absent_when_no_peers(tmp_path, monkeypatch):
    """Omitted rather than empty: the rule keys off presence."""
    _settings_at(monkeypatch, tmp_path)
    _resolve_to(monkeypatch, "sid-mine")
    monkeypatch.setattr(missioncache_db, "live_sessions_for_project", lambda *a, **k: [])
    assert "live_sessions" not in _call(_ctx(tmp_path))


def test_duplicate_event_is_reported_not_claimed(tmp_path, monkeypatch):
    """A repeat must not appear in sections_updated, which would claim a section
    this call did not write."""
    _settings_at(monkeypatch, tmp_path)
    _resolve_to(monkeypatch, None)
    ctx = _ctx(tmp_path)
    event = {"heading": "Steering call", "body": "- x"}
    first = _call(ctx, imported_event=event)
    second = _call(ctx, imported_event=event)
    assert "imported_event" in first["sections_updated"]
    assert "imported_event" not in second["sections_updated"]
    assert second["imported_event_duplicate"] is True


# ── the notification contract across every mutating writer ────────────────
#
# Spec source: rules/missioncache.md "Cross-session notifications" - a
# project's files changing under a live session means that session gets told.
# The PM mutators, update_tasks_file and rename_task rewrite project state
# exactly like update_context_file does, so they carry the same live_sessions
# contract: key present with peers, absent without.


def _fake_peers(monkeypatch, peers):
    monkeypatch.setattr(
        missioncache_db, "live_sessions_for_project", lambda *a, **k: list(peers)
    )
    monkeypatch.setattr(helpers, "_resolve_session_id", lambda _=None: "sid-mine")


def test_update_tasks_file_reports_live_sessions(tmp_path, monkeypatch):
    from mcp_missioncache.config import Settings

    monkeypatch.setattr(tools_docs, "settings", Settings(root=tmp_path))
    monkeypatch.setattr(
        "mcp_missioncache.project_files.settings", Settings(root=tmp_path)
    )
    _fake_peers(monkeypatch, [{"session_id": "sid-peer", "title": "demo", "last_active": "now"}])
    tasks = tmp_path / "demo-tasks.md"
    tasks.write_text(
        "# Demo - Tasks\n**Last Updated:** 2026-08-01\n\n## Tasks\n\n- [ ] 1. First\n",
        encoding="utf-8",
    )
    result = asyncio.run(
        tools_docs.update_tasks_file(tasks_file=str(tasks), completed_tasks=["First"])
    )
    assert result["success"] is True
    assert result["live_sessions"][0]["session_id"] == "sid-peer"

def test_update_tasks_file_legacy_unprefixed_skips_lookup(tmp_path, monkeypatch):
    """A bare tasks.md carries no project name - documented as the skip case."""
    from mcp_missioncache.config import Settings

    monkeypatch.setattr(tools_docs, "settings", Settings(root=tmp_path))
    monkeypatch.setattr(
        "mcp_missioncache.project_files.settings", Settings(root=tmp_path)
    )
    _fake_peers(monkeypatch, [{"session_id": "s", "title": "t", "last_active": "now"}])
    tasks = tmp_path / "tasks.md"
    tasks.write_text(
        "# Legacy - Tasks\n**Last Updated:** 2026-08-01\n\n## Tasks\n\n- [ ] 1. First\n",
        encoding="utf-8",
    )
    result = asyncio.run(
        tools_docs.update_tasks_file(tasks_file=str(tasks), completed_tasks=["First"])
    )
    assert result["success"] is True
    assert "live_sessions" not in result

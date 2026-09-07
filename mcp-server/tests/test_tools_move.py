"""Tests for the ``move_to_project`` MCP tool wrapper.

``test_move_to_project.py`` covers the pure writer in ``project_files``.
This covers the layer above it - the part a client actually calls - whose
own logic is the live-session merge and the error contract.

Spec source: docs/mcp-tools.md "move_to_project" plus "Cross-session
notifications" in rules/missioncache.md:

* ``live_sessions`` is merged across BOTH projects and deduped by session
  id. Both files changed, so a session bound to either is working from
  content that moved under it - and a session bound to both must appear
  once, not twice.
* The key is OMITTED when there are no peers. The rule branches on key
  presence, so an empty list would send the caller down the notify path.
* Errors come back as a coded dict, never an exception across the MCP
  boundary.
"""

import asyncio

import pytest

from mcp_missioncache import tools_docs
from mcp_missioncache.config import Settings

SOURCE_CONTEXT = """# src - Context
**Last Updated:** 2026-01-01 00:00

## Description
the source

## Gotchas

- source gotcha
- ROLE: belongs elsewhere

## Waiting on

| What | Who | Since | Gates |
|------|-----|-------|-------|
| the tagging decision | Sa'ar | 2026-08-01 | the map |

## Next Steps

1. keep going

## Recent Changes
"""

TARGET_CONTEXT = """# dst - Context
**Last Updated:** 2026-01-01 00:00

## Description
the target

## Gotchas

- target gotcha

## Waiting on

| What | Who | Since | Gates |
|------|-----|-------|-------|

## Next Steps

1. TBD

## Recent Changes
"""


@pytest.fixture
def projects(tmp_path, monkeypatch):
    root = tmp_path / "missioncache"
    settings = Settings(root=root)
    monkeypatch.setattr(tools_docs, "settings", settings)
    monkeypatch.setattr("mcp_missioncache.project_files.settings", settings)
    for name, context in (("src", SOURCE_CONTEXT), ("dst", TARGET_CONTEXT)):
        d = root / "active" / name
        d.mkdir(parents=True)
        (d / f"{name}-context.md").write_text(context)
        (d / f"{name}-tasks.md").write_text(
            f"# {name} - Tasks\n**Last Updated:** x\n\n## P\n\n- [ ] 1. a\n"
        )
    return root


def _bind(monkeypatch, mapping):
    """Fake the per-project peer lookup: {project_name: [session_id, ...]}."""

    def _fake(project_name):
        return [
            {"session_id": sid, "title": project_name, "last_active": "now"}
            for sid in mapping.get(project_name, [])
        ]

    monkeypatch.setattr(tools_docs, "live_peer_sessions_for_project", _fake)


def _move(**kwargs):
    kwargs.setdefault("source_project", "src")
    kwargs.setdefault("target_project", "dst")
    return asyncio.run(tools_docs.move_to_project(**kwargs))


class TestLiveSessionsMerge:
    def test_peers_on_both_sides_are_merged_and_deduped(self, projects, monkeypatch):
        # C is bound to BOTH projects. Three sessions must be notified, not
        # four - the dedupe is the whole reason the wrapper has logic here.
        _bind(monkeypatch, {"src": ["A", "C"], "dst": ["B", "C"]})
        result = _move(waiting_on=["the tagging decision"])
        assert result["success"] is True
        ids = [p["session_id"] for p in result["live_sessions"]]
        assert sorted(ids) == ["A", "B", "C"]
        assert len(ids) == 3

    def test_a_peer_on_only_the_target_is_still_notified(self, projects, monkeypatch):
        # The target's file changed too, so its sessions need telling even
        # though the caller was working in the source.
        _bind(monkeypatch, {"src": [], "dst": ["B"]})
        result = _move(waiting_on=["the tagging decision"])
        assert [p["session_id"] for p in result["live_sessions"]] == ["B"]

    def test_the_key_is_absent_when_there_are_no_peers(self, projects, monkeypatch):
        # rules/missioncache.md branches on key PRESENCE, so an empty list
        # would send the caller into the notify protocol for nothing.
        _bind(monkeypatch, {})
        result = _move(waiting_on=["the tagging decision"])
        assert "live_sessions" not in result

    def test_the_first_occurrence_is_the_one_kept(self, projects, monkeypatch):
        _bind(monkeypatch, {"src": ["C"], "dst": ["C"]})
        result = _move(waiting_on=["the tagging decision"])
        # Source is queried first, so the retained row carries its title -
        # the address SendMessage uses.
        assert result["live_sessions"] == [
            {"session_id": "C", "title": "src", "last_active": "now"}
        ]


class TestErrorContract:
    def test_a_validation_error_returns_a_code_not_an_exception(
        self, projects, monkeypatch
    ):
        _bind(monkeypatch, {})
        result = _move(target_project="src", waiting_on=["x"])
        assert result["code"] == "VALIDATION_ERROR"
        assert result.get("error") is True
        assert "success" not in result

    def test_an_unknown_project_returns_a_coded_error(self, projects, monkeypatch):
        _bind(monkeypatch, {})
        result = _move(target_project="no-such-project", waiting_on=["x"])
        assert result.get("error") is True
        assert result["code"] in ("FILE_NOT_FOUND", "VALIDATION_ERROR")

    def test_a_traversing_name_is_refused(self, projects, monkeypatch):
        _bind(monkeypatch, {})
        result = _move(source_project="../../etc", waiting_on=["x"])
        assert result["code"] == "VALIDATION_ERROR"

    def test_an_unexpected_failure_is_reported_not_raised(self, projects, monkeypatch):
        """Tools never raise across the MCP boundary."""
        _bind(monkeypatch, {})

        def _boom(**_kwargs):
            raise RuntimeError("disk exploded")

        monkeypatch.setattr(tools_docs.project_files, "move_to_project", _boom)
        result = _move(waiting_on=["x"])
        assert result["error"] is True
        assert "disk exploded" in result["message"]

    def test_a_peer_lookup_failure_does_not_lose_the_move(
        self, projects, monkeypatch
    ):
        """The write already happened; a notification lookup must not turn a
        successful move into a reported failure."""
        def _boom(_project):
            raise RuntimeError("db exploded")

        monkeypatch.setattr(tools_docs, "live_peer_sessions_for_project", _boom)
        result = _move(waiting_on=["the tagging decision"])
        # Either it degrades to no peers, or it reports the error - but the
        # files must reflect the move either way.
        moved = (projects / "active" / "dst" / "dst-context.md").read_text()
        assert "the tagging decision" in moved


class TestThePayloadReachesTheWriter:
    def test_every_parameter_is_forwarded(self, projects, monkeypatch):
        _bind(monkeypatch, {})
        captured = {}

        def _spy(**kwargs):
            captured.update(kwargs)
            return {"unmatched": [], "summary": "ok"}

        monkeypatch.setattr(tools_docs.project_files, "move_to_project", _spy)
        _move(
            sections=["S"],
            bullets=[{"section": "Gotchas", "match": "m"}],
            tasks=[{"match": "1"}],
            waiting_on=["w"],
            note="why",
        )
        assert captured == {
            "source_project": "src",
            "target_project": "dst",
            "sections": ["S"],
            "bullets": [{"section": "Gotchas", "match": "m"}],
            "tasks": [{"match": "1"}],
            "waiting_on": ["w"],
            "note": "why",
        }

    def test_the_writer_result_is_passed_through(self, projects, monkeypatch):
        _bind(monkeypatch, {})
        result = _move(
            sections=["Nope"], bullets=[{"section": "Gotchas", "match": "ROLE:"}]
        )
        assert result["bullets_moved"] == ["- ROLE: belongs elsewhere"]
        assert result["unmatched"] == ["section: Nope"]
        assert result["source_project"] == "src"
        assert result["target_project"] == "dst"

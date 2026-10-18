"""Tests for /api/lead, the dashboard's read-only Lead view.

Spec: the view is served with or without a lead session, and ``lead`` says
whether one is designated and alive. An unchanged watermark answers
``changed: false`` without a build, which is what lets the page poll.
"""

import pathlib

import missioncache_db
import pytest

from missioncache_dashboard import server


@pytest.fixture
def sandboxed(tmp_path, monkeypatch):
    mc_root = tmp_path / ".missioncache"
    mc_root.mkdir()
    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    monkeypatch.setattr(missioncache_db, "MISSIONCACHE_ROOT", mc_root)
    monkeypatch.setattr(missioncache_db, "DB_PATH", tmp_path / "tasks.db")
    monkeypatch.setattr(missioncache_db, "HOOKS_STATE_DB_PATH", fake_home / ".claude" / "hooks-state.db")
    monkeypatch.setattr(missioncache_db, "_LEGACY_CLAUDE_DB", tmp_path / "no-legacy-db")
    monkeypatch.setattr(missioncache_db, "_LEGACY_CLAUDE_ORBIT_ROOT", tmp_path / "no-legacy-orbit")
    monkeypatch.setattr(missioncache_db, "_LEGACY_ORBIT_DB", tmp_path / "no-legacy-orbit-db")
    monkeypatch.setattr(missioncache_db, "_LEGACY_ORBIT_ROOT", tmp_path / "no-legacy-orbit-root")
    monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: fake_home))
    return tmp_path


def test_served_without_a_lead(sandboxed):
    out = server.get_lead()
    assert out["changed"] is True
    assert out["lead"] == {"session_id": None, "live": False, "since": None}
    assert out["projects"] == []


def test_a_designated_lead_whose_process_is_gone_is_not_live(sandboxed):
    missioncache_db.set_lead_session("aaaaaaaa-1111-2222-3333-444444444444")
    out = server.get_lead()
    assert out["lead"]["session_id"] == "aaaaaaaa-1111-2222-3333-444444444444"
    assert out["lead"]["live"] is False


def test_an_unchanged_watermark_skips_the_build(sandboxed):
    first = server.get_lead()
    again = server.get_lead(watermark=first["watermark"])
    assert again == {"changed": False, "watermark": first["watermark"]}


def test_picks_written_by_the_lead_reach_the_view(sandboxed):
    from missioncache_db import lead_board

    missioncache_db.set_lead_session("aaaaaaaa-1111-2222-3333-444444444444")
    lead_board.set_lead_picks("aaaaaaaa-1111-2222-3333-444444444444", [{"what": "Publish the docs site", "why": "one task left"}])
    assert server.get_lead()["picks"]["items"] == [
        {"what": "Publish the docs site", "project": None, "why": "one task left"}]


def test_go_to_tab_brings_the_sessions_tab_forward(sandboxed, monkeypatch):
    from missioncache_db import lead_board

    asked = []
    monkeypatch.setattr(lead_board, "focus_terminal", lambda sid: asked.append(sid) or {"success": True})
    sid = "aaaaaaaa-1111-2222-3333-444444444444"
    assert server.focus_lead_session(server.LeadFocusPayload(session_id=sid)) == {"success": True}
    assert asked == [sid]


def test_go_to_tab_refuses_what_is_not_a_session_id(sandboxed, monkeypatch):
    from missioncache_db import lead_board

    monkeypatch.setattr(lead_board, "focus_available", lambda: True)
    monkeypatch.setattr(lead_board.subprocess, "run", lambda *a, **k: pytest.fail("osascript ran"))
    out = server.focus_lead_session(server.LeadFocusPayload(session_id="x'; tell application"))
    assert out == {"success": False, "reason": "not a session id"}

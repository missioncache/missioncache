"""Tests for the brief's dashboard half: /api/agenda, /api/sessions/live and
the calendar settings.

Spec: the agenda endpoint never 500s and distinguishes "not configured" from
"nothing today"; the live-sessions endpoint reports the pid-based set with an
explicit `method` so the UI can word it honestly, plus the designated lead;
calendar settings round-trip through their own config key, and a command
source is refused unless its first element is an existing absolute path,
because the dashboard process executes it.
"""

import asyncio
import json
import os
import pathlib

import pytest

import missioncache_db
from missioncache_dashboard import server
from missioncache_dashboard.lib import config


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
    monkeypatch.setattr(config, "CONFIG_FILE", fake_home / ".claude" / "missioncache-dashboard-config.json")
    from missioncache_db import agenda
    monkeypatch.setattr(agenda, "_config_file", lambda: config.CONFIG_FILE)
    return tmp_path


class TestAgendaEndpoint:
    def test_unconfigured_is_not_an_error(self, sandboxed):
        out = server.get_agenda()
        assert out["configured"] is False
        assert out["events"] == []

    def test_bad_date_is_422(self, sandboxed):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            server.get_agenda(date="nope")
        assert exc.value.status_code == 422

    def test_configured_file_source_renders(self, sandboxed):
        ics = sandboxed / "cal.ics"
        ics.write_text(
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:a\nDTSTART:20260908T090000Z\n"
            "DTEND:20260908T093000Z\nSUMMARY:Standup\nEND:VEVENT\nEND:VCALENDAR\n"
        )
        config.set_calendar_config({"sources": [{"name": "W", "kind": "ics", "location": str(ics)}]})
        out = server.get_agenda(date="2026-09-08")
        assert out["configured"] is True
        assert [e["title"] for e in out["events"]] == ["Standup"]


class TestLiveSessionsEndpoint:
    def test_shape_and_honesty_field(self, sandboxed):
        out = server.get_live_sessions()
        assert out["method"] == "pid"
        assert out["sessions"] == []
        assert out["bound_count"] == 0
        assert out["lead_session"] is None

    def test_reports_a_live_lead(self, sandboxed):
        pid_path = missioncache_db.session_pid_path("sid-lead")
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(json.dumps({"sessionId": "sid-lead", "pid": os.getpid(), "startTime": None}))
        missioncache_db.set_lead_session("sid-lead")
        out = server.get_live_sessions()
        assert out["lead_session"]["title"] == missioncache_db.LEAD_SESSION_TITLE


class TestCalendarSettings:
    def test_round_trip_through_its_own_key(self, sandboxed):
        payload = server.CalendarPayload(
            sources=[server.CalendarSource(name="W", kind="ics", location="~/w.ics")],
            cache_ttl_seconds=60,
            timezone="Asia/Jerusalem",
        )
        out = asyncio.run(server.update_calendar_settings(payload))
        assert out["ok"] is True
        assert out["calendar"]["timezone"] == "Asia/Jerusalem"
        assert out["calendar"]["cache_ttl_seconds"] == 60
        assert asyncio.run(server.get_settings())["calendar"]["sources"][0]["name"] == "W"

    def test_relative_command_is_refused(self, sandboxed):
        with pytest.raises(ValueError):
            server.CalendarSource(name="T", kind="command", command=["my-cal"])

    def test_ics_without_location_is_refused(self, sandboxed):
        with pytest.raises(ValueError):
            server.CalendarSource(name="T", kind="ics")

    def test_command_with_absolute_existing_path_is_accepted(self, sandboxed, tmp_path):
        script = tmp_path / "cal.sh"
        script.write_text("#!/bin/sh\necho '[]'\n")
        src = server.CalendarSource(name="T", kind="command", command=[str(script), "{date}"])
        assert src.command[0] == str(script)

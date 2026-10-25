"""Tests for /api/resume, the dashboard's view of the resume lists.

Spec: the approved session-resume plan. The GET lists Saved then Closed with
a derived resume command. Keep, forget, pin and rename change one entry; the
bodyless ones must refuse a request from another origin, because CORS
preflight never runs for them. An unknown or malformed id is a 404.
"""

import pathlib

import missioncache_db
import pytest
from fastapi import HTTPException

from missioncache_dashboard import server
from missioncache_db import resume_sessions

SID = "aaaaaaaa-1111-4111-8111-111111111111"


@pytest.fixture
def sandboxed(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    monkeypatch.setattr(missioncache_db, "MISSIONCACHE_ROOT", tmp_path / ".missioncache")
    monkeypatch.setattr(missioncache_db, "DB_PATH", tmp_path / "tasks.db")
    monkeypatch.setattr(missioncache_db, "HOOKS_STATE_DB_PATH", fake_home / ".claude" / "hooks-state.db")
    monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: fake_home))
    conn = resume_sessions.hook_connect()
    resume_sessions.record_seen(conn, SID, "/work/repo", None)
    resume_sessions.close_session(conn, SID, "other")
    conn.close()
    return tmp_path


class _Request:
    def __init__(self, headers=None):
        self.headers = headers or {}


def test_lists_the_closed_session_with_its_command(sandboxed):
    [row] = server.get_resume_sessions("closed")["sessions"]
    assert row["session_id"] == SID
    assert row["command"] == f"cd /work/repo && claude --resume {SID}"
    assert server.get_resume_sessions("saved")["sessions"] == []


def test_bad_list_name_is_a_400(sandboxed):
    with pytest.raises(HTTPException) as e:
        server.get_resume_sessions("everything")
    assert e.value.status_code == 400


def test_keep_moves_it_to_saved(sandboxed):
    out = server.act_on_resume_session(SID, "keep", _Request())
    assert out["session"]["list"] == "saved"


def test_forget_deletes_it(sandboxed):
    server.act_on_resume_session(SID, "forget", _Request())
    assert server.get_resume_sessions()["sessions"] == []


def test_a_cross_origin_bodyless_post_is_refused(sandboxed):
    with pytest.raises(HTTPException) as e:
        server.act_on_resume_session(SID, "forget", _Request({"sec-fetch-site": "cross-site"}))
    assert e.value.status_code == 403
    assert len(server.get_resume_sessions()["sessions"]) == 1


def test_unknown_or_malformed_id_is_a_404(sandboxed):
    # A malformed id is refused before the database, even if a row has it.
    conn = resume_sessions.hook_connect()
    conn.execute("INSERT INTO resume_sessions (session_id, cwd, auto_name, first_seen_at, last_seen_at, closed_at) "
                 "VALUES ('bad id', '/w', 'x', 't', 't', 't')")
    conn.commit()
    conn.close()
    for sid in ("bbbbbbbb-2222-4222-8222-222222222222", "../etc", "bad id"):
        with pytest.raises(HTTPException) as e:
            server.act_on_resume_session(sid, "keep", _Request())
        assert e.value.status_code == 404
    with pytest.raises(HTTPException) as e:
        server.rename_resume_session("bad id", server.ResumeRenamePayload(name="x"), _Request())
    assert e.value.status_code == 404


def test_pin_and_unpin(sandboxed):
    out = server.act_on_resume_session(SID, "pin", _Request())
    assert out["session"]["pinned"] is True and out["session"]["list"] == "saved"
    out = server.act_on_resume_session(SID, "unpin", _Request())
    assert out["session"]["pinned"] is False


def test_rename_and_its_validation(sandboxed):
    out = server.rename_resume_session(SID, server.ResumeRenamePayload(name="mine"), _Request())
    assert out["session"]["name"] == "mine"
    with pytest.raises(HTTPException) as e:
        server.rename_resume_session(SID, server.ResumeRenamePayload(name="a\x07b"), _Request())
    assert e.value.status_code == 400


def test_rename_route_is_not_swallowed_by_the_action_route():
    """FastAPI matches routes in order, so /rename must come before
    /{action}, or a rename would be answered as an unknown action."""
    paths = [r.path for r in server.app.routes if getattr(r, "path", "").startswith("/api/resume/")]
    assert paths.index("/api/resume/{session_id}/rename") < paths.index("/api/resume/{session_id}/{action}")

"""The lead board's MCP tools: thin wrappers over missioncache_db.lead_board.

Spec source: the lead-board plan (~/.claude/plans/replicated-splashing-crown.md),
"report_session_status", "get_lead_board" and "focus_terminal". What the
wrappers add on top of the db layer:

* report_session_status records the CALLING session, resolved from this MCP
  subprocess's CLAUDE_CODE_SESSION_ID, and refuses when it cannot be resolved.
* get_lead_board says whether the caller is the designated lead, and answers
  changed=false for an unchanged watermark.
* focus_terminal answers only the lead session, and reports why it could not
  jump instead of raising.
"""

import asyncio

import missioncache_db
import pytest
from mcp_missioncache import db as db_module
from mcp_missioncache import tools_pm
from missioncache_db import lead_board

SID = "aaaaaaaa-0000-0000-0000-000000000001"
OTHER = "bbbbbbbb-0000-0000-0000-000000000002"


def _run(coro):
    return asyncio.run(coro)


def test_report_records_the_calling_session(isolated_orbit, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    assert _run(tools_pm.report_session_status(state="needs_you", needs_reason="question")) == {
        "success": True}
    row = lead_board.agent_status_rows([SID])[SID]
    assert (row["state"], row["needs_reason"]) == ("needs_you", "question")


def test_report_refuses_without_a_session_id(isolated_orbit):
    result = _run(tools_pm.report_session_status(state="idle"))
    assert result["error"] is True


def test_report_refuses_an_unknown_state(isolated_orbit, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    assert _run(tools_pm.report_session_status(state="busy"))["code"] == "VALIDATION_ERROR"


def test_the_board_knows_its_lead_and_skips_an_unchanged_poll(isolated_orbit, monkeypatch):
    db_module.get_db()
    missioncache_db.HOOKS_STATE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    assert missioncache_db.set_lead_session(SID)
    first = _run(tools_pm.get_lead_board())
    assert first["success"] is True and first["changed"] is True
    assert first["lead"] == {"session_id": SID, "is_me": True}
    again = _run(tools_pm.get_lead_board(watermark=first["watermark"]))
    assert again == {"success": True, "changed": False, "watermark": first["watermark"]}


def test_a_bad_scope_is_a_validation_error(isolated_orbit):
    db_module.get_db()
    assert _run(tools_pm.get_lead_board(scope="mine"))["code"] == "VALIDATION_ERROR"


def _make_lead(monkeypatch, caller):
    db_module.get_db()
    missioncache_db.HOOKS_STATE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    assert missioncache_db.set_lead_session(SID)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", caller)


def test_focus_says_why_it_could_not_jump(isolated_orbit, monkeypatch):
    _make_lead(monkeypatch, SID)
    monkeypatch.setattr(lead_board, "focus_available", lambda: True)
    assert _run(tools_pm.focus_terminal(session_id=OTHER)) == {
        "success": False, "reason": "no terminal recorded for this session"}


def test_only_the_lead_can_jump_to_a_tab(isolated_orbit, monkeypatch):
    _make_lead(monkeypatch, OTHER)
    monkeypatch.setattr(lead_board, "focus_terminal", lambda sid: pytest.fail("focus ran"))
    result = _run(tools_pm.focus_terminal(session_id=SID))
    assert result == {"success": False, "reason": "only the lead session can jump to a tab"}


def test_only_the_lead_writes_picks(isolated_orbit, monkeypatch):
    _make_lead(monkeypatch, OTHER)
    result = _run(tools_pm.set_lead_picks(picks=[{"what": "x"}]))
    assert result == {"success": False, "reason": "only the lead session writes today's picks"}
    assert lead_board.lead_picks() is None


def test_the_lead_writes_picks(isolated_orbit, monkeypatch):
    _make_lead(monkeypatch, SID)
    result = _run(tools_pm.set_lead_picks(picks=[{"what": "Publish the docs site", "why": "one task left"}]))
    assert result["success"] is True
    assert lead_board.lead_picks()["items"] == [{"what": "Publish the docs site", "project": None, "why": "one task left"}]


def test_a_malformed_pick_is_a_validation_error(isolated_orbit, monkeypatch):
    _make_lead(monkeypatch, SID)
    result = _run(tools_pm.set_lead_picks(picks=[{"why": "no action"}]))
    assert result["code"] == "VALIDATION_ERROR"

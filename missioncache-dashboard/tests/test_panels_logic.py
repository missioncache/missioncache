"""The panels' rules that need no terminal: activity, unseen, names, order,
stepping, a Claude Code session's status from the mod, and which tab runs
which session when only the titles tie them."""

from datetime import datetime

from missioncache_dashboard.panels import logic

SID_A = "aaaaaaaa-1111-4111-8111-111111111111"
SID_B = "bbbbbbbb-2222-4222-8222-222222222222"


def _agent(state: str, at: float) -> dict:
    return {"state": state, "updated_at": datetime.fromtimestamp(at).strftime("%Y-%m-%d %H:%M:%S")}


def _own(status: str, at: float, sid: str = SID_A, name: str = "brave-otter") -> dict:
    return {"sessionId": sid, "name": name, "status": status, "statusUpdatedAt": at * 1000}


def test_a_shell_at_its_prompt_counts_only_input() -> None:
    assert logic.shell_activity(atime=10, mtime=50, job="zsh") == 10
    assert logic.shell_activity(atime=10, mtime=50, job="htop") == 10
    assert logic.shell_activity(atime=10, mtime=50, job="pytest") == 50


def test_working_with_an_idle_claude_record_is_done() -> None:
    assert logic.effective_status("working", {"status": "idle"}) == "done"
    assert logic.effective_status("working", {"status": "busy"}) == "working"
    assert logic.effective_status("waiting", {"status": "idle"}) == "waiting"


def test_a_new_session_starts_seen() -> None:
    entry = logic.advance(None, "done", activity=100, own_change=None, now=200)
    assert not logic.is_unseen(entry)


def test_finishing_after_the_last_focus_is_unseen() -> None:
    entry = logic.advance(None, "working", activity=100, own_change=None, now=100)
    entry = logic.advance(entry, "done", activity=100, own_change=None, now=200)
    assert logic.is_unseen(entry)
    entry["focused"] = 300
    assert not logic.is_unseen(entry)


def test_going_idle_is_not_a_change() -> None:
    entry = {"status": "done", "changed": 100, "focused": 150}
    entry = logic.advance(entry, "idle", activity=100, own_change=None, now=400)
    assert entry["changed"] == 100
    assert entry["status"] == "idle"


def test_working_is_never_unseen() -> None:
    assert not logic.is_unseen({"status": "working", "changed": 300, "focused": 100})


def test_row_name_prefers_the_missioncache_title() -> None:
    assert logic.row_name("✳ fix login", None, "api-release-check") == "api-release-check"
    assert logic.row_name("✳ fix login", None, None) == "fix login"
    assert logic.row_name("Claude Code", {"name": "brave-otter"}, None) == "brave-otter"


def test_lead_mark() -> None:
    assert logic.lead_mark("missioncache-lead", "missioncache-lead") == "live"
    assert logic.lead_mark("missioncache-lead", None) == "off"
    assert logic.lead_mark("docs-site", "missioncache-lead") is None


def test_rows_are_newest_first_with_the_lead_on_top() -> None:
    rows = [
        {"id": "a", "changed": 10, "lead": None},
        {"id": "lead", "changed": 1, "lead": "live"},
        {"id": "b", "changed": 30, "lead": None},
    ]
    assert [r["id"] for r in logic.order_rows(rows)] == ["lead", "b", "a"]


def test_step_wraps_and_starts_from_the_top() -> None:
    order = ["a", "b", "c"]
    assert logic.step_target(order, "c", 1) == "a"
    assert logic.step_target(order, "a", -1) == "c"
    assert logic.step_target(order, "elsewhere", 1) == "a"
    assert logic.step_target([], "a", 1) is None


def test_the_mods_states_in_the_panels_words() -> None:
    idle = _own("idle", 1_000)
    assert logic.claude_status(_agent("working", 2_000), idle) == ("working", 2_000)
    assert logic.claude_status(_agent("compacting", 2_000), idle)[0] == "working"
    assert logic.claude_status(_agent("needs_you", 2_000), idle)[0] == "waiting"
    assert logic.claude_status(_agent("idle", 2_000), idle)[0] == "done"


def test_a_newer_busy_record_wins_over_the_mod() -> None:
    # The first turn after /resume can write no working row: the record's
    # busy is newer than the mod's idle from before.
    assert logic.claude_status(_agent("idle", 1_000), _own("busy", 2_000)) == ("working", 2_000)


def test_idle_after_a_missed_turn_end_is_done() -> None:
    # A denied permission sends nothing: the mod still says waiting.
    assert logic.claude_status(_agent("needs_you", 1_000), _own("idle", 2_000)) == ("done", 2_000)


def test_the_mods_whole_seconds_still_count_as_newest() -> None:
    assert logic.claude_status(_agent("needs_you", 2_000), _own("busy", 2_000.6))[0] == "waiting"


def test_without_the_mod_only_working_and_idle() -> None:
    assert logic.claude_status(None, _own("busy", 2_000)) == ("working", 2_000)
    assert logic.claude_status(None, _own("idle", 2_000)) == ("idle", 2_000)


def _no_transcript(c: dict) -> None:
    return None


def test_a_tab_joins_by_the_sessions_name() -> None:
    a, b = _own("idle", 1, SID_A, "brave-otter"), _own("idle", 1, SID_B, "calm-heron")
    tabs = [("t1", "✳ docs-site"), ("t2", "◐ brave-otter"), ("t3", "PowerShell")]
    joined = logic.join_claude(tabs, [a, b], {SID_B: "docs-site"}, _no_transcript)
    assert joined == {"t2": a, "t1": b}


def test_a_shell_left_with_the_name_loses_to_the_claude_tab() -> None:
    # Open names the tab, and the name stays after Claude Code exits.
    a = _own("idle", 1, SID_A, "brave-otter")
    joined = logic.join_claude([("old", "brave-otter"), ("new", "✳ brave-otter")], [a], {}, _no_transcript)
    assert joined == {"new": a}


def test_a_transcript_title_joins_only_when_it_is_unique() -> None:
    a, b = _own("idle", 1, SID_A, "brave-otter"), _own("idle", 1, SID_B, "calm-heron")
    titles = {SID_A: "fix login", SID_B: "fix login"}
    # Two sessions with the title and one tab: either could be in it.
    assert logic.join_claude([("t1", "✳ fix login")], [a, b], {}, lambda c: titles[c["sessionId"]]) == {}
    titles[SID_B] = "add retry"
    tabs = [("t1", "✳ fix login"), ("t2", "✳ fix login"), ("t3", "✳ add retry")]
    assert logic.join_claude(tabs, [a, b], {}, lambda c: titles[c["sessionId"]]) == {"t3": b}


def test_the_last_session_takes_the_last_claude_tab() -> None:
    a = _own("idle", 1, SID_A, "brave-otter")
    joined = logic.join_claude([("t1", "Claude Code"), ("t2", "PowerShell")], [a], {}, _no_transcript)
    assert joined == {"t1": a}
    two = [("t1", "Claude Code"), ("t2", "✳ something")]
    assert logic.join_claude(two, [a], {}, _no_transcript) == {}


def test_two_sessions_with_one_name_join_no_tab() -> None:
    # Either could be in either tab, and a wrong join acts on the other one.
    a, b = _own("idle", 1, SID_A, "brave-otter"), _own("idle", 1, SID_B, "brave-otter")
    assert logic.join_claude([("t1", "✳ brave-otter")], [a, b], {}, _no_transcript) == {}
    two = [("t1", "✳ brave-otter"), ("t2", "✳ brave-otter")]
    assert logic.join_claude(two, [a, b], {}, _no_transcript) == {}


def test_a_name_on_two_claude_tabs_joins_neither() -> None:
    a = _own("idle", 1, SID_A, "brave-otter")
    two = [("t1", "✳ brave-otter"), ("t2", "◐ brave-otter")]
    assert logic.join_claude(two, [a], {}, _no_transcript) == {}


def test_a_session_titled_elsewhere_does_not_take_the_last_tab() -> None:
    # A session in an editor has a title no tab shows. The last Claude tab
    # is a different session that has already exited.
    a = _own("idle", 1, SID_A, "brave-otter")
    tabs = [("t1", "✳ old work"), ("t2", "PowerShell")]
    assert logic.join_claude(tabs, [a], {}, lambda c: "fix login") == {}

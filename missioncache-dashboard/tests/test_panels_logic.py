"""The panels' rules that need no terminal: activity, unseen, names, order, stepping."""

from missioncache_dashboard.panels import logic


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

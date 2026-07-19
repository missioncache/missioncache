"""Tests for the statusline's Saved cell.

Spec source: the Saved-cell contract - a bound project renders a "Saved"
cell carrying its OWN context file's mtime, formatted ALWAYS with the
date (a project resumed days after its last save must not read as saved
today, so bare HH:MM is never used), linking to the project's Context
tab in the dashboard modal. Forks show their own Saved stamp alongside
the existing parent signal. A project with no (or unstattable) context
file carries mtime 0.0 and the cell is omitted.
"""

import os
import sqlite3
import urllib.parse
from datetime import datetime, timedelta

import pytest

import missioncache_dashboard.statusline as mod
from missioncache_dashboard.statusline import _format_saved_time


def _bound_conn(name):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE project_state (session_id TEXT PRIMARY KEY, "
        "project_name TEXT, updated_at TEXT)"
    )
    conn.execute(
        "INSERT INTO project_state VALUES (?, ?, ?)",
        ("sess-1", name, datetime.now().isoformat()),
    )
    conn.commit()
    return conn


@pytest.fixture
def saved_fs(tmp_path, monkeypatch):
    """A sandboxed ~/.missioncache/active plus state dir."""
    active = tmp_path / ".missioncache" / "active"
    state = tmp_path / "state"
    for d in (active, state / "shared-seen"):
        d.mkdir(parents=True)
    monkeypatch.setattr(mod, "MISSIONCACHE_ACTIVE", active)
    monkeypatch.setattr(mod, "STATE_DIR", state)
    return active


# ── _format_saved_time (pure) ────────────────────────────────────────────


class TestFormatSavedTime:
    def test_today_still_includes_date(self):
        """The whole point of the format: a today save shows the date too,
        unlike _format_wall_time's bare HH:MM."""
        now = datetime.now().replace(hour=12, minute=34, second=0)
        assert _format_saved_time(now.timestamp()) == now.strftime("%b %-d %H:%M")

    def test_past_day_includes_date(self):
        past = (datetime.now() - timedelta(days=4)).replace(
            hour=9, minute=5, second=0
        )
        assert _format_saved_time(past.timestamp()) == past.strftime("%b %-d %H:%M")


# ── get_project_info carries the context mtime (IO) ──────────────────────


class TestGetProjectInfoSaved:
    def test_regular_project_carries_context_mtime(self, saved_fs, monkeypatch):
        proj = saved_fs / "solo-proj"
        proj.mkdir()
        ctx = proj / "solo-proj-context.md"
        ctx.write_text("# Solo - Context\n\n## Description\n")
        (proj / "solo-proj-tasks.md").write_text("- [x] a\n- [ ] b\n")
        # Pin a distinctive past mtime so the assertion cannot pass by luck.
        os.utime(ctx, (1_700_000_000, 1_700_000_000))
        monkeypatch.setattr(mod, "_get_hooks_db", lambda: _bound_conn("solo-proj"))

        info = mod.get_project_info("sess-1", 60)
        assert info.name == "solo-proj"
        assert info.context_saved_mtime == 1_700_000_000

    def test_tasksless_project_carries_context_mtime(self, saved_fs, monkeypatch):
        """The early no-tasks return must thread the field too."""
        proj = saved_fs / "no-tasks-proj"
        proj.mkdir()
        ctx = proj / "no-tasks-proj-context.md"
        ctx.write_text("# NT - Context\n\n## Description\n")
        os.utime(ctx, (1_700_000_000, 1_700_000_000))
        monkeypatch.setattr(
            mod, "_get_hooks_db", lambda: _bound_conn("no-tasks-proj")
        )

        info = mod.get_project_info("sess-1", 60)
        assert info.context_saved_mtime == 1_700_000_000

    def test_fork_child_carries_own_mtime_not_parents(self, saved_fs, monkeypatch):
        parent = saved_fs / "parent-proj"
        parent.mkdir()
        parent_ctx = parent / "parent-proj-context.md"
        parent_ctx.write_text("# Parent - Context\n\n## Description\n")
        os.utime(parent_ctx, (1_600_000_000, 1_600_000_000))

        child = saved_fs / "child-proj"
        child.mkdir()
        child_ctx = child / "child-proj-context.md"
        child_ctx.write_text(
            "# Child - Context\n**Fork of:** parent-proj\n\n## Description\n"
        )
        os.utime(child_ctx, (1_700_000_000, 1_700_000_000))
        monkeypatch.setattr(mod, "_get_hooks_db", lambda: _bound_conn("child-proj"))

        info = mod.get_project_info("sess-1", 60)
        assert info.fork_of == "parent-proj"
        assert info.context_saved_mtime == 1_700_000_000

    def test_no_context_file_is_zero(self, saved_fs, monkeypatch):
        proj = saved_fs / "bare-proj"
        proj.mkdir()
        (proj / "bare-proj-tasks.md").write_text("- [ ] a\n")
        monkeypatch.setattr(mod, "_get_hooks_db", lambda: _bound_conn("bare-proj"))

        info = mod.get_project_info("sess-1", 60)
        assert info.name == "bare-proj"
        assert info.context_saved_mtime == 0.0

    def test_non_utf8_context_keeps_mtime_drops_fork(self, saved_fs, monkeypatch):
        """Stat happens before the read: a binary context file loses only
        the fork annotation, never the Saved stamp."""
        proj = saved_fs / "bin-proj"
        proj.mkdir()
        ctx = proj / "bin-proj-context.md"
        ctx.write_bytes(b"\xff\xfe garbage \xff **Fork of:** x\n")
        os.utime(ctx, (1_700_000_000, 1_700_000_000))
        monkeypatch.setattr(mod, "_get_hooks_db", lambda: _bound_conn("bin-proj"))

        info = mod.get_project_info("sess-1", 60)
        assert info.context_saved_mtime == 1_700_000_000
        assert info.fork_of == ""


# ── render assembly ──────────────────────────────────────────────────────


class TestSavedRenderAssembly:
    def test_saved_item_carries_stamp_and_context_link(self):
        """The assembled cell (mirroring main()'s Saved block) carries the
        label, the full-date stamp, and the tab=context modal link."""
        stamp = _format_saved_time(1_700_000_000)
        url = (
            f"{mod._DASHBOARD_URL}/#projects"
            f"?task={urllib.parse.quote('my proj', safe='')}&tab=context"
        )
        line = mod._item(
            mod.COLORS["saved"], "\U0001f4be", "Saved", mod._osc8_link(url, stamp)
        )
        assert "Saved" in line
        assert stamp in line
        assert "tab=context" in line
        assert "my%20proj" in line

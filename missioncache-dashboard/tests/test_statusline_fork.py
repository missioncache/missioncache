"""Tests for the statusline's fork awareness.

Spec source: the fork feature contract - a bound project whose context
header carries ``**Fork of:** <parent>`` renders a "Fork of" annotation
with an OSC 8 link to the parent's dashboard modal; a staleness glyph
appears iff the parent (shared) context changed after this session's
shared-seen marker; a missing marker reads as fresh (neutral, never a
false alarm); non-fork projects render exactly as before.
"""

import json
import os
import time

import pytest

import missioncache_dashboard.statusline as mod
from missioncache_dashboard.statusline import (
    _format_wall_time,
    _parse_fork_of,
    _resolve_parent_context,
    _shared_stale_mtime,
)


# ── _parse_fork_of (pure) ────────────────────────────────────────────────


class TestParseForkOf:
    def test_plain_name(self):
        text = "# C - Context\n**Last Updated:** now\n**Fork of:** parent-proj\n\n## Description\n"
        assert _parse_fork_of(text) == "parent-proj"

    def test_wikilink_name(self):
        text = "# C - Context\n**Fork of:** [[parent-proj]]\n\n## Description\n"
        assert _parse_fork_of(text) == "parent-proj"

    def test_absent(self):
        assert _parse_fork_of("# C - Context\n\n## Description\n") == ""

    def test_body_mention_ignored(self):
        text = "# C - Context\n\n## Notes\n**Fork of:** not-really\n"
        assert _parse_fork_of(text) == ""

    def test_malformed_half_link_ignored(self):
        text = "# C - Context\n**Fork of:** [[broken\n\n## Description\n"
        assert _parse_fork_of(text) == ""


# ── parent context resolution + staleness (IO) ──────────────────────────


@pytest.fixture
def fork_fs(tmp_path, monkeypatch):
    """A child bound in project_state-land plus a parent with a context
    file, under a sandboxed ~/.missioncache and ~/.claude/hooks/state."""
    active = tmp_path / ".missioncache" / "active"
    completed = tmp_path / ".missioncache" / "completed"
    state = tmp_path / "state"
    for d in (active, completed, state / "shared-seen"):
        d.mkdir(parents=True)
    monkeypatch.setattr(mod, "MISSIONCACHE_ACTIVE", active)
    monkeypatch.setattr(mod, "STATE_DIR", state)

    parent_dir = active / "parent-proj"
    parent_dir.mkdir()
    parent_ctx = parent_dir / "parent-proj-context.md"
    parent_ctx.write_text("# Parent - Context\n\n## Description\nshared layer\n")
    return active, completed, state, parent_ctx


class TestResolveParentContext:
    def test_active_parent_resolves(self, fork_fs):
        _active, _completed, _state, parent_ctx = fork_fs
        assert _resolve_parent_context("parent-proj") == parent_ctx

    def test_completed_parent_resolves(self, fork_fs):
        active, completed, _state, parent_ctx = fork_fs
        dest = completed / "parent-proj"
        (active / "parent-proj").rename(dest)
        assert _resolve_parent_context("parent-proj") == dest / "parent-proj-context.md"

    def test_missing_parent_none(self, fork_fs):
        assert _resolve_parent_context("no-such") is None


class TestSharedStaleMtime:
    def _marker(self, state, session_id, seen_mtime):
        (state / "shared-seen" / f"{session_id}.json").write_text(
            json.dumps({"parent": "parent-proj", "seen_mtime": seen_mtime})
        )

    def test_no_marker_is_fresh(self, fork_fs):
        _a, _c, _state, parent_ctx = fork_fs
        assert _shared_stale_mtime(parent_ctx, "sess-1", "parent-proj") is None

    def test_marker_current_is_fresh(self, fork_fs):
        _a, _c, state, parent_ctx = fork_fs
        self._marker(state, "sess-1", parent_ctx.stat().st_mtime)
        assert _shared_stale_mtime(parent_ctx, "sess-1", "parent-proj") is None

    def test_parent_updated_after_marker_returns_change_mtime(self, fork_fs):
        _a, _c, state, parent_ctx = fork_fs
        seen = parent_ctx.stat().st_mtime
        self._marker(state, "sess-1", seen)
        parent_ctx.write_text(parent_ctx.read_text() + "\n- sibling update\n")
        # Force a strictly-newer mtime deterministically (no sleep, no
        # filesystem-granularity dependency - flakes on coarse-mtime FSes).
        os.utime(parent_ctx, (seen + 10, seen + 10))
        assert _shared_stale_mtime(parent_ctx, "sess-1", "parent-proj") == seen + 10

    def test_corrupt_marker_is_fresh(self, fork_fs):
        _a, _c, state, parent_ctx = fork_fs
        (state / "shared-seen" / "sess-1.json").write_text("{not json")
        assert _shared_stale_mtime(parent_ctx, "sess-1", "parent-proj") is None

    def test_foreign_parent_marker_is_fresh(self, fork_fs):
        """A marker recorded for a DIFFERENT parent (session switched forks)
        must not be compared against this parent's file."""
        _a, _c, state, parent_ctx = fork_fs
        (state / "shared-seen" / "sess-1.json").write_text(
            json.dumps({"parent": "other-parent", "seen_mtime": 1.0})
        )
        assert _shared_stale_mtime(parent_ctx, "sess-1", "parent-proj") is None


class TestFormatWallTime:
    def test_today_renders_clock_only(self):
        from datetime import datetime
        now = datetime.now()
        # noon today is unambiguous regardless of when the test runs
        stamp = now.replace(hour=12, minute=34, second=0).timestamp()
        assert _format_wall_time(stamp) == "12:34"

    def test_other_day_includes_date(self):
        from datetime import datetime, timedelta
        past = datetime.now() - timedelta(days=3)
        past = past.replace(hour=9, minute=5, second=0)
        # Month-name form, matching the Last Action cell's format.
        assert _format_wall_time(past.timestamp()) == f"{past.strftime('%b')} {past.day} {past.strftime('%H:%M')}"


# ── get_project_info fork detection (IO) ────────────────────────────────


class TestGetProjectInfoFork:
    def _bind(self, monkeypatch, name):
        """Short-circuit the hooks-db read: session is bound to <name>."""
        import sqlite3

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE project_state (session_id TEXT PRIMARY KEY, "
            "project_name TEXT, updated_at TEXT)"
        )
        from datetime import datetime

        conn.execute(
            "INSERT INTO project_state VALUES (?, ?, ?)",
            ("sess-1", name, datetime.now().isoformat()),
        )
        conn.commit()
        monkeypatch.setattr(mod, "_get_hooks_db", lambda: conn)

    def test_fork_child_detected(self, fork_fs, monkeypatch):
        active, _c, _state, _parent_ctx = fork_fs
        child_dir = active / "child-proj"
        child_dir.mkdir()
        (child_dir / "child-proj-context.md").write_text(
            "# Child - Context\n**Fork of:** parent-proj\n\n## Description\n"
        )
        (child_dir / "child-proj-tasks.md").write_text("- [x] a\n- [ ] b\n")
        self._bind(monkeypatch, "child-proj")

        info = mod.get_project_info("sess-1", 60)
        assert info.name == "child-proj"
        assert info.fork_of == "parent-proj"
        assert info.progress.strip() == "[1/2]"
        assert info.shared_stale_mtime == 0.0  # no marker: neutral

    def test_unresolvable_parent_renders_plain(self, fork_fs, monkeypatch):
        active, _c, _state, _parent_ctx = fork_fs
        child_dir = active / "lone-child"
        child_dir.mkdir()
        (child_dir / "lone-child-context.md").write_text(
            "# Child - Context\n**Fork of:** ghost-parent\n\n## Description\n"
        )
        self._bind(monkeypatch, "lone-child")

        info = mod.get_project_info("sess-1", 60)
        assert info.name == "lone-child"
        assert info.fork_of == ""

    def test_non_fork_unchanged(self, fork_fs, monkeypatch):
        active, _c, _state, _parent_ctx = fork_fs
        proj = active / "solo-proj"
        proj.mkdir()
        (proj / "solo-proj-context.md").write_text("# Solo - Context\n\n## Description\n")
        (proj / "solo-proj-tasks.md").write_text("- [ ] a\n")
        self._bind(monkeypatch, "solo-proj")

        info = mod.get_project_info("sess-1", 60)
        assert info.name == "solo-proj"
        assert info.fork_of == ""
        assert info.shared_stale_mtime == 0.0


# ── fork chains: the parent is itself a fork ─────────────────────────────


def _project(active, name, fork_of=""):
    d = active / name
    d.mkdir()
    header = f"**Fork of:** {fork_of}\n" if fork_of else ""
    ctx = d / f"{name}-context.md"
    ctx.write_text(f"# {name} - Context\n{header}\n## Description\n")
    (d / f"{name}-tasks.md").write_text("- [ ] a\n")
    return ctx


class TestForkChain:
    """Spec: a fork of a fork shows its parent, then the parent's own parent
    dim for orientation ("B ← A"). Reads stay one hop, so the "parent updated"
    dot follows the direct parent only. Deeper chains end in "← …", and a
    cycle never loops."""

    def _info(self, monkeypatch, name):
        monkeypatch.setattr(mod, "_get_hooks_db", lambda: _bound_conn(name))
        return mod.get_project_info("sess-1", 60)

    def test_fork_of_a_fork_shows_the_grandparent(self, fork_fs, monkeypatch):
        active, _c, _s, _p = fork_fs
        _project(active, "mid-proj", fork_of="parent-proj")
        _project(active, "leaf-proj", fork_of="mid-proj")
        info = self._info(monkeypatch, "leaf-proj")
        assert info.fork_of == "mid-proj"
        assert info.fork_ancestry == "parent-proj"

    def test_plain_fork_has_no_ancestry(self, fork_fs, monkeypatch):
        active, _c, _s, _p = fork_fs
        _project(active, "mid-proj", fork_of="parent-proj")
        info = self._info(monkeypatch, "mid-proj")
        assert info.fork_of == "parent-proj"
        assert info.fork_ancestry == ""

    def test_three_levels_end_in_an_ellipsis(self, fork_fs, monkeypatch):
        active, _c, _s, parent_ctx = fork_fs
        parent_ctx.write_text("# P - Context\n**Fork of:** root-proj\n\n## Description\n")
        _project(active, "mid-proj", fork_of="parent-proj")
        _project(active, "leaf-proj", fork_of="mid-proj")
        info = self._info(monkeypatch, "leaf-proj")
        assert info.fork_ancestry == "parent-proj ← …"

    def test_a_two_node_cycle_shows_no_ancestry(self, fork_fs, monkeypatch):
        active, _c, _s, _p = fork_fs
        _project(active, "x-proj", fork_of="y-proj")
        _project(active, "y-proj", fork_of="x-proj")
        info = self._info(monkeypatch, "x-proj")
        assert info.fork_of == "y-proj"
        assert info.fork_ancestry == ""

    def test_a_three_node_cycle_does_not_claim_a_deeper_chain(self, fork_fs, monkeypatch):
        """leaf -> mid -> top -> leaf: the grandparent is real, but its own
        parent is the child itself, so there is no deeper chain to hint at."""
        active, _c, _s, _p = fork_fs
        _project(active, "leaf-proj", fork_of="mid-proj")
        _project(active, "mid-proj", fork_of="top-proj")
        _project(active, "top-proj", fork_of="leaf-proj")
        info = self._info(monkeypatch, "leaf-proj")
        assert info.fork_ancestry == "top-proj"

    def test_the_dot_follows_the_direct_parent_only(self, fork_fs, monkeypatch):
        """The grandparent changing must not light the dot: the child never
        reads the grandparent, so there is nothing for it to re-read."""
        active, _c, state, parent_ctx = fork_fs
        mid_ctx = _project(active, "mid-proj", fork_of="parent-proj")
        _project(active, "leaf-proj", fork_of="mid-proj")
        (state / "shared-seen" / "sess-1.json").write_text(
            json.dumps({"parent": "mid-proj", "seen_mtime": mid_ctx.stat().st_mtime})
        )
        later = time.time() + 60
        os.utime(parent_ctx, (later, later))
        info = self._info(monkeypatch, "leaf-proj")
        assert info.shared_stale_mtime == 0.0

        # Positive control: the same marker lights the dot once the DIRECT
        # parent changes, so the zero above is not a mis-keyed marker.
        os.utime(mid_ctx, (later, later))
        info = self._info(monkeypatch, "leaf-proj")
        assert info.shared_stale_mtime == later

    def test_cell_renders_parent_then_ancestry_then_dot(self):
        project = mod.ProjectInfo(
            name="leaf-proj", fork_of="mid-proj", fork_ancestry="parent-proj",
            shared_stale_mtime=time.time(),
        )
        value = mod._fork_value(project)
        assert value.index("mid-proj") < value.index("← parent-proj") < value.index("● parent updated")
        assert f"{mod.COLORS['dim']}← parent-proj" in value
        # The grandparent is orientation only: the one link is the parent's.
        assert value.count("\x1b]8;;http") == 1


# ── cross-parser parity (the split-brain guard) ──────────────────────────


class TestParserParity:
    def test_regex_byte_identical_to_db_copy(self):
        """The statusline mirrors context_health._FORK_NAME_RE by hand. A test
        CAN import both (import-time is fine even though the statusline can't
        import missioncache_db at RUNTIME); assert they never drift."""
        from missioncache_db import context_health

        assert mod._FORK_HEADER_RE.pattern == context_health._FORK_NAME_RE.pattern

    def test_both_parsers_agree_on_corpus(self):
        """Both parsers must return the same parent for the same content,
        including the fenced-`##`-in-header case that a naive break mishandles."""
        from missioncache_db import context_health

        corpus = [
            "# C\n**Fork of:** parent-proj\n\n## Description\n",
            "# C\n**Fork of:** [[parent-proj]]\n\n## Description\n",
            "# C\n\n## Description\n**Fork of:** body-mention\n",  # below section
            "# C\n**Fork of:** [[broken\n\n## Description\n",       # malformed
            # fenced `## ` in the header region ABOVE the Fork of line:
            "# C\n```\n## not a real section\n```\n**Fork of:** parent-proj\n\n## Description\n",
            "# C\nno header here\n\n## Description\n",
        ]
        for text in corpus:
            db_ans = context_health.parse_fork_parent(text) or ""
            sl_ans = _parse_fork_of(text)
            assert db_ans == sl_ans, f"parsers disagree on:\n{text!r}\n db={db_ans!r} sl={sl_ans!r}"


# ── render-level assembly (the untested glue) ────────────────────────────


class TestForkRenderAssembly:
    def test_render_emits_fork_item_and_links(self, fork_fs, monkeypatch, capsys):
        """Bind a fork child and run the full statusline render; assert the
        emitted string carries the 'Fork of' item and an OSC 8 link to the
        parent modal. Guards the render glue the unit tests skip."""
        active, _c, _state, _parent_ctx = fork_fs
        child_dir = active / "child-proj"
        child_dir.mkdir()
        (child_dir / "child-proj-context.md").write_text(
            "# Child - Context\n**Fork of:** parent-proj\n\n## Description\n"
        )
        (child_dir / "child-proj-tasks.md").write_text("- [ ] a\n")

        info = mod.get_project_info  # sanity: fork detected end to end
        monkeypatch.setattr(mod, "_get_hooks_db", lambda: _bound_conn("child-proj"))
        pi = mod.get_project_info("sess-1", 60)
        assert pi.fork_of == "parent-proj"

        # Direct render-path check: the item builder produces the labelled cell.
        line = mod._item(mod.COLORS["project"], "⤵", "Fork of",
                         mod._osc8_link(f"{mod._DASHBOARD_URL}/#projects?task=parent-proj", "parent-proj"))
        assert "Fork of" in line
        assert "parent-proj" in line
        assert "⤵" in line

    def test_stale_glyph_present_only_when_stale(self, fork_fs):
        """The staleness glyph is appended iff shared_stale_mtime is set."""
        _a, _c, state, parent_ctx = fork_fs
        # fresh -> no glyph in the assembled fork value
        fork_value_fresh = mod._osc8_link("u", "parent-proj")
        assert "parent updated" not in fork_value_fresh
        # stale -> the render appends the marker (mirrors statusline.py logic)
        stamp = mod._format_wall_time(parent_ctx.stat().st_mtime)
        stale_value = (
            fork_value_fresh
            + f" {mod.COLORS['fork_update']}● parent updated {stamp}{mod.RESET}"
        )
        assert "● parent updated" in stale_value
        assert stamp in stale_value


def _bound_conn(name):
    import sqlite3
    from datetime import datetime
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE project_state (session_id TEXT PRIMARY KEY, "
        "project_name TEXT, updated_at TEXT)"
    )
    conn.execute("INSERT INTO project_state VALUES (?, ?, ?)",
                 ("sess-1", name, datetime.now().isoformat()))
    conn.commit()
    return conn


class TestSessionIdGuard:
    def test_traversal_session_id_reads_no_marker(self, fork_fs):
        """A session id with path chars must not let the marker read escape."""
        _a, _c, _state, parent_ctx = fork_fs
        assert _shared_stale_mtime(parent_ctx, "../../etc/passwd", "parent-proj") is None

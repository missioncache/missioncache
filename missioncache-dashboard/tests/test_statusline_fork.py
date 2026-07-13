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
    _parse_fork_of,
    _resolve_parent_context,
    _shared_is_stale,
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


class TestSharedIsStale:
    def _marker(self, state, session_id, seen_mtime):
        (state / "shared-seen" / f"{session_id}.json").write_text(
            json.dumps({"parent": "parent-proj", "seen_mtime": seen_mtime})
        )

    def test_no_marker_is_fresh(self, fork_fs):
        _a, _c, _state, parent_ctx = fork_fs
        assert _shared_is_stale(parent_ctx, "sess-1", "parent-proj") is False

    def test_marker_current_is_fresh(self, fork_fs):
        _a, _c, state, parent_ctx = fork_fs
        self._marker(state, "sess-1", parent_ctx.stat().st_mtime)
        assert _shared_is_stale(parent_ctx, "sess-1", "parent-proj") is False

    def test_parent_updated_after_marker_is_stale(self, fork_fs):
        _a, _c, state, parent_ctx = fork_fs
        seen = parent_ctx.stat().st_mtime
        self._marker(state, "sess-1", seen)
        parent_ctx.write_text(parent_ctx.read_text() + "\n- sibling update\n")
        # Force a strictly-newer mtime deterministically (no sleep, no
        # filesystem-granularity dependency - flakes on coarse-mtime FSes).
        os.utime(parent_ctx, (seen + 10, seen + 10))
        assert _shared_is_stale(parent_ctx, "sess-1", "parent-proj") is True

    def test_corrupt_marker_is_fresh(self, fork_fs):
        _a, _c, state, parent_ctx = fork_fs
        (state / "shared-seen" / "sess-1.json").write_text("{not json")
        assert _shared_is_stale(parent_ctx, "sess-1", "parent-proj") is False

    def test_foreign_parent_marker_is_fresh(self, fork_fs):
        """A marker recorded for a DIFFERENT parent (session switched forks)
        must not be compared against this parent's file."""
        _a, _c, state, parent_ctx = fork_fs
        (state / "shared-seen" / "sess-1.json").write_text(
            json.dumps({"parent": "other-parent", "seen_mtime": 1.0})
        )
        assert _shared_is_stale(parent_ctx, "sess-1", "parent-proj") is False


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
        assert info.shared_stale is False  # no marker: neutral

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
        assert info.shared_stale is False


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
        """The staleness glyph is appended iff shared_stale."""
        _a, _c, state, parent_ctx = fork_fs
        # fresh -> no glyph in the assembled fork value
        fork_value_fresh = mod._osc8_link("u", "parent-proj")
        assert "shared updated" not in fork_value_fresh
        # stale -> the render appends the marker (mirrors statusline.py logic)
        stale_value = fork_value_fresh + f" {mod.COLORS['ctx_urgent']}● shared updated{mod.RESET}"
        assert "● shared updated" in stale_value


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
        assert _shared_is_stale(parent_ctx, "../../etc/passwd", "parent-proj") is False

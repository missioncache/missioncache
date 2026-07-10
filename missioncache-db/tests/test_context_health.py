"""Tests for missioncache_db.context_health.

Spec source: the context-file conventions plan (canonical section order,
Waiting on = What/Who/Since/Gates table before Next Steps, Recent Changes
capped at RECENT_CHANGES_CAP dated subsections with overflow rolled to a
per-project journal that reads oldest-first, pointer line at the BOTTOM of
the section). Every assertion traces to that contract, not to the parser
implementation.
"""

from datetime import date, datetime
from pathlib import Path

import pytest

from missioncache_db import context_health as ch


def _rc_subsections(n: int, start: int = 1) -> str:
    """n dated subsections, newest first (day numbers descending)."""
    blocks = []
    for i in range(n, 0, -1):
        blocks.append(f"### 2026-06-{i + start - 1:02d} 10:00\n\n- change {i}\n")
    return "\n".join(blocks)


def _context(waiting_rows: str = "", recent: str = "### 2026-07-01 10:00\n\n- created\n") -> str:
    return f"""# Demo - Context

**Last Updated:** 2026-07-10 12:00

## Description

A demo project.

## Gotchas

- TBD

## Waiting on

{ch.WAITING_ON_NOTE}

| What | Who | Since | Gates |
|------|-----|-------|-------|
{waiting_rows}
## Next Steps

1. Do the thing

## Recent Changes

{recent}
## Key Architectural Decisions

- TBD

## Key Files

| File | Purpose |
|------|---------|
"""


class TestDeriveJournalPath:
    def test_prefixed(self, tmp_path):
        p = tmp_path / "foo-context.md"
        assert ch.derive_journal_path(p) == tmp_path / "foo-journal.md"

    def test_legacy_bare(self, tmp_path):
        p = tmp_path / "sub" / "context.md"
        assert ch.derive_journal_path(p) == tmp_path / "sub" / "journal.md"


class TestParseWaitingOn:
    def test_wellformed_rows(self):
        content = _context(
            "| Reply on PR | Jose | 2026-07-10 | GC-1 rework |\n"
            "| Egress check | Nitzan | ~2026-07-09 | GC-2 closure |\n"
        )
        rows = ch.parse_waiting_on(content)
        assert rows == [
            {"what": "Reply on PR", "who": "Jose", "since": "2026-07-10", "gates": "GC-1 rework"},
            {"what": "Egress check", "who": "Nitzan", "since": "~2026-07-09", "gates": "GC-2 closure"},
        ]

    def test_empty_table(self):
        assert ch.parse_waiting_on(_context()) == []

    def test_missing_section(self):
        assert ch.parse_waiting_on("# X\n\n## Next Steps\n\n1. a\n") == []

    def test_short_row_padded(self):
        content = _context("| Only what | Someone |\n")
        rows = ch.parse_waiting_on(content)
        assert rows[0] == {"what": "Only what", "who": "Someone", "since": "", "gates": ""}


class TestWaitingOnRendering:
    def test_build_section_roundtrips_through_parse(self):
        rows = [{"what": "A", "who": "B", "since": "2026-07-01", "gates": "C"}]
        section = ch.build_waiting_on_section(rows)
        assert ch.parse_waiting_on("# H\n\n" + section + "\n## Next Steps\n") == rows
        assert ch.WAITING_ON_NOTE in section

    def test_insert_before_next_steps(self):
        content = "# X\n\n## Description\n\nd\n\n## Next Steps\n\n1. a\n"
        out = ch.insert_waiting_on_before_next_steps(
            content, ch.build_waiting_on_section([])
        )
        assert out.index("## Waiting on") < out.index("## Next Steps")
        assert out.index("## Description") < out.index("## Waiting on")
        # Original prose intact.
        assert "## Description\n\nd\n" in out

    def test_insert_falls_back_to_recent_changes(self):
        content = "# X\n\n## Description\n\nd\n\n## Recent Changes\n\n### t\n\n- c\n"
        out = ch.insert_waiting_on_before_next_steps(
            content, ch.build_waiting_on_section([])
        )
        assert out.index("## Waiting on") < out.index("## Recent Changes")

    def test_insert_appends_when_no_anchor(self):
        out = ch.insert_waiting_on_before_next_steps(
            "# X\n\n## Description\n\nd\n", ch.build_waiting_on_section([])
        )
        assert out.rstrip().endswith("|------|-----|-------|-------|")


class TestRecentChangesParsing:
    def test_counts_subsections_under_first_heading_only(self):
        content = _context(recent=_rc_subsections(3))
        assert len(ch.parse_recent_changes_subsections(content)) == 3

    def test_ignores_legacy_sibling_h2(self):
        content = (
            _context(recent=_rc_subsections(2))
            + "\n## Recent Changes (2026-04-30 10:20)\n\nold prose\n\n"
            + "### 2026-04-30 09:00\n\n- legacy entry\n"
        )
        # Only the 2 under the FIRST heading count; the legacy block's ###
        # belongs to the sibling h2 and must not be mis-capped.
        assert len(ch.parse_recent_changes_subsections(content)) == 2

    def test_missing_section_returns_empty(self):
        assert ch.parse_recent_changes_subsections("# X\n\n## Description\n\nd\n") == []


class TestSplitForCap:
    def test_under_limit_noop(self):
        content = _context(recent=_rc_subsections(3))
        new, journal, moved = ch.split_recent_changes_for_cap(content, "demo-journal.md")
        assert new == content
        assert journal is None
        assert moved == 0

    def test_over_limit_keeps_newest(self):
        content = _context(recent=_rc_subsections(15))
        new, journal, moved = ch.split_recent_changes_for_cap(content, "demo-journal.md")
        assert moved == 3
        kept = ch.parse_recent_changes_subsections(new)
        assert len(kept) == ch.RECENT_CHANGES_CAP
        # Newest (change 15 -> day 15) kept, oldest (days 1-3) moved out.
        assert "- change 15" in new
        assert "- change 3" not in new
        assert journal is not None and "- change 3" in journal

    def test_journal_is_oldest_first(self):
        content = _context(recent=_rc_subsections(14))
        _, journal, moved = ch.split_recent_changes_for_cap(content, "demo-journal.md")
        assert moved == 2
        assert journal is not None
        # Overflow = the 2 oldest (change 1, change 2); journal must read
        # oldest -> newest top to bottom.
        assert journal.index("- change 1") < journal.index("- change 2")

    def test_pointer_at_section_bottom(self):
        content = _context(recent=_rc_subsections(13))
        new, _, _ = ch.split_recent_changes_for_cap(content, "demo-journal.md")
        pointer = ch.RECENT_CHANGES_POINTER.format(journal_name="demo-journal.md")
        body = ch.extract_section(new, "Recent Changes")
        assert body is not None and pointer in body
        # Bottom: after the last kept subsection's content.
        assert body.rindex(pointer) > body.rindex("- change")

    def test_pointer_not_duplicated_on_second_rollover(self):
        content = _context(recent=_rc_subsections(13))
        once, _, _ = ch.split_recent_changes_for_cap(content, "demo-journal.md")
        # Prepend one more entry (simulates the live writer), roll again.
        heading_end = once.index("## Recent Changes") + len("## Recent Changes\n")
        again = (
            once[:heading_end]
            + "\n### 2026-06-20 10:00\n\n- change 16\n\n"
            + once[heading_end:]
        )
        twice, _journal, moved = ch.split_recent_changes_for_cap(again, "demo-journal.md")
        assert moved == 1
        pointer = ch.RECENT_CHANGES_POINTER.format(journal_name="demo-journal.md")
        assert twice.count(pointer) == 1
        body = ch.extract_section(twice, "Recent Changes")
        assert body is not None and body.rindex(pointer) > body.rindex("- change")

    def test_sections_after_recent_changes_untouched(self):
        content = _context(recent=_rc_subsections(15))
        new, _, _ = ch.split_recent_changes_for_cap(content, "demo-journal.md")
        assert "## Key Architectural Decisions" in new
        assert "## Key Files" in new
        assert new.index("## Recent Changes") < new.index("## Key Architectural Decisions")


class TestDateParsing:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("**Last Updated:** 2026-07-10 12:30", datetime(2026, 7, 10, 12, 30)),
            ("**Last Updated:** 2026-07-10", datetime(2026, 7, 10)),
        ],
    )
    def test_last_updated_valid(self, raw, expected):
        assert ch.parse_last_updated(f"# X\n\n{raw}\n") == expected

    def test_last_updated_malformed(self):
        assert ch.parse_last_updated("# X\n\n**Last Updated:** soonish\n") is None
        assert ch.parse_last_updated("# X\n\nno header\n") is None

    @pytest.mark.parametrize(
        "cell,expected",
        [
            ("2026-07-09", date(2026, 7, 9)),
            ("~2026-07-09", date(2026, 7, 9)),
            ("yesterday", None),
            ("", None),
            ("2026-13-40", None),
        ],
    )
    def test_since_cell(self, cell, expected):
        assert ch.parse_since_date(cell) == expected


class TestHealthCheck:
    NOW = datetime(2026, 7, 11, 12, 0)

    def test_clean_returns_empty(self, tmp_path):
        content = _context().replace("2026-07-10 12:00", "2026-07-11 09:00")
        assert ch.check_context_health(content, tmp_path / "x.md", now=self.NOW) == []

    def test_stale_last_updated(self, tmp_path):
        content = _context().replace("2026-07-10 12:00", "2026-06-01 09:00")
        warnings = ch.check_context_health(content, tmp_path / "x.md", now=self.NOW)
        assert any("Last Updated" in w for w in warnings)

    def test_fresh_last_updated_not_flagged(self, tmp_path):
        warnings = ch.check_context_health(_context(), tmp_path / "x.md", now=self.NOW)
        assert not any("Last Updated" in w for w in warnings)

    def test_stale_waiting_row(self, tmp_path):
        content = _context("| Old ask | Bob | 2026-06-20 | thing |\n")
        warnings = ch.check_context_health(content, tmp_path / "x.md", now=self.NOW)
        assert any("Old ask" in w and "Bob" in w for w in warnings)

    def test_malformed_since_skipped(self, tmp_path):
        content = _context("| Vague ask | Bob | soon | thing |\n")
        warnings = ch.check_context_health(content, tmp_path / "x.md", now=self.NOW)
        assert not any("Vague ask" in w for w in warnings)

    def test_size_over_budget(self, tmp_path):
        f = tmp_path / "big.md"
        content = _context() + "x" * (ch.CONTEXT_SIZE_BUDGET_KB * 1024)
        f.write_text(content)
        warnings = ch.check_context_health(content, f, now=self.NOW)
        assert any("budget" in w for w in warnings)

    def test_missing_core_sections_each_flagged(self, tmp_path):
        content = "# X\n\n**Last Updated:** 2026-07-11\n\n## Description\n\nd\n"
        warnings = ch.check_context_health(content, tmp_path / "x.md", now=self.NOW)
        for name in ("Gotchas", "Waiting on", "Next Steps", "Recent Changes"):
            assert any(f"## {name}" in w for w in warnings)
        assert not any("## Description" in w for w in warnings)

    def test_recent_changes_over_cap_flagged(self, tmp_path):
        content = _context(recent=_rc_subsections(15))
        warnings = ch.check_context_health(content, tmp_path / "x.md", now=self.NOW)
        assert any("cap" in w for w in warnings)


class TestBuildDigest:
    def test_all_keys_present(self, tmp_path):
        f = tmp_path / "demo-context.md"
        content = _context("| A | B | 2026-07-10 | C |\n")
        f.write_text(content)
        digest = ch.build_digest(content, f)
        assert set(digest) == {
            "last_updated", "hub", "related_projects", "waiting_on", "next_steps",
            "recent_changes_last3", "section_index", "file_size_bytes",
            "health_warnings",
        }
        assert digest["last_updated"] == "2026-07-10 12:00"
        assert digest["file_size_bytes"] == f.stat().st_size

    def test_waiting_and_next_steps_verbatim(self, tmp_path):
        content = _context("| A | B | 2026-07-10 | C |\n")
        digest = ch.build_digest(content, tmp_path / "x.md")
        assert "| A | B | 2026-07-10 | C |" in digest["waiting_on"]
        assert "1. Do the thing" in digest["next_steps"]

    def test_last3_recent_changes_newest_first(self, tmp_path):
        content = _context(recent=_rc_subsections(5))
        digest = ch.build_digest(content, tmp_path / "x.md")
        assert len(digest["recent_changes_last3"]) == 3
        assert "- change 5" in digest["recent_changes_last3"][0]
        assert "- change 3" in digest["recent_changes_last3"][2]

    def test_hub_and_related_lines(self, tmp_path):
        content = _context().replace(
            "**Last Updated:** 2026-07-10 12:00",
            "**Last Updated:** 2026-07-10 12:00\nHub: [[demo-hub]]\n"
            "**Related projects:** [[other]] (shared infra)",
        )
        digest = ch.build_digest(content, tmp_path / "x.md")
        assert digest["hub"] == "Hub: [[demo-hub]]"
        assert digest["related_projects"] == "**Related projects:** [[other]] (shared infra)"

    def test_section_index_one_based_lines(self, tmp_path):
        content = "# X\n\n## Description\n\nd\n\n## Next Steps\n\n1. a\n"
        digest = ch.build_digest(content, tmp_path / "x.md")
        assert digest["section_index"][0] == {"name": "Description", "line": 3}
        assert digest["section_index"][1] == {"name": "Next Steps", "line": 7}

    def test_large_content_handled(self, tmp_path):
        # Larger than the 256KB Read-tool cap - the whole point of the digest.
        content = _context() + ("## Filler\n\n" + "x" * 400_000 + "\n")
        digest = ch.build_digest(content, tmp_path / "missing.md")
        assert digest["file_size_bytes"] > 256 * 1024
        assert digest["next_steps"] is not None


class TestFenceAwareness:
    """A fenced code block containing column-0 headings must be invisible
    to every structure scan (the harsh-critic Critical, 2026-07-11)."""

    FENCED_FAKE = """# Demo - Context

**Last Updated:** 2026-07-10 12:00

## Description

Example of the section shape:

```markdown
## Recent Changes

### 2020-01-01 00:00

- fake entry inside a fence
```

## Gotchas

- TBD

## Waiting on

{note}

| What | Who | Since | Gates |
|------|-----|-------|-------|

## Next Steps

1. Do the thing

## Recent Changes

### 2026-07-10 11:00

- real newest entry

### 2026-07-09 11:00

- real older entry
"""

    def _content(self):
        return self.FENCED_FAKE.format(note=ch.WAITING_ON_NOTE)

    def test_mask_fences_is_length_preserving(self):
        content = self._content()
        masked = ch.mask_fences(content)
        assert len(masked) == len(content)
        assert "fake entry" not in masked
        assert "real newest entry" in masked

    def test_fenced_heading_does_not_shadow_real_section(self):
        subs = ch.parse_recent_changes_subsections(self._content())
        headings = [h for h, _ in subs]
        assert headings == ["### 2026-07-10 11:00", "### 2026-07-09 11:00"]

    def test_prepend_lands_under_real_heading_not_in_fence(self):
        out = ch.prepend_recent_changes(self._content(), "2026-07-11 09:00", "- new entry")
        # The new entry sits after the REAL heading (which follows Next Steps),
        # never inside the fenced example in Description.
        real_heading = out.rindex("## Recent Changes")
        assert out.index("- new entry") > real_heading
        # Fence content untouched.
        assert "- fake entry inside a fence" in out

    def test_fenced_h3_does_not_inflate_count_or_trigger_false_rollover(self):
        # Exactly cap real entries + a fenced ### -> no rollover, fence intact.
        real = "\n".join(
            f"### 2026-06-{i:02d} 10:00\n\n- change {i}\n" for i in range(ch.RECENT_CHANGES_CAP, 0, -1)
        )
        content = self._content().replace(
            "### 2026-07-10 11:00\n\n- real newest entry\n\n### 2026-07-09 11:00\n\n- real older entry\n",
            real,
        )
        fenced_entry = "```text\n### 2019-01-01 00:00\nnot an entry\n```\n"
        content = content.replace("- change 5\n", "- change 5\n\n" + fenced_entry)
        new, journal, moved = ch.split_recent_changes_for_cap(content, "demo-journal.md")
        assert moved == 0
        assert journal is None
        assert new == content
        assert "### 2019-01-01 00:00" in new  # fence never torn

    def test_fenced_pipe_lines_not_table_rows(self):
        content = self._content().replace(
            "1. Do the thing",
            "1. Do the thing\n\n```text\n| not | a | real | row |\n```",
        )
        rows = ch.parse_waiting_on(content)
        assert rows == []


class TestPipeEscaping:
    """Cell values containing pipes must round-trip without column shifts
    (flagged independently by three reviewers)."""

    def test_pipe_in_cell_roundtrips(self):
        rows = [{"what": "Fix a|b split", "who": "Jose", "since": "2026-07-10", "gates": "GC-1 | GC-2"}]
        section = ch.build_waiting_on_section(rows)
        parsed = ch.parse_waiting_on("# H\n\n" + section + "\n## Next Steps\n")
        assert parsed == rows

    def test_newline_in_cell_flattened(self):
        rows = [{"what": "multi\nline", "who": "A", "since": "2026-07-10", "gates": "g"}]
        section = ch.build_waiting_on_section(rows)
        parsed = ch.parse_waiting_on("# H\n\n" + section + "\n## Next Steps\n")
        assert parsed[0]["what"] == "multi line"

    def test_rewrite_preserves_pipe_bearing_row(self):
        # Adding an unrelated row must not corrupt an existing escaped row.
        rows = [{"what": "A | B", "who": "X", "since": "2026-07-10", "gates": "g1"}]
        content = "# H\n\n" + ch.build_waiting_on_section(rows) + "\n## Next Steps\n\n1. x\n"
        rows2 = ch.parse_waiting_on(content)
        rows2.append({"what": "New", "who": "Y", "since": "2026-07-11", "gates": "g2"})
        content2 = ch.replace_waiting_on_table(content, rows2)
        parsed = ch.parse_waiting_on(content2)
        assert parsed[0]["what"] == "A | B"
        assert parsed[1]["what"] == "New"


class TestFreshWaitingRowNotFlagged:
    """Symmetric negative for the staleness check (mutation-verified gap:
    a check that flags EVERY row used to pass the suite)."""

    def test_fresh_row_not_flagged(self, tmp_path):
        now = datetime(2026, 7, 11, 12, 0)
        content = _context("| Fresh ask | Ann | 2026-07-09 | thing |\n").replace(
            "2026-07-10 12:00", "2026-07-11 09:00"
        )
        warnings = ch.check_context_health(content, tmp_path / "x.md", now=now)
        assert warnings == []

    def test_boundary_exactly_threshold_not_flagged(self, tmp_path):
        now = datetime(2026, 7, 11, 12, 0)
        since = "2026-07-04"  # exactly STALE_WAITING_DAYS old
        content = _context(f"| Edge ask | Ann | {since} | thing |\n").replace(
            "2026-07-10 12:00", "2026-07-11 09:00"
        )
        warnings = ch.check_context_health(content, tmp_path / "x.md", now=now)
        assert not any("Edge ask" in w for w in warnings)


class TestTemplateInvariants:
    """The Waiting-on note + table header are a cross-file contract between
    context_health and all three template copies - guard against drift."""

    REPO_ROOT = Path(__file__).resolve().parents[2]
    TEMPLATES = [
        REPO_ROOT / "mcp-server" / "src" / "mcp_missioncache" / "templates" / "context.md",
        REPO_ROOT / "templates" / "context.md",
        REPO_ROOT / "missioncache-auto" / "missioncache_auto" / "templates" / "__init__.py",
    ]

    @pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.parent.name)
    def test_note_and_header_and_core_sections_present(self, template):
        content = template.read_text()
        assert ch.WAITING_ON_NOTE in content
        assert ch.WAITING_ON_TABLE_HEADER in content
        for name in ch.CORE_SECTIONS:
            assert f"## {name}" in content

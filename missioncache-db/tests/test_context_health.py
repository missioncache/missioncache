"""Tests for missioncache_db.context_health.

Spec source: the context-file conventions plan (canonical section order,
Waiting on = What/Who/Since/Gates table before Next Steps, Recent Changes
capped at RECENT_CHANGES_CAP dated subsections with overflow rolled to a
per-project journal that reads oldest-first, pointer line at the BOTTOM of
the section). Every assertion traces to that contract, not to the parser
implementation.
"""

from datetime import date, datetime
import re
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

    def test_non_dated_h3_inside_entry_stays_with_entry(self):
        # A bare '### ' sub-heading in an entry body is NOT a subsection
        # boundary - only dated '### <timestamp>' headings are. The one
        # logical entry must parse as one subsection, keeping its tail.
        recent = "### 2026-07-11 10:00\n\n- did X\n\n### Design note\n\nmore detail\n"
        content = _context(recent=recent)
        subs = ch.parse_recent_changes_subsections(content)
        assert len(subs) == 1
        heading, body = subs[0]
        assert heading == "### 2026-07-11 10:00"
        assert "### Design note" in body
        assert "more detail" in body


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
            "last_updated", "hub", "fork_of", "related_projects", "waiting_on",
            "next_steps", "recent_changes_last3", "section_index",
            "file_size_bytes", "health_warnings",
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

    def test_four_backtick_fence_masks_inner_three_backtick_block(self):
        # A ```` fence shown to display a ``` sample stays open until the
        # matching ```` closer (CommonMark: closer length >= opener). The
        # inner ``` lines are content, so the fake section/entry inside must
        # stay masked and never shadow the real Recent Changes below it.
        content = (
            "# Demo - Context\n\n"
            "**Last Updated:** 2026-07-10 12:00\n\n"
            "## Description\n\n"
            "````markdown\n"
            "```\n"
            "## Recent Changes\n\n### 2020-01-01 00:00\n\n- fake entry\n"
            "```\n"
            "````\n\n"
            "## Recent Changes\n\n"
            "### 2026-07-10 11:00\n\n- real entry\n"
        )
        masked = ch.mask_fences(content)
        assert len(masked) == len(content)
        assert "fake entry" not in masked
        assert "real entry" in masked
        subs = ch.parse_recent_changes_subsections(content)
        assert [h for h, _ in subs] == ["### 2026-07-10 11:00"]


class TestSectionBodyFenceAware:
    """replace_section_body / append_to_section_body must target the REAL
    section, never a column-0 heading inside a fenced code example that
    appears before it (Codex adversarial review, 2026-07-11: the sibling of
    the Recent Changes fence bug, on Next Steps / Gotchas / Key Files /
    Key Architectural Decisions)."""

    FIXTURE = """# Demo - Context

**Last Updated:** 2026-07-10 12:00

## Description

The canonical order, shown as an example:

```markdown
## Gotchas

## Next Steps

## Recent Changes
```

## Gotchas

- TBD

## Next Steps

1. old step

## Recent Changes

### 2026-07-10 11:00

- entry
"""

    FENCE_BLOCK = "```markdown\n## Gotchas\n\n## Next Steps\n\n## Recent Changes\n```"

    def test_replace_targets_real_next_steps_not_fenced(self):
        out = ch.replace_section_body(self.FIXTURE, "Next Steps", "1. new step")
        assert "1. new step" in out
        assert "1. old step" not in out
        # The fenced example is byte-for-byte intact - not torn open.
        assert self.FENCE_BLOCK in out

    def test_replace_leaves_following_sections_intact(self):
        out = ch.replace_section_body(self.FIXTURE, "Next Steps", "1. new step")
        # Recent Changes (the section after Next Steps) survives unharmed.
        assert "### 2026-07-10 11:00" in out
        assert "- entry" in out

    def test_append_targets_real_gotchas_and_strips_tbd(self):
        out = ch.append_to_section_body(
            self.FIXTURE, "Gotchas", "- a real gotcha", drop_lines=("- TBD", "1. TBD")
        )
        assert "- a real gotcha" in out
        # The placeholder in the REAL Gotchas is gone; the fenced example
        # (which carries no '- TBD') is untouched.
        gotchas_body = ch.extract_section(out, "Gotchas")
        assert gotchas_body is not None
        assert "- TBD" not in gotchas_body
        assert self.FENCE_BLOCK in out

    def test_append_keeps_drop_lines_only_on_exact_match(self):
        # A body line that merely CONTAINS 'TBD' is preserved; only exact
        # '- TBD' / '1. TBD' placeholder lines are dropped.
        content = self.FIXTURE.replace("- TBD", "- TBD: still deciding the retry cap")
        out = ch.append_to_section_body(
            content, "Gotchas", "- new", drop_lines=("- TBD", "1. TBD")
        )
        assert "- TBD: still deciding the retry cap" in out

    def test_replace_creates_section_at_eof_when_absent(self):
        base = "# Doc\n\n**Last Updated:** 2026-01-01\n\n## Description\n\nx\n"
        out = ch.replace_section_body(base, "Next Steps", "1. step")
        assert ch.extract_section(out, "Next Steps") is not None
        assert "1. step" in out

    def test_append_creates_section_at_eof_when_absent(self):
        base = "# Doc\n\n**Last Updated:** 2026-01-01\n\n## Description\n\nx\n"
        out = ch.append_to_section_body(base, "Notes", "- note")
        assert ch.extract_section(out, "Notes") is not None
        assert "- note" in out


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


# ── fork header in the digest ─────────────────────────────────────────────


class TestForkOfDigest:
    def test_fork_of_extracted_from_header(self, tmp_path):
        """A **Fork of:** line in the header region surfaces in the digest."""
        content = (
            "# child - Context\n"
            "**Last Updated:** 2026-07-14 10:00\n"
            "**Fork of:** parent-proj\n"
            "\n## Description\nBody.\n"
        )
        digest = ch.build_digest(content, tmp_path / "x.md")
        assert digest["fork_of"] == "**Fork of:** parent-proj"

    def test_fork_of_absent(self, tmp_path):
        """No header line -> fork_of is None."""
        content = "# solo - Context\n**Last Updated:** now\n\n## Description\nBody.\n"
        digest = ch.build_digest(content, tmp_path / "x.md")
        assert digest["fork_of"] is None

    def test_fork_of_below_first_section_ignored(self, tmp_path):
        """The contract reads Fork of only from the header region; a mention
        inside the body (e.g. quoted in a code fence) does not count."""
        content = (
            "# solo - Context\n**Last Updated:** now\n\n"
            "## Description\n```\n**Fork of:** not-really\n```\n"
        )
        digest = ch.build_digest(content, tmp_path / "x.md")
        assert digest["fork_of"] is None


# Spec source for the two classes below: the "Cross-project events" convention
# in rules/missioncache.md - an imported-event section sits ABOVE Waiting on and
# the link is recorded on a `**Related projects:**` header line so both sides
# know it exists.

_WITH_WAITING = (
    "# demo - Context\n"
    "**Last Updated:** 2026-08-01 10:00\n"
    "\n## Description\nBody.\n"
    "\n## Waiting on\n\n| What | Who | Since | Gates |\n|---|---|---|---|\n"
    "\n## Next Steps\n\n1. Thing\n"
)


class TestInsertSectionBefore:
    def test_inserts_before_the_first_matching_anchor(self):
        out = ch.insert_section_before(
            _WITH_WAITING, "## Event\n\nBody.", ("Waiting on", "Next Steps")
        )
        names = [s["name"] for s in ch.section_index(out)]
        assert names == ["Description", "Event", "Waiting on", "Next Steps"]

    def test_falls_back_to_the_next_anchor(self):
        """A file predating the Waiting on convention still gets the section in
        the right relative place."""
        content = "# demo - Context\n\n## Description\nBody.\n\n## Next Steps\n\n1. Thing\n"
        out = ch.insert_section_before(
            content, "## Event\n\nBody.", ("Waiting on", "Next Steps")
        )
        names = [s["name"] for s in ch.section_index(out)]
        assert names == ["Description", "Event", "Next Steps"]

    def test_appends_at_eof_when_no_anchor_exists(self):
        content = "# demo - Context\n\n## Description\nBody.\n"
        out = ch.insert_section_before(content, "## Event\n\nBody.", ("Waiting on",))
        assert [s["name"] for s in ch.section_index(out)] == ["Description", "Event"]

    def test_anchor_inside_a_fence_is_not_matched(self):
        """Fence-aware, like every other section locator here."""
        content = (
            "# demo - Context\n\n## Description\n```\n## Waiting on\n```\n"
            "\n## Next Steps\n\n1. Thing\n"
        )
        out = ch.insert_section_before(
            content, "## Event\n\nBody.", ("Waiting on", "Next Steps")
        )
        names = [s["name"] for s in ch.section_index(out)]
        assert names == ["Description", "Event", "Next Steps"]


class TestUpsertRelatedProjects:
    def test_creates_the_header_line_in_the_header_region(self):
        out = ch.upsert_related_projects(_WITH_WAITING, "other-proj", "shares the feed")
        assert (
            ch.build_digest(out, Path("x.md"))["related_projects"]
            == "**Related projects:** [[other-proj]] (shares the feed)"
        )

    def test_extends_an_existing_line(self):
        once = ch.upsert_related_projects(_WITH_WAITING, "a-proj", "feed")
        twice = ch.upsert_related_projects(once, "b-proj", "cluster")
        assert (
            ch.build_digest(twice, Path("x.md"))["related_projects"]
            == "**Related projects:** [[a-proj]] (feed), [[b-proj]] (cluster)"
        )

    def test_is_a_no_op_for_a_project_already_listed(self):
        once = ch.upsert_related_projects(_WITH_WAITING, "a-proj", "feed")
        assert ch.upsert_related_projects(once, "a-proj", "different note") == once

    def test_note_is_optional(self):
        out = ch.upsert_related_projects(_WITH_WAITING, "a-proj")
        assert (
            ch.build_digest(out, Path("x.md"))["related_projects"]
            == "**Related projects:** [[a-proj]]"
        )

    def test_does_not_disturb_the_sections(self):
        out = ch.upsert_related_projects(_WITH_WAITING, "a-proj", "feed")
        assert [s["name"] for s in ch.section_index(out)] == [
            "Description",
            "Waiting on",
            "Next Steps",
        ]


class TestUnclosedFenceDoesNotBlindTheParsers:
    """The 2026-09-04 damage class: a truncated snapshot leaves a dangling fence.

    Under the old CommonMark reading the dangling opener masked the rest of
    the file, so `_section_span` returned None for sections that plainly
    exist and every writer appended a duplicate at EOF instead.
    """

    CONTENT = (
        "# P - Context\n\n"
        "## Recent Changes\n\n"
        "### 2026-08-14 22:43\n\n"
        "- a snapshot that got cut mid-block:\n\n"
        "```markdown\n"
        "- Migration scripts. How we know which scr...\n\n"
        "## Key Architectural Decisions\n\n"
        "- a real decision\n\n"
        "## Key Files\n\n"
        "| File | Purpose |\n"
        "|------|---------|\n"
    )

    def test_headings_below_a_dangling_fence_stay_visible(self):
        names = [e["name"] for e in ch.section_index(self.CONTENT)]
        assert names == [
            "Recent Changes",
            "Key Architectural Decisions",
            "Key Files",
        ]

    def test_section_span_finds_a_section_below_the_dangling_fence(self):
        assert ch.extract_section(self.CONTENT, "Key Files") is not None

    def test_append_merges_instead_of_creating_a_duplicate(self):
        out = ch.append_to_section_body(
            self.CONTENT, "Key Files", "| `a.py` | does a |"
        )
        assert out.count("## Key Files") == 1

    def test_a_balanced_fence_still_hides_its_contents(self):
        content = "# P\n\n## Real\n\nx\n\n```\n## Not A Section\n```\n"
        names = [e["name"] for e in ch.section_index(content)]
        assert names == ["Real"]

    def test_the_dangling_opener_is_reported(self):
        assert ch.unbalanced_fence_line(self.CONTENT) == 9
        assert ch.unbalanced_fence_line("# P\n\n```\nx\n```\n") is None


class TestSanitizeBullet:
    """No sanitized text may carry a column-0 structure anchor."""

    POISON = (
        "**Pre-Compact Snapshot** (auto-saved before compaction)\n\n"
        "Recent assistant responses (oldest first):\n\n"
        "## Updated: aip-qa-guild\n\n"
        "**Session binding:** `abc`\n\n"
        "## 2026-08-14 a heading that looks dated\n\n"
        "```markdown\n"
        "- truncated mid-fence...\n"
    )

    def test_no_column_zero_h2_survives(self):
        out = ch.sanitize_bullet(self.POISON)
        assert not re.search(r"^## ", out, re.MULTILINE)

    def test_no_column_zero_dated_h3_survives(self):
        # Demotion ALONE would turn "## 2026-08-14 x" into "### 2026-08-14 x",
        # which is a perfectly good fake Recent Changes boundary. The indent
        # is what actually closes this.
        out = ch.sanitize_bullet(self.POISON)
        assert not re.search(r"^### \d{4}", out, re.MULTILINE)

    def test_the_dangling_fence_is_closed(self):
        out = ch.sanitize_bullet(self.POISON)
        assert ch.unbalanced_fence_line(out) is None

    def test_headings_are_kept_as_headings_not_deleted(self):
        out = ch.sanitize_bullet(self.POISON)
        assert "**Session binding:** `abc`" in out
        # One to three spaces: markdown stops treating a line as a heading at
        # four, where it becomes an indented code block instead. Asserting
        # only "### Updated" in out passes at any width.
        assert re.search(r"^ {1,3}### Updated: aip-qa-guild$", out, re.MULTILINE)

    def test_code_block_contents_keep_their_relative_indentation(self):
        # Every continuation line is indented by two as list continuation,
        # the fence included, so a fenced block inside a list item renders
        # with its own spacing intact - the renderer strips the two relative
        # to the marker. What must NOT change is the spacing WITHIN the block.
        out = ch.sanitize_bullet("text\n\n```\n  exact = spacing\n```\n")
        body = [l for l in out.split("\n") if "exact" in l][0]
        assert body == "    exact = spacing"  # 2 (continuation) + 2 (original)
        assert ch.dangling_fence(out) is None

    def test_single_line_and_empty_are_untouched(self):
        assert ch.sanitize_bullet("just a line") == "just a line"
        assert ch.sanitize_bullet("") == ""

    def test_a_poisoned_bullet_no_longer_truncates_recent_changes(self):
        content = (
            "# P\n\n## Recent Changes\n\n### 2026-01-01 00:00\n\n- old entry\n\n"
            "## Gotchas\n\n- g\n"
        )
        out = ch.prepend_recent_changes(
            content,
            "2026-09-04 12:00",
            "- " + ch.sanitize_bullet(self.POISON),
        )
        # Both the new and the pre-existing entry stay inside the section.
        assert len(ch.parse_recent_changes_subsections(out)) == 2
        assert ch.orphaned_recent_changes(out) == []


class TestAmbiguousSectionGuard:
    def test_writing_to_a_duplicated_section_is_refused(self):
        content = "# P\n\n## Key Files\n\n| a | b |\n\n## Key Files\n\n| c | d |\n"
        with pytest.raises(ch.AmbiguousSectionError):
            ch.append_to_section_body(content, "Key Files", "| e | f |")
        with pytest.raises(ch.AmbiguousSectionError):
            ch.replace_section_body(content, "Key Files", "x")

    def test_a_single_section_is_unaffected(self):
        content = "# P\n\n## Key Files\n\n| a | b |\n"
        assert "| e | f |" in ch.append_to_section_body(
            content, "Key Files", "| e | f |"
        )

    def test_a_heading_inside_a_fence_does_not_count_as_a_duplicate(self):
        content = "# P\n\n## Key Files\n\n```\n## Key Files\n```\n"
        assert "x" in ch.append_to_section_body(content, "Key Files", "x")


class TestSectionAndItemRemoval:
    DOC = (
        "# P - Context\n\n"
        "## Gotchas\n\n"
        "- keep me\n"
        "- WRONG (falsified): a theory\n"
        "  ### a demoted heading inside the item\n"
        "  and its continuation\n"
        "- keep me too\n\n"
        "## Guild items (2026-08-16)\n\n"
        "body\n\n"
        "## Key Files\n\n"
        "| File | Purpose |\n"
        "|------|---------|\n"
        "| `a.py` | does a |\n"
    )

    def test_remove_section_matches_the_full_heading_exactly(self):
        out, body = ch.remove_section(self.DOC, "Guild items (2026-08-16)")
        assert body is not None and "body" in body
        assert "Guild items" not in out
        assert "## Gotchas" in out and "## Key Files" in out

    def test_a_heading_prefix_must_not_match(self):
        # Without exact matching, removing "Key" would delete "## Key Files".
        assert ch.remove_section(self.DOC, "Key")[1] is None
        assert ch.remove_section(self.DOC, "Guild items")[1] is None

    def test_remove_list_item_takes_the_continuation_lines_with_it(self):
        out, item = ch.remove_list_item(self.DOC, "Gotchas", "falsified")
        assert item is not None
        assert "a demoted heading inside the item" in item
        assert "and its continuation" not in out
        assert "- keep me\n" in out and "- keep me too" in out

    def test_a_table_header_row_is_never_removed(self):
        assert ch.remove_list_item(self.DOC, "Key Files", "File")[1] is None

    def test_a_table_data_row_is_removed(self):
        out, item = ch.remove_list_item(self.DOC, "Key Files", "a.py")
        assert item == "| `a.py` | does a |"
        assert "| File | Purpose |" in out

    def test_no_match_and_missing_section_return_none(self):
        assert ch.remove_list_item(self.DOC, "Gotchas", "zzz")[1] is None
        assert ch.remove_list_item(self.DOC, "Nope", "x")[1] is None


class TestRepairContent:
    DAMAGED = (
        "# P - Context\n"
        "**Last Updated:** 2026-09-01 10:00\n\n"
        "## Recent Changes\n\n"
        "### 2026-09-01 10:00\n\n"
        "- newest\n\n"
        "Older entries live in `p-journal.md` (oldest first).\n\n"
        "## Updated: p\n\n"
        "a pasted save report\n\n"
        "### 2026-07-29 00:17\n\n"
        "- stranded older entry\n\n"
        "### 2026-07-28 10:13\n\n"
        "- another stranded entry\n\n"
        "Older entries live in `p-journal.md` (oldest first).\n\n"
        "## Key Files\n\n"
        "| File | Purpose |\n"
        "|------|---------|\n"
        "| `a.py` | does a |\n\n"
        "## Key Files\n\n"
        "| `b.py` | does b |\n"
    )

    def test_stranded_entries_come_back_into_the_section(self):
        out, _, report = ch.repair_content(self.DAMAGED, "p-journal.md")
        assert report["reabsorbed_entries"] == 2
        assert ch.orphaned_recent_changes(out) == []
        assert len(ch.parse_recent_changes_subsections(out)) == 3

    def test_reabsorbed_entries_are_ordered_newest_first(self):
        # DAMAGED's blocks are already descending, so "sorted == sorted" is
        # satisfied whether the sort runs or not. Feed it OUT of order.
        scrambled = (
            "# P - Context\n**Last Updated:** 2026-09-01 10:00\n\n"
            "## Recent Changes\n\n### 2026-07-01 10:00\n\n- oldest\n\n"
            "## Stray\n\n"
            "### 2026-08-01 10:00\n\n- middle\n\n"
            "### 2026-09-05 10:00\n\n- newest\n"
        )
        out, _, _ = ch.repair_content(scrambled, "p-journal.md")
        headings = [h for h, _ in ch.parse_recent_changes_subsections(out)]
        assert headings == [
            "### 2026-09-05 10:00",
            "### 2026-08-01 10:00",
            "### 2026-07-01 10:00",
        ]

    def test_duplicate_sections_merge_into_the_first(self):
        out, _, report = ch.repair_content(self.DAMAGED, "p-journal.md")
        assert report["merged_sections"] == ["Key Files (2 -> 1)"]
        assert out.count("## Key Files") == 1
        assert "| `a.py` | does a |" in out and "| `b.py` | does b |" in out

    def test_the_stray_prose_section_is_left_for_the_user_to_remove(self):
        # Repair fixes structure. Deciding a section is junk is the user's
        # call, made through update_context_file(sections_remove=[...]).
        out, _, _ = ch.repair_content(self.DAMAGED, "p-journal.md")
        assert "## Updated: p" in out
        assert "a pasted save report" in out

    def test_an_unbalanced_fence_is_reported_and_not_edited(self):
        content = "# P\n\n## Recent Changes\n\n### 2026-09-01 10:00\n\n- x\n\n```\ncut...\n"
        out, _, report = ch.repair_content(content, "p-journal.md")
        assert report["unbalanced_fence_line"] == 9
        assert out.count("```") == 1

    def test_a_healthy_file_is_returned_unchanged(self):
        content = (
            "# P\n\n## Recent Changes\n\n### 2026-09-01 10:00\n\n- only entry\n"
        )
        out, journal, report = ch.repair_content(content, "p-journal.md")
        assert out == content and journal is None
        assert report["merged_sections"] == [] and report["reabsorbed_entries"] == 0

    def test_the_cap_rolls_the_overflow_after_reabsorbing(self):
        entries = "".join(
            f"### 2026-08-{day:02d} 10:00\n\n- entry {day}\n\n" for day in range(1, 16)
        )
        content = f"# P\n\n## Recent Changes\n\n## Stray\n\n{entries}"
        out, journal, report = ch.repair_content(content, "p-journal.md")
        assert report["reabsorbed_entries"] == 15
        assert report["rolled_to_journal"] == 3
        assert journal is not None
        kept = [h for h, _ in ch.parse_recent_changes_subsections(out)]
        assert len(kept) == 12
        # WHICH twelve, not just how many. Counts alone are identical when
        # the newest-first sort is skipped, and the outcome is inverted: the
        # cap then archives the three NEWEST entries and keeps the oldest
        # twelve, which is the opposite of the section's purpose.
        assert kept[0] == "### 2026-08-15 10:00"
        assert kept[-1] == "### 2026-08-04 10:00"
        rolled = [l for l in journal.splitlines() if l.startswith("### ")]
        assert rolled == [
            "### 2026-08-01 10:00",
            "### 2026-08-02 10:00",
            "### 2026-08-03 10:00",
        ]


class TestStructuralHealthWarnings:
    def test_all_three_structural_findings_are_reported(self, tmp_path):
        path = tmp_path / "p-context.md"
        content = TestRepairContent.DAMAGED + "\n```\ncut...\n"
        path.write_text(content)
        warnings = ch.check_context_health(content, path)
        joined = " | ".join(warnings)
        assert "unbalanced code fence" in joined
        assert "'## Key Files' sections" in joined
        assert "outside the section" in joined

    def test_a_healthy_file_reports_no_structural_findings(self, tmp_path):
        path = tmp_path / "p-context.md"
        content = "# P\n\n## Recent Changes\n\n### 2026-09-01 10:00\n\n- x\n"
        path.write_text(content)
        joined = " | ".join(ch.check_context_health(content, path))
        assert "unbalanced" not in joined and "outside the section" not in joined


class TestLegacyRecentChangesSiblingsAreNotDuplicates:
    """The pre-2026-07-11 shape must stay writable.

    Files from before the conventions migration carry a run of legacy
    `## Recent Changes (2026-04-19 07:53)` sibling headings with no bare
    `## Recent Changes` among them - measured: 5 completed projects on this
    machine, one with 35 of them. `_section_span` reads those by design
    (prefix-tolerant), so if the ambiguity guard counted prefix matches it
    would refuse every write to a file that is merely old, and `repair`
    could not clear it because `duplicate_sections` keys on the full
    heading text and correctly sees none.
    """

    LEGACY = (
        "# P - Context\n\n"
        "## Recent Changes (2026-04-19 07:53)\n\n- a\n\n"
        "## Recent Changes (2026-04-19 08:56)\n\n- b\n\n"
        "## Recent Changes (2026-04-20 01:03)\n\n- c\n"
    )

    def test_the_guard_and_repair_agree_that_there_are_no_duplicates(self):
        assert ch.duplicate_sections(self.LEGACY) == {}
        # No exception - the write is allowed through. And it must land IN
        # the first sibling, not create a fourth section: replace_section_body
        # falls back to appending at EOF when the span is None, which would
        # satisfy a bare `"- new" in out` while producing the exact damage
        # this class exists to prevent.
        out = ch.replace_section_body(self.LEGACY, "Recent Changes", "- new")
        assert out.count("## Recent Changes") == 3
        body = ch.extract_section(out, "Recent Changes")
        assert body is not None and "- new" in body

    def test_a_genuine_repeat_of_the_same_heading_is_still_caught(self):
        content = self.LEGACY + "\n## Recent Changes (2026-04-19 07:53)\n\n- dup\n"
        assert ch.duplicate_sections(content) == {
            "Recent Changes (2026-04-19 07:53)": 2
        }
        with pytest.raises(ch.AmbiguousSectionError):
            ch.append_to_section_body(
                content, "Recent Changes (2026-04-19 07:53)", "- x"
            )


class TestDanglingFenceUsesTheOpenersDelimiter:
    """A closer must match the opener's character and length.

    Three backticks close neither a `~~~` block nor a four-backtick block,
    so a sanitizer that always appends ``` leaves the text still unbalanced -
    and the whole point of closing it is that the following headings stop
    being fence content.
    """

    @pytest.mark.parametrize(
        "opener", ["```", "````", "`````", "~~~", "~~~~"]
    )
    def test_every_legal_fence_form_is_closed(self, opener):
        text = f"prose\n\n{opener}markdown\n- cut mid block..."
        out = ch.sanitize_bullet(text)
        assert ch.dangling_fence(out) is None, f"{opener!r} left unbalanced"

    @pytest.mark.parametrize("opener", ["~~~", "````"])
    def test_headings_after_a_closed_fence_are_still_neutralized(self, opener):
        # Until the fence closes, demote_headings treats what follows as code
        # and skips it. Closing it first is what puts those headings back in
        # scope, so a wrong closer silently leaves them at column 0.
        text = f"{opener}\ncode\n{opener}\n\n## Real Heading\n\n{opener}\ncut..."
        out = ch.sanitize_bullet(text)
        assert not re.search(r"^## ", out, re.MULTILINE)
        assert ch.dangling_fence(out) is None

    def test_dangling_fence_reports_line_and_delimiter(self):
        assert ch.dangling_fence("a\n\n~~~\nb\n") == (3, "~~~")
        assert ch.dangling_fence("a\n\n```\nb\n```\n") is None

    def test_a_shorter_inner_fence_does_not_close_a_longer_one(self):
        # CommonMark: the closer must be at least as long as the opener.
        assert ch.dangling_fence("````\n```\ninner\n") is not None


class TestRepairDoesNotEatLegacySiblingSections:
    """Repair must merge only what the detector counted.

    A prefix-tolerant merge regex collapses `## Recent Changes` x2 AND the
    legacy `## Recent Changes (2026-04-19 07:53)` siblings into one, deleting
    three headings `duplicate_sections` never counted, while reporting
    "2 -> 1". The two must use the same exact match.
    """

    MIXED = (
        "# P - Context\n\n"
        "## Recent Changes\n\n### 2026-09-01 10:00\n\n- newest\n\n"
        "## Recent Changes\n\n### 2026-08-01 10:00\n\n- second bare\n\n"
        "## Recent Changes (2026-04-19 07:53)\n\n- legacy A\n\n"
        "## Recent Changes (2026-04-19 08:56)\n\n- legacy B\n"
    )

    def test_only_the_exact_duplicates_merge(self):
        out, _, report = ch.repair_content(self.MIXED, "p-journal.md")
        assert report["merged_sections"] == ["Recent Changes (2 -> 1)"]
        headings = [l for l in out.splitlines() if l.startswith("## ")]
        assert headings == [
            "## Recent Changes",
            "## Recent Changes (2026-04-19 07:53)",
            "## Recent Changes (2026-04-19 08:56)",
        ], "the legacy dated siblings must survive"

    def test_the_report_count_matches_what_actually_merged(self):
        before = self.MIXED.count("## Recent Changes")
        out, _, report = ch.repair_content(self.MIXED, "p-journal.md")
        after = out.count("## Recent Changes")
        # "Recent Changes (2 -> 1)" claims one heading disappeared.
        assert before - after == 1, f"report said 2 -> 1 but {before - after} vanished"


class TestRemoveSectionReturnsTheBodyItCut:
    DOC = (
        "# P\n\n"
        "## Notes (old)\n\nthe OLD body\n\n"
        "## Notes\n\nthe CURRENT body\n\n"
        "## Next Steps\n\n1. go\n"
    )

    def test_the_returned_body_belongs_to_the_removed_section(self):
        # Reader and remover must resolve to the SAME section. They used to
        # diverge: extract_section took the first prefix match (`## Notes
        # (old)`) while remove_section cut the exact one, so a move carried
        # the body of one section and deleted another. Both now prefer the
        # exact heading, and remove_section returns what it actually cut.
        assert "the CURRENT body" in (ch.extract_section(self.DOC, "Notes") or "")
        out, body = ch.remove_section(self.DOC, "Notes")
        assert "the CURRENT body" in body
        assert "the OLD body" not in body
        assert "## Notes (old)" in out and "the OLD body" in out

    def test_a_miss_returns_none(self):
        assert ch.remove_section(self.DOC, "Nope")[1] is None


class TestOrphanWarningOnlyPromisesWhatRepairDoes:
    def test_a_hand_written_dated_heading_is_not_promised_to_repair(self, tmp_path):
        content = (
            "# P\n\n## Recent Changes\n\n### 2026-09-01 10:00\n\n- x\n\n"
            "## Some Event\n\n### 2026-08-14 sync notes\n\nprose\n"
        )
        path = tmp_path / "p-context.md"
        path.write_text(content)
        warning = next(
            w for w in ch.check_context_health(content, path) if "outside the section" in w
        )
        assert "repair` leaves them alone" in warning
        # And repair genuinely does not move it.
        out, _, report = ch.repair_content(content, "p-journal.md")
        assert report["reabsorbed_entries"] == 0
        assert "## Some Event" in out

    def test_a_writer_shaped_orphan_is_promised_to_repair(self, tmp_path):
        content = (
            "# P\n\n## Recent Changes\n\n### 2026-09-01 10:00\n\n- x\n\n"
            "## Stray\n\n### 2026-08-14 10:00\n\n- stranded\n"
        )
        path = tmp_path / "p-context.md"
        path.write_text(content)
        warning = next(
            w for w in ch.check_context_health(content, path) if "outside the section" in w
        )
        assert "run `missioncache-db repair`" in warning


class TestClosingFenceCarriesNoInfoString:
    """CommonMark: an info string is opener-only.

    Without this, a truncated ```markdown block pairs with the NEXT block's
    ```python OPENER, masking everything between them - which hid a whole
    Recent Changes entry while every diagnostic stayed quiet.
    """

    def test_an_info_string_line_cannot_close_a_block(self):
        doc = "# P\n\n```markdown\ncontent\n```python\nmore\n```\n"
        # One block from ```markdown to the bare ```, not two.
        assert ch.mask_fences(doc).count("content") == 0
        assert ch.dangling_fence(doc) is None

    def test_a_bare_closer_still_closes(self):
        assert ch.dangling_fence("# P\n\n```py\nx\n```\n") is None

    def test_trailing_whitespace_on_a_closer_is_allowed(self):
        assert ch.dangling_fence("# P\n\n```\nx\n```   \n") is None


class TestEntriesHiddenByFences:
    """The residue of the truncated-snapshot damage.

    A cut-off block is closed by a LATER block's closer, so the fences
    balance and the content between is legitimately code. Nothing looks
    wrong, and the entries in that span are invisible to every reader.
    """

    DAMAGED = (
        "# P - Context\n\n## Recent Changes\n\n"
        "### 2026-09-02 10:00\n\n- newest, cut off mid-block:\n\n"
        "```markdown\n- cut...\n\n"
        "### 2026-08-01 10:00\n\n- an older entry\n\n"
        "```python\nprint('sample')\n```\n"
    )

    def test_the_swallowed_entry_is_reported_with_its_line(self):
        assert ch.entries_hidden_by_fences(self.DAMAGED) == [12]

    def test_the_parser_genuinely_cannot_see_it(self):
        # The report exists because the loss is real and unrecoverable.
        assert len(ch.parse_recent_changes_subsections(self.DAMAGED)) == 1

    def test_the_health_check_names_the_loss(self, tmp_path):
        path = tmp_path / "p-context.md"
        path.write_text(self.DAMAGED)
        joined = " | ".join(ch.check_context_health(self.DAMAGED, path))
        assert "inside a code fence" in joined
        assert "line 12" in joined

    def test_a_healthy_code_sample_does_not_warn(self, tmp_path):
        ok = "# P\n\n## Recent Changes\n\n### 2026-09-01 10:00\n\n- x\n\n```python\n# note\n```\n"
        path = tmp_path / "p-context.md"
        path.write_text(ok)
        assert ch.entries_hidden_by_fences(ok) == []
        assert not [w for w in ch.check_context_health(ok, path) if "code fence" in w]


class TestRepairRefusesWhenTheFenceIsUnknown:
    """Restructuring on a guess can hoist text out of a code sample."""

    FENCE_TRAP = (
        "# P\n\n## Gotchas\n\n- WRONG: a real gotcha\n\n"
        "## Key Files\n\n| File | Purpose |\n|------|---------|\n\nexample:\n\n"
        "```markdown\n## Gotchas\n\n- WRONG: text from the code sample\n"
    )

    def test_nothing_is_restructured_behind_an_unclosed_fence(self):
        out, journal, report = ch.repair_content(self.FENCE_TRAP, "p-journal.md")
        assert report["skipped_for_fence"] is True
        assert report["merged_sections"] == []
        assert out == self.FENCE_TRAP
        assert journal is None

    def test_the_code_sample_text_is_not_hoisted_into_the_live_section(self):
        out, _, _ = ch.repair_content(self.FENCE_TRAP, "p-journal.md")
        gotchas = ch.extract_section(out, "Gotchas")
        assert "text from the code sample" not in gotchas

    def test_a_clean_file_is_still_repaired(self):
        clean = (
            "# P\n\n## Recent Changes\n\n### 2026-09-01 10:00\n\n- x\n\n"
            "## Key Files\n\n| a | b |\n\n## Key Files\n\n| c | d |\n"
        )
        _, _, report = ch.repair_content(clean, "p-journal.md")
        assert report["skipped_for_fence"] is False
        assert report["merged_sections"] == ["Key Files (2 -> 1)"]


class TestRemoveListItemIsFenceAwareAndExact:
    def test_a_line_inside_a_code_block_is_not_a_list_item(self):
        doc = "# P\n\n## Gotchas\n\n- keep\n- run this:\n\n```\n- rm -rf /x\n```\n\n- also keep\n"
        out, item = ch.remove_list_item(doc, "Gotchas", "rm -rf")
        # The whole bullet that OWNS the code block goes, fence included -
        # never the inner line alone, which left a dangling opener behind.
        assert item is not None and item.startswith("- run this:")
        assert ch.dangling_fence(out) is None
        assert "- keep" in out and "- also keep" in out

    def test_a_truncated_section_name_does_not_resolve(self):
        # _section_span is prefix-tolerant, which walked straight past the
        # caller's forbidden-section guard.
        doc = "# P\n\n## Recent Changes\n\n### 2026-09-01 10:00\n\n- entry one\n"
        assert ch.remove_list_item(doc, "Recent Change", "entry")[1] is None
        assert ch.remove_list_item(doc, "Recent", "entry")[1] is None

    def test_trailing_prose_is_not_swallowed_by_the_last_bullet(self):
        doc = "# P\n\n## Gotchas\n\n- a\n- b\n\nNOTE: closing prose.\n"
        out, item = ch.remove_list_item(doc, "Gotchas", "- b")
        assert item == "- b"
        assert "NOTE: closing prose." in out


class TestExactHeadingWinsOverPrefix:
    def test_a_legacy_sibling_does_not_shadow_the_real_section(self):
        doc = (
            "# P\n\n## Recent Changes (2026-04-19 07:53)\n\n- legacy\n\n"
            "## Recent Changes\n\n- the real one\n"
        )
        assert "the real one" in (ch.extract_section(doc, "Recent Changes") or "")
        assert "legacy" not in (ch.extract_section(doc, "Recent Changes") or "")

    def test_prefix_tolerance_still_reads_a_legacy_only_file(self):
        legacy = "# P\n\n## Recent Changes (2026-04-19 07:53)\n\n- legacy\n"
        assert "legacy" in (ch.extract_section(legacy, "Recent Changes") or "")


class TestSanitizedTextIsSafeAsABulletBody:
    """Every caller writes `f"- {sanitize_bullet(x)}"`.

    The `- ` prefix is not whitespace, so _FENCE_RE cannot see an opener on
    the first line - while the closer appended by rule 1 lands at column 0
    and becomes an opener. Measured: one such bullet produced a duplicate
    `## Recent Changes` and made check_context_health report four core
    sections "missing" that were plainly present, with dangling_fence and
    duplicate_sections both silent.
    """

    @pytest.mark.parametrize(
        "raw",
        [
            "```\n## Recent Changes\nstuff",
            "~~~\n## Gotchas\nx",
            "````\n## Gotchas\nx",
            "## Updated: p\n\nbody\n\n```\ncut...",
            "before\n```python\ncode\n## H\n```\nafter",
            "text\n\n```markdown\n- cut...",
            "plain one-liner",
        ],
    )
    def test_no_input_breaks_the_file_when_written_as_a_bullet(self, raw):
        doc = (
            "# P\n\n## Recent Changes\n\n### 2026-09-01 10:00\n\n"
            + "- " + ch.sanitize_bullet(raw)
            + "\n\n## Gotchas\n\n- g\n\n## Next Steps\n\n1. go\n\n```\nlater\n```\n"
        )
        assert [e["name"] for e in ch.section_index(doc)] == [
            "Recent Changes", "Gotchas", "Next Steps",
        ]
        assert ch.duplicate_sections(doc) == {}
        assert ch.dangling_fence(doc) is None

    def test_a_leading_fence_is_pushed_onto_its_own_line(self):
        out = ch.sanitize_bullet("```\ncode\n```")
        assert out.split("\n")[0] == ""

    def test_the_text_survives(self):
        out = ch.sanitize_bullet("```\n## Recent Changes\nstuff")
        assert "## Recent Changes" in out and "stuff" in out


class TestPmMirrorIsNotRefusedOnADamagedFile:
    def test_strict_false_writes_into_the_first_of_two(self):
        # The PM mirror runs after the DB row is committed and its caller
        # swallows exceptions, so a refusal there loses the write silently.
        doc = "# P\n\n## Action Items\n\nold\n\n## Action Items\n\nother\n"
        with pytest.raises(ch.AmbiguousSectionError):
            ch.replace_section_body(doc, "Action Items", "new")
        out = ch.replace_section_body(doc, "Action Items", "new", strict=False)
        assert "new" in out

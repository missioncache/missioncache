"""Tests for the tasks.md checklist parser.

Pure-function tests on string inputs. The parser is consumed by both
the MCP tools (validating set_active_orbit_tasks input) and the
update_tasks_file completed-numbers diff.
"""

from __future__ import annotations

from mcp_orbit.tasks_parse import (
    ChecklistItem,
    find_item,
    parse_tasks_md,
)


class TestParseTasksMd:
    def test_basic_checklist(self):
        content = (
            "# Title\n"
            "- [ ] 8. Draft Show HN post\n"
            "- [x] 9. Done item\n"
        )
        items = parse_tasks_md(content)
        assert items == [
            ChecklistItem(number="8", text="Draft Show HN post", checked=False),
            ChecklistItem(number="9", text="Done item", checked=True),
        ]

    def test_indented_subtasks(self):
        content = (
            "- [ ] 54. Parent\n"
            "  - [ ] 54a. Sub a\n"
            "  - [x] 54b. Sub b\n"
        )
        items = parse_tasks_md(content)
        assert [(i.number, i.checked) for i in items] == [
            ("54", False),
            ("54a", False),
            ("54b", True),
        ]

    def test_dotted_numbers(self):
        items = parse_tasks_md("- [ ] 0.1. Phase 0 sub\n- [ ] 0.2. Another\n")
        assert [i.number for i in items] == ["0.1", "0.2"]

    def test_uppercase_x_treated_as_checked(self):
        items = parse_tasks_md("- [X] 8. text\n")
        assert items[0].checked is True

    def test_skips_non_checklist_lines(self):
        content = (
            "## Header\n"
            "Some prose\n"
            "- list item without checkbox\n"
            "- [ ] 8. real one\n"
        )
        items = parse_tasks_md(content)
        assert len(items) == 1
        assert items[0].number == "8"

    def test_empty_input(self):
        assert parse_tasks_md("") == []

    def test_strips_trailing_whitespace_from_text(self):
        items = parse_tasks_md("- [ ] 8. text with trailing spaces   \n")
        assert items[0].text == "text with trailing spaces"


class TestFindItem:
    def test_finds_existing(self):
        items = parse_tasks_md("- [ ] 8. foo\n- [ ] 54a. bar\n")
        item = find_item(items, "54a")
        assert item is not None
        assert item.text == "bar"

    def test_returns_none_for_missing(self):
        items = parse_tasks_md("- [ ] 8. foo\n")
        assert find_item(items, "99") is None

    def test_exact_match_only(self):
        """``"54"`` does not match ``"54a"`` - exact string equality."""
        items = parse_tasks_md("- [ ] 54a. sub\n")
        assert find_item(items, "54") is None

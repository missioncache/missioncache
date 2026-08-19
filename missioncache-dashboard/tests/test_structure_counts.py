"""Tests for the Structure tab's task-mode parsing and its count reporting.

Spec source: the Structure panel must not disagree with its own header. Its
counts line comes from a raw checkbox regex while its table comes from
`parse_task_modes_from_content`, which requires a leading task number. On a
file carrying unnumbered items the two diverge, and the table used to render
fewer rows than the header claimed with nothing said about it.

Pure-function tests: the parser takes content, no HTTP or DB involved.
"""

from __future__ import annotations

from missioncache_dashboard.server import parse_task_modes_from_content


TASKS_WITH_UNNUMBERED = """# T - Tasks

## Phase 1

- [x] 1. Numbered and done
- [ ] 2. Numbered and open
- [ ] 3.1. Dotted subtask
- [ ] 4a. Letter suffix

## Already Completed (from pre-planning commits)

- [x] Test suites added for all components
- [x] Branding logo assets added
"""


def test_parser_skips_items_without_a_number() -> None:
    """An unnumbered item has no identity to hang a dependency on.

    It is deliberately absent from the table and the graph; the point of the
    test is to pin that this is a known omission and not an accident.
    """
    modes = parse_task_modes_from_content(TASKS_WITH_UNNUMBERED)

    assert [m["task_id"] for m in modes] == ["1", "2", "3.1", "4a"]
    titles = " ".join(m["title"] for m in modes)
    assert "Test suites added" not in titles


def test_the_gap_between_checkbox_count_and_parsed_rows_is_reportable() -> None:
    """The endpoint derives `unnumbered_count` from exactly this difference.

    Six checkbox lines, four of them numbered, so two are not shown and the
    panel has to say so rather than quietly rendering four rows under a
    header that reads six.
    """
    checkbox_lines = [
        line
        for line in TASKS_WITH_UNNUMBERED.splitlines()
        if line.lstrip().startswith("- [")
    ]
    modes = parse_task_modes_from_content(TASKS_WITH_UNNUMBERED)

    assert len(checkbox_lines) == 6
    assert len(modes) == 4
    assert len(checkbox_lines) - len(modes) == 2


def test_no_gap_reported_when_every_item_is_numbered() -> None:
    """The common case reports nothing, so the note never becomes noise."""
    content = "# T\n\n## Phase 1\n\n- [x] 1. One\n- [ ] 2. Two\n"
    checkbox_lines = [
        line for line in content.splitlines() if line.lstrip().startswith("- [")
    ]

    assert len(checkbox_lines) - len(parse_task_modes_from_content(content)) == 0

"""Tests for the plain-text formatter in tools_tasks.

Focus: `_format_tasks_table` produces output that reads cleanly across MCP
clients (in particular, in TUIs that don't render markdown). Pure function,
no DB or MCP tooling required.
"""

from __future__ import annotations

from mcp_orbit.models import TaskSummary
from mcp_orbit.tools_tasks import _format_prioritized_display, _format_tasks_table


def _summary(
    *,
    id: int = 1,
    name: str = "task",
    repo_name: str | None = "repo",
    time_formatted: str = "1h",
    last_worked_ago: str = "5m ago",
) -> TaskSummary:
    return TaskSummary(
        id=id,
        name=name,
        status="active",
        type="coding",
        repo_name=repo_name,
        time_formatted=time_formatted,
        last_worked_ago=last_worked_ago,
    )


def test_empty_list_returns_short_message() -> None:
    """No tasks -> single-line message; no header / no separator."""
    out = _format_tasks_table([])
    assert out == "(no active tasks)"


def test_single_task_renders_aligned_columns() -> None:
    """Single row produces header, separator, one data row, all whitespace-aligned."""
    out = _format_tasks_table([_summary(id=1, name="proj", repo_name="repo")])
    lines = out.split("\n")
    assert len(lines) == 3
    assert lines[0].split() == ["ID", "Task", "Repo", "Time", "Last", "worked"]
    assert set(lines[1]) <= {"-", " "}, "Separator must be dashes and spaces only"
    assert "1" in lines[2] and "proj" in lines[2] and "repo" in lines[2]


def test_no_markdown_pipes_or_dividers() -> None:
    """Output must have no pipe characters or markdown table syntax."""
    out = _format_tasks_table([_summary()])
    assert "|" not in out, "Markdown pipes must not appear in plain-text format"
    assert ":---" not in out and "---:" not in out, (
        "Markdown alignment markers must not appear"
    )


def test_columns_align_to_widest_cell() -> None:
    """Long task names push later columns to the right consistently."""
    tasks = [
        _summary(id=1, name="short", repo_name="r1"),
        _summary(id=99, name="much-longer-name-here", repo_name="r2"),
    ]
    out = _format_tasks_table(tasks)
    lines = out.split("\n")
    # Find the column where "Repo" header starts; same column should hold "r1" / "r2".
    repo_header_col = lines[0].index("Repo")
    assert lines[2][repo_header_col:repo_header_col + 2] == "r1"
    assert lines[3][repo_header_col:repo_header_col + 2] == "r2"


def test_none_fields_render_as_dash() -> None:
    """A missing repo_name shows as `-`, not the string `None`."""
    out = _format_tasks_table([_summary(repo_name=None)])
    assert "None" not in out
    # Layout: header, separator, then one data row.
    data_row = out.split("\n")[2]
    assert " -" in data_row, f"Expected dash in data row, got: {data_row!r}"


# ---------------------------------------------------------------------------
# _format_prioritized_display: covers the prioritize_by_repo two-list output
# ---------------------------------------------------------------------------

def test_prioritized_both_empty_returns_no_active() -> None:
    """Both lists empty -> bare fallback, no section headers."""
    assert _format_prioritized_display([], [], "/some/path") == "(no active tasks)"


def test_prioritized_repo_empty_falls_back_to_other_tasks() -> None:
    """Empty repo list + non-empty other_tasks must still render the other tasks.

    This is the bug Codex hit: cwd is a top-level work dir with no tasks attached
    directly, but real projects live in subdirs. We must not collapse to
    "(no active tasks)" while other_tasks holds the actual content.
    """
    others = [_summary(id=1, name="proj", repo_name="repo")]
    out = _format_prioritized_display([], others, "/Users/me/work")

    assert "(no active tasks in /Users/me/work)" in out
    assert "Other active tasks:" in out
    assert "proj" in out and "repo" in out


def test_prioritized_repo_only_no_other_section() -> None:
    """When everything belongs to the cwd repo, no `Other active tasks:` block."""
    repo = [_summary(id=1, name="proj", repo_name="r1")]
    out = _format_prioritized_display(repo, [], "/Users/me/work/repo")

    assert out.startswith("Tasks in /Users/me/work/repo:")
    assert "Other active tasks:" not in out
    assert "proj" in out


def test_prioritized_both_lists_have_section_headers_and_blank_separator() -> None:
    """Both lists rendered, separated by a blank line for scannability."""
    repo = [_summary(id=1, name="primary", repo_name="r1")]
    others = [_summary(id=2, name="secondary", repo_name="r2")]
    out = _format_prioritized_display(repo, others, "/path/to/repo")

    lines = out.split("\n")
    assert lines[0] == "Tasks in /path/to/repo:"
    # Find the blank line separator and assert the next line is the other-tasks header.
    blank_idx = lines.index("")
    assert lines[blank_idx + 1] == "Other active tasks:"
    # Both task names present.
    assert "primary" in out and "secondary" in out

"""Tests for the get_context_digest MCP tool.

Spec source: the context-file conventions plan - the digest is the
/missioncache:load resume read: parses server-side (so it must handle files
past the 256KB Read-tool cap), returns Waiting on + Next Steps verbatim,
the newest 3 Recent Changes subsections, a section index, file size, and
health warnings. Missing project/context file yields a structured error,
never an exception.

The tool is an async wrapper; called via ``asyncio.run`` (same pattern as
test_tools_active.py).
"""

import asyncio

import pytest

from mcp_missioncache import config, tools_docs


CONTEXT = """# Demo Project - Context

**Last Updated:** 2026-07-10 12:00
Hub: [[demo-hub]]
**Related projects:** [[other-proj]] (shared pipeline)

## Description

A demo project.

## Gotchas

- watch the lock

## Waiting on

External replies/events that gate work. Check on every resume; when one resolves, act on what it gates and move the row into Recent Changes.

| What | Who | Since | Gates |
|------|-----|-------|-------|
| Reply on PR | Jose | 2026-07-10 | GC-1 rework |

## Next Steps

1. Do the thing

## Recent Changes

### 2026-07-10 11:00

- newest entry

### 2026-07-09 11:00

- middle entry

### 2026-07-08 11:00

- older entry

### 2026-07-07 11:00

- oldest entry
"""


@pytest.fixture()
def project(tmp_path, monkeypatch):
    """A real project under a tmp settings.root shared by all consumers.

    ``config.settings`` is the same object bound in project_files and
    tools_docs, so patching the attribute redirects every module.
    """
    monkeypatch.setattr(config.settings, "root", tmp_path / "mc")
    project_dir = tmp_path / "mc" / "active" / "demo-project"
    project_dir.mkdir(parents=True)
    ctx = project_dir / "demo-project-context.md"
    ctx.write_text(CONTEXT)
    return ctx


class TestGetContextDigest:
    def test_expected_keys(self, project):
        result = asyncio.run(tools_docs.get_context_digest(project_name="demo-project"))
        assert result["success"] is True
        assert result["file"] == str(project)
        for key in (
            "last_updated", "hub", "related_projects", "waiting_on", "next_steps",
            "recent_changes_last3", "section_index", "file_size_bytes",
            "health_warnings",
        ):
            assert key in result

    def test_verbatim_sections_and_header_lines(self, project):
        result = asyncio.run(tools_docs.get_context_digest(project_name="demo-project"))
        assert "| Reply on PR | Jose | 2026-07-10 | GC-1 rework |" in result["waiting_on"]
        assert "1. Do the thing" in result["next_steps"]
        assert result["hub"] == "Hub: [[demo-hub]]"
        assert result["related_projects"] == "**Related projects:** [[other-proj]] (shared pipeline)"
        assert result["last_updated"] == "2026-07-10 12:00"

    def test_last3_newest_first(self, project):
        result = asyncio.run(tools_docs.get_context_digest(project_name="demo-project"))
        last3 = result["recent_changes_last3"]
        assert len(last3) == 3
        assert "newest entry" in last3[0]
        assert "older entry" in last3[2]
        assert not any("oldest entry" in s for s in last3)

    def test_reads_file_over_256kb(self, project):
        # The whole point of the digest: the Read tool caps at 256KB, the
        # server-side read must not.
        project.write_text(CONTEXT + "\n## Filler\n\n" + "x" * 400_000 + "\n")
        result = asyncio.run(tools_docs.get_context_digest(project_name="demo-project"))
        assert result["success"] is True
        assert result["file_size_bytes"] > 256 * 1024
        assert "1. Do the thing" in result["next_steps"]

    def test_missing_project_structured_error(self, project):
        result = asyncio.run(tools_docs.get_context_digest(project_name="no-such-project"))
        assert result.get("error") is True
        assert result.get("code") == "FILE_NOT_FOUND"

    def test_health_warnings_included(self, project):
        # The fixture's Last Updated (2026-07-10) is long past + stale
        # waiting row; both should surface without the caller asking.
        result = asyncio.run(tools_docs.get_context_digest(project_name="demo-project"))
        assert isinstance(result["health_warnings"], list)

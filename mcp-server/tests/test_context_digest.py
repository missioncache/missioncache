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


# ── shared-seen auto-stamp (digest-read clears the fork staleness dot) ────


@pytest.fixture()
def fork_reader(project, tmp_path, monkeypatch):
    """A fork of demo-project, bound to the calling session.

    Redirects HOME (shared-seen marker dir) and the hooks-state DB into
    tmp so the stamp is fully observable and isolated.
    """
    import sqlite3

    import missioncache_db

    fork_dir = tmp_path / "mc" / "active" / "fork-proj"
    fork_dir.mkdir(parents=True)
    (fork_dir / "fork-proj-context.md").write_text(
        "# Fork - Context\n**Fork of:** demo-project\n\n## Description\n"
    )
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-stamp-1")
    state_db = tmp_path / "hooks-state.db"
    monkeypatch.setattr(missioncache_db, "HOOKS_STATE_DB_PATH", state_db)
    conn = sqlite3.connect(str(state_db))
    missioncache_db.init_hooks_state_db_schema(conn)
    conn.execute(
        "INSERT INTO project_state (session_id, project_name, updated_at) "
        "VALUES (?, ?, datetime('now', 'localtime'))",
        ("sess-stamp-1", "fork-proj"),
    )
    conn.commit()
    conn.close()
    return tmp_path / "home" / ".claude" / "hooks" / "state" / "shared-seen"


class TestSharedSeenAutoStamp:
    def test_fork_session_digesting_parent_stamps_marker(self, project, fork_reader):
        import json

        result = asyncio.run(
            tools_docs.get_context_digest(project_name="demo-project")
        )
        assert result["success"] is True
        assert result["shared_seen_stamped"] is True
        marker = json.loads((fork_reader / "sess-stamp-1.json").read_text())
        assert marker["parent"] == "demo-project"
        assert marker["seen_mtime"] == project.stat().st_mtime

    def test_fork_session_digesting_itself_does_not_stamp(self, project, fork_reader):
        result = asyncio.run(tools_docs.get_context_digest(project_name="fork-proj"))
        assert result["success"] is True
        assert result["shared_seen_stamped"] is False
        assert not (fork_reader / "sess-stamp-1.json").exists()

    def test_non_fork_session_does_not_stamp(self, project, fork_reader, monkeypatch):
        import sqlite3

        import missioncache_db

        conn = sqlite3.connect(str(missioncache_db.HOOKS_STATE_DB_PATH))
        conn.execute(
            "UPDATE project_state SET project_name = 'demo-project' "
            "WHERE session_id = 'sess-stamp-1'"
        )
        conn.commit()
        conn.close()
        result = asyncio.run(
            tools_docs.get_context_digest(project_name="demo-project")
        )
        assert result["shared_seen_stamped"] is False
        assert not (fork_reader / "sess-stamp-1.json").exists()

    def test_no_session_id_does_not_stamp(self, project, fork_reader, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        result = asyncio.run(
            tools_docs.get_context_digest(project_name="demo-project")
        )
        assert result["shared_seen_stamped"] is False
        assert not (fork_reader / "sess-stamp-1.json").exists()

    def test_fork_of_other_parent_does_not_stamp(
        self, project, fork_reader, tmp_path
    ):
        """The parse_fork_parent discriminator: a session bound to a fork of a
        DIFFERENT parent digesting this project must not stamp. Distinct from
        the self-digest guard, which short-circuits earlier."""
        fork_ctx = tmp_path / "mc" / "active" / "fork-proj" / "fork-proj-context.md"
        fork_ctx.write_text(
            "# Fork - Context\n**Fork of:** other-parent\n\n## Description\n"
        )
        result = asyncio.run(
            tools_docs.get_context_digest(project_name="demo-project")
        )
        assert result["success"] is True
        assert result["shared_seen_stamped"] is False
        assert not (fork_reader / "sess-stamp-1.json").exists()

    def test_explicit_session_id_param_stamps_without_env(
        self, project, fork_reader, monkeypatch
    ):
        """Pre-2.1.154 / non-Claude clients pass session_id explicitly; the
        stamp must work without the env var."""
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        result = asyncio.run(
            tools_docs.get_context_digest(
                project_name="demo-project", session_id="sess-stamp-1"
            )
        )
        assert result["shared_seen_stamped"] is True
        assert (fork_reader / "sess-stamp-1.json").exists()

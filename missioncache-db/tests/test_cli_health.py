"""Tests for the ``missioncache-db health`` CLI subcommand.

Spec source: the context-file conventions plan - health scans all active
projects under MISSIONCACHE_ROOT/active/, reports per-project warnings from
``context_health.check_context_health``, treats a missing context file as a
finding, and is report-only (exit 0 always, warnings or not).

Runs ``main()`` in-process with ``sys.argv`` monkeypatched and
``missioncache_db.MISSIONCACHE_ROOT`` redirected to tmp (the health branch
reads the module global at call time for exactly this reason).
"""

import sys

import missioncache_db
from missioncache_db import context_health as ch


HEALTHY_CONTEXT = """# Demo - Context

**Last Updated:** {last_updated}

## Description

A demo project.

## Gotchas

- TBD

## Waiting on

{note}

| What | Who | Since | Gates |
|------|-----|-------|-------|
{rows}
## Next Steps

1. Do the thing

## Recent Changes

### {last_updated}

- created
"""


def _write_project(root, name, content):
    project_dir = root / "active" / name
    project_dir.mkdir(parents=True)
    (project_dir / f"{name}-context.md").write_text(content)
    return project_dir


def _run_health(monkeypatch, root):
    monkeypatch.setattr(missioncache_db, "MISSIONCACHE_ROOT", root)
    monkeypatch.setattr(missioncache_db, "DB_PATH", root / "tasks.db")
    monkeypatch.setattr(sys, "argv", ["missioncache-db", "health"])
    missioncache_db.main()


def _fresh(last_updated="2099-01-01 09:00", rows=""):
    return HEALTHY_CONTEXT.format(
        last_updated=last_updated, note=ch.WAITING_ON_NOTE, rows=rows
    )


class TestHealthCommand:
    def test_clean_project_reports_ok(self, monkeypatch, tmp_path, capsys):
        _write_project(tmp_path, "clean-proj", _fresh())
        _run_health(monkeypatch, tmp_path)
        out = capsys.readouterr().out
        assert "clean-proj: ok" in out
        assert "1 active projects checked, 0 warnings" in out

    def test_stale_project_reported(self, monkeypatch, tmp_path, capsys):
        _write_project(tmp_path, "stale-proj", _fresh(last_updated="2020-01-01 09:00"))
        _run_health(monkeypatch, tmp_path)
        out = capsys.readouterr().out
        assert "stale-proj:" in out
        assert "Last Updated" in out

    def test_missing_sections_flagged(self, monkeypatch, tmp_path, capsys):
        _write_project(
            tmp_path,
            "bare-proj",
            "# Bare - Context\n\n**Last Updated:** 2099-01-01\n\n## Description\n\nd\n",
        )
        _run_health(monkeypatch, tmp_path)
        out = capsys.readouterr().out
        assert "missing core section: ## Waiting on" in out
        assert "missing core section: ## Next Steps" in out

    def test_missing_context_file_flagged(self, monkeypatch, tmp_path, capsys):
        (tmp_path / "active" / "empty-proj").mkdir(parents=True)
        _run_health(monkeypatch, tmp_path)
        out = capsys.readouterr().out
        assert "empty-proj:" in out
        assert "no context file found" in out

    def test_exit_zero_with_warnings(self, monkeypatch, tmp_path):
        _write_project(tmp_path, "stale-proj", _fresh(last_updated="2020-01-01 09:00"))
        # Report-only contract: main() returns without raising SystemExit.
        _run_health(monkeypatch, tmp_path)

    def test_empty_active_dir_clean_run(self, monkeypatch, tmp_path, capsys):
        _run_health(monkeypatch, tmp_path)
        out = capsys.readouterr().out
        assert "0 active projects checked, 0 warnings" in out

    def test_legacy_bare_context_filename_found(self, monkeypatch, tmp_path, capsys):
        project_dir = tmp_path / "active" / "legacy-proj"
        project_dir.mkdir(parents=True)
        (project_dir / "context.md").write_text(_fresh())
        _run_health(monkeypatch, tmp_path)
        out = capsys.readouterr().out
        assert "legacy-proj: ok" in out

    def test_stale_waiting_row_reported(self, monkeypatch, tmp_path, capsys):
        _write_project(
            tmp_path,
            "waiting-proj",
            _fresh(rows="| Old ask | Bob | 2020-01-01 | thing |\n"),
        )
        _run_health(monkeypatch, tmp_path)
        out = capsys.readouterr().out
        assert "Old ask" in out and "Bob" in out

    def test_unreadable_file_is_finding_not_crash(self, monkeypatch, tmp_path, capsys):
        """One undecodable file must not void the sweep for other projects
        (the documented report-only, exit-0 contract)."""
        bad_dir = tmp_path / "active" / "aaa-bad-proj"
        bad_dir.mkdir(parents=True)
        (bad_dir / "aaa-bad-proj-context.md").write_bytes(b"\xff\xfe\x00garbage\xff")
        _write_project(tmp_path, "zzz-good-proj", _fresh())
        _run_health(monkeypatch, tmp_path)
        out = capsys.readouterr().out
        assert "aaa-bad-proj:" in out
        assert "unreadable" in out
        # The project sorted AFTER the bad one still got checked.
        assert "zzz-good-proj: ok" in out
        assert "2 active projects checked" in out

    def test_exit_zero_output_asserted(self, monkeypatch, tmp_path, capsys):
        """Report-only contract with an explicit output assertion: warnings
        print AND main() returns without SystemExit."""
        _write_project(tmp_path, "stale-proj", _fresh(last_updated="2020-01-01 09:00"))
        _run_health(monkeypatch, tmp_path)
        out = capsys.readouterr().out
        assert "Last Updated" in out
        assert "1 active projects checked" in out

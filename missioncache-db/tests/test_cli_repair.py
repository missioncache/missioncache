"""Tests for the ``missioncache-db repair`` CLI subcommand.

Spec source: docs/cli.md "Repairing a damaged context file". The contract:

* Dry run by DEFAULT. Nothing is written without ``--apply``, because this
  rewrites files the user cannot easily reconstruct.
* It merges duplicate sections, moves stranded Recent Changes entries back
  into the section, and lets the cap roll the overflow into the journal.
* It REPORTS an unbalanced fence and repairs nothing at all in that file:
  with the fence state unknown, which lines are code is a guess, and
  merging on a guess can hoist text out of a code sample.
* It reports stranded entries it could not move, rather than letting
  "reabsorbed 0" read as "the file was clean".
* Report-only exit status, like ``health``: one unreadable project is a
  finding for that project, never an abort of the sweep.

Runs ``main()`` in-process with ``sys.argv`` monkeypatched and
``missioncache_db.MISSIONCACHE_ROOT`` redirected to tmp, mirroring
``test_cli_health.py`` - the repair branch reads the module global at call
time for exactly this reason.
"""

import sys

import missioncache_db
from missioncache_db import context_health as ch


def _entries(days, month="08"):
    """Dated writer-shaped subsections, one per day given."""
    return "".join(
        f"### 2026-{month}-{d:02d} 10:00\n\n- entry {d}\n\n" for d in days
    )


def _write(root, name, content, base="active", journal=None):
    project_dir = root / base / name
    project_dir.mkdir(parents=True)
    (project_dir / f"{name}-context.md").write_text(content)
    if journal is not None:
        (project_dir / f"{name}-journal.md").write_text(journal)
    return project_dir


def _run(monkeypatch, root, *args):
    monkeypatch.setattr(missioncache_db, "MISSIONCACHE_ROOT", root)
    monkeypatch.setattr(missioncache_db, "DB_PATH", root / "tasks.db")
    monkeypatch.setattr(sys, "argv", ["missioncache-db", "repair", *args])
    missioncache_db.main()


# A stray `## ` heading inside Recent Changes strands everything below it.
STRANDED = (
    "# P - Context\n**Last Updated:** 2099-01-01 09:00\n\n"
    "## Recent Changes\n\n### 2026-09-01 10:00\n\n- newest\n\n"
    "## Updated: p\n\na pasted save report\n\n" + _entries(range(1, 16))
)

DUPLICATES = (
    "# P - Context\n**Last Updated:** 2099-01-01 09:00\n\n"
    "## Recent Changes\n\n### 2026-09-01 10:00\n\n- x\n\n"
    "## Key Files\n\n| a | b |\n\n## Key Files\n\n| c | d |\n"
)


class TestDryRunIsTheDefault:
    def test_a_bare_run_writes_nothing(self, monkeypatch, tmp_path, capsys):
        d = _write(tmp_path, "p", STRANDED)
        before = (d / "p-context.md").read_text()
        _run(monkeypatch, tmp_path)
        out = capsys.readouterr().out
        assert "DRY RUN: p:" in out
        assert "re-run with --apply to write" in out
        assert (d / "p-context.md").read_text() == before
        assert not (d / "p-journal.md").exists()
        assert not list(d.glob("*.tmp"))

    def test_apply_writes(self, monkeypatch, tmp_path, capsys):
        d = _write(tmp_path, "p", STRANDED)
        before = (d / "p-context.md").read_text()
        _run(monkeypatch, tmp_path, "--apply")
        out = capsys.readouterr().out
        assert "DRY RUN" not in out
        assert (d / "p-context.md").read_text() != before


class TestTheJournalBranch:
    """The write path that had never executed under any test.

    ``repair_content`` only returns journal text when the cap trips, so a
    fixture has to push Recent Changes past twelve entries to reach it.
    """

    def test_the_journal_is_created_with_its_header(self, monkeypatch, tmp_path):
        d = _write(tmp_path, "p", STRANDED)
        _run(monkeypatch, tmp_path, "--apply")
        journal = d / "p-journal.md"
        assert journal.exists()
        assert journal.read_text().startswith(ch.journal_header("p"))
        assert len(ch.parse_recent_changes_subsections((d / "p-context.md").read_text())) == 12

    def test_an_existing_journal_keeps_its_entries(self, monkeypatch, tmp_path):
        existing = ch.journal_header("p") + "\n### 2025-01-01 10:00\n\n- ancient\n"
        d = _write(tmp_path, "p", STRANDED, journal=existing)
        _run(monkeypatch, tmp_path, "--apply")
        text = (d / "p-journal.md").read_text()
        # The pre-existing entry survives ABOVE the newly rolled ones. This
        # is the arm that would silently truncate history if the
        # rstrip/join regressed.
        assert "- ancient" in text
        assert text.index("- ancient") < text.index("- entry 1")
        assert text.count(ch.journal_header("p")) == 1

    def test_the_oldest_entries_are_the_ones_rolled(self, monkeypatch, tmp_path):
        d = _write(tmp_path, "p", STRANDED)
        _run(monkeypatch, tmp_path, "--apply")
        kept = [
            h for h, _ in ch.parse_recent_changes_subsections(
                (d / "p-context.md").read_text()
            )
        ]
        journal = (d / "p-journal.md").read_text()
        assert kept[0] == "### 2026-09-01 10:00"
        assert "### 2026-08-01 10:00" in journal
        assert "### 2026-08-15 10:00" not in journal


class TestWhatItRepairs:
    def test_duplicate_sections_merge(self, monkeypatch, tmp_path, capsys):
        d = _write(tmp_path, "p", DUPLICATES)
        _run(monkeypatch, tmp_path, "--apply")
        assert "merged duplicate sections: Key Files (2 -> 1)" in capsys.readouterr().out
        content = (d / "p-context.md").read_text()
        assert content.count("## Key Files") == 1
        assert "| a | b |" in content and "| c | d |" in content

    def test_stranded_entries_come_back(self, monkeypatch, tmp_path, capsys):
        d = _write(tmp_path, "p", STRANDED)
        _run(monkeypatch, tmp_path, "--apply")
        assert "stranded Recent Changes entries moved back" in capsys.readouterr().out
        assert ch.orphaned_recent_changes((d / "p-context.md").read_text()) == []

    def test_the_stray_section_is_left_for_the_user(self, monkeypatch, tmp_path):
        # Deciding a section is junk is the user's call; repair recovers the
        # entries and leaves the prose for `sections_remove`.
        d = _write(tmp_path, "p", STRANDED)
        _run(monkeypatch, tmp_path, "--apply")
        content = (d / "p-context.md").read_text()
        assert "## Updated: p" in content
        assert "a pasted save report" in content

    def test_a_healthy_project_reports_nothing(self, monkeypatch, tmp_path, capsys):
        healthy = (
            "# P\n**Last Updated:** 2099-01-01 09:00\n\n"
            "## Recent Changes\n\n### 2026-09-01 10:00\n\n- only entry\n"
        )
        d = _write(tmp_path, "p", healthy)
        _run(monkeypatch, tmp_path, "--apply")
        out = capsys.readouterr().out
        assert "0 with findings" in out
        assert (d / "p-context.md").read_text() == healthy


class TestWhatItRefusesToRepair:
    FENCE_TRAP = (
        "# P\n**Last Updated:** 2099-01-01 09:00\n\n"
        "## Gotchas\n\n- a real gotcha\n\n"
        "## Recent Changes\n\n### 2026-09-01 10:00\n\n- x\n\n"
        "## Key Files\n\n| a | b |\n\nexample:\n\n"
        "```markdown\n## Gotchas\n\n- text from inside the code sample\n"
    )

    def test_an_unbalanced_fence_stops_the_whole_repair(
        self, monkeypatch, tmp_path, capsys
    ):
        d = _write(tmp_path, "p", self.FENCE_TRAP)
        _run(monkeypatch, tmp_path, "--apply")
        out = capsys.readouterr().out
        assert "unbalanced code fence at line" in out
        assert "NOTHING was repaired" in out
        # Byte-identical: reporting the fence and then restructuring on the
        # guess anyway is the worst of both, and it hoisted a line out of a
        # code sample into the live Gotchas section.
        assert (d / "p-context.md").read_text() == self.FENCE_TRAP

    def test_a_fence_only_finding_never_writes(self, monkeypatch, tmp_path):
        only_fence = (
            "# P\n**Last Updated:** 2099-01-01 09:00\n\n"
            "## Recent Changes\n\n### 2026-09-01 10:00\n\n- x\n\n```\ncut...\n"
        )
        d = _write(tmp_path, "p", only_fence)
        _run(monkeypatch, tmp_path, "--apply")
        assert (d / "p-context.md").read_text() == only_fence

    def test_entries_it_cannot_move_are_reported_not_hidden(
        self, monkeypatch, tmp_path, capsys
    ):
        # No Recent Changes section at all: there is nowhere to move them,
        # and "reabsorbed 0" must not read as "the file was clean".
        nowhere = (
            "# P\n**Last Updated:** 2099-01-01 09:00\n\n"
            "## Notes\n\n### 2026-08-01 10:00\n\n- stranded\n"
        )
        _write(tmp_path, "p", nowhere)
        _run(monkeypatch, tmp_path)
        out = capsys.readouterr().out
        assert "could NOT be moved back" in out
        assert "no '## Recent Changes' section" in out

    def test_a_hand_written_heading_says_so(self, monkeypatch, tmp_path, capsys):
        hand = (
            "# P\n**Last Updated:** 2099-01-01 09:00\n\n"
            "## Recent Changes\n\n### 2026-09-01 10:00\n\n- x\n\n"
            "## Some Event\n\n### 2026-08-14 sync notes\n\nprose\n"
        )
        _write(tmp_path, "p", hand)
        _run(monkeypatch, tmp_path)
        assert "hand-written headings" in capsys.readouterr().out


class TestTargetSelection:
    def test_a_bare_run_covers_active_only(self, monkeypatch, tmp_path, capsys):
        _write(tmp_path, "act", STRANDED)
        _write(tmp_path, "done", STRANDED, base="completed")
        _run(monkeypatch, tmp_path)
        out = capsys.readouterr().out
        assert "act:" in out
        assert "done:" not in out
        assert "1 projects checked" in out

    def test_all_includes_completed(self, monkeypatch, tmp_path, capsys):
        _write(tmp_path, "act", STRANDED)
        _write(tmp_path, "done", STRANDED, base="completed")
        _run(monkeypatch, tmp_path, "--all")
        out = capsys.readouterr().out
        assert "act:" in out and "done:" in out
        assert "2 projects checked" in out

    def test_a_named_target_reaches_a_completed_project(
        self, monkeypatch, tmp_path, capsys
    ):
        _write(tmp_path, "act", STRANDED)
        d = _write(tmp_path, "done", STRANDED, base="completed")
        _run(monkeypatch, tmp_path, "done", "--apply")
        out = capsys.readouterr().out
        assert "act:" not in out
        assert ch.orphaned_recent_changes((d / "done-context.md").read_text()) == []

    def test_a_missing_target_is_reported(self, monkeypatch, tmp_path, capsys):
        _write(tmp_path, "act", STRANDED)
        _run(monkeypatch, tmp_path, "nosuchproject")
        out = capsys.readouterr().out
        assert "nosuchproject: not found" in out
        assert "0 projects checked" in out


class TestResilience:
    def test_a_legacy_unprefixed_context_is_repaired(self, monkeypatch, tmp_path):
        d = tmp_path / "active" / "sub"
        d.mkdir(parents=True)
        (d / "context.md").write_text(STRANDED)
        _run(monkeypatch, tmp_path, "--apply")
        assert ch.orphaned_recent_changes((d / "context.md").read_text()) == []
        assert (d / "journal.md").exists()

    def test_an_unreadable_file_does_not_abort_the_sweep(
        self, monkeypatch, tmp_path, capsys
    ):
        bad = tmp_path / "active" / "binary"
        bad.mkdir(parents=True)
        (bad / "binary-context.md").write_bytes(b"\xff\xfe not utf-8 \xff")
        good = _write(tmp_path, "zz-good", STRANDED)
        _run(monkeypatch, tmp_path, "--apply")
        out = capsys.readouterr().out
        assert "binary: unreadable (UnicodeDecodeError)" in out
        # The project AFTER the unreadable one is still repaired.
        assert ch.orphaned_recent_changes((good / "zz-good-context.md").read_text()) == []

    def test_a_project_without_a_context_file_is_skipped(self, monkeypatch, tmp_path):
        (tmp_path / "active" / "empty").mkdir(parents=True)
        _run(monkeypatch, tmp_path)  # must not raise

    def test_no_active_dir_is_a_clean_run(self, monkeypatch, tmp_path, capsys):
        _run(monkeypatch, tmp_path)
        assert "0 projects checked" in capsys.readouterr().out

    def test_it_is_idempotent(self, monkeypatch, tmp_path):
        d = _write(tmp_path, "p", STRANDED)
        _run(monkeypatch, tmp_path, "--apply")
        once = (d / "p-context.md").read_text()
        once_journal = (d / "p-journal.md").read_text()
        _run(monkeypatch, tmp_path, "--apply")
        assert (d / "p-context.md").read_text() == once
        assert (d / "p-journal.md").read_text() == once_journal

    def test_the_write_is_atomic(self, monkeypatch, tmp_path):
        d = _write(tmp_path, "p", STRANDED)
        _run(monkeypatch, tmp_path, "--apply")
        # tmp files replaced, lock sidecar kept (never deleted, per the
        # filelock contract).
        assert not list(d.glob("*.tmp"))
        assert (d / "p-context.md.lock").exists()

    def test_report_only_exit_status(self, monkeypatch, tmp_path):
        _write(tmp_path, "p", STRANDED)
        # Same contract as `health`: findings are not a failure exit.
        _run(monkeypatch, tmp_path)

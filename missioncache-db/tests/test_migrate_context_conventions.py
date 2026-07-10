"""Tests for scripts/migrate_context_conventions.py.

Spec source: the context-file conventions migration plan - additive-only
(Waiting on inserted before Next Steps), misplaced-entry repair (the
unanchored-regex victims), legacy h2 consolidation, cap rollover into an
oldest-first journal, idempotent, dry-run writes nothing, and the hardcoded
skip project is fully untouched.

The script lives in scripts/ (not on pytest testpaths) so it is loaded via
importlib.util.spec_from_file_location, same pattern as the dashboard's
migrate-script tests.
"""

import importlib.util
from pathlib import Path

import pytest

from missioncache_db import context_health as ch


def _load_script():
    script = (
        Path(__file__).resolve().parents[2] / "scripts" / "migrate_context_conventions.py"
    )
    spec = importlib.util.spec_from_file_location("migrate_context_conventions", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def script(monkeypatch, tmp_path):
    mod = _load_script()
    monkeypatch.setattr(mod, "MISSIONCACHE_ROOT", tmp_path)
    return mod


def _entries(n, month=6):
    blocks = []
    for i in range(n, 0, -1):
        blocks.append(f"### 2026-{month:02d}-{i:02d} 10:00\n\n- change {i}\n")
    return "\n".join(blocks)


def _basic_context(recent=None):
    recent = recent if recent is not None else "### 2026-07-01 10:00\n\n- created\n"
    return f"""# Demo - Context

**Last Updated:** 2026-07-10 12:00

## Description

A demo project.

## Gotchas

- TBD

## Next Steps

1. Do the thing

## Recent Changes

{recent}
## Key Files

| File | Purpose |
|------|---------|
"""


def _write_project(root, name, content):
    d = root / "active" / name
    d.mkdir(parents=True)
    ctx = d / f"{name}-context.md"
    ctx.write_text(content)
    return ctx


class TestWaitingOnInsertion:
    def test_inserts_before_next_steps(self, script, tmp_path):
        ctx = _write_project(tmp_path, "proj", _basic_context())
        script.main([])
        content = ctx.read_text()
        assert "## Waiting on" in content
        assert content.index("## Waiting on") < content.index("## Next Steps")
        assert content.index("## Gotchas") < content.index("## Waiting on")
        assert ch.WAITING_ON_NOTE in content

    def test_skips_when_present(self, script, tmp_path):
        base = _basic_context()
        with_waiting = ch.insert_waiting_on_before_next_steps(
            base, ch.build_waiting_on_section([])
        )
        ctx = _write_project(tmp_path, "proj", with_waiting)
        script.main([])
        assert ctx.read_text().count("## Waiting on") == 1


class TestCapRollover:
    def test_rolls_overflow_to_journal(self, script, tmp_path):
        ctx = _write_project(tmp_path, "proj", _basic_context(recent=_entries(15)))
        script.main([])
        journal = ctx.parent / "proj-journal.md"
        assert journal.exists()
        jcontent = journal.read_text()
        assert jcontent.startswith("# proj - Journal")
        content = ctx.read_text()
        kept = ch.parse_recent_changes_subsections(content)
        assert len(kept) == ch.RECENT_CHANGES_CAP
        assert "- change 3" in jcontent and "- change 3" not in content

    def test_journal_oldest_first(self, script, tmp_path):
        _write_project(tmp_path, "proj", _basic_context(recent=_entries(15)))
        script.main([])
        jcontent = (tmp_path / "active" / "proj" / "proj-journal.md").read_text()
        assert jcontent.index("- change 1") < jcontent.index("- change 2") < jcontent.index("- change 3")

    def test_pointer_line_added(self, script, tmp_path):
        ctx = _write_project(tmp_path, "proj", _basic_context(recent=_entries(15)))
        script.main([])
        pointer = ch.RECENT_CHANGES_POINTER.format(journal_name="proj-journal.md")
        body = ch.extract_section(ctx.read_text(), "Recent Changes")
        assert pointer in body

    def test_under_cap_no_journal(self, script, tmp_path):
        ctx = _write_project(tmp_path, "proj", _basic_context(recent=_entries(3)))
        script.main([])
        assert not (ctx.parent / "proj-journal.md").exists()


class TestMisplacedEntryRepair:
    """The missioncache-release shape: a prose bullet contains the literal
    `## Recent Changes`, and the pre-fix writer injected dated blocks
    right after it, inside another section."""

    AFFECTED = """# Demo - Context

**Last Updated:** 2026-07-10 12:00

## Description

A demo project.

## Key Architectural Decisions

- Some decision about locking.
- Section shape: single `## Recent Changes` heading with dated subsections.

### 2026-07-10 22:00

- newest misplaced entry

### 2026-07-09 22:00

- older misplaced entry

## Gotchas

- TBD

## Next Steps

1. Do the thing

## Recent Changes (2026-04-30 10:20)

Diagnosed three intertwined bugs; plan approved.

## Recent Changes (2026-04-23 11:33)

Older prose entry about renames.

### 2026-04-24 09:00

- legacy dated entry
"""

    def test_misplaced_run_moved_into_rc_flow(self, script, tmp_path):
        ctx = _write_project(tmp_path, "proj", self.AFFECTED)
        script.main([])
        content = ctx.read_text()
        # Decisions section no longer carries the dated blocks.
        decisions = ch.extract_section(content, "Key Architectural Decisions")
        assert "misplaced entry" not in decisions
        assert "- Some decision about locking." in decisions
        assert "- Section shape:" in decisions  # prose bullet kept verbatim
        # Entries now live under the single canonical RC heading.
        subs = ch.parse_recent_changes_subsections(content)
        headings = [h for h, _ in subs]
        assert "### 2026-07-10 22:00" in headings

    def test_legacy_h2s_consolidated_to_single_heading(self, script, tmp_path):
        ctx = _write_project(tmp_path, "proj", self.AFFECTED)
        script.main([])
        content = ctx.read_text()
        import re

        rc_headings = re.findall(r"^## Recent Changes[^\n]*$", content, re.M)
        assert rc_headings == ["## Recent Changes"]
        # Legacy prose became dated entries (in file or journal).
        journal = (ctx.parent / "proj-journal.md")
        everything = content + (journal.read_text() if journal.exists() else "")
        assert "Diagnosed three intertwined bugs" in everything
        assert "Older prose entry about renames." in everything
        assert "- legacy dated entry" in everything

    def test_newest_misplaced_entries_stay_in_file(self, script, tmp_path):
        ctx = _write_project(tmp_path, "proj", self.AFFECTED)
        script.main([])
        content = ctx.read_text()
        # 5 total entries (2 misplaced + 04-30 prose + legacy dated + 04-23
        # prose) - under the cap, so all stay; newest first.
        subs = ch.parse_recent_changes_subsections(content)
        assert len(subs) == 5
        assert subs[0][0] == "### 2026-07-10 22:00"

    def test_key_prose_preserved_verbatim(self, script, tmp_path):
        """Untouched-section prose survives the repair verbatim. (Substring
        checks, not full byte-identity - the RC/Waiting-on regions between
        these spans legitimately change.)"""
        ctx = _write_project(tmp_path, "proj", self.AFFECTED)
        script.main([])
        content = ctx.read_text()
        for untouched in (
            "## Description\n\nA demo project.\n",
            "- Some decision about locking.",
            "1. Do the thing",
        ):
            assert untouched in content


class TestIdempotencyAndSafety:
    def test_second_run_noop(self, script, tmp_path, capsys):
        ctx = _write_project(tmp_path, "proj", _basic_context(recent=_entries(15)))
        script.main([])
        after_first = ctx.read_text()
        journal_after_first = (ctx.parent / "proj-journal.md").read_text()
        script.main([])
        assert ctx.read_text() == after_first
        assert (ctx.parent / "proj-journal.md").read_text() == journal_after_first
        assert "already migrated, no changes" in capsys.readouterr().out

    def test_dry_run_writes_nothing(self, script, tmp_path, capsys):
        ctx = _write_project(tmp_path, "proj", _basic_context(recent=_entries(15)))
        before = ctx.read_text()
        script.main(["--dry-run"])
        assert ctx.read_text() == before
        assert not (ctx.parent / "proj-journal.md").exists()
        assert "DRY RUN" in capsys.readouterr().out

    def test_skip_project_fully_untouched(self, script, tmp_path, capsys, monkeypatch):
        name = "live-session-proj"
        monkeypatch.setattr(script, "SKIP_PROJECTS", {name})
        ctx = _write_project(tmp_path, name, _basic_context(recent=_entries(20)))
        before = ctx.read_text()
        script.main([])
        assert ctx.read_text() == before
        assert not (ctx.parent / f"{name}-journal.md").exists()
        assert "SKIPPED" in capsys.readouterr().out

    def test_no_tmp_leftovers(self, script, tmp_path):
        ctx = _write_project(tmp_path, "proj", _basic_context(recent=_entries(15)))
        script.main([])
        leftovers = list(ctx.parent.glob("*.tmp"))
        assert leftovers == []

    def test_missing_context_file_reported(self, script, tmp_path, capsys):
        (tmp_path / "active" / "empty-proj").mkdir(parents=True)
        script.main([])
        assert "no context file" in capsys.readouterr().out


class TestMergedEntryDateSort:
    """Merged entries (misplaced + legacy) are only approximately ordered in
    the document - the migration must date-sort before the cap decides what
    stays (harsh-critic #4)."""

    OUT_OF_ORDER = """# Demo - Context

**Last Updated:** 2026-07-10 12:00

## Description

d

## Next Steps

1. x

## Recent Changes (2026-04-30 10:20)

april prose entry

### 2026-06-01 10:00

- june entry

### 2026-07-12 10:00

- NEWEST entry, buried last in doc order
"""

    def test_newest_by_date_kept_when_over_cap(self, script, tmp_path, monkeypatch):
        monkeypatch.setattr(ch, "RECENT_CHANGES_CAP", 2)
        ctx = _write_project(tmp_path, "proj", self.OUT_OF_ORDER)
        script.main([])
        content = ctx.read_text()
        subs = ch.parse_recent_changes_subsections(content)
        headings = [h for h, _ in subs]
        # Newest two by DATE stay (2026-07-12, 2026-06-01); the April prose
        # (synthetic entry from the legacy heading) rolls to the journal.
        assert headings == ["### 2026-07-12 10:00", "### 2026-06-01 10:00"]
        journal = (ctx.parent / "proj-journal.md").read_text()
        assert "april prose entry" in journal

    def test_sorted_newest_first_in_file(self, script, tmp_path):
        ctx = _write_project(tmp_path, "proj", self.OUT_OF_ORDER)
        script.main([])
        subs = ch.parse_recent_changes_subsections(ctx.read_text())
        assert subs[0][0] == "### 2026-07-12 10:00"


class TestMigrationFenceAwareness:
    FENCED = """# Demo - Context

**Last Updated:** 2026-07-10 12:00

## Description

Shape example:

```markdown
## Recent Changes

### 2020-01-01 00:00

- fenced fake
```

## Next Steps

1. x

## Recent Changes

### 2026-07-10 10:00

- real entry
"""

    def test_fenced_fake_heading_not_treated_as_section(self, script, tmp_path):
        ctx = _write_project(tmp_path, "proj", self.FENCED)
        script.main([])
        content = ctx.read_text()
        # Fence intact, single real heading, real entry still there.
        assert "- fenced fake" in content
        assert content.count("```") == 2
        subs = ch.parse_recent_changes_subsections(content)
        assert [h for h, _ in subs] == ["### 2026-07-10 10:00"]

"""Integration tests for MissionCache file create/update operations.

Tests use tmp_path for all file I/O and monkeypatch to redirect root_dir.
"""

import re

import pytest

from mcp_missioncache.config import Settings
from mcp_missioncache.errors import ErrorCode, MissionCacheError, MissionCacheFileNotFoundError
from mcp_missioncache.project_files import (
    create_missioncache_files,
    get_missioncache_files,
    parse_task_progress,
    update_context_file,
    update_tasks_file,
)


@pytest.fixture(autouse=True)
def _redirect_root_dir(tmp_path, monkeypatch):
    """Point root_dir to tmp_path so file operations don't touch real filesystem."""
    test_settings = Settings(root=tmp_path / "orbit")
    monkeypatch.setattr("mcp_missioncache.project_files.settings", test_settings)


# ── create_missioncache_files ────────────────────────────────────────────────────


class TestCreateMissionCacheFiles:
    def test_creates_three_files(self, tmp_path):
        """create_missioncache_files produces plan, context, and tasks files."""
        result = create_missioncache_files(
            task_name="test-task",
            description="A test project",
            tasks=["Set up repo", "Write code"],
        )

        assert result.plan_file is not None
        assert result.context_file is not None
        assert result.tasks_file is not None

        # Files should actually exist on disk
        from pathlib import Path

        assert Path(result.plan_file).exists()
        assert Path(result.context_file).exists()
        assert Path(result.tasks_file).exists()

    def test_template_placeholders_filled(self, tmp_path):
        """No raw {{placeholder}} tokens should remain in generated files."""
        result = create_missioncache_files(
            task_name="test-task",
            description="Filled description",
            jira_key="PROJ-1234",
            branch="feature/test-task",
            tasks=["First task"],
        )

        from pathlib import Path

        for fpath in (result.plan_file, result.context_file, result.tasks_file):
            content = Path(fpath).read_text()
            leftover = re.findall(r"\{\{[a-z_]+\}\}", content)
            assert leftover == [], f"Unfilled placeholders in {fpath}: {leftover}"

    def test_duplicate_raises_already_exists(self, tmp_path):
        """Re-creating a project with the same name raises ALREADY_EXISTS."""
        create_missioncache_files(task_name="dup-task", tasks=["one"])

        with pytest.raises(MissionCacheError) as excinfo:
            create_missioncache_files(task_name="dup-task", tasks=["two"])

        assert excinfo.value.code == ErrorCode.ALREADY_EXISTS
        assert "dup-task" in excinfo.value.message
        assert "existing_files" in excinfo.value.details

    def test_duplicate_preserves_original_files(self, tmp_path):
        """The ALREADY_EXISTS guard runs BEFORE any write, so files are intact."""
        first = create_missioncache_files(task_name="preserve-task", tasks=["original"])

        from pathlib import Path

        original_tasks = Path(first.tasks_file).read_text()

        with pytest.raises(MissionCacheError):
            create_missioncache_files(task_name="preserve-task", tasks=["clobber"])

        assert Path(first.tasks_file).read_text() == original_tasks
        assert "original" in original_tasks
        assert "clobber" not in original_tasks

    def test_force_overwrites_existing(self, tmp_path):
        """force=True bypasses the ALREADY_EXISTS guard and rewrites files."""
        from pathlib import Path

        first = create_missioncache_files(task_name="force-task", tasks=["v1"])
        original = Path(first.tasks_file).read_text()
        assert "v1" in original

        second = create_missioncache_files(
            task_name="force-task", tasks=["v2"], force=True
        )
        rewritten = Path(second.tasks_file).read_text()
        assert "v2" in rewritten
        assert "v1" not in rewritten

    def test_guard_catches_legacy_unprefixed_filenames(self, tmp_path):
        """ALREADY_EXISTS fires when the dir has only legacy unprefixed files.

        get_missioncache_files reads both prefixed and legacy names; the guard
        must check both, otherwise fresh prefixed files would shadow
        existing legacy content at read time.
        """
        from mcp_missioncache.project_files import get_task_dir

        task_dir = get_task_dir("legacy-task")
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "plan.md").write_text("# legacy plan content")
        (task_dir / "context.md").write_text("# legacy context content")
        (task_dir / "tasks.md").write_text("- [ ] legacy task")

        with pytest.raises(MissionCacheError) as excinfo:
            create_missioncache_files(task_name="legacy-task", tasks=["new"])

        assert excinfo.value.code == ErrorCode.ALREADY_EXISTS


# ── get_missioncache_files ──────────────────────────────────────────────────────


class TestGetMissionCacheFiles:
    def test_finds_files_in_active_dir(self, tmp_path):
        create_missioncache_files(task_name="active-task", tasks=["x"])

        result = get_missioncache_files("active-task")

        assert result.plan_file is not None
        assert result.context_file is not None
        assert result.tasks_file is not None
        assert "active/active-task" in result.task_dir

    def test_finds_files_in_completed_dir(self, tmp_path):
        """When a project is archived to completed/, get_missioncache_files finds it.

        Reproduces MAJOR-10 from the QA report - a fresh /missioncache:load on a
        completed project used to report has_missioncache_files=False because the
        lookup only scanned active/.
        """
        from mcp_missioncache.project_files import settings

        create_missioncache_files(task_name="archived-task", tasks=["done"])

        active_dir = settings.root / "active" / "archived-task"
        completed_dir = settings.root / "completed" / "archived-task"
        completed_dir.parent.mkdir(parents=True, exist_ok=True)
        active_dir.rename(completed_dir)
        assert not active_dir.exists()
        assert completed_dir.exists()

        result = get_missioncache_files("archived-task")

        assert result.plan_file is not None
        assert result.context_file is not None
        assert result.tasks_file is not None
        assert "completed/archived-task" in result.task_dir

    def test_returns_empty_paths_when_nothing_exists(self, tmp_path):
        result = get_missioncache_files("nonexistent-task")

        assert result.plan_file is None
        assert result.context_file is None
        assert result.tasks_file is None

    def test_active_takes_priority_over_completed(self, tmp_path):
        """If a project exists in both active/ AND completed/ (e.g., reopened
        without deleting the archived copy), the active version wins."""
        from mcp_missioncache.project_files import settings

        create_missioncache_files(task_name="dual-task", tasks=["active-version"])

        completed_dir = settings.root / "completed" / "dual-task"
        completed_dir.mkdir(parents=True, exist_ok=True)
        (completed_dir / "dual-task-tasks.md").write_text("completed-version")

        result = get_missioncache_files("dual-task")

        assert result.tasks_file is not None
        assert "active/dual-task" in result.task_dir
        from pathlib import Path

        assert "active-version" in Path(result.tasks_file).read_text()


# ── update_context_file ──────────────────────────────────────────────────


class TestUpdateContextFile:
    def test_updates_timestamp(self, tmp_path, sample_context_md):
        """update_context_file refreshes the Last Updated timestamp."""
        ctx_file = tmp_path / "context.md"
        ctx_file.write_text(sample_context_md)

        updated = update_context_file(str(ctx_file))["content"]
        assert "**Last Updated:**" in updated
        # Should NOT contain the old timestamp
        assert "2026-04-01 10:00" not in updated

    def test_appends_recent_changes(self, tmp_path, sample_context_md):
        """update_context_file with recent_changes adds entries to Recent Changes."""
        ctx_file = tmp_path / "context.md"
        ctx_file.write_text(sample_context_md)

        updated = update_context_file(
            str(ctx_file),
            recent_changes=["Added new module", "Fixed tests"],
        )["content"]

        assert "Added new module" in updated
        assert "Fixed tests" in updated


# ── Recent Changes consolidation (regression guards for commit 4776f3f) ──


class TestRecentChangesConsolidation:
    """Verify update_context_file consolidates Recent Changes into a single h2.

    Pre-2026-04-23 versions added a new top-level `## Recent Changes (timestamp)`
    h2 on every save, fragmenting the file. The fix at commit 4776f3f inserts
    new entries as `### timestamp` h3 subsections under the FIRST existing
    `## Recent Changes` h2 (with or without timestamp suffix). This class
    locks in that contract so the tool can't silently regress.
    """

    def _h2_count(self, content: str) -> int:
        """Count standalone h2 lines for `## Recent Changes` (any suffix)."""
        return len(
            re.findall(r"^## Recent Changes(\s.*)?$", content, re.MULTILINE)
        )

    def _h3_count(self, content: str) -> int:
        """Count `### YYYY-MM-DD ...` h3 lines (the per-save subsections)."""
        return len(re.findall(r"^### \d{4}-\d{2}-\d{2}", content, re.MULTILINE))

    def test_appends_under_existing_clean_h2(self, tmp_path):
        """File with `## Recent Changes` (no timestamp) gets a new h3 child."""
        ctx = tmp_path / "context.md"
        ctx.write_text(
            "# Title\n\n**Last Updated:** 2026-04-01\n\n"
            "## Recent Changes\n\n"
            "### 2026-04-26 12:00\n\n- old entry\n"
        )
        update_context_file(str(ctx), recent_changes=["new entry"])
        content = ctx.read_text()
        assert self._h2_count(content) == 1
        assert self._h3_count(content) == 2  # original + new
        assert "old entry" in content
        assert "new entry" in content

    def test_appends_under_first_legacy_h2(self, tmp_path):
        """File with `## Recent Changes (timestamp)` legacy form: new entry as h3 under it.

        The tool does NOT migrate the legacy h2 (that's the migration script's job)
        but MUST insert the new entry under it as a child h3, not as a sibling h2.
        """
        ctx = tmp_path / "context.md"
        ctx.write_text(
            "# Title\n\n**Last Updated:** 2026-04-01\n\n"
            "## Recent Changes (2026-04-23 11:33)\n\nlegacy body content\n"
        )
        update_context_file(str(ctx), recent_changes=["new entry"])
        content = ctx.read_text()
        # Legacy h2 stays put.
        assert "## Recent Changes (2026-04-23 11:33)" in content
        # No second h2 was created.
        assert self._h2_count(content) == 1
        # New entry is present as a h3 AFTER the legacy h2.
        legacy_pos = content.find("## Recent Changes (2026-04-23 11:33)")
        new_pos = content.find("new entry")
        assert legacy_pos != -1
        assert new_pos != -1
        assert new_pos > legacy_pos

    def test_creates_section_when_missing(self, tmp_path):
        """File without any Recent Changes section: new h2 + h3 are created."""
        ctx = tmp_path / "context.md"
        ctx.write_text(
            "# Title\n\n**Last Updated:** 2026-04-01\n\n"
            "## Description\n\nA project.\n"
        )
        update_context_file(str(ctx), recent_changes=["first entry"])
        content = ctx.read_text()
        assert self._h2_count(content) == 1
        assert "first entry" in content

    def test_inserts_under_first_when_multiple_legacy_h2s(self, tmp_path):
        """File with multiple legacy h2s gets the new entry under the FIRST one only.

        This represents the user's actual file shape pre-migration: residual
        accumulation from pre-fix sessions. The tool itself doesn't clean
        up the residue (the migration script does); it just must not make
        things worse by adding yet another sibling h2.
        """
        ctx = tmp_path / "context.md"
        ctx.write_text(
            "# Title\n\n**Last Updated:** 2026-04-01\n\n"
            "## Recent Changes (2026-04-23)\n\n- A\n\n"
            "## Recent Changes (2026-04-22)\n\n- B\n\n"
            "## Recent Changes (2026-04-21)\n\n- C\n"
        )
        update_context_file(str(ctx), recent_changes=["new"])
        content = ctx.read_text()
        first_h2 = content.find("## Recent Changes (2026-04-23)")
        new_entry = content.find("- new")
        second_h2 = content.find("## Recent Changes (2026-04-22)")
        # New entry lands between the first and second h2 - i.e. as a child of the first.
        assert first_h2 < new_entry < second_h2
        # The 3 legacy h2s are unchanged in count (tool doesn't migrate; migration
        # script does that separately).
        assert content.count("## Recent Changes (2026-04-2") == 3

    def test_three_consecutive_saves_yield_one_h2_three_h3s(self, tmp_path):
        """Regression guard: 3 saves on a fresh file produce 1 h2 with 3 h3 children.

        This is the original bug shape - pre-fix this would produce 3 sibling
        h2s. Post-fix: exactly 1 h2 with 3 dated h3 subsections.
        """
        import time
        ctx = tmp_path / "context.md"
        ctx.write_text(
            "# Title\n\n**Last Updated:** 2026-04-01\n\n"
            "## Recent Changes\n\n"
        )
        for i in range(3):
            time.sleep(1)  # ensure distinct timestamps
            update_context_file(str(ctx), recent_changes=[f"entry-{i}"])
        content = ctx.read_text()
        assert self._h2_count(content) == 1
        assert self._h3_count(content) == 3
        for i in range(3):
            assert f"entry-{i}" in content

    def test_preserves_h2_with_trailing_context_suffix(self, tmp_path):
        """Legacy h2 with text after the close paren keeps its full line on insert.

        Some old files have `## Recent Changes (2026-04-19 18:31) - Codex Round 2`
        style headings. The match should not strip the trailing context.
        """
        ctx = tmp_path / "context.md"
        ctx.write_text(
            "# Title\n\n**Last Updated:** 2026-04-01\n\n"
            "## Recent Changes (2026-04-19 18:31) - Codex Round 2\n\n"
            "old content\n"
        )
        update_context_file(str(ctx), recent_changes=["new"])
        content = ctx.read_text()
        # Trailing context on the h2 is intact.
        assert "## Recent Changes (2026-04-19 18:31) - Codex Round 2" in content


# ── update_tasks_file ────────────────────────────────────────────────────


class TestUpdateTasksFile:
    def test_marks_task_completed(self, tmp_path, sample_tasks_md):
        """update_tasks_file marks matching task descriptions as [x]."""
        tasks_file = tmp_path / "tasks.md"
        tasks_file.write_text(sample_tasks_md)

        result = update_tasks_file(
            str(tasks_file),
            completed_tasks=["Implement core logic"],
        )

        content = tasks_file.read_text()
        # The task "3. Implement core logic" should now be checked
        assert re.search(r"- \[x\].*Implement core logic", content, re.IGNORECASE)
        assert len(result["updates_made"]) > 0

    def test_updates_progress_percentage(self, tmp_path, sample_tasks_md):
        """update_tasks_file returns progress with correct completion percentage."""
        tasks_file = tmp_path / "tasks.md"
        tasks_file.write_text(sample_tasks_md)

        result = update_tasks_file(
            str(tasks_file),
            completed_tasks=["Implement core logic"],
        )

        progress = result["progress"]
        assert progress is not None
        # Originally 2/5 completed, now 3/5 = 60%
        assert progress["completion_pct"] == 60
        assert progress["completed_items"] == 3
        assert progress["total_items"] == 5

    def test_returns_completed_numbers_for_transitions(
        self, tmp_path, sample_tasks_md
    ):
        """Newly-checked items are reported as their numbers in ``completed_numbers``."""
        tasks_file = tmp_path / "tasks.md"
        tasks_file.write_text(sample_tasks_md)

        result = update_tasks_file(
            str(tasks_file),
            completed_tasks=["Implement core logic"],
        )

        # Item "3. Implement core logic" was [ ] before, [x] after -> reported.
        assert result["completed_numbers"] == ["3"]

    def test_completed_numbers_excludes_already_checked(
        self, tmp_path, sample_tasks_md
    ):
        """Items already ``[x]`` before the call don't appear in completed_numbers.

        The pre/post diff gates membership so callers only see real
        transitions. Without this guarantee, the auto-clear hook would
        spuriously remove pointers for tasks that were already done.
        """
        tasks_file = tmp_path / "tasks.md"
        tasks_file.write_text(sample_tasks_md)

        # "Set up project structure" is item 1, already [x] in the fixture.
        result = update_tasks_file(
            str(tasks_file),
            completed_tasks=["Set up project structure"],
        )
        assert result["completed_numbers"] == []

    def test_no_completed_tasks_arg_yields_empty_completed_numbers(
        self, tmp_path, sample_tasks_md
    ):
        tasks_file = tmp_path / "tasks.md"
        tasks_file.write_text(sample_tasks_md)

        result = update_tasks_file(
            str(tasks_file),
            notes=["just a note"],
        )
        assert result["completed_numbers"] == []

    def test_marks_by_number_ignoring_trailing_prose(
        self, tmp_path, sample_tasks_md
    ):
        """An entry leading with the checklist number is matched by number,
        so trailing annotations don't break the match (the bug that left
        boxes unticked when callers appended '- DONE: ...')."""
        tasks_file = tmp_path / "tasks.md"
        tasks_file.write_text(sample_tasks_md)

        result = update_tasks_file(
            str(tasks_file),
            completed_tasks=["3. Implement core logic - DONE: shipped in PR #312"],
        )

        content = tasks_file.read_text()
        assert re.search(r"- \[x\] 3\. Implement core logic", content)
        assert result["completed_numbers"] == ["3"]
        assert result["unmatched"] == []

    def test_bare_number_marks_task(self, tmp_path, sample_tasks_md):
        """A bare checklist number marks that item complete."""
        tasks_file = tmp_path / "tasks.md"
        tasks_file.write_text(sample_tasks_md)

        result = update_tasks_file(str(tasks_file), completed_tasks=["4"])

        assert result["completed_numbers"] == ["4"]
        assert result["unmatched"] == []

    def test_unmatched_entries_reported(self, tmp_path, sample_tasks_md):
        """Entries that resolve to no checklist item come back in
        ``unmatched`` instead of being silently dropped."""
        tasks_file = tmp_path / "tasks.md"
        tasks_file.write_text(sample_tasks_md)

        result = update_tasks_file(
            str(tasks_file),
            completed_tasks=["999. nonexistent task", "totally unrelated text"],
        )

        assert result["completed_numbers"] == []
        assert result["unmatched"] == [
            "999. nonexistent task",
            "totally unrelated text",
        ]

    def test_text_fallback_still_matches(self, tmp_path, sample_tasks_md):
        """An entry with no leading number falls back to a substring match."""
        tasks_file = tmp_path / "tasks.md"
        tasks_file.write_text(sample_tasks_md)

        result = update_tasks_file(
            str(tasks_file),
            completed_tasks=["Write tests"],
        )

        assert result["completed_numbers"] == ["4"]
        assert result["unmatched"] == []


# ── atomic write semantics (MAJOR-12) ────────────────────────────────────


class TestAtomicWrites:
    """Verify update_context_file and update_tasks_file serialize concurrent
    writes via fcntl.flock + os.replace, so no caller's edits are silently lost.
    """

    def test_concurrent_recent_changes_all_preserved(
        self, tmp_path, sample_context_md
    ):
        """N concurrent update_context_file calls must preserve every entry.

        Without flock around the read-modify-write, writers race and
        last-writer-wins overwrites earlier additions. With the lock, each
        worker reads the latest content, appends its own change, replaces.
        """
        import threading

        ctx_file = tmp_path / "context.md"
        ctx_file.write_text(sample_context_md)

        n = 8
        barrier = threading.Barrier(n)

        def worker(label):
            barrier.wait()  # release all workers simultaneously
            update_context_file(
                str(ctx_file), recent_changes=[f"change-{label}"]
            )

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        content = ctx_file.read_text()
        for i in range(n):
            assert f"change-{i}" in content, (
                f"change-{i} lost - lock did not serialize writers"
            )

    def test_concurrent_completed_tasks_all_preserved(
        self, tmp_path, sample_tasks_md_hierarchical
    ):
        """N concurrent update_tasks_file calls each marking a different
        task complete must all land. Mirrors the missioncache-auto parallel path
        where multiple workers report progress on disjoint subtasks.
        """
        import threading

        tasks_file = tmp_path / "tasks.md"
        tasks_file.write_text(sample_tasks_md_hierarchical)

        # Pull pending task descriptions out of the fixture
        pending = re.findall(
            r"^\s*[-*]\s*\[\s*\]\s*\d+(?:\.\d+)?\.\s*(.+)$",
            sample_tasks_md_hierarchical,
            re.MULTILINE,
        )
        assert len(pending) >= 3, "fixture should have pending tasks to race"
        targets = pending[:3]
        barrier = threading.Barrier(len(targets))

        def worker(desc):
            barrier.wait()
            update_tasks_file(str(tasks_file), completed_tasks=[desc])

        threads = [threading.Thread(target=worker, args=(d,)) for d in targets]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        content = tasks_file.read_text()
        for desc in targets:
            # Either the original `- [ ]` got flipped to `- [x]`, or another
            # writer's completion landed on this exact line. Verify each
            # target description is now in a checked checkbox row.
            assert re.search(
                rf"- \[x\][^\n]*{re.escape(desc)}", content, re.IGNORECASE
            ), f"completion of '{desc}' lost - lock did not serialize writers"

    def test_lockfile_persists_as_sidecar(self, tmp_path, sample_context_md):
        """The .lock sidecar is created on first write and left in place.

        We deliberately don't delete it - lockfile create/delete under
        contention is racy. Future writers reuse the existing inode.
        """
        ctx_file = tmp_path / "context.md"
        ctx_file.write_text(sample_context_md)

        update_context_file(str(ctx_file), recent_changes=["one"])

        lock_file = ctx_file.with_name(ctx_file.name + ".lock")
        assert lock_file.exists(), "sidecar .lock should exist after update"

    def test_no_tmp_file_leftover(self, tmp_path, sample_context_md):
        """os.replace is atomic - the .tmp staging file is renamed away,
        not left as a leftover for the next reader to trip over.
        """
        ctx_file = tmp_path / "context.md"
        ctx_file.write_text(sample_context_md)

        update_context_file(str(ctx_file), recent_changes=["one"])

        tmp_file = ctx_file.with_name(ctx_file.name + ".tmp")
        assert not tmp_file.exists(), ".tmp staging file should be gone"


# ── Waiting on maintenance + Recent Changes cap (context-file conventions) ──


WAITING_CONTEXT = """# Demo - Context

**Last Updated:** 2026-04-01 10:00

## Description

A demo project.

## Gotchas

- TBD

## Waiting on

External replies/events that gate work. Check on every resume; when one resolves, act on what it gates and move the row into Recent Changes.

| What | Who | Since | Gates |
|------|-----|-------|-------|
| Reply on PR | Jose | 2026-07-10 | GC-1 rework |
| Egress check | Nitzan | 2026-07-09 | GC-2 closure |

## Next Steps

1. Do the thing

## Recent Changes

### 2026-07-01 10:00

- created
"""


class TestWaitingOnAdd:
    def test_adds_row_to_existing_section(self, tmp_path):
        ctx = tmp_path / "demo-context.md"
        ctx.write_text(WAITING_CONTEXT)
        update_context_file(
            str(ctx),
            waiting_on_add=[
                {"what": "SA reply", "who": "Shasho", "since": "2026-07-10", "gates": "IAP binding"}
            ],
        )
        content = ctx.read_text()
        assert "| SA reply | Shasho | 2026-07-10 | IAP binding |" in content
        # Existing rows survive.
        assert "| Reply on PR | Jose | 2026-07-10 | GC-1 rework |" in content
        # Usage note prose untouched.
        assert "External replies/events that gate work" in content

    def test_self_heals_missing_section_before_next_steps(self, tmp_path, sample_context_md):
        ctx = tmp_path / "context.md"
        ctx.write_text(sample_context_md)
        assert "## Waiting on" not in sample_context_md
        update_context_file(
            str(ctx),
            waiting_on_add=[{"what": "A thing", "who": "Bob", "gates": "X"}],
        )
        content = ctx.read_text()
        assert "## Waiting on" in content
        assert content.index("## Waiting on") < content.index("## Next Steps")
        assert "| A thing | Bob |" in content

    def test_default_since_is_today(self, tmp_path):
        from datetime import datetime

        ctx = tmp_path / "demo-context.md"
        ctx.write_text(WAITING_CONTEXT)
        # Capture the date BEFORE the call so a midnight rollover between
        # write and assert can't flip the expectation.
        before = datetime.now().strftime("%Y-%m-%d")
        update_context_file(
            str(ctx), waiting_on_add=[{"what": "New ask", "who": "Ann"}]
        )
        after = datetime.now().strftime("%Y-%m-%d")
        content = ctx.read_text()
        assert (
            f"| New ask | Ann | {before} |" in content
            or f"| New ask | Ann | {after} |" in content
        )

    def test_pipe_in_cell_survives_write_roundtrip(self, tmp_path):
        """Integration-level guard for the pipe-escaping fix (flagged by
        three reviewers): a cell pipe must not shift columns on rewrite."""
        from missioncache_db import context_health as ch

        ctx = tmp_path / "demo-context.md"
        ctx.write_text(WAITING_CONTEXT)
        update_context_file(
            str(ctx),
            waiting_on_add=[
                {"what": "Fix a|b split", "who": "X", "since": "2026-07-11", "gates": "GC-1 | GC-2"}
            ],
        )
        # Second write touching the table re-parses and re-renders all rows.
        update_context_file(
            str(ctx),
            waiting_on_add=[{"what": "Unrelated", "who": "Y", "since": "2026-07-11", "gates": "g"}],
        )
        rows = ch.parse_waiting_on(ctx.read_text())
        piped = next(r for r in rows if "Fix a" in r["what"])
        assert piped == {
            "what": "Fix a|b split", "who": "X", "since": "2026-07-11", "gates": "GC-1 | GC-2"
        }

    def test_multiple_rows_appended_in_order(self, tmp_path):
        ctx = tmp_path / "demo-context.md"
        ctx.write_text(WAITING_CONTEXT)
        update_context_file(
            str(ctx),
            waiting_on_add=[
                {"what": "First", "who": "A", "since": "2026-07-11", "gates": "g1"},
                {"what": "Second", "who": "B", "since": "2026-07-11", "gates": "g2"},
            ],
        )
        content = ctx.read_text()
        assert content.index("| First |") < content.index("| Second |")


class TestWaitingOnResolve:
    def test_removes_first_match_and_records_resolution(self, tmp_path):
        ctx = tmp_path / "demo-context.md"
        ctx.write_text(WAITING_CONTEXT)
        result = update_context_file(
            str(ctx),
            waiting_on_resolve=[{"match": "Egress check", "outcome": "confirmed open"}],
        )
        content = ctx.read_text()
        assert "| Egress check |" not in content
        # Resolution lands in today's Recent Changes subsection.
        assert (
            "- Resolved (was waiting on Nitzan): Egress check - confirmed open"
            in content
        )
        assert result["waiting_on_unmatched"] == []
        # Other row untouched.
        assert "| Reply on PR | Jose |" in content

    def test_unmatched_returned_never_silent(self, tmp_path):
        ctx = tmp_path / "demo-context.md"
        ctx.write_text(WAITING_CONTEXT)
        result = update_context_file(
            str(ctx),
            waiting_on_resolve=[{"match": "No such row", "outcome": "n/a"}],
        )
        assert result["waiting_on_unmatched"] == ["No such row"]
        # Table unchanged.
        assert "| Reply on PR |" in ctx.read_text()

    def test_resolve_without_section_all_unmatched(self, tmp_path, sample_context_md):
        ctx = tmp_path / "context.md"
        ctx.write_text(sample_context_md)
        result = update_context_file(
            str(ctx), waiting_on_resolve=[{"match": "anything", "outcome": "x"}]
        )
        assert result["waiting_on_unmatched"] == ["anything"]

    def test_substring_match(self, tmp_path):
        ctx = tmp_path / "demo-context.md"
        ctx.write_text(WAITING_CONTEXT)
        update_context_file(
            str(ctx), waiting_on_resolve=[{"match": "Egress", "outcome": "done"}]
        )
        assert "| Egress check |" not in ctx.read_text()

    def test_resolution_joins_recent_changes_entries(self, tmp_path):
        """Resolved bullets and recent_changes share ONE dated subsection."""
        ctx = tmp_path / "demo-context.md"
        ctx.write_text(WAITING_CONTEXT)
        update_context_file(
            str(ctx),
            recent_changes=["Shipped the fix"],
            waiting_on_resolve=[{"match": "Reply on PR", "outcome": "merged"}],
        )
        content = ctx.read_text()
        # Both bullets under the same (single new) ### subsection.
        subsection_count = len(re.findall(r"^### \d{4}-", content, re.MULTILINE))
        assert subsection_count == 2  # original + one new
        assert "Resolved (was waiting on Jose)" in content
        assert "- Shipped the fix" in content


class TestWaitingOnPreservesNextStepsReplacement:
    def test_next_steps_replace_leaves_waiting_on_intact(self, tmp_path):
        ctx = tmp_path / "demo-context.md"
        ctx.write_text(WAITING_CONTEXT)
        update_context_file(str(ctx), next_steps=["New step one", "New step two"])
        content = ctx.read_text()
        assert "1. New step one" in content
        assert "1. Do the thing" not in content
        # Waiting on rows and note untouched by the Next Steps replacement.
        assert "| Reply on PR | Jose | 2026-07-10 | GC-1 rework |" in content
        assert "External replies/events that gate work" in content


class TestRecentChangesCap:
    def _saturate(self, ctx, n):
        for i in range(n):
            update_context_file(str(ctx), recent_changes=[f"entry {i}"])

    def test_saves_beyond_cap_roll_to_journal(self, tmp_path):
        from missioncache_db import context_health as ch

        ctx = tmp_path / "demo-context.md"
        ctx.write_text(WAITING_CONTEXT)  # starts with 1 subsection
        self._saturate(ctx, ch.RECENT_CHANGES_CAP)  # 1 + 12 = 13 -> 1 rolls
        journal = tmp_path / "demo-journal.md"
        assert journal.exists()
        jcontent = journal.read_text()
        # The OLDEST subsection (the original "- created") rolled over.
        assert "- created" in jcontent
        content = ctx.read_text()
        assert "- created" not in content
        subsections = ch.parse_recent_changes_subsections(content)
        assert len(subsections) == ch.RECENT_CHANGES_CAP

    def test_journal_created_with_header(self, tmp_path):
        from missioncache_db import context_health as ch

        ctx = tmp_path / "demo-context.md"
        ctx.write_text(WAITING_CONTEXT)
        self._saturate(ctx, ch.RECENT_CHANGES_CAP)
        jcontent = (tmp_path / "demo-journal.md").read_text()
        assert jcontent.startswith("# ")
        assert "oldest" in jcontent

    def test_journal_reads_oldest_first_across_rollovers(self, tmp_path):
        from missioncache_db import context_health as ch

        ctx = tmp_path / "demo-context.md"
        ctx.write_text(WAITING_CONTEXT)
        self._saturate(ctx, ch.RECENT_CHANGES_CAP + 2)  # rolls 3 times total
        jcontent = (tmp_path / "demo-journal.md").read_text()
        # "- created" was oldest, then "entry 0", then "entry 1".
        assert (
            jcontent.index("- created")
            < jcontent.index("- entry 0")
            < jcontent.index("- entry 1")
        )

    def test_pointer_line_at_bottom_survives_next_save(self, tmp_path):
        from missioncache_db import context_health as ch

        ctx = tmp_path / "demo-context.md"
        ctx.write_text(WAITING_CONTEXT)
        self._saturate(ctx, ch.RECENT_CHANGES_CAP)  # first rollover: pointer added
        # One more save (prepends AND rolls again): pointer must stay single
        # and stay at the bottom of the section.
        update_context_file(str(ctx), recent_changes=["after pointer"])
        content = ctx.read_text()
        pointer = ch.RECENT_CHANGES_POINTER.format(journal_name="demo-journal.md")
        assert content.count(pointer) == 1
        body = ch.extract_section(content, "Recent Changes")
        assert body.rindex(pointer) > body.rindex("- entry")

    def test_under_cap_no_journal(self, tmp_path):
        ctx = tmp_path / "demo-context.md"
        ctx.write_text(WAITING_CONTEXT)
        result = update_context_file(str(ctx), recent_changes=["one more"])
        assert result["journal_rolled_over"] == 0
        assert not (tmp_path / "demo-journal.md").exists()

    def test_rolled_over_count_returned(self, tmp_path):
        from missioncache_db import context_health as ch

        ctx = tmp_path / "demo-context.md"
        ctx.write_text(WAITING_CONTEXT)
        self._saturate(ctx, ch.RECENT_CHANGES_CAP - 1)  # exactly at cap
        result = update_context_file(str(ctx), recent_changes=["overflow trigger"])
        assert result["journal_rolled_over"] == 1


class TestJournalDerivation:
    def test_prefixed_context_writes_prefixed_journal(self, tmp_path):
        from missioncache_db import context_health as ch

        ctx = tmp_path / "myproj-context.md"
        ctx.write_text(WAITING_CONTEXT)
        for i in range(ch.RECENT_CHANGES_CAP):
            update_context_file(str(ctx), recent_changes=[f"e{i}"])
        assert (tmp_path / "myproj-journal.md").exists()

    def test_legacy_bare_context_writes_bare_journal(self, tmp_path):
        from missioncache_db import context_health as ch

        ctx = tmp_path / "context.md"
        ctx.write_text(WAITING_CONTEXT)
        for i in range(ch.RECENT_CHANGES_CAP):
            update_context_file(str(ctx), recent_changes=[f"e{i}"])
        assert (tmp_path / "journal.md").exists()


class TestUpdateContextReturnContract:
    def test_returns_dict_with_all_keys(self, tmp_path, sample_context_md):
        ctx = tmp_path / "context.md"
        ctx.write_text(sample_context_md)
        result = update_context_file(str(ctx), recent_changes=["x"])
        assert set(result) == {"content", "waiting_on_unmatched", "journal_rolled_over"}
        assert isinstance(result["content"], str)
        assert result["waiting_on_unmatched"] == []
        assert result["journal_rolled_over"] == 0


class TestAnchoredHeadingMatch:
    """Regression guards for the unanchored-regex bug (found 2026-07-11).

    A prose bullet containing the literal string `## Recent Changes` (or any
    section heading) used to be matched as the heading itself, sending weeks
    of prepended entries into the middle of another section. All heading
    matches are now ^-anchored with MULTILINE.
    """

    MIDLINE_MENTION = (
        "# Title\n\n**Last Updated:** 2026-04-01\n\n"
        "## Key Architectural Decisions\n\n"
        "- Section shape: single `## Recent Changes` heading with dated "
        "subsections prepended newest-first.\n\n"
        "## Next Steps\n\n1. old step\n\n"
        "## Recent Changes\n\n### 2026-07-01 10:00\n\n- existing entry\n"
    )

    def test_recent_changes_prepend_ignores_midline_mention(self, tmp_path):
        ctx = tmp_path / "context.md"
        ctx.write_text(self.MIDLINE_MENTION)
        update_context_file(str(ctx), recent_changes=["new entry"])
        content = ctx.read_text()
        # The new entry lands under the REAL heading, after the decisions
        # bullet region - not inside Key Architectural Decisions.
        real_heading = content.index("\n## Recent Changes\n")
        assert content.index("- new entry") > real_heading
        # The decisions bullet is untouched and still ahead of Next Steps.
        assert content.index("- Section shape:") < content.index("## Next Steps")

    def test_next_steps_replace_ignores_midline_mention(self, tmp_path):
        ctx = tmp_path / "context.md"
        ctx.write_text(
            "# Title\n\n**Last Updated:** 2026-04-01\n\n"
            "## Description\n\nMentions ## Next Steps mid-line in prose.\n\n"
            "## Next Steps\n\n1. old step\n"
        )
        update_context_file(str(ctx), next_steps=["new step"])
        content = ctx.read_text()
        assert "1. new step" in content
        assert "1. old step" not in content
        # The prose mention survives; Description body was not treated as
        # the Next Steps section.
        assert "Mentions ## Next Steps mid-line in prose." in content


class TestFencedHeadingNotCorrupted:
    """update_context_file must not treat a column-0 ## heading inside a
    fenced code block as the target section (Codex adversarial review,
    2026-07-11). Sibling of TestAnchoredHeadingMatch, which guards the
    prose-mention (mid-line) case; this guards the fenced-example case for
    the section helpers (_update_section / _append_to_section).
    """

    FENCED = (
        "# Title\n\n**Last Updated:** 2026-04-01\n\n"
        "## Description\n\n"
        "Canonical order, shown as an example:\n\n"
        "```markdown\n## Gotchas\n\n## Next Steps\n\n## Recent Changes\n```\n\n"
        "## Gotchas\n\n- TBD\n\n"
        "## Next Steps\n\n1. old step\n\n"
        "## Recent Changes\n\n### 2026-04-01 09:00\n\n- seed\n"
    )

    FENCE_BLOCK = "```markdown\n## Gotchas\n\n## Next Steps\n\n## Recent Changes\n```"

    def test_next_steps_replace_skips_fenced_heading(self, tmp_path):
        ctx = tmp_path / "context.md"
        ctx.write_text(self.FENCED)
        update_context_file(str(ctx), next_steps=["new step"])
        content = ctx.read_text()
        assert "1. new step" in content
        assert "1. old step" not in content
        # The fenced example is byte-for-byte intact - not torn open.
        assert self.FENCE_BLOCK in content

    def test_gotchas_append_skips_fenced_heading(self, tmp_path):
        from missioncache_db import context_health as ch

        ctx = tmp_path / "context.md"
        ctx.write_text(self.FENCED)
        update_context_file(str(ctx), gotchas=["a real gotcha"])
        content = ctx.read_text()
        # The entry lands in the REAL Gotchas, and its '- TBD' placeholder
        # is stripped; the fenced example is untouched.
        gotchas_body = ch.extract_section(content, "Gotchas")
        assert gotchas_body is not None
        assert "a real gotcha" in gotchas_body
        assert "- TBD" not in gotchas_body
        assert self.FENCE_BLOCK in content

    def test_fenced_recent_changes_left_intact_by_section_writes(self, tmp_path):
        ctx = tmp_path / "context.md"
        ctx.write_text(self.FENCED)
        # A Next Steps replacement must not disturb the fenced example NOR
        # the real Recent Changes seed entry that follows it.
        update_context_file(str(ctx), next_steps=["new step"])
        content = ctx.read_text()
        assert "- seed" in content
        assert "### 2026-04-01 09:00" in content

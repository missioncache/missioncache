"""Integration tests for MissionCache file create/update operations.

Tests use tmp_path for all file I/O and monkeypatch to redirect root_dir.
"""

import re
from pathlib import Path

import pytest
from missioncache_db import context_health as ch

from mcp_missioncache.config import Settings
from mcp_missioncache.errors import ErrorCode, MissionCacheError, MissionCacheFileNotFoundError
from mcp_missioncache.tasks_parse import parse_tasks_md as _parse_tasks
from mcp_missioncache.project_files import (
    BULLETS_REMOVE_FORBIDDEN,
    PROTECTED_SECTIONS,
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
        assert Path(result.task_dir).parts[-2:] == ("active", "active-task")

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
        assert Path(result.task_dir).parts[-2:] == ("completed", "archived-task")

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
        assert Path(result.task_dir).parts[-2:] == ("active", "dual-task")
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
    def test_new_task_does_not_corrupt_a_line_mentioning_the_anchor(self, tmp_path):
        """A task whose own text names a heading must survive an insertion.

        The insertion used to run an unanchored re.sub with the default
        count=0 against "## Phase 2" / "## Validation" / "## Notes", so every
        occurrence got an insertion, including one inside an existing task's
        description. That split the existing line in half and wrote the new
        task twice.
        """
        tasks_file = tmp_path / "tasks.md"
        tasks_file.write_text(
            "# T - Tasks\n\n## Phase 1\n\n"
            "- [x] 1. Refactor the `## Phase 2` anchor handling\n\n"
            "## Phase 2: Real heading\n\n- [ ] 2. Something\n\n## Notes\n\n- none\n"
        )

        update_tasks_file(str(tasks_file), new_tasks=["NEW TASK"])

        content = tasks_file.read_text()
        assert "- [x] 1. Refactor the `## Phase 2` anchor handling" in content, \
            "the existing task line was split by the insertion"
        assert content.count("NEW TASK") == 1, "the new task was inserted more than once"

    def test_new_task_numbering_sees_uppercase_and_letter_suffixes(self, tmp_path):
        """next_num comes from the canonical parser, not a local regex.

        The old regex matched `[x\\s]` and a bare `\\d+`, so a hand-edited
        `[X]` and a letter-suffixed `54a` were both invisible to it and the
        next number collided with an item already in the file.
        """
        tasks_file = tmp_path / "tasks.md"
        tasks_file.write_text(
            "# T\n\n## Phase 1\n\n- [X] 7. upper-case checkbox\n- [ ] 54a. letter suffix\n"
        )

        update_tasks_file(str(tasks_file), new_tasks=["after"])

        assert "- [ ] 55. after" in tasks_file.read_text()

    def test_same_day_additions_share_one_section(self, tmp_path):
        """Two calls on one day append to the same dated section.

        A section per call would produce a heading per call on a busy day.
        """
        tasks_file = tmp_path / "tasks.md"
        tasks_file.write_text("# T\n\n## Phase 1\n\n- [ ] 1. a\n")

        update_tasks_file(str(tasks_file), new_tasks=["first"])
        update_tasks_file(str(tasks_file), new_tasks=["second"])

        content = tasks_file.read_text()
        headings = [l for l in content.splitlines() if l.startswith("## Additions (")]
        assert len(headings) == 1, f"expected one dated section, got {headings}"
        assert "- [ ] 2. first" in content
        assert "- [ ] 3. second" in content

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

    def test_completing_parent_by_number_leaves_subtasks_unchecked(self, tmp_path):
        """Completing parent "1" must flip only the parent line, not the
        "1." prefix of its subtasks "1.1"/"1.2" (the prefix-match bug)."""
        tasks_file = tmp_path / "tasks.md"
        tasks_file.write_text(
            "# Tasks\n\n**Last Updated:** 2026-04-01 10:00\n\n## Tasks\n\n"
            "- [ ] 1. Parent\n"
            "  - [ ] 1.1. Child A\n"
            "  - [ ] 1.2. Child B\n"
            "- [ ] 2. Sibling\n"
        )

        result = update_tasks_file(str(tasks_file), completed_tasks=["1. Parent"])

        content = tasks_file.read_text()
        assert re.search(r"- \[x\] 1\. Parent", content)
        assert re.search(r"- \[ \] 1\.1\. Child A", content)
        assert re.search(r"- \[ \] 1\.2\. Child B", content)
        # Only the parent transitioned - subtasks are untouched.
        assert result["completed_numbers"] == ["1"]

    def test_completing_subtask_by_number_leaves_deeper_nesting(self, tmp_path):
        """Completing "1.2" must not also flip "1.2.3" via the prefix match."""
        tasks_file = tmp_path / "tasks.md"
        tasks_file.write_text(
            "# Tasks\n\n**Last Updated:** 2026-04-01 10:00\n\n## Tasks\n\n"
            "- [ ] 1. Parent\n"
            "  - [ ] 1.2. Child\n"
            "    - [ ] 1.2.3. Grandchild\n"
        )

        result = update_tasks_file(str(tasks_file), completed_tasks=["1.2"])

        content = tasks_file.read_text()
        assert re.search(r"- \[x\] 1\.2\. Child", content)
        assert re.search(r"- \[ \] 1\.2\.3\. Grandchild", content)
        assert result["completed_numbers"] == ["1.2"]

    def test_substring_fallback_marks_only_first_match(self, tmp_path):
        """A bare-word entry that appears in several task lines flips only
        the first, never every sibling that shares the phrase."""
        tasks_file = tmp_path / "tasks.md"
        tasks_file.write_text(
            "# Tasks\n\n**Last Updated:** 2026-04-01 10:00\n\n## Tasks\n\n"
            "- [ ] 3. Set up database\n"
            "- [ ] 4. Set up API\n"
            "- [ ] 5. Tear down\n"
        )

        result = update_tasks_file(str(tasks_file), completed_tasks=["Set up"])

        content = tasks_file.read_text()
        assert re.search(r"- \[x\] 3\. Set up database", content)
        assert re.search(r"- \[ \] 4\. Set up API", content)
        assert result["completed_numbers"] == ["3"]


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
        assert set(result) == {
            "content",
            "waiting_on_unmatched",
            "journal_rolled_over",
            "imported_event_applied",
            "sections_removed",
            "sections_unmatched",
            "bullets_removed",
            "bullets_unmatched",
        }
        assert isinstance(result["content"], str)
        assert result["waiting_on_unmatched"] == []
        assert result["journal_rolled_over"] == 0
        assert result["sections_removed"] == []
        assert result["sections_unmatched"] == []
        assert result["bullets_removed"] == []
        assert result["bullets_unmatched"] == []
        # False when no event was passed at all, so the MCP layer can tell a
        # skipped duplicate from a section it actually wrote.
        assert result["imported_event_applied"] is False


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


# ── imported_event (the locked writer for the cross-project convention) ──


class TestImportedEvent:
    """Spec source: the "Cross-project events" convention in
    rules/missioncache.md - a self-contained ``## <event> (<date>)`` section
    ABOVE Waiting on, plus a ``**Related projects:**`` header line so both sides
    know the link exists. This parameter exists so following that convention
    goes through the sidecar lock instead of a direct Edit.
    """

    CONTEXT = (
        "# demo - Context\n"
        "**Last Updated:** 2026-04-01 10:00\n"
        "\n## Description\n\nBody.\n"
        "\n## Waiting on\n\n| What | Who | Since | Gates |\n|---|---|---|---|\n"
        "\n## Next Steps\n\n1. Thing\n"
        "\n## Recent Changes\n"
    )

    def _write(self, tmp_path, **event):
        ctx = tmp_path / "context.md"
        ctx.write_text(self.CONTEXT)
        update_context_file(str(ctx), imported_event=event)
        return ctx.read_text()

    def test_section_lands_above_waiting_on(self, tmp_path):
        content = self._write(tmp_path, heading="Steering call", body="- Scope cut.")
        names = [s["name"] for s in ch.section_index(content)]
        assert names.index("Steering call (%s)" % _today(content)) < names.index(
            "Waiting on"
        )

    def test_body_is_written_verbatim(self, tmp_path):
        content = self._write(tmp_path, heading="Steering call", body="- Scope cut.")
        assert "- Scope cut." in content

    def test_date_is_appended_when_the_heading_lacks_one(self, tmp_path):
        content = self._write(tmp_path, heading="Steering call", body="- x")
        assert f"## Steering call ({_today(content)})" in content

    def test_a_heading_that_already_dates_itself_is_left_alone(self, tmp_path):
        content = self._write(
            tmp_path, heading="Steering call (2026-01-05)", body="- x"
        )
        assert "## Steering call (2026-01-05)" in content
        assert content.count("Steering call") == 1

    def test_related_project_lands_on_the_header_line(self, tmp_path):
        content = self._write(
            tmp_path,
            heading="Steering call",
            body="- x",
            related_project="other-proj",
            related_note="shares the feed",
        )
        assert "**Related projects:** [[other-proj]] (shares the feed)" in content

    def test_ignored_without_a_body(self, tmp_path):
        """Half an event is not an event - a heading with nothing under it
        would be worse than no section at all."""
        content = self._write(tmp_path, heading="Steering call", body="")
        assert "Steering call" not in content

    def test_ignored_without_a_heading(self, tmp_path):
        content = self._write(tmp_path, heading="", body="- x")
        assert "- x" not in content

    def test_waiting_on_is_created_first_when_absent(self, tmp_path):
        """On a file predating the Waiting on convention, a waiting_on_add in
        the same call self-heals the section and the event still sits above it."""
        ctx = tmp_path / "context.md"
        ctx.write_text(
            "# demo - Context\n**Last Updated:** 2026-04-01 10:00\n"
            "\n## Description\n\nBody.\n\n## Next Steps\n\n1. Thing\n"
        )
        update_context_file(
            str(ctx),
            waiting_on_add=[{"what": "review", "who": "Dana", "gates": "rollout"}],
            imported_event={"heading": "Steering call", "body": "- x"},
        )
        names = [s["name"] for s in ch.section_index(ctx.read_text())]
        assert names.index("Steering call (%s)" % _today(ctx.read_text())) < names.index(
            "Waiting on"
        )


def _today(content: str) -> str:
    """The date update_context_file stamped on this write."""
    return re.search(r"\*\*Last Updated:\*\* (\d{4}-\d{2}-\d{2})", content).group(1)


# ── imported_event: structure forgery and idempotency ────────────────────


class TestImportedEventCannotForgeStructure:
    """Spec source: rules/missioncache.md requires the imported-event section to
    be self-contained and to sit above Waiting on. A section that escapes its own
    boundaries breaks the digest contract every other reader depends on.

    All four cases below were reproduced against the pre-fix writer.
    """

    CONTEXT = (
        "# demo - Context\n**Last Updated:** 2026-04-01 10:00\n"
        "\n## Description\n\nBody.\n"
        "\n## Waiting on\n\n| What | Who | Since | Gates |\n|---|---|---|---|\n"
        "| real row | Dana | 2026-08-01 | rollout |\n"
        "\n## Next Steps\n\n1. MY REAL NEXT STEP\n"
        "\n## Recent Changes\n"
    )

    def _ctx(self, tmp_path):
        ctx = tmp_path / "context.md"
        ctx.write_text(self.CONTEXT)
        return ctx

    def test_body_heading_becomes_a_subsection_not_a_sibling(self, tmp_path):
        """A pasted meeting summary containing '## Next Steps' must not shadow
        the project's real Next Steps. This fires on ordinary content."""
        ctx = self._ctx(tmp_path)
        update_context_file(
            str(ctx),
            imported_event={
                "heading": "Steering call",
                "body": "- Scope cut.\n\n## Next Steps\n\n1. theirs",
            },
        )
        content = ctx.read_text()
        assert [s["name"] for s in ch.section_index(content)].count("Next Steps") == 1
        assert "MY REAL NEXT STEP" in (ch.extract_section(content, "Next Steps") or "")
        assert "### Next Steps" in content, "the body heading should be demoted"

    def test_body_heading_inside_a_fence_is_left_alone(self, tmp_path):
        ctx = self._ctx(tmp_path)
        update_context_file(
            str(ctx),
            imported_event={"heading": "Call", "body": "```\n## not a heading\n```"},
        )
        assert "## not a heading" in ctx.read_text()

    def test_repeating_the_same_event_is_a_no_op(self, tmp_path):
        """A retried tool call must not stack duplicate sections - the receiver
        is told to 'read the section it names', which two sections make
        ambiguous."""
        ctx = self._ctx(tmp_path)
        event = {"heading": "Steering call", "body": "- Scope cut."}
        for _ in range(3):
            update_context_file(str(ctx), imported_event=event)
        names = [s["name"] for s in ch.section_index(ctx.read_text())]
        assert len([n for n in names if n.startswith("Steering call")]) == 1

    def test_a_ticket_id_is_not_mistaken_for_a_date(self, tmp_path):
        """The date suffix must still be appended when the heading merely
        contains a date-shaped substring."""
        ctx = self._ctx(tmp_path)
        update_context_file(
            str(ctx),
            imported_event={"heading": "Ticket ABCD-1234-56-7890 handoff", "body": "- x"},
        )
        heading = next(
            s["name"] for s in ch.section_index(ctx.read_text()) if "Ticket" in s["name"]
        )
        assert re.search(r"\(\d{4}-\d{2}-\d{2}\)$", heading), heading


class TestPlanContentNormalization:
    """Task-86 finding: agents pass lists and dicts in plan_content despite
    the declared dict[str, str], and str.replace crashed on them
    (TypeError: replace() argument 2 must be str, not list). Valid-shaped
    agent input must render as markdown, never crash."""

    def test_list_values_render_as_bullets(self, tmp_path):
        result = create_missioncache_files(
            task_name="plan-list-task",
            tasks=["one"],
            plan_content={"goals": ["ship it", "test it"], "risks": ["none really"]},
        )
        from pathlib import Path

        plan = Path(result.plan_file).read_text(encoding="utf-8")
        assert "- ship it\n- test it" in plan
        assert "- none really" in plan

    def test_dict_value_renders_as_key_bullets(self, tmp_path):
        result = create_missioncache_files(
            task_name="plan-dict-task",
            tasks=["one"],
            plan_content={"files": {"a.py": "entry point", "b.py": "helpers"}},
        )
        from pathlib import Path

        plan = Path(result.plan_file).read_text(encoding="utf-8")
        assert "- **a.py**: entry point" in plan
        assert "- **b.py**: helpers" in plan

    def test_strings_and_defaults_unchanged(self, tmp_path):
        result = create_missioncache_files(
            task_name="plan-str-task",
            tasks=["one"],
            plan_content={"summary": "just a string"},
        )
        from pathlib import Path

        plan = Path(result.plan_file).read_text(encoding="utf-8")
        assert "just a string" in plan
        assert "N/A - research phase skipped" in plan

    def test_empty_string_falls_back_to_default(self, tmp_path):
        result = create_missioncache_files(
            task_name="plan-empty-task",
            tasks=["one"],
            plan_content={"research_findings": ""},
        )
        from pathlib import Path

        plan = Path(result.plan_file).read_text(encoding="utf-8")
        assert "N/A - research phase skipped" in plan

    def test_non_container_value_stringified(self, tmp_path):
        result = create_missioncache_files(
            task_name="plan-num-task",
            tasks=["one"],
            plan_content={"dependencies": 3},
        )
        from pathlib import Path

        plan = Path(result.plan_file).read_text(encoding="utf-8")
        assert "3" in plan


# ── removal params ───────────────────────────────────────────────────────

REMOVABLE_CONTEXT = """# P - Context
**Last Updated:** 2026-01-01 00:00

## Description
p

## Gotchas

- keep me
- WRONG (falsified): a dead theory

## Guild items handed to Lior (2026-08-16)

The four items.

## Waiting on

| What | Who | Since | Gates |
|------|-----|-------|-------|
| the tagging decision | Sa'ar | 2026-08-01 | the map |

## Next Steps

1. go

## Recent Changes

## Key Files

| File | Purpose |
|------|---------|
| `a.py` | does a |
"""


# The guard lists, stated as the CONTRACT rather than read from the code.
# Parametrizing over the frozensets themselves is circular: deleting a name
# from the set also deletes its test case, so the mutation survives. These
# literals are what the docs promise; the tests below assert the code's sets
# equal them, so a name added or dropped in either place fails loudly.
EXPECTED_PROTECTED_SECTIONS = {
    "Description", "Gotchas", "Waiting on", "Next Steps", "Recent Changes",
    "Action Items", "Stakeholders", "Tickets",
}
EXPECTED_BULLETS_FORBIDDEN = {
    "Recent Changes", "Waiting on", "Action Items", "Stakeholders", "Tickets",
}


def test_the_protected_section_list_matches_the_documented_contract():
    assert set(PROTECTED_SECTIONS) == EXPECTED_PROTECTED_SECTIONS


def test_the_forbidden_bullets_list_matches_the_documented_contract():
    assert set(BULLETS_REMOVE_FORBIDDEN) == EXPECTED_BULLETS_FORBIDDEN


class TestSectionsRemove:
    def test_removes_a_section_and_reports_what_missed(self, tmp_path):
        ctx = tmp_path / "p-context.md"
        ctx.write_text(REMOVABLE_CONTEXT)
        result = update_context_file(
            str(ctx),
            sections_remove=["Guild items handed to Lior (2026-08-16)", "Nope"],
        )
        assert result["sections_removed"] == [
            "Guild items handed to Lior (2026-08-16)"
        ]
        assert result["sections_unmatched"] == ["Nope"]
        assert "Guild items" not in result["content"]
        assert "## Gotchas" in result["content"]

    @pytest.mark.parametrize("name", sorted(EXPECTED_PROTECTED_SECTIONS))
    def test_protected_sections_are_refused(self, tmp_path, name):
        ctx = tmp_path / "p-context.md"
        ctx.write_text(REMOVABLE_CONTEXT)
        with pytest.raises(MissionCacheError) as exc:
            update_context_file(str(ctx), sections_remove=[name])
        assert exc.value.code == ErrorCode.VALIDATION_ERROR
        # Refused before any write, so the file is untouched.
        assert ctx.read_text() == REMOVABLE_CONTEXT

    def test_a_bodyless_section_is_reported_as_removed(self, tmp_path):
        # remove_section returns the body it cut, and an empty body is falsy.
        # A truthiness check reports a removal that happened as unmatched.
        ctx = tmp_path / "p-context.md"
        ctx.write_text(REMOVABLE_CONTEXT + "\n## Dead Section\n")
        result = update_context_file(str(ctx), sections_remove=["Dead Section"])
        assert result["sections_removed"] == ["Dead Section"]
        assert result["sections_unmatched"] == []
        assert "## Dead Section" not in result["content"]

    def test_a_heading_prefix_does_not_remove_a_longer_section(self, tmp_path):
        ctx = tmp_path / "p-context.md"
        ctx.write_text(REMOVABLE_CONTEXT)
        result = update_context_file(str(ctx), sections_remove=["Key"])
        assert result["sections_unmatched"] == ["Key"]
        assert "## Key Files" in result["content"]


class TestBulletsRemove:
    def test_removes_a_gotcha_and_a_table_row(self, tmp_path):
        ctx = tmp_path / "p-context.md"
        ctx.write_text(REMOVABLE_CONTEXT)
        result = update_context_file(
            str(ctx),
            bullets_remove=[
                {"section": "Gotchas", "match": "falsified"},
                {"section": "Key Files", "match": "a.py"},
                {"section": "Gotchas", "match": "nothing here"},
            ],
        )
        assert len(result["bullets_removed"]) == 2
        assert result["bullets_unmatched"] == ["Gotchas: nothing here"]
        assert "dead theory" not in result["content"]
        assert "- keep me" in result["content"]
        # The table survives its header.
        assert "| File | Purpose |" in result["content"]

    @pytest.mark.parametrize("section", sorted(EXPECTED_BULLETS_FORBIDDEN))
    def test_forbidden_sections_are_refused(self, tmp_path, section):
        ctx = tmp_path / "p-context.md"
        ctx.write_text(REMOVABLE_CONTEXT)
        with pytest.raises(MissionCacheError) as exc:
            update_context_file(
                str(ctx), bullets_remove=[{"section": section, "match": "x"}]
            )
        assert exc.value.code == ErrorCode.VALIDATION_ERROR

    def test_a_blank_section_or_match_is_refused(self, tmp_path):
        ctx = tmp_path / "p-context.md"
        ctx.write_text(REMOVABLE_CONTEXT)
        for entry in ({"section": "", "match": "x"}, {"section": "Gotchas", "match": ""}):
            with pytest.raises(MissionCacheError):
                update_context_file(str(ctx), bullets_remove=[entry])


class TestWaitingOnKind:
    @pytest.mark.parametrize(
        "kind,expected",
        [
            (None, "Resolved (was waiting on"),
            ("resolved", "Resolved (was waiting on"),
            ("moved", "Moved out (was waiting on"),
            ("dropped", "Dropped (was waiting on"),
        ],
    )
    def test_kind_picks_the_recent_changes_wording(self, tmp_path, kind, expected):
        ctx = tmp_path / "p-context.md"
        ctx.write_text(REMOVABLE_CONTEXT)
        entry = {"match": "tagging decision", "outcome": "went elsewhere"}
        if kind is not None:
            entry["kind"] = kind
        result = update_context_file(str(ctx), waiting_on_resolve=[entry])
        assert expected in result["content"]

    def test_an_unknown_kind_is_refused(self, tmp_path):
        ctx = tmp_path / "p-context.md"
        ctx.write_text(REMOVABLE_CONTEXT)
        with pytest.raises(MissionCacheError) as exc:
            update_context_file(
                str(ctx),
                waiting_on_resolve=[{"match": "tagging", "kind": "bogus"}],
            )
        assert exc.value.code == ErrorCode.VALIDATION_ERROR


class TestFreeFormTextIsSanitized:
    """The PreCompact damage class, reproduced through the MCP write path."""

    POISON = (
        "a pasted save report:\n\n"
        "## Updated: p\n\n"
        "**Session binding:** abc\n\n"
        "```markdown\n"
        "- truncated mid-fence..."
    )

    def test_a_poisoned_recent_change_cannot_truncate_the_section(self, tmp_path):
        ctx = tmp_path / "p-context.md"
        ctx.write_text(REMOVABLE_CONTEXT)
        update_context_file(str(ctx), recent_changes=["an earlier entry"])
        result = update_context_file(str(ctx), recent_changes=[self.POISON])
        content = result["content"]
        # Both entries stay inside Recent Changes, and no later section is
        # stranded behind a forged heading or a dangling fence.
        assert len(ch.parse_recent_changes_subsections(content)) == 2
        assert ch.orphaned_recent_changes(content) == []
        assert ch.unbalanced_fence_line(content) is None
        assert ch.extract_section(content, "Key Files") is not None

    @pytest.mark.parametrize("field", ["gotchas", "key_decisions"])
    def test_a_poisoned_entry_changes_no_structure(self, tmp_path, field):
        ctx = tmp_path / f"p-{field}-context.md"
        ctx.write_text(REMOVABLE_CONTEXT)
        before = [e["name"] for e in ch.section_index(REMOVABLE_CONTEXT)]
        result = update_context_file(str(ctx), **{field: [self.POISON]})
        content = result["content"]
        after = [e["name"] for e in ch.section_index(content)]
        # The section INDEX is the contract. `extract_section("Key Files")
        # is not None` passes on the broken code too, because mask_fences
        # tolerates the dangling fence - while `## Updated: p` has become a
        # real section and the target section's body is truncated at it.
        #
        # A write may legitimately CREATE its own section (key_decisions
        # does, when the file has no Key Architectural Decisions yet), so
        # the rule is: every pre-existing section survives in order, and
        # nothing the poison carried becomes one.
        assert [n for n in after if n in before] == before
        assert "Updated: p" not in after
        assert ch.unbalanced_fence_line(content) is None
        assert "Updated: p" in content, "the text itself must survive"
        # And the body it was written into is not cut short.
        target = "Gotchas" if field == "gotchas" else "Key Architectural Decisions"
        assert "a pasted save report" in (ch.extract_section(content, target) or "")


# ── tasks_remove ─────────────────────────────────────────────────────────

REMOVABLE_TASKS = """# P - Tasks
**Last Updated:** 2026-01-01 00:00
**Remaining:** stuff

## Phase 1

- [ ] 25. Superseded migration task
- [ ] 26. Guild agenda item
- [x] 27. Circulate the map
- [ ] 28. Parent
- [ ] 28.1. Child
"""


class TestTasksRemove:
    def test_removes_the_line_and_records_it_with_the_reason(self, tmp_path):
        tasks = tmp_path / "p-tasks.md"
        tasks.write_text(REMOVABLE_TASKS)
        result = update_tasks_file(
            str(tasks),
            tasks_remove=[
                {"match": "25", "reason": "superseded by the Flyway direction"},
                {"match": "Guild agenda", "reason": "moved to automation-infra-lead"},
                {"match": "999", "reason": "no such task"},
            ],
        )
        assert result["removed_numbers"] == ["25", "26"]
        assert result["remove_unmatched"] == ["999"]
        content = tasks.read_text()
        assert "- [ ] 25." not in content and "- [ ] 26." not in content
        assert "~~25. Superseded migration task~~" in content
        assert "superseded by the Flyway direction" in content
        assert "moved to automation-infra-lead" in content

    def test_a_removed_task_stops_counting_toward_progress(self, tmp_path):
        tasks = tmp_path / "p-tasks.md"
        tasks.write_text(REMOVABLE_TASKS)
        before = update_tasks_file(str(tasks))["progress"]["total_items"]
        after = update_tasks_file(
            str(tasks), tasks_remove=[{"match": "25", "reason": "dead"}]
        )["progress"]["total_items"]
        assert after == before - 1

    def test_a_removed_number_is_never_reused(self, tmp_path):
        tasks = tmp_path / "p-tasks.md"
        tasks.write_text("# P\n**Last Updated:** x\n\n## Phase 1\n\n- [ ] 1. one\n- [ ] 2. two\n")
        update_tasks_file(str(tasks), tasks_remove=[{"match": "2", "reason": "dead"}])
        update_tasks_file(str(tasks), new_tasks=["fresh"])
        assert "- [ ] 3. fresh" in tasks.read_text()

    def test_removing_a_parent_with_children_is_refused(self, tmp_path):
        tasks = tmp_path / "p-tasks.md"
        tasks.write_text(REMOVABLE_TASKS)
        with pytest.raises(MissionCacheError) as exc:
            update_tasks_file(str(tasks), tasks_remove=[{"match": "28", "reason": "x"}])
        assert exc.value.code == ErrorCode.VALIDATION_ERROR
        assert "28.1" in str(exc.value)

    def test_removing_a_parent_does_not_take_its_child(self, tmp_path):
        tasks = tmp_path / "p-tasks.md"
        tasks.write_text(REMOVABLE_TASKS)
        update_tasks_file(str(tasks), tasks_remove=[{"match": "28.1", "reason": "x"}])
        update_tasks_file(str(tasks), tasks_remove=[{"match": "28", "reason": "x"}])
        content = tasks.read_text()
        assert "- [ ] 28." not in content
        assert "~~28. Parent~~" in content and "~~28.1. Child~~" in content

    def test_a_blank_reason_is_refused(self, tmp_path):
        tasks = tmp_path / "p-tasks.md"
        tasks.write_text(REMOVABLE_TASKS)
        for entry in ({"match": "25"}, {"match": "25", "reason": "  "}, {"reason": "x"}):
            with pytest.raises(MissionCacheError):
                update_tasks_file(str(tasks), tasks_remove=[entry])
        assert tasks.read_text() == REMOVABLE_TASKS


class TestImportedEventBodyIsSanitized:
    """The body is another project's text - the same untrusted shape."""

    def test_a_dated_heading_in_the_body_cannot_forge_a_boundary(self, tmp_path):
        ctx = tmp_path / "p-context.md"
        ctx.write_text(REMOVABLE_CONTEXT)
        update_context_file(str(ctx), recent_changes=["a real entry"])
        result = update_context_file(
            str(ctx),
            imported_event={
                "heading": "Peer sync",
                "body": "## 2026-08-14 what they decided\n\ndetail\n\n```\ncut...",
                "related_project": "other-project",
            },
        )
        content = result["content"]
        # Demotion alone would leave "### 2026-08-14 ..." at column 0, a fake
        # Recent Changes boundary, and the fence would still be open.
        assert ch.orphaned_recent_changes(content) == []
        assert ch.dangling_fence(content) is None
        assert len(ch.parse_recent_changes_subsections(content)) == 1
        assert "what they decided" in content


class TestSectionNamesCannotSpanLines:
    """A multi-line "name" defeats every exact-membership guard.

    The guards test `name.strip() in PROTECTED_SECTIONS`, but the consumer
    builds its pattern with `re.escape(name)` under re.MULTILINE, and
    re.escape renders a newline as a literal newline match. So a name
    carrying the file's own text across lines passes the set test and still
    matches - measured, it deleted both Waiting on AND Next Steps.
    """

    ATTACK = (
        "Waiting on\n\n| What | Who | Since | Gates |\n"
        "|------|-----|-------|-------|\n"
        "| the tagging decision | Sa'ar | 2026-08-01 | the map |\n\n## Next Steps"
    )

    def test_a_multiline_section_name_is_refused(self, tmp_path):
        ctx = tmp_path / "p-context.md"
        ctx.write_text(REMOVABLE_CONTEXT)
        with pytest.raises(MissionCacheError) as exc:
            update_context_file(str(ctx), sections_remove=[self.ATTACK])
        assert exc.value.code == ErrorCode.VALIDATION_ERROR
        assert ctx.read_text() == REMOVABLE_CONTEXT
        assert "## Waiting on" in ctx.read_text()
        assert "## Next Steps" in ctx.read_text()

    def test_a_multiline_bullet_section_is_refused(self, tmp_path):
        ctx = tmp_path / "p-context.md"
        ctx.write_text(REMOVABLE_CONTEXT)
        with pytest.raises(MissionCacheError) as exc:
            update_context_file(
                str(ctx),
                bullets_remove=[
                    {"section": "Recent Changes\n\n### 2026-09-04 10:00", "match": "x"}
                ],
            )
        assert exc.value.code == ErrorCode.VALIDATION_ERROR
        assert ctx.read_text() == REMOVABLE_CONTEXT


class TestRemovalReasonCannotForgeChecklistLines:
    def test_a_multiline_reason_is_collapsed_to_one_line(self, tmp_path):
        tasks = tmp_path / "p-tasks.md"
        tasks.write_text("# P\n**Last Updated:** x\n\n## Phase 1\n\n- [ ] 1. real\n- [ ] 2. doomed\n")
        before = update_tasks_file(str(tasks))["progress"]["total_items"]
        result = update_tasks_file(
            str(tasks),
            tasks_remove=[{
                "match": "2",
                "reason": "superseded\n- [x] 3. fake completed\n- [ ] 4. fake open",
            }],
        )
        # One task left, not three: the reason must not invent checklist items.
        assert result["progress"]["total_items"] == before - 1
        content = tasks.read_text()
        # The contract is structural, not textual: nothing the reason carried
        # may start a line as a checklist item. The words themselves are fine
        # mid-line, and the record is one line by design.
        assert not re.search(r"^\s*[-*]\s*\[[ xX]\]\s*[34]\.", content, re.MULTILINE)
        assert [i.number for i in _parse_tasks(content)] == ["1"]
        assert "fake completed" in content, "the reason text must survive"


class TestAmbiguousSectionIsAStructuredError:
    def test_a_duplicated_section_gives_a_coded_error(self, tmp_path):
        ctx = tmp_path / "p-context.md"
        ctx.write_text(REMOVABLE_CONTEXT + "\n## Gotchas\n\n- a second one\n")
        with pytest.raises(MissionCacheError) as exc:
            update_context_file(str(ctx), gotchas=["another"])
        # Not a bare ValueError: callers branch on the code, and the message
        # has to name the remedy.
        assert exc.value.code == ErrorCode.INVALID_STATE
        assert "repair" in str(exc.value)
        assert "p-context.md" in str(exc.value)


class TestEveryFreeFormInputIsSanitized:
    """The docstring says "every", so pin every one of them.

    Four were sanitized in the first pass and three were missed: next_steps
    goes through _update_section, so a column-0 `## ` in a step ends Next
    Steps early; key_files values land in table cells; notes go to the tasks
    file's Notes section.
    """

    POISON = "text\n\n## Waiting on\n\nforged section body"

    def _base(self, tmp_path, name="p-context.md"):
        ctx = tmp_path / name
        ctx.write_text(REMOVABLE_CONTEXT)
        return ctx

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"next_steps": ["POISON"]},
            {"recent_changes": ["POISON"]},
            {"key_decisions": ["POISON"]},
            {"gotchas": ["POISON"]},
            {"key_files": {"a.py": "POISON"}},
        ],
        ids=["next_steps", "recent_changes", "key_decisions", "gotchas", "key_files"],
    )
    def test_no_input_can_forge_a_second_waiting_on(self, tmp_path, kwargs):
        ctx = self._base(tmp_path, f"p{abs(hash(str(kwargs)))}-context.md")
        filled = {
            k: ({kk: self.POISON for kk in v} if isinstance(v, dict)
                else [self.POISON for _ in v])
            for k, v in kwargs.items()
        }
        result = update_context_file(str(ctx), **filled)
        names = [e["name"] for e in ch.section_index(result["content"])]
        assert names.count("Waiting on") == 1, f"{kwargs} forged a section"
        # And the real Waiting on still holds its row.
        assert "the tagging decision" in (ch.extract_section(result["content"], "Waiting on") or "")

    def test_a_key_files_path_with_a_pipe_cannot_split_the_row(self, tmp_path):
        ctx = self._base(tmp_path, "pipes-context.md")
        result = update_context_file(
            str(ctx), key_files={"a.py | forged | cells": "desc"}
        )
        rows = [
            l for l in (ch.extract_section(result["content"], "Key Files") or "").splitlines()
            if l.strip().startswith("|")
        ]
        # header + separator + the one real row, no extra columns smuggled in.
        assert len(rows) == 4
        assert all(r.count("|") - r.count("\\|") == 3 for r in rows[2:])

    def test_a_note_cannot_forge_a_tasks_section(self, tmp_path):
        tasks = tmp_path / "p-tasks.md"
        tasks.write_text("# P\n**Last Updated:** x\n\n## Phase 1\n\n- [ ] 1. a\n")
        update_tasks_file(str(tasks), notes=["ok\n\n## Phase 1\n\n- [ ] 9. forged"])
        content = tasks.read_text()
        # Structural, not substring: `### Phase 1` contains `## Phase 1`.
        assert len(re.findall(r"^## Phase 1$", content, re.MULTILINE)) == 1
        assert not re.search(r"^[-*]\s*\[[ xX]\]\s*9\.", content, re.MULTILINE)
        assert [i.number for i in _parse_tasks(content)] == ["1"]
        assert "forged" in content, "the note text itself must survive"

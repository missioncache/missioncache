"""Integration tests for orbit file create/update operations.

Tests use tmp_path for all file I/O and monkeypatch to redirect orbit_root.
"""

import re

import pytest

from mcp_orbit.config import Settings
from mcp_orbit.errors import ErrorCode, OrbitError, OrbitFileNotFoundError
from mcp_orbit.orbit import (
    create_orbit_files,
    get_orbit_files,
    parse_task_progress,
    update_context_file,
    update_tasks_file,
)


@pytest.fixture(autouse=True)
def _redirect_orbit_root(tmp_path, monkeypatch):
    """Point orbit_root to tmp_path so file operations don't touch real filesystem."""
    test_settings = Settings(orbit_root=tmp_path / "orbit")
    monkeypatch.setattr("mcp_orbit.orbit.settings", test_settings)


# ── create_orbit_files ────────────────────────────────────────────────────


class TestCreateOrbitFiles:
    def test_creates_three_files(self, tmp_path):
        """create_orbit_files produces plan, context, and tasks files."""
        result = create_orbit_files(
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
        result = create_orbit_files(
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
        create_orbit_files(task_name="dup-task", tasks=["one"])

        with pytest.raises(OrbitError) as excinfo:
            create_orbit_files(task_name="dup-task", tasks=["two"])

        assert excinfo.value.code == ErrorCode.ALREADY_EXISTS
        assert "dup-task" in excinfo.value.message
        assert "existing_files" in excinfo.value.details

    def test_duplicate_preserves_original_files(self, tmp_path):
        """The ALREADY_EXISTS guard runs BEFORE any write, so files are intact."""
        first = create_orbit_files(task_name="preserve-task", tasks=["original"])

        from pathlib import Path

        original_tasks = Path(first.tasks_file).read_text()

        with pytest.raises(OrbitError):
            create_orbit_files(task_name="preserve-task", tasks=["clobber"])

        assert Path(first.tasks_file).read_text() == original_tasks
        assert "original" in original_tasks
        assert "clobber" not in original_tasks

    def test_force_overwrites_existing(self, tmp_path):
        """force=True bypasses the ALREADY_EXISTS guard and rewrites files."""
        from pathlib import Path

        first = create_orbit_files(task_name="force-task", tasks=["v1"])
        original = Path(first.tasks_file).read_text()
        assert "v1" in original

        second = create_orbit_files(
            task_name="force-task", tasks=["v2"], force=True
        )
        rewritten = Path(second.tasks_file).read_text()
        assert "v2" in rewritten
        assert "v1" not in rewritten

    def test_guard_catches_legacy_unprefixed_filenames(self, tmp_path):
        """ALREADY_EXISTS fires when the dir has only legacy unprefixed files.

        get_orbit_files reads both prefixed and legacy names; the guard
        must check both, otherwise fresh prefixed files would shadow
        existing legacy content at read time.
        """
        from mcp_orbit.orbit import get_task_dir

        task_dir = get_task_dir("legacy-task")
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "plan.md").write_text("# legacy plan content")
        (task_dir / "context.md").write_text("# legacy context content")
        (task_dir / "tasks.md").write_text("- [ ] legacy task")

        with pytest.raises(OrbitError) as excinfo:
            create_orbit_files(task_name="legacy-task", tasks=["new"])

        assert excinfo.value.code == ErrorCode.ALREADY_EXISTS


# ── get_orbit_files ──────────────────────────────────────────────────────


class TestGetOrbitFiles:
    def test_finds_files_in_active_dir(self, tmp_path):
        create_orbit_files(task_name="active-task", tasks=["x"])

        result = get_orbit_files("active-task")

        assert result.plan_file is not None
        assert result.context_file is not None
        assert result.tasks_file is not None
        assert "active/active-task" in result.task_dir

    def test_finds_files_in_completed_dir(self, tmp_path):
        """When a project is archived to completed/, get_orbit_files finds it.

        Reproduces MAJOR-10 from the QA report - a fresh /orbit:go on a
        completed project used to report has_orbit_files=False because the
        lookup only scanned active/.
        """
        from mcp_orbit.orbit import settings

        create_orbit_files(task_name="archived-task", tasks=["done"])

        active_dir = settings.orbit_root / "active" / "archived-task"
        completed_dir = settings.orbit_root / "completed" / "archived-task"
        completed_dir.parent.mkdir(parents=True, exist_ok=True)
        active_dir.rename(completed_dir)
        assert not active_dir.exists()
        assert completed_dir.exists()

        result = get_orbit_files("archived-task")

        assert result.plan_file is not None
        assert result.context_file is not None
        assert result.tasks_file is not None
        assert "completed/archived-task" in result.task_dir

    def test_returns_empty_paths_when_nothing_exists(self, tmp_path):
        result = get_orbit_files("nonexistent-task")

        assert result.plan_file is None
        assert result.context_file is None
        assert result.tasks_file is None

    def test_active_takes_priority_over_completed(self, tmp_path):
        """If a project exists in both active/ AND completed/ (e.g., reopened
        without deleting the archived copy), the active version wins."""
        from mcp_orbit.orbit import settings

        create_orbit_files(task_name="dual-task", tasks=["active-version"])

        completed_dir = settings.orbit_root / "completed" / "dual-task"
        completed_dir.mkdir(parents=True, exist_ok=True)
        (completed_dir / "dual-task-tasks.md").write_text("completed-version")

        result = get_orbit_files("dual-task")

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

        updated = update_context_file(str(ctx_file))
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
        )

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
        task complete must all land. Mirrors the orbit-auto parallel path
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

"""File I/O tests for statusline helper functions.

Tests use tmp_path and monkeypatch to isolate filesystem operations.
"""

import json
import pathlib
import time

import pytest

import orbit_dashboard.statusline as mod


# ── is_version_reviewed ──────────────────────────────────────────────────


class TestIsVersionReviewed:
    def test_matching_version_returns_true(self, tmp_path, monkeypatch):
        """Returns True when the cached version matches the queried version."""
        reviewed_file = tmp_path / "whats-new-version"
        reviewed_file.write_text("1.2.3")

        monkeypatch.setattr(
            mod, "is_version_reviewed",
            lambda v: reviewed_file.exists() and reviewed_file.read_text().strip() == v,
        )
        # Test the real function by constructing the file where it looks
        cache_dir = tmp_path / ".claude" / "cache"
        cache_dir.mkdir(parents=True)
        (cache_dir / "whats-new-version").write_text("1.2.3")

        monkeypatch.undo()  # remove lambda patch

        # Patch Path.home at the pathlib level so the function's
        # Path.home() / ".claude" / "cache" / "whats-new-version" resolves to tmp_path
        monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: tmp_path))

        assert mod.is_version_reviewed("1.2.3") is True

    def test_different_version_returns_false(self, tmp_path, monkeypatch):
        """Returns False when the cached version differs from the queried version."""
        cache_dir = tmp_path / ".claude" / "cache"
        cache_dir.mkdir(parents=True)
        (cache_dir / "whats-new-version").write_text("1.2.3")

        monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: tmp_path))

        assert mod.is_version_reviewed("2.0.0") is False

    def test_missing_file_returns_false(self, tmp_path, monkeypatch):
        """Returns False when the version cache file doesn't exist."""
        monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: tmp_path))

        assert mod.is_version_reviewed("1.0.0") is False


# ── get_health_status caching ────────────────────────────────────────────


class TestGetHealthStatusCache:
    def test_fresh_cache_returns_cached_incidents(self, tmp_path, monkeypatch):
        """When cache is fresh (within TTL), returns cached incidents without HTTP."""
        cache_file = tmp_path / "health-cache.json"
        cached_data = {
            "timestamp": time.time(),  # fresh
            "incidents": [{"service": "OK"}],
        }
        cache_file.write_text(json.dumps(cached_data))

        monkeypatch.setattr(mod, "HEALTH_CACHE", cache_file)

        result = mod.get_health_status()
        assert result == [{"service": "OK"}]

    def test_expired_cache_not_returned(self, tmp_path, monkeypatch):
        """When cache is expired, the function does NOT return stale data.

        It attempts an HTTP fetch (which we let fail), falling back to [{"service": "OK"}].
        """
        cache_file = tmp_path / "health-cache.json"
        stale_data = {
            "timestamp": time.time() - mod.HEALTH_TTL - 100,  # expired
            "incidents": [{"service": "Code", "name": "Stale incident"}],
        }
        cache_file.write_text(json.dumps(stale_data))

        monkeypatch.setattr(mod, "HEALTH_CACHE", cache_file)
        # Patch urlopen to raise so we don't make real HTTP calls
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *a, **kw: (_ for _ in ()).throw(Exception("no network")),
        )

        result = mod.get_health_status()
        # Should NOT contain the stale incident
        assert result == [{"service": "OK"}]


# ── _read_tasks_content ───────────────────────────────────────────────────


class TestReadTasksContent:
    def test_reads_real_tasks_file(self, tmp_path):
        """Reads the tasks.md file; parses to the expected fraction."""
        project_dir = tmp_path / "my-project"
        project_dir.mkdir()
        (project_dir / "my-project-tasks.md").write_text(
            "- [x] 1. done\n"
            "- [x] 2. done\n"
            "- [ ] 3. todo\n"
            "- [ ] 4. todo\n"
            "- [ ] 5. todo\n"
        )
        content = mod._read_tasks_content(project_dir, "my-project")

        assert mod._parse_task_progress(content) == "[2/5]"

    def test_template_placeholder_returns_tbd(self, tmp_path):
        """A fresh project with only the template placeholder parses to [TBD]."""
        project_dir = tmp_path / "fresh-project"
        project_dir.mkdir()
        (project_dir / "fresh-project-tasks.md").write_text("- [ ] TBD\n")
        content = mod._read_tasks_content(project_dir, "fresh-project")

        assert mod._parse_task_progress(content) == "[TBD]"

    def test_missing_file_returns_empty(self, tmp_path):
        """Missing tasks file returns empty content (statusline falls back)."""
        project_dir = tmp_path / "nonexistent"
        # Do NOT create the directory or file.

        assert mod._read_tasks_content(project_dir, "nonexistent") == ""

    def test_unreadable_path_returns_empty(self, tmp_path):
        """An OSError while reading returns empty (defensive fallback)."""
        # Point the helper at a directory where the "tasks file" is itself a
        # directory - read_text() raises OSError (IsADirectoryError).
        project_dir = tmp_path / "weird"
        project_dir.mkdir()
        (project_dir / "weird-tasks.md").mkdir()  # collision

        assert mod._read_tasks_content(project_dir, "weird") == ""


# ── _get_active_task (reads orbit active-task pointer) ────


class TestGetActiveTask:
    """Reads ``~/.claude/hooks/state/active-orbit-task/<session>.json``.

    The pointer is written by the ``set_active_orbit_tasks`` MCP tool and
    holds the orbit checklist task numbers currently in progress. The
    statusline composes ``_read_active_task_pointer`` and
    ``_format_active_task`` via ``_get_active_task``; tests cover both the
    raw pointer read and the per-shape display formatting.
    """

    PROJECT = "orbit-public-release"
    TASKS_MD = (
        "- [ ] 8. Draft Show HN post\n"
        "- [ ] 54. M11.2 - Per-tool hooks tracker\n"
        "  - [ ] 54a. M11.2 - VSCode statusline extension\n"
        "  - [ ] 54b. M11.2 - OpenCode TS plugin\n"
        "  - [ ] 54c. M11.2 - Codex hooks\n"
        "- [ ] 56. Verify data-preservation contract\n"
        "- [ ] 57. macOS dashboard-as-app opt-in\n"
    )

    def _write_pointer(
        self, tmp_path, session_id, project_name, task_numbers
    ):
        pdir = tmp_path / ".claude" / "hooks" / "state" / "active-orbit-task"
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / f"{session_id}.json").write_text(
            json.dumps(
                {
                    "project_name": project_name,
                    "task_numbers": task_numbers,
                    "updated": "2026-04-28T00:00:00+00:00",
                }
            )
        )

    def test_single_task_renders_number_and_text(self, tmp_path, monkeypatch):
        """One active task: ``<number>. <text>``."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        self._write_pointer(tmp_path, "sess-1", self.PROJECT, ["54a"])

        assert mod._get_active_task("sess-1", self.TASKS_MD, self.PROJECT) == (
            "54a. M11.2 - VSCode statusline extension"
        )

    def test_unknown_number_falls_back_to_just_number(self, tmp_path, monkeypatch):
        """Pointer references a number not in tasks.md -> render bare number.

        Defensive: if tasks.md was edited and the pointer wasn't refreshed, we
        still surface SOMETHING rather than hide the field. Caller can fix
        the pointer via ``set_active_orbit_tasks`` or ``clear_active_orbit_tasks``.
        """
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        self._write_pointer(tmp_path, "sess-2", self.PROJECT, ["999"])

        assert mod._get_active_task("sess-2", self.TASKS_MD, self.PROJECT) == "999"

    def test_three_siblings_collapse_to_parent_text(self, tmp_path, monkeypatch):
        """``["54a","54b","54c"]`` collapses to parent 54's text + count."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        self._write_pointer(
            tmp_path, "sess-3", self.PROJECT, ["54a", "54b", "54c"]
        )

        assert mod._get_active_task("sess-3", self.TASKS_MD, self.PROJECT) == (
            "M11.2 - Per-tool hooks tracker (3 active)"
        )

    def test_two_siblings_collapse_to_parent_text(self, tmp_path, monkeypatch):
        """Boundary case: 2 siblings sharing a parent collapse the same way as 3."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        self._write_pointer(tmp_path, "sess-3b", self.PROJECT, ["54a", "54b"])

        assert mod._get_active_task("sess-3b", self.TASKS_MD, self.PROJECT) == (
            "M11.2 - Per-tool hooks tracker (2 active)"
        )

    def test_two_unrelated_tasks_render_as_number_list(self, tmp_path, monkeypatch):
        """No common parent -> comma-separated numbers."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        self._write_pointer(tmp_path, "sess-4", self.PROJECT, ["54a", "56"])

        assert mod._get_active_task("sess-4", self.TASKS_MD, self.PROJECT) == (
            "54a, 56"
        )

    def test_three_unrelated_tasks_render_as_number_list(self, tmp_path, monkeypatch):
        """Three with no common parent -> all three as a comma list."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        self._write_pointer(tmp_path, "sess-5", self.PROJECT, ["54a", "56", "57"])

        assert mod._get_active_task("sess-5", self.TASKS_MD, self.PROJECT) == (
            "54a, 56, 57"
        )

    def test_four_plus_truncates_with_overflow_count(self, tmp_path, monkeypatch):
        """4+ active -> first 3 + ``(+N)``."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        self._write_pointer(
            tmp_path, "sess-6", self.PROJECT, ["54a", "56", "57", "8"]
        )

        assert mod._get_active_task("sess-6", self.TASKS_MD, self.PROJECT) == (
            "54a, 56, 57 (+1)"
        )

    def test_empty_pointer_hides_field(self, tmp_path, monkeypatch):
        """Pointer file with empty task_numbers -> ``""`` so caller hides field."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        self._write_pointer(tmp_path, "sess-7", self.PROJECT, [])

        assert mod._get_active_task("sess-7", self.TASKS_MD, self.PROJECT) == ""

    def test_missing_pointer_hides_field(self, tmp_path, monkeypatch):
        """No pointer file -> ``""``."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        assert mod._get_active_task("never-set", self.TASKS_MD, self.PROJECT) == ""

    def test_empty_session_id_short_circuits(self, tmp_path, monkeypatch):
        """Empty session id never touches disk."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        assert mod._get_active_task("", self.TASKS_MD, self.PROJECT) == ""

    def test_corrupt_pointer_json_hides_field(self, tmp_path, monkeypatch):
        """Malformed pointer JSON is treated as missing, not crashed on."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        pdir = tmp_path / ".claude" / "hooks" / "state" / "active-orbit-task"
        pdir.mkdir(parents=True)
        (pdir / "sess-8.json").write_text("not valid json")

        assert mod._get_active_task("sess-8", self.TASKS_MD, self.PROJECT) == ""

    def test_per_session_isolation(self, tmp_path, monkeypatch):
        """Concurrent sessions don't see each other's pointer."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        self._write_pointer(tmp_path, "sess-A", self.PROJECT, ["54a"])
        self._write_pointer(tmp_path, "sess-B", self.PROJECT, ["56"])

        assert mod._get_active_task("sess-A", self.TASKS_MD, self.PROJECT) == (
            "54a. M11.2 - VSCode statusline extension"
        )
        assert mod._get_active_task("sess-B", self.TASKS_MD, self.PROJECT) == (
            "56. Verify data-preservation contract"
        )

    def test_pointer_from_other_project_is_suppressed(self, tmp_path, monkeypatch):
        """Switching projects in the same session must not render the prior
        project's task numbers against the new project's tasks.md.

        Pointers are keyed by session_id alone. Without a project_name guard,
        the prior project's task_numbers would be looked up in the new
        project's tasks.md - showing the wrong line's text if the number
        coincidentally exists, or a bare misleading number if it doesn't.
        """
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        # Pointer says we're working on 54a in project-a.
        self._write_pointer(tmp_path, "sess-X", "project-a", ["54a"])

        # Now the session is rendering for project-b (same TASKS_MD body, but
        # the project context differs). The Task field must hide.
        assert mod._get_active_task("sess-X", self.TASKS_MD, "project-b") == ""

        # Sanity check: passing the matching project_name still renders.
        assert mod._get_active_task("sess-X", self.TASKS_MD, "project-a") == (
            "54a. M11.2 - VSCode statusline extension"
        )

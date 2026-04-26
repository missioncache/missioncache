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


# ── _get_active_task (reads Claude Code's ~/.claude/tasks/<session>/) ────


class TestGetActiveTask:
    """Reads Claude Code's per-session task list from ~/.claude/tasks/.

    Each task is a separate JSON file under the session directory. Statusline
    picks the first task with status='in_progress' and prefers activeForm
    over subject for natural-sounding spinner-style display.
    """

    def _write_task(
        self,
        tmp_path,
        session_id,
        task_id,
        status,
        subject="task subject",
        active_form=None,
    ):
        task_dir = tmp_path / ".claude" / "tasks" / session_id
        task_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "id": task_id,
            "subject": subject,
            "status": status,
            "blocks": [],
            "blockedBy": [],
        }
        if active_form is not None:
            payload["activeForm"] = active_form
        (task_dir / f"{task_id}.json").write_text(json.dumps(payload))

    def test_returns_active_form_when_in_progress(self, tmp_path, monkeypatch):
        """activeForm wins over subject when both are present."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        self._write_task(
            tmp_path, "sess-1", "1", "in_progress",
            subject="Fix the auth bug", active_form="Fixing the auth bug"
        )

        assert mod._get_active_task("sess-1") == "Fixing the auth bug"

    def test_falls_back_to_subject_without_active_form(self, tmp_path, monkeypatch):
        """When activeForm is absent, subject is returned."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        self._write_task(
            tmp_path, "sess-2", "1", "in_progress",
            subject="Fix the auth bug", active_form=None
        )

        assert mod._get_active_task("sess-2") == "Fix the auth bug"

    def test_skips_pending_and_completed_tasks(self, tmp_path, monkeypatch):
        """Only in_progress tasks count. Completed and pending are ignored."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        self._write_task(tmp_path, "sess-3", "1", "completed", subject="finished")
        self._write_task(tmp_path, "sess-3", "2", "pending", subject="not started")
        self._write_task(
            tmp_path, "sess-3", "3", "in_progress", subject="active one"
        )

        assert mod._get_active_task("sess-3") == "active one"

    def test_returns_empty_when_no_in_progress(self, tmp_path, monkeypatch):
        """Tasks exist but none in_progress -> empty."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        self._write_task(tmp_path, "sess-4", "1", "completed")
        self._write_task(tmp_path, "sess-4", "2", "pending")

        assert mod._get_active_task("sess-4") == ""

    def test_returns_empty_for_missing_session_id(self, tmp_path, monkeypatch):
        """Empty session_id short-circuits to empty without touching disk."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        assert mod._get_active_task("") == ""

    def test_returns_empty_when_session_dir_missing(self, tmp_path, monkeypatch):
        """No session dir means no tasks."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        assert mod._get_active_task("never-recorded-session") == ""

    def test_corrupt_json_in_one_file_does_not_break_others(
        self, tmp_path, monkeypatch
    ):
        """A malformed task file is skipped; remaining tasks still scanned."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        task_dir = tmp_path / ".claude" / "tasks" / "sess-5"
        task_dir.mkdir(parents=True)
        (task_dir / "1.json").write_text("not valid json")
        self._write_task(
            tmp_path, "sess-5", "2", "in_progress", subject="real one"
        )

        assert mod._get_active_task("sess-5") == "real one"

    def test_per_session_isolation(self, tmp_path, monkeypatch):
        """Each session's read sees only its own directory."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        self._write_task(
            tmp_path, "sess-A", "1", "in_progress", subject="A's work"
        )
        self._write_task(
            tmp_path, "sess-B", "1", "in_progress", subject="B's work"
        )

        assert mod._get_active_task("sess-A") == "A's work"
        assert mod._get_active_task("sess-B") == "B's work"

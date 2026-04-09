"""File I/O tests for statusline helper functions.

Tests use tmp_path and monkeypatch to isolate filesystem operations.
"""

import json
import pathlib
import time

import pytest

import statusline as mod


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


# ── _get_project_progress ─────────────────────────────────────────────────


class TestGetProjectProgress:
    def test_reads_real_tasks_file(self, tmp_path):
        """Reads the tasks.md file and returns a space-prefixed fraction."""
        project_dir = tmp_path / "my-project"
        project_dir.mkdir()
        (project_dir / "my-project-tasks.md").write_text(
            "- [x] 1. done\n"
            "- [x] 2. done\n"
            "- [ ] 3. todo\n"
            "- [ ] 4. todo\n"
            "- [ ] 5. todo\n"
        )

        assert mod._get_project_progress(project_dir, "my-project") == " [2/5]"

    def test_template_placeholder_returns_tbd(self, tmp_path):
        """A fresh project with only the template placeholder shows [TBD]."""
        project_dir = tmp_path / "fresh-project"
        project_dir.mkdir()
        (project_dir / "fresh-project-tasks.md").write_text("- [ ] TBD\n")

        assert mod._get_project_progress(project_dir, "fresh-project") == " [TBD]"

    def test_missing_file_returns_empty(self, tmp_path):
        """Missing tasks file returns empty string (statusline falls back)."""
        project_dir = tmp_path / "nonexistent"
        # Do NOT create the directory or file.

        assert mod._get_project_progress(project_dir, "nonexistent") == ""

    def test_unreadable_path_returns_empty(self, tmp_path):
        """An OSError while reading returns empty (defensive fallback)."""
        # Point the helper at a directory where the "tasks file" is itself a
        # directory - read_text() raises OSError (IsADirectoryError).
        project_dir = tmp_path / "weird"
        project_dir.mkdir()
        (project_dir / "weird-tasks.md").mkdir()  # collision

        assert mod._get_project_progress(project_dir, "weird") == ""

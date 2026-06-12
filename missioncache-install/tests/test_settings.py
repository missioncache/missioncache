"""Tests for orbit_install.settings - user-facing ~/.claude/settings.json edits.

These are the tests that guard the user's machine: anything the installer
writes to settings.json must be reversible, idempotent, and never clobber
an existing non-orbit statusLine without an explicit backup.
"""

from __future__ import annotations

import json
from pathlib import Path

from orbit_install import settings


def test_load_returns_empty_dict_when_settings_missing(isolated_home: Path) -> None:
    """A missing settings.json reads as {}, never raises."""
    assert settings.load() == {}


def test_save_creates_parent_directories(tmp_path: Path, monkeypatch) -> None:
    """save() creates ~/.claude/ if absent so first-ever-installs work."""
    nested = tmp_path / "nested" / "claude" / "settings.json"
    monkeypatch.setattr(settings, "SETTINGS_FILE", nested)

    settings.save({"key": "value"})

    assert nested.exists(), "save() should create parent directories"
    assert json.loads(nested.read_text()) == {"key": "value"}


def test_set_statusline_backs_up_existing_different_command(isolated_home: Path) -> None:
    """Overwriting a different command creates settings.json.bak with the original."""
    original = {"statusLine": {"type": "command", "command": "python ~/my-line.py"}}
    settings.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    settings.SETTINGS_FILE.write_text(json.dumps(original))

    bak = settings.set_statusline("orbit-statusline")

    assert bak is not None, \
        "set_statusline should return the backup path when overwriting a different command"
    assert bak.exists(), "Backup file should physically exist"
    assert json.loads(bak.read_text()) == original, \
        "Backup must contain the user's original settings"
    assert json.loads(settings.SETTINGS_FILE.read_text())["statusLine"]["command"] \
        == "orbit-statusline"


def test_set_statusline_no_backup_when_identical(isolated_home: Path) -> None:
    """Idempotent re-install (same command) should not create redundant backups."""
    settings.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    settings.SETTINGS_FILE.write_text(json.dumps({
        "statusLine": {"type": "command", "command": "orbit-statusline"}
    }))

    bak = settings.set_statusline("orbit-statusline")

    assert bak is None, \
        "Re-writing the same statusLine command should not produce a backup"


def test_set_statusline_fresh_install_no_backup(isolated_home: Path) -> None:
    """With no existing statusLine, install is clean with no backup."""
    bak = settings.set_statusline("orbit-statusline")
    assert bak is None, "Fresh install should not produce a backup file"
    assert json.loads(settings.SETTINGS_FILE.read_text())["statusLine"]["command"] \
        == "orbit-statusline"


def test_unset_statusline_removes_block(isolated_home: Path) -> None:
    """unset_statusline() strips the statusLine key from settings."""
    settings.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    settings.SETTINGS_FILE.write_text(json.dumps({
        "statusLine": {"type": "command", "command": "orbit-statusline"},
        "otherKey": "keepMe",
    }))

    settings.unset_statusline()

    data = json.loads(settings.SETTINGS_FILE.read_text())
    assert "statusLine" not in data, "statusLine block should be removed"
    assert data["otherKey"] == "keepMe", "Other keys must be preserved"


def test_unset_statusline_is_noop_when_absent(isolated_home: Path) -> None:
    """Removing a statusLine that isn't there is harmless."""
    settings.unset_statusline()  # should not raise


def test_enable_and_disable_plugin_roundtrip(isolated_home: Path) -> None:
    """enable_plugin / disable_plugin is a clean round-trip."""
    settings.enable_plugin("orbit@local")

    enabled = settings.load().get("enabledPlugins", {})
    assert enabled.get("orbit@local") is True, \
        "enable_plugin should set the plugin entry to True"

    settings.disable_plugin("orbit@local")

    enabled = settings.load().get("enabledPlugins", {})
    assert "orbit@local" not in enabled, \
        "disable_plugin should remove the plugin entry entirely"


def test_ensure_edit_count_hook_is_idempotent(isolated_home: Path) -> None:
    """Calling ensure_edit_count_hook twice wires the hook only once."""
    assert settings.ensure_edit_count_hook() is True, \
        "First call should report a change"
    assert settings.ensure_edit_count_hook() is False, \
        "Second call should be a no-op"

    data = json.loads(settings.SETTINGS_FILE.read_text())
    matching_hooks = [
        h for entry in data["hooks"]["PostToolUse"]
        if entry.get("matcher") == settings.EDIT_COUNT_MATCHER
        for h in entry["hooks"]
        if h.get("type") == "http" and h.get("url") == settings.EDIT_COUNT_URL
    ]
    assert len(matching_hooks) == 1, \
        "Hook should appear exactly once after two calls"


def test_ensure_edit_count_hook_preserves_existing_matcher(isolated_home: Path) -> None:
    """If a matcher entry already exists, the URL is appended to its hooks list."""
    settings.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    settings.SETTINGS_FILE.write_text(json.dumps({
        "hooks": {
            "PostToolUse": [
                {"matcher": settings.EDIT_COUNT_MATCHER, "hooks": [
                    {"type": "http", "url": "http://example.com/other"}
                ]}
            ]
        }
    }))

    assert settings.ensure_edit_count_hook() is True

    data = json.loads(settings.SETTINGS_FILE.read_text())
    urls = [h["url"] for h in data["hooks"]["PostToolUse"][0]["hooks"]]
    assert "http://example.com/other" in urls, \
        "Existing unrelated hook must be preserved"
    assert settings.EDIT_COUNT_URL in urls, \
        "Our URL must be added to the existing matcher"


def test_remove_edit_count_hook_preserves_others(isolated_home: Path) -> None:
    """Removing edit-count keeps any other HTTP hooks on the same matcher."""
    settings.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    settings.SETTINGS_FILE.write_text(json.dumps({
        "hooks": {
            "PostToolUse": [
                {"matcher": settings.EDIT_COUNT_MATCHER, "hooks": [
                    {"type": "http", "url": settings.EDIT_COUNT_URL},
                    {"type": "http", "url": "http://example.com/other"},
                ]}
            ]
        }
    }))

    assert settings.remove_edit_count_hook() is True

    remaining = json.loads(settings.SETTINGS_FILE.read_text())["hooks"]["PostToolUse"][0]["hooks"]
    urls = [h["url"] for h in remaining]
    assert settings.EDIT_COUNT_URL not in urls, "Our URL should be gone"
    assert "http://example.com/other" in urls, "Other hooks must remain"


def test_remove_edit_count_hook_empty_matcher_removes_entry(isolated_home: Path) -> None:
    """If removing leaves a matcher with no hooks, the whole entry is dropped."""
    settings.ensure_edit_count_hook()
    settings.remove_edit_count_hook()
    data = json.loads(settings.SETTINGS_FILE.read_text())
    assert data["hooks"]["PostToolUse"] == [], \
        "Empty matcher entries should be cleaned up"


def test_remove_edit_count_hook_noop_when_absent(isolated_home: Path) -> None:
    """Removing an edit-count hook that was never wired returns False."""
    assert settings.remove_edit_count_hook() is False

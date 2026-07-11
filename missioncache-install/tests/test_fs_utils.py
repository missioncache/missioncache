"""Tests for missioncache_install.fs_utils - atomic writes and one-time backups."""

from __future__ import annotations

from pathlib import Path

import pytest

from missioncache_install import fs_utils


def test_atomic_write_creates_and_replaces(tmp_path: Path) -> None:
    """atomic_write_text creates parent dirs, writes, and leaves no temp file behind."""
    fs_utils._backed_up.clear()
    target = tmp_path / "sub" / "f.json"

    fs_utils.atomic_write_text(target, "hello")
    assert target.read_text() == "hello"

    fs_utils.atomic_write_text(target, "world")
    assert target.read_text() == "world"

    assert [p.name for p in target.parent.iterdir()] == ["f.json"], \
        "the temp file must be renamed away, not left in the directory"


def test_backup_once_only_on_first_touch(tmp_path: Path) -> None:
    """backup_once copies the original the first time, then no-ops for the rest of the run."""
    fs_utils._backed_up.clear()
    target = tmp_path / "config.toml"
    target.write_text("v1")

    bak = fs_utils.backup_once(target)
    assert bak == target.with_suffix(".toml.bak")
    assert bak.read_text() == "v1"

    target.write_text("v2")
    assert fs_utils.backup_once(target) is None, "second call in the same run is a no-op"
    assert bak.read_text() == "v1", "the original backup must not be overwritten"


def test_backup_once_absent_file_is_noop(tmp_path: Path) -> None:
    """A file that does not exist yet is marked touched but never backed up."""
    fs_utils._backed_up.clear()
    target = tmp_path / "missing.json"

    assert fs_utils.backup_once(target) is None
    # Once we create it, later writes are our own output - still no backup.
    target.write_text("ours")
    assert fs_utils.backup_once(target) is None
    assert not target.with_suffix(".json.bak").exists()


def test_write_config_text_backs_up_then_writes(tmp_path: Path) -> None:
    """write_config_text preserves the pre-existing content and writes the new content atomically."""
    fs_utils._backed_up.clear()
    target = tmp_path / "opencode.json"
    target.write_text('{"original": true}')

    fs_utils.write_config_text(target, '{"new": true}')

    assert target.read_text() == '{"new": true}'
    assert target.with_suffix(".json.bak").read_text() == '{"original": true}'


# ---------------------------------------------------------------------------
# Crash-safety: a failure during the atomic swap must never truncate or
# corrupt the pre-existing file, and must leave no temp artifact behind.
# ---------------------------------------------------------------------------

def test_atomic_write_replace_failure_preserves_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If os.replace dies mid-swap, the original file is intact and no temp leaks."""
    fs_utils._backed_up.clear()
    target = tmp_path / "config.json"
    original = '{"keep": "me"}'
    target.write_text(original)

    def boom(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(fs_utils.os, "replace", boom)

    with pytest.raises(OSError, match="simulated replace failure"):
        fs_utils.atomic_write_text(target, '{"new": "data"}')

    # (a) The pre-existing content is byte-for-byte unchanged.
    assert target.read_text() == original, "a failed replace must not alter the target"
    # (b) No temp file (mkstemp's `.<name>.<rand>.tmp`) is left in the directory.
    leftovers = sorted(p.name for p in tmp_path.iterdir() if p != target)
    assert leftovers == [], f"the temp file must be cleaned up on failure, found: {leftovers}"


def test_atomic_write_replace_failure_on_new_file_leaves_no_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replace failure while creating a brand-new file leaves no partial file or temp."""
    fs_utils._backed_up.clear()
    target = tmp_path / "brand_new.json"

    def boom(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(fs_utils.os, "replace", boom)

    with pytest.raises(OSError, match="simulated replace failure"):
        fs_utils.atomic_write_text(target, '{"new": "data"}')

    assert not target.exists(), "a failed create must not leave a partial target file"
    leftovers = sorted(p.name for p in tmp_path.iterdir())
    assert leftovers == [], f"no temp artifact should survive the failure, found: {leftovers}"


def test_backup_once_propagates_copy_failure_without_touching_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A copy failure inside backup_once surfaces - the backup is never silently skipped."""
    fs_utils._backed_up.clear()
    target = tmp_path / "config.toml"
    target.write_text("original")

    def boom(src, dst, *args, **kwargs):
        raise OSError("simulated copy failure")

    monkeypatch.setattr(fs_utils.shutil, "copy2", boom)

    with pytest.raises(OSError, match="simulated copy failure"):
        fs_utils.backup_once(target)

    # A failed backup must not corrupt or move the source file.
    assert target.read_text() == "original"


def test_write_config_text_replace_failure_leaves_backup_and_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end crash-safety: a replace failure leaves the original + a recoverable .bak."""
    fs_utils._backed_up.clear()
    target = tmp_path / "settings.json"
    original = '{"statusLine": "old"}'
    target.write_text(original)

    def boom(src, dst):
        raise OSError("simulated crash during replace")

    monkeypatch.setattr(fs_utils.os, "replace", boom)

    with pytest.raises(OSError, match="simulated crash during replace"):
        fs_utils.write_config_text(target, '{"statusLine": "new"}')

    # The user's file is untouched...
    assert target.read_text() == original
    # ...and a last-known-good backup was taken before the write was attempted.
    assert target.with_suffix(".json.bak").read_text() == original
    # No temp artifact leaks into the config directory.
    leftovers = sorted(
        p.name for p in tmp_path.iterdir()
        if p.name not in {"settings.json", "settings.json.bak"}
    )
    assert leftovers == [], f"crash must not leak a temp file, found: {leftovers}"

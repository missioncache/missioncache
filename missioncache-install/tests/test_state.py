"""Tests for missioncache_install.state - state file round-trip and recovery."""

from __future__ import annotations

import json
from pathlib import Path

from missioncache_install import state


def test_load_returns_empty_state_when_file_missing(isolated_home: Path) -> None:
    """load() returns a fresh empty state dict when the state file is absent."""
    assert not state.STATE_FILE.exists()
    result = state.load()
    assert result["components"] == {}, "Fresh state should have no components"
    assert result["schema_version"] == state.STATE_SCHEMA_VERSION, \
        "Fresh state should stamp the current schema version"
    assert "installed_at" in result, "Fresh state must include installed_at"


def test_load_recovers_from_corrupt_json(isolated_home: Path) -> None:
    """A corrupt state file is moved aside and a fresh empty state returned.

    Prevents the installer from hard-crashing on a partially-written state file.
    """
    state.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state.STATE_FILE.write_text("{ this is not json }")

    result = state.load()

    assert result["components"] == {}, \
        "Corrupt state should be replaced with a fresh empty dict"
    assert state.STATE_FILE.with_suffix(".json.corrupt").exists(), \
        "The bad file should have been preserved at .corrupt for debugging"


def test_save_stamps_updated_at(isolated_home: Path) -> None:
    """save() always stamps updated_at so callers can see freshness."""
    state.save({"components": {}})
    loaded = json.loads(state.STATE_FILE.read_text())
    assert "updated_at" in loaded, "save() must stamp updated_at"


def test_record_and_remove_component_roundtrip(isolated_home: Path) -> None:
    """record_component() followed by remove_component() preserves metadata."""
    info = {"version": "1.0.1", "port": 8787}

    state.record_component("dashboard", info)
    assert state.installed_components() == ["dashboard"]

    previous = state.remove_component("dashboard")

    assert previous == info, \
        f"remove_component must return the prior info dict, got {previous}"
    assert state.installed_components() == [], \
        "After removal, component list should be empty"


def test_remove_component_returns_none_when_absent(isolated_home: Path) -> None:
    """Removing a component that was never installed is a no-op."""
    previous = state.remove_component("nonexistent")
    assert previous is None


def test_set_mode_persists_across_reads(isolated_home: Path) -> None:
    """set_mode() is immediately visible to subsequent load() calls."""
    state.set_mode("local")
    assert state.load()["mode"] == "local"


def test_multiple_components_coexist(isolated_home: Path) -> None:
    """Recording several components does not overwrite prior entries."""
    state.record_component("plugin", {"mode": "marketplace"})
    state.record_component("dashboard", {"port": 8787})
    state.record_component("missioncache_auto", {"mode": "pypi"})

    names = set(state.installed_components())
    assert names == {"plugin", "dashboard", "missioncache_auto"}, \
        f"All three components should be tracked, got {names}"


def test_load_migrates_legacy_orbit_component_keys(isolated_home: Path) -> None:
    """Legacy orbit_db / orbit_auto component keys are rewritten on load.

    Pre-rename installs persisted the orbit-named keys; load() must rename
    them in place and rewrite the state file so --update/--uninstall see
    the current names. A second load() must not rewrite again (no churn).
    """
    state.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state.STATE_FILE.write_text(
        json.dumps(
            {
                "schema_version": state.STATE_SCHEMA_VERSION,
                "installed_at": "2026-01-01T00:00:00+00:00",
                "mode": "pypi",
                "components": {
                    "orbit_db": {"mode": "pypi"},
                    "orbit_auto": {"mode": "pypi"},
                    "plugin": {"mode": "marketplace"},
                },
            }
        )
    )

    result = state.load()

    components = result["components"]
    assert "orbit_db" not in components and "orbit_auto" not in components, \
        "Legacy orbit-named keys must be renamed away on load"
    assert components["missioncache_db"] == {"mode": "pypi"}, \
        "missioncache_db must carry the metadata that was on orbit_db"
    assert components["missioncache_auto"] == {"mode": "pypi"}, \
        "missioncache_auto must carry the metadata that was on orbit_auto"
    assert components["plugin"] == {"mode": "marketplace"}, \
        "Untouched components must survive the migration unchanged"

    on_disk = json.loads(state.STATE_FILE.read_text())
    assert set(on_disk["components"].keys()) == {
        "missioncache_db", "missioncache_auto", "plugin"
    }, "Migration must persist the renamed keys back to disk"

    first_mtime = state.STATE_FILE.stat().st_mtime_ns
    state.load()
    assert state.STATE_FILE.stat().st_mtime_ns == first_mtime, \
        "A second load() on already-migrated state must not rewrite the file"

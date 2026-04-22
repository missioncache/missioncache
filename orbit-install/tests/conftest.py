"""Shared fixtures for orbit-install tests.

All tests that touch disk get a sandboxed home directory via `isolated_home`.
This redirects Path.home() and the module-level STATE_FILE / SETTINGS_FILE
constants to a pytest tmp_path, so real ~/.claude is never touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orbit_install import settings, state


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect Path.home() and module-level state/settings paths to tmp_path.

    Every test that writes to ~/.claude should depend on this fixture.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        state, "STATE_FILE", tmp_path / ".claude" / "orbit-install.state.json"
    )
    monkeypatch.setattr(
        settings, "SETTINGS_FILE", tmp_path / ".claude" / "settings.json"
    )
    return tmp_path

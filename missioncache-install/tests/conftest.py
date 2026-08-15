"""Shared fixtures for missioncache-install tests.

All tests that touch disk get a sandboxed home directory via `isolated_home`.
This redirects Path.home() and the module-level STATE_FILE / SETTINGS_FILE
constants to a pytest tmp_path, so real ~/.claude is never touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from missioncache_install import command_clients, fs_utils, installers, mcp_clients, settings, state


@pytest.fixture(autouse=True)
def _no_real_hook_warm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never spawn the real `uv run ... python -V` warm inside the unit suite.

    _warm_hook_interpreter runs on every successful install_plugin and can
    trigger a python-build-standalone download. Autouse so no test that drives
    a plugin install accidentally reaches out to uv or the network. A test
    that wants to exercise the warm monkeypatches it back.
    """
    monkeypatch.setattr(installers, "_warm_hook_interpreter", lambda: None)


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect Path.home() and module-level state/settings paths to tmp_path.

    Every test that writes to ~/.claude should depend on this fixture.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        state, "STATE_FILE", tmp_path / ".claude" / "missioncache-install.state.json"
    )
    monkeypatch.setattr(
        settings, "SETTINGS_FILE", tmp_path / ".claude" / "settings.json"
    )
    # mcp_clients constants snapshot Path.home() at import time, so the
    # monkeypatch above is not enough - rewrite them to point under tmp_path.
    monkeypatch.setattr(
        mcp_clients,
        "OPENCODE_CONFIG_PATH",
        tmp_path / ".config" / "opencode" / "opencode.json",
    )
    monkeypatch.setattr(
        mcp_clients,
        "VSCODE_USER_MCP_PATH",
        tmp_path / "Library" / "Application Support" / "Code" / "User" / "mcp.json",
    )
    # command_clients constants - same snapshot-at-import-time problem.
    monkeypatch.setattr(
        command_clients,
        "OPENCODE_COMMANDS_DIR",
        tmp_path / ".config" / "opencode" / "commands",
    )
    monkeypatch.setattr(
        command_clients,
        "VSCODE_PROMPTS_DIR",
        tmp_path / ".missioncache" / "vscode" / "prompts",
    )
    monkeypatch.setattr(
        command_clients,
        "VSCODE_USER_SETTINGS_PATH",
        tmp_path / "Library" / "Application Support" / "Code" / "User" / "settings.json",
    )
    monkeypatch.setattr(
        command_clients,
        "CODEX_MARKETPLACE_DIR",
        tmp_path / ".missioncache" / "codex-marketplace",
    )
    monkeypatch.setattr(
        command_clients,
        "CODEX_CONFIG_TOML",
        tmp_path / ".codex" / "config.toml",
    )
    monkeypatch.setattr(
        mcp_clients,
        "CODEX_CONFIG_TOML",
        tmp_path / ".codex" / "config.toml",
    )
    # The one-time-per-run backup tracker is process-global; clear it so each
    # test starts with a clean "nothing backed up yet" slate.
    fs_utils._backed_up.clear()
    return tmp_path

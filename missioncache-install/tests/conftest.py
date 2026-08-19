"""Shared fixtures for missioncache-install tests.

Every test gets a sandboxed home directory via `isolated_home`, which
redirects Path.home() and the module-level STATE_FILE / SETTINGS_FILE
constants to a pytest tmp_path, so the real ~/.claude is never touched.

The fixture is autouse because opt-in did not hold: on 2026-08-19 a full
suite run rewrote the developer's real install state to mode "local" (31
tests here never requested the fixture, and those reaching main() hit
state.set_mode against the real file, resolving "local" because pytest runs
with the repo root as cwd). The next `--update` from outside the clone then
died on a repo_root it had no reason to have. A fixture that has to be
remembered is a fixture that will be forgotten.
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


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect Path.home() and module-level state/settings paths to tmp_path.

    Autouse, so a test cannot reach the real ~/.claude by forgetting to ask.
    Tests still declare it by name when they need the sandbox path itself.
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

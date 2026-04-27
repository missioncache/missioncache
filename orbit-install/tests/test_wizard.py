"""Tests for orbit_install.wizard - component-prompt flow.

Focus: verify that the y/N prompt fires for each component the user can
actually install, and is silently skipped for MCP-tool integrations whose
target tool is not installed locally.
"""

from __future__ import annotations

import sys

import pytest

from orbit_install import installers, mcp_clients, wizard


@pytest.fixture
def fake_prompt(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every component for which `_select_components` prompts y/N.

    Each call appends the prompt text. Always answers True so the returned
    selection mirrors the components actually offered to the user.
    """
    prompts: list[str] = []

    def record(prompt: str, default: bool = True) -> bool:
        prompts.append(prompt)
        return True

    monkeypatch.setattr(wizard.ui, "ask_yn", record)
    return prompts


def test_select_components_prompts_for_every_component_when_all_tools_present(
    fake_prompt: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With detectors all True, every prompt-eligible component is offered.

    The three slash command companions (codex_commands, opencode_commands,
    vscode_commands) are NOT prompted independently - they ride along with
    their parent (codex / opencode / vscode) prompt. Selecting yes for every
    prompt returns the full component list including the auto-added children.
    """
    monkeypatch.setattr(wizard, "_TOOL_DETECTORS", {
        "codex": lambda: True,
        "opencode": lambda: True,
        "vscode": lambda: True,
    })

    selected = wizard._select_components()

    assert selected == list(installers.ALL_COMPONENTS), (
        "Selecting yes for every prompt should return every component, including "
        "the auto-paired *_commands children"
    )
    expected_prompts = [
        c for c in installers.ALL_COMPONENTS
        if c not in wizard.COMMAND_IMPLIES.values()
    ]
    assert len(fake_prompt) == len(expected_prompts), (
        "Prompt count must equal ALL_COMPONENTS minus the three implied children"
    )


def test_select_components_skips_mcp_tools_when_undetected(
    fake_prompt: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No codex/opencode/vscode -> neither parent nor child is offered/installed."""
    monkeypatch.setattr(wizard, "_TOOL_DETECTORS", {
        "codex": lambda: False,
        "opencode": lambda: False,
        "vscode": lambda: False,
    })

    selected = wizard._select_components()

    for c in ("codex", "opencode", "vscode",
              "codex_commands", "opencode_commands", "vscode_commands"):
        assert c not in selected, f"{c} should not be installed when its tool is absent"
    # Claude-side components are still offered.
    assert "plugin" in selected
    assert "dashboard" in selected
    # Prompt count = ALL_COMPONENTS minus the 3 absent tools and their 3 children.
    expected_prompted = [
        c for c in installers.ALL_COMPONENTS
        if c not in ("codex", "opencode", "vscode")
        and c not in wizard.COMMAND_IMPLIES.values()
    ]
    assert len(fake_prompt) == len(expected_prompted)


def test_select_components_pairs_each_tool_with_its_commands_companion(
    fake_prompt: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Saying yes to a tool's parent prompt selects both MCP and slash commands.

    No second prompt fires for the *_commands child - the parent owns the choice.
    """
    monkeypatch.setattr(wizard, "_TOOL_DETECTORS", {
        "codex": lambda: True,
        "opencode": lambda: True,
        "vscode": lambda: True,
    })

    selected = wizard._select_components()

    for parent, child in wizard.COMMAND_IMPLIES.items():
        assert parent in selected, f"{parent} should be in selected (always-yes fixture)"
        assert child in selected, f"{child} must be auto-paired with {parent}"
    # No prompt text mentions the *_commands names directly.
    joined = " ".join(fake_prompt)
    for child in wizard.COMMAND_IMPLIES.values():
        assert child.replace("_", " ") not in joined.lower(), (
            f"Child component {child} should never produce its own prompt"
        )


def test_select_components_partial_tool_detection(
    fake_prompt: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the tools whose detector returns True get prompted."""
    monkeypatch.setattr(
        wizard,
        "_TOOL_DETECTORS",
        {
            "codex": lambda: True,
            "opencode": lambda: False,
            "vscode": lambda: True,
        },
    )

    selected = wizard._select_components()

    assert "codex" in selected
    assert "vscode" in selected
    assert "opencode" not in selected, (
        "OpenCode should be skipped when its detector returns False"
    )


# ---------------------------------------------------------------------------
# _TOOL_DETECTORS wiring (sanity checks against the real helpers)
# ---------------------------------------------------------------------------

def test_codex_detector_uses_shutil_which(monkeypatch: pytest.MonkeyPatch) -> None:
    """The codex detector is shutil.which("codex"), not a different binary name."""
    queries: list[str] = []

    def fake_which(name: str) -> str | None:
        queries.append(name)
        return "/opt/homebrew/bin/codex" if name == "codex" else None

    monkeypatch.setattr(wizard.shutil, "which", fake_which)
    assert wizard._TOOL_DETECTORS["codex"]() is True
    assert "codex" in queries


def test_opencode_detector_delegates_to_mcp_clients() -> None:
    """The opencode detector is the same callable that install_opencode uses."""
    assert wizard._TOOL_DETECTORS["opencode"] is mcp_clients._opencode_detected


def test_vscode_detector_returns_false_off_darwin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VSCode detector short-circuits to False on non-darwin even if app exists."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(mcp_clients, "_vscode_detected", lambda: True)
    assert wizard._TOOL_DETECTORS["vscode"]() is False


def test_vscode_detector_runs_app_check_on_darwin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VSCode detector hits the app-bundle check on darwin."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(mcp_clients, "_vscode_detected", lambda: True)
    assert wizard._TOOL_DETECTORS["vscode"]() is True

    monkeypatch.setattr(mcp_clients, "_vscode_detected", lambda: False)
    assert wizard._TOOL_DETECTORS["vscode"]() is False

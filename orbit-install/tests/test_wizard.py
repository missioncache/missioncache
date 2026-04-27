"""Tests for orbit_install.wizard - component-prompt flow.

Focus: verify that the y/N prompt fires for each component the user can
actually install, and is silently skipped for MCP-tool integrations whose
target tool is not installed locally.

Also covers `run_uninstall_wizard` - the parallel multi-select flow for
`--uninstall` without `--all`.
"""

from __future__ import annotations

import sys

import pytest

from orbit_install import installers, mcp_clients, state, wizard


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


# ---------------------------------------------------------------------------
# run_uninstall_wizard
# ---------------------------------------------------------------------------

def _seed_state(installed: list[str]) -> None:
    """Populate a fake state.json with the given component list."""
    s = state.load()
    s.setdefault("components", {})
    for c in installed:
        s["components"][c] = {"installed_at": "2026-04-27T00:00:00Z"}
    state.save(s)


def test_uninstall_wizard_fails_loudly_on_empty_state(
    isolated_home, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No tracked components -> ui.fail (loud failure), not silent no-op.

    Pre-2026-04-27 behavior was warn+return-None which produced exit code 0
    with no work done - the textbook silent-success-no-action failure mode.
    """
    failures: list[str] = []

    def fake_fail(msg: str, exit_code: int = 1) -> None:
        failures.append(msg)
        raise SystemExit(exit_code)

    monkeypatch.setattr(wizard.ui, "fail", fake_fail)
    monkeypatch.setattr(wizard.sys.stdin, "isatty", lambda: True)

    with pytest.raises(SystemExit):
        wizard.run_uninstall_wizard()

    assert failures, "Empty state must call ui.fail, not silently return None"
    assert "No prior orbit-install" in failures[0], (
        "Empty-state error must explain why nothing can be uninstalled"
    )


def test_uninstall_wizard_refuses_on_non_tty(
    isolated_home, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-TTY -> ui.fail with guidance toward --all or --uninstall <list>."""
    _seed_state(["plugin", "dashboard"])
    failures: list[str] = []

    def fake_fail(msg: str, exit_code: int = 1) -> None:
        failures.append(msg)
        raise SystemExit(exit_code)

    monkeypatch.setattr(wizard.ui, "fail", fake_fail)
    monkeypatch.setattr(wizard.sys.stdin, "isatty", lambda: False)

    with pytest.raises(SystemExit):
        wizard.run_uninstall_wizard()

    assert failures, "ui.fail should have been called"
    msg = failures[0]
    assert "--uninstall --all" in msg or "comp1" in msg, (
        "Non-TTY error must guide users toward --all or --uninstall <list>"
    )


def test_uninstall_wizard_returns_all_on_all_keyword(
    isolated_home, monkeypatch: pytest.MonkeyPatch
) -> None:
    """User typing 'all' returns every tracked component in state order."""
    _seed_state(["plugin", "dashboard", "codex"])
    monkeypatch.setattr(wizard.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "all")
    monkeypatch.setattr(wizard.ui, "banner", lambda: None)

    result = wizard.run_uninstall_wizard()

    assert result == ["plugin", "dashboard", "codex"]


def test_uninstall_wizard_parses_index_list(
    isolated_home, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Comma-separated 1-based indices map to the right components.

    Display order is `ALL_COMPONENTS` order (not state-insertion order),
    so seeded order doesn't affect the indices the user sees.
    """
    # Seed in a non-canonical order to confirm display sorting is robust.
    _seed_state(["vscode", "plugin", "codex", "dashboard"])
    monkeypatch.setattr(wizard.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "2,4")
    monkeypatch.setattr(wizard.ui, "banner", lambda: None)

    result = wizard.run_uninstall_wizard()

    # Display order matches ALL_COMPONENTS: plugin, dashboard, ..., codex, ..., vscode.
    # So index 2 = dashboard, index 4 = vscode.
    assert result == ["dashboard", "vscode"], (
        "Indices must resolve via ALL_COMPONENTS display order, not state.json "
        "insertion order"
    )


def test_uninstall_wizard_preserves_user_index_order(
    isolated_home, monkeypatch: pytest.MonkeyPatch
) -> None:
    """User's index order is preserved (e.g. `4,2` -> [4th, 2nd])."""
    _seed_state(["plugin", "dashboard", "codex", "vscode"])
    monkeypatch.setattr(wizard.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "4,2")
    monkeypatch.setattr(wizard.ui, "banner", lambda: None)

    result = wizard.run_uninstall_wizard()

    # User typed 4 first, 2 second. Display order: plugin, dashboard, codex, vscode.
    # 4 = vscode, 2 = dashboard. Result must reflect user-given order, not sorted.
    assert result == ["vscode", "dashboard"]


def test_uninstall_wizard_dedupes_repeated_indices(
    isolated_home, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`1,1,1` becomes `[plugin]`, not `[plugin, plugin, plugin]`."""
    _seed_state(["plugin", "dashboard"])
    monkeypatch.setattr(wizard.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "1,1,1")
    monkeypatch.setattr(wizard.ui, "banner", lambda: None)

    result = wizard.run_uninstall_wizard()

    assert result == ["plugin"], "Repeated indices must dedupe to single entries"


def test_uninstall_wizard_filters_unknown_state_keys(
    isolated_home, monkeypatch: pytest.MonkeyPatch
) -> None:
    """State.json with no-longer-recognized component names is filtered + warned.

    Schema-evolution defense: a stale state.json from an older orbit-install
    version may reference deleted component names. The wizard must warn
    and skip them rather than offering them to the user (who would pick a
    number that maps to a silent no-op uninstall).
    """
    _seed_state(["plugin", "_legacy_component_"])
    warnings: list[str] = []
    monkeypatch.setattr(wizard.ui, "warn", lambda msg: warnings.append(msg))
    monkeypatch.setattr(wizard.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "all")
    monkeypatch.setattr(wizard.ui, "banner", lambda: None)

    result = wizard.run_uninstall_wizard()

    assert result == ["plugin"], (
        "Wizard must drop the orphan and offer only ALL_COMPONENTS-valid entries"
    )
    assert any("_legacy_component_" in w for w in warnings), (
        "User must be told about the orphaned state entry"
    )


def test_uninstall_wizard_returns_none_on_blank_input(
    isolated_home, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blank input cancels without raising."""
    _seed_state(["plugin"])
    monkeypatch.setattr(wizard.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    monkeypatch.setattr(wizard.ui, "banner", lambda: None)
    info_msgs: list[str] = []
    monkeypatch.setattr(wizard.ui, "info", lambda msg: info_msgs.append(msg))

    result = wizard.run_uninstall_wizard()

    assert result is None
    assert any("Cancelled" in m for m in info_msgs)


def test_uninstall_wizard_rejects_out_of_range_index(
    isolated_home, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Index above the tracked-component count fails with a clear range message."""
    _seed_state(["plugin", "dashboard"])
    failures: list[str] = []

    def fake_fail(msg: str, exit_code: int = 1) -> None:
        failures.append(msg)
        raise SystemExit(exit_code)

    monkeypatch.setattr(wizard.ui, "fail", fake_fail)
    monkeypatch.setattr(wizard.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "5")
    monkeypatch.setattr(wizard.ui, "banner", lambda: None)

    with pytest.raises(SystemExit):
        wizard.run_uninstall_wizard()

    assert any("range" in f.lower() for f in failures), (
        "Out-of-range error must mention valid range to guide the user"
    )


def test_uninstall_wizard_rejects_non_numeric_input(
    isolated_home, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Garbage input that isn't 'all' or numeric fails with a clear message."""
    _seed_state(["plugin"])
    failures: list[str] = []

    def fake_fail(msg: str, exit_code: int = 1) -> None:
        failures.append(msg)
        raise SystemExit(exit_code)

    monkeypatch.setattr(wizard.ui, "fail", fake_fail)
    monkeypatch.setattr(wizard.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "everything")
    monkeypatch.setattr(wizard.ui, "banner", lambda: None)

    with pytest.raises(SystemExit):
        wizard.run_uninstall_wizard()

    assert any("invalid" in f.lower() for f in failures)

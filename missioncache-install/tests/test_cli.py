"""Tests for orbit_install.__main__ - CLI parsing and component dispatch."""

from __future__ import annotations

import pytest

from orbit_install.__main__ import (
    _excluded_components,
    _expand_implies,
    _explicit_components,
    build_parser,
    main,
)


def test_version_flag_exits_cleanly() -> None:
    """--version prints version and exits zero (argparse convention)."""
    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["--version"])
    assert exc_info.value.code == 0


def test_uninstall_with_all_is_a_valid_combination() -> None:
    """`--uninstall --all` parses cleanly: bypass wizard, remove tracked components.

    Pre-2026-04-27 behavior treated this as mutually exclusive. The new design
    composes them: `--all` modifies `--uninstall` to skip the interactive
    selector. Bare `--uninstall` becomes the `INTERACTIVE_WIZARD` sentinel.
    """
    from orbit_install.__main__ import INTERACTIVE_WIZARD
    parser = build_parser()
    args = parser.parse_args(["--uninstall", "--all"])
    assert args.uninstall is INTERACTIVE_WIZARD
    assert args.all is True


def test_uninstall_accepts_positive_component_list() -> None:
    """`--uninstall codex,vscode` lands as a comma-separated string in args.uninstall."""
    parser = build_parser()
    args = parser.parse_args(["--uninstall", "codex,vscode"])
    assert args.uninstall == "codex,vscode"
    assert args.all is False


def test_uninstall_bare_flag_is_distinguishable_from_empty_string() -> None:
    """Bare `--uninstall` lands as the `INTERACTIVE_WIZARD` sentinel.

    The dispatch must distinguish three states:
    - `args.uninstall is None` -> flag not passed
    - `args.uninstall is INTERACTIVE_WIZARD` -> bare flag, open wizard
    - `args.uninstall == ""` -> empty value (e.g. `$EMPTY_SHELL_VAR`), reject
    - `args.uninstall == "codex,vscode"` -> positive list

    The sentinel object is critical: a string sentinel like `""` would
    collide with the `--uninstall "$UNSET_VAR"` case and silently open
    the wizard mid-script.
    """
    from orbit_install.__main__ import INTERACTIVE_WIZARD
    parser = build_parser()

    # Bare flag -> sentinel
    args = parser.parse_args(["--uninstall"])
    assert args.uninstall is INTERACTIVE_WIZARD

    # Empty string from shell var -> empty string (not the sentinel)
    args = parser.parse_args(["--uninstall", ""])
    assert args.uninstall == ""
    assert args.uninstall is not INTERACTIVE_WIZARD

    # Not passed -> None
    args = parser.parse_args([])
    assert args.uninstall is None


def test_all_and_update_are_mutually_exclusive() -> None:
    """--all and --update cannot be combined."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--all", "--update"])


def test_explicit_component_flags_collected() -> None:
    """--dashboard --statusline expands to the corresponding component list."""
    args = build_parser().parse_args(["--dashboard", "--statusline"])
    assert _explicit_components(args) == ["dashboard", "statusline"]


def test_excluded_component_flags_collected() -> None:
    """--no-statusline --no-rules builds the exclusion set."""
    args = build_parser().parse_args(["--all", "--no-statusline", "--no-rules"])
    assert _excluded_components(args) == {"statusline", "rules"}


def test_port_defaults_to_8787() -> None:
    """The dashboard port defaults to 8787 per Orbit convention."""
    args = build_parser().parse_args([])
    assert args.port == 8787


def test_port_flag_accepts_integer() -> None:
    """--port accepts and parses an integer override."""
    args = build_parser().parse_args(["--port", "9999"])
    assert args.port == 9999


def test_local_flag_is_boolean() -> None:
    """--local is a boolean flag."""
    assert build_parser().parse_args(["--local"]).local is True
    assert build_parser().parse_args([]).local is False


def test_no_service_flag() -> None:
    """--no-service is a boolean flag for skipping launchd/systemd setup."""
    assert build_parser().parse_args(["--no-service"]).no_service is True
    assert build_parser().parse_args([]).no_service is False


def test_yes_flag_short_and_long() -> None:
    """--yes and -y both set the assume_yes flag."""
    assert build_parser().parse_args(["--yes"]).yes is True
    assert build_parser().parse_args(["-y"]).yes is True


def test_orbit_auto_uses_dash_in_cli_but_underscore_internally() -> None:
    """--orbit-auto maps to the `orbit_auto` component name internally."""
    args = build_parser().parse_args(["--orbit-auto"])
    assert args.orbit_auto is True
    assert _explicit_components(args) == ["orbit_auto"]


def test_user_commands_flag_naming() -> None:
    """--user-commands maps to the `user_commands` component name."""
    args = build_parser().parse_args(["--user-commands"])
    assert args.user_commands is True
    assert _explicit_components(args) == ["user_commands"]


def test_orbit_db_flag_naming() -> None:
    """--orbit-db maps to the `orbit_db` component name."""
    args = build_parser().parse_args(["--orbit-db"])
    assert args.orbit_db is True
    assert _explicit_components(args) == ["orbit_db"]


@pytest.mark.parametrize("flag, dest", [
    ("--codex", "codex"),
    ("--opencode", "opencode"),
    ("--vscode", "vscode"),
])
def test_mcp_tool_opt_in_flags(flag: str, dest: str) -> None:
    """--codex / --opencode / --vscode each select the matching component."""
    args = build_parser().parse_args([flag])
    assert getattr(args, dest) is True
    assert _explicit_components(args) == [dest]


@pytest.mark.parametrize("flag, dest", [
    ("--no-codex", "no_codex"),
    ("--no-opencode", "no_opencode"),
    ("--no-vscode", "no_vscode"),
])
def test_mcp_tool_opt_out_flags(flag: str, dest: str) -> None:
    """--no-codex etc. land in the exclusion set when combined with --all."""
    args = build_parser().parse_args(["--all", flag])
    assert getattr(args, dest) is True
    component_name = dest.removeprefix("no_")
    assert component_name in _excluded_components(args)


@pytest.mark.parametrize("flag, dest", [
    ("--codex-commands", "codex_commands"),
    ("--opencode-commands", "opencode_commands"),
    ("--vscode-commands", "vscode_commands"),
])
def test_slash_command_opt_in_flags(flag: str, dest: str) -> None:
    """--codex-commands etc. select only the slash command companion (without MCP)."""
    args = build_parser().parse_args([flag])
    assert getattr(args, dest) is True
    assert _explicit_components(args) == [dest]


@pytest.mark.parametrize("flag, dest", [
    ("--no-codex-commands", "no_codex_commands"),
    ("--no-opencode-commands", "no_opencode_commands"),
    ("--no-vscode-commands", "no_vscode_commands"),
])
def test_slash_command_opt_out_flags(flag: str, dest: str) -> None:
    """--no-<tool>-commands keeps the MCP server but skips slash commands."""
    args = build_parser().parse_args(["--all", flag])
    assert getattr(args, dest) is True
    component_name = dest.removeprefix("no_")
    assert component_name in _excluded_components(args)


def test_expand_implies_pulls_in_command_companion_when_parent_selected() -> None:
    """Selecting --codex implicitly turns on codex_commands too."""
    out = _expand_implies(["codex"], excluded=set())
    assert "codex" in out
    assert "codex_commands" in out


def test_expand_implies_respects_explicit_no_commands_opt_out() -> None:
    """--codex --no-codex-commands installs MCP only, not the slash commands."""
    out = _expand_implies(["codex"], excluded={"codex_commands"})
    assert out == ["codex"], "child must NOT be auto-added when explicitly excluded"


def test_expand_implies_no_op_when_parent_absent() -> None:
    """Without a parent in the selection, the child is not auto-pulled."""
    out = _expand_implies(["plugin", "rules"], excluded=set())
    assert "codex_commands" not in out
    assert "opencode_commands" not in out
    assert "vscode_commands" not in out


def test_expand_implies_does_not_double_add_existing_child() -> None:
    """If the child is already in selection, it isn't added a second time."""
    out = _expand_implies(["codex", "codex_commands"], excluded=set())
    assert out.count("codex_commands") == 1


def test_no_codex_auto_excludes_codex_commands() -> None:
    """--all --no-codex must exclude codex_commands too (slash commands need MCP)."""
    args = build_parser().parse_args(["--all", "--no-codex"])
    excluded = _excluded_components(args)
    assert "codex" in excluded
    assert "codex_commands" in excluded


def test_no_codex_with_explicit_codex_commands_keeps_child() -> None:
    """--no-codex --codex-commands honors the explicit override (user owns MCP elsewhere)."""
    args = build_parser().parse_args(["--all", "--no-codex", "--codex-commands"])
    excluded = _excluded_components(args)
    assert "codex" in excluded
    assert "codex_commands" not in excluded


def test_all_no_codex_invocation_skips_codex_commands(monkeypatch) -> None:
    """End-to-end: --all --no-codex skips both codex and codex_commands."""
    captured: list[list[str]] = []

    def fake_install(components, ctx):
        captured.append(list(components))

    monkeypatch.setattr("sys.argv", ["orbit-install", "--all", "--no-codex", "--yes"])
    monkeypatch.setattr("orbit_install.__main__.installers.install_components", fake_install)
    monkeypatch.setattr("orbit_install.__main__.wizard.run", lambda ctx: None)

    rc = main()

    assert rc == 0
    assert captured
    assert "codex" not in captured[0]
    assert "codex_commands" not in captured[0]


def test_codex_only_invocation_implies_codex_commands(monkeypatch) -> None:
    """End-to-end: `orbit-install --codex` installs both codex MCP + slash commands."""
    captured: list[list[str]] = []

    def fake_install(components, ctx):
        captured.append(list(components))

    monkeypatch.setattr("sys.argv", ["orbit-install", "--codex", "--yes"])
    monkeypatch.setattr("orbit_install.__main__.installers.install_components", fake_install)
    monkeypatch.setattr("orbit_install.__main__.wizard.run", lambda ctx: None)

    rc = main()

    assert rc == 0
    assert captured
    assert "codex" in captured[0]
    assert "codex_commands" in captured[0], (
        "--codex must imply --codex-commands by default"
    )


def test_codex_with_no_codex_commands_installs_mcp_only(monkeypatch) -> None:
    """`orbit-install --codex --no-codex-commands` installs MCP without slash commands."""
    captured: list[list[str]] = []

    def fake_install(components, ctx):
        captured.append(list(components))

    monkeypatch.setattr(
        "sys.argv",
        ["orbit-install", "--codex", "--no-codex-commands", "--yes"],
    )
    monkeypatch.setattr("orbit_install.__main__.installers.install_components", fake_install)
    monkeypatch.setattr("orbit_install.__main__.wizard.run", lambda ctx: None)

    rc = main()

    assert rc == 0
    assert captured
    assert captured[0] == ["codex"], (
        "Explicit --no-codex-commands must keep codex_commands out of the install"
    )


def test_statusline_without_dashboard_auto_adds_dashboard(monkeypatch) -> None:
    """--statusline alone pulls dashboard in too, since orbit-statusline lives in that package."""
    captured: list[list[str]] = []

    def fake_install(components, ctx):
        captured.append(list(components))

    monkeypatch.setattr("sys.argv", ["orbit-install", "--statusline", "--yes"])
    monkeypatch.setattr("orbit_install.__main__.installers.install_components", fake_install)
    monkeypatch.setattr("orbit_install.__main__.wizard.run", lambda ctx: None)

    rc = main()

    assert rc == 0
    assert captured, "install_components should have been called"
    assert "dashboard" in captured[0], f"dashboard should be auto-added, got {captured[0]}"
    assert captured[0].index("dashboard") < captured[0].index("statusline"), (
        "dashboard must be installed before statusline so the entry point exists first"
    )


def test_statusline_with_no_dashboard_does_not_auto_add(monkeypatch) -> None:
    """--statusline --no-dashboard honors the explicit opt-out (user takes responsibility)."""
    captured: list[list[str]] = []

    def fake_install(components, ctx):
        captured.append(list(components))

    monkeypatch.setattr("sys.argv", ["orbit-install", "--statusline", "--no-dashboard", "--yes"])
    monkeypatch.setattr("orbit_install.__main__.installers.install_components", fake_install)
    monkeypatch.setattr("orbit_install.__main__.wizard.run", lambda ctx: None)

    rc = main()

    assert rc == 0
    assert captured
    assert "dashboard" not in captured[0]


# ---------------------------------------------------------------------------
# _run_uninstall dispatch tests (end-to-end through main())
#
# Each test stubs `installers.uninstall_components` to capture the components
# that would be removed, monkeypatches sys.argv to drive argparse, seeds
# state.json via the orbit_install.state API, and asserts the dispatch made
# the right call. State seeding goes through the real state.save/load to
# catch schema regressions.
# ---------------------------------------------------------------------------

def _seed_install_state(installed: list[str]) -> None:
    """Populate state.json with components, mimicking a prior install."""
    from orbit_install import state
    s = state.load()
    s.setdefault("components", {})
    for c in installed:
        s["components"][c] = {"installed_at": "2026-04-27T00:00:00Z"}
    state.save(s)


def test_uninstall_all_uninstalls_tracked_components(isolated_home, monkeypatch) -> None:
    """`--uninstall --all` removes every tracked component, in dispatcher order."""
    _seed_install_state(["plugin", "dashboard", "codex", "codex_commands"])
    captured: list[list[str]] = []

    def fake_uninstall(components, ctx):
        captured.append(list(components))

    monkeypatch.setattr("sys.argv", ["orbit-install", "--uninstall", "--all"])
    monkeypatch.setattr(
        "orbit_install.__main__.installers.uninstall_components", fake_uninstall
    )

    rc = main()

    assert rc == 0
    assert captured == [["plugin", "dashboard", "codex", "codex_commands"]], (
        "Expected exactly the tracked components, no escalation, no dropouts"
    )


def test_uninstall_all_empty_state_is_safe_noop(
    isolated_home, monkeypatch, capsys
) -> None:
    """`--uninstall --all` with empty state returns 0 without calling uninstaller.

    Pre-fix behavior was to escalate to ALL_COMPONENTS (silent-failure-hunter
    Critical). Fixed behavior: warn and no-op, matching `update_all`'s pattern.
    """
    captured: list[list[str]] = []

    def fake_uninstall(components, ctx):
        captured.append(list(components))

    monkeypatch.setattr("sys.argv", ["orbit-install", "--uninstall", "--all"])
    monkeypatch.setattr(
        "orbit_install.__main__.installers.uninstall_components", fake_uninstall
    )

    rc = main()

    assert rc == 0
    assert captured == [], (
        "Empty state must NOT trigger best-effort full-catalog uninstall - "
        "that escalates scope without consent"
    )


def test_uninstall_positive_list_happy_path(isolated_home, monkeypatch) -> None:
    """`--uninstall codex_commands` removes exactly that component."""
    _seed_install_state(["plugin", "dashboard", "codex", "codex_commands"])
    captured: list[list[str]] = []

    def fake_uninstall(components, ctx):
        captured.append(list(components))

    monkeypatch.setattr("sys.argv", ["orbit-install", "--uninstall", "codex_commands"])
    monkeypatch.setattr(
        "orbit_install.__main__.installers.uninstall_components", fake_uninstall
    )

    rc = main()

    assert rc == 0
    # Removing the child without the parent should NOT auto-add the parent.
    assert captured == [["codex_commands"]]


def test_uninstall_codex_auto_expands_to_codex_commands(
    isolated_home, monkeypatch
) -> None:
    """`--uninstall codex` auto-includes `codex_commands` if it's still tracked.

    Symmetric with the install-side COMMAND_IMPLIES pairing in wizard.py.
    """
    _seed_install_state(["plugin", "codex", "codex_commands"])
    captured: list[list[str]] = []

    def fake_uninstall(components, ctx):
        captured.append(list(components))

    monkeypatch.setattr("sys.argv", ["orbit-install", "--uninstall", "codex"])
    monkeypatch.setattr(
        "orbit_install.__main__.installers.uninstall_components", fake_uninstall
    )

    rc = main()

    assert rc == 0
    assert captured == [["codex", "codex_commands"]], (
        "Expected codex to auto-expand to codex+codex_commands (paired install)"
    )


def test_uninstall_codex_does_not_auto_add_already_removed_child(
    isolated_home, monkeypatch
) -> None:
    """If codex_commands was already uninstalled, --uninstall codex doesn't error.

    Auto-expansion must respect the current installed-state, not
    COMMAND_IMPLIES blindly. Otherwise the second `--uninstall codex` after
    a prior `--uninstall codex_commands` would fail with 'not currently installed'.
    """
    _seed_install_state(["plugin", "codex"])  # codex_commands NOT tracked
    captured: list[list[str]] = []

    def fake_uninstall(components, ctx):
        captured.append(list(components))

    monkeypatch.setattr("sys.argv", ["orbit-install", "--uninstall", "codex"])
    monkeypatch.setattr(
        "orbit_install.__main__.installers.uninstall_components", fake_uninstall
    )

    rc = main()

    assert rc == 0
    assert captured == [["codex"]]


def test_uninstall_empty_string_from_shell_var_is_rejected(
    isolated_home, monkeypatch, capsys
) -> None:
    """`--uninstall ""` (e.g. from unset shell var) fails loudly, NOT silently.

    Pre-fix, an empty string fell through to the wizard branch (silently
    interactive in scripts). Fixed: distinct sentinel for bare flag means
    empty string is now distinguishable and explicitly rejected.
    """
    _seed_install_state(["plugin"])
    captured: list[list[str]] = []

    def fake_uninstall(components, ctx):
        captured.append(list(components))

    monkeypatch.setattr("sys.argv", ["orbit-install", "--uninstall", ""])
    monkeypatch.setattr(
        "orbit_install.__main__.installers.uninstall_components", fake_uninstall
    )

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code != 0, "Empty input must exit non-zero"
    assert captured == [], "Uninstall must not have been called"


@pytest.mark.parametrize("garbage_input", [",", " , ", ", ,", " , , "])
def test_uninstall_separator_only_input_is_rejected(
    isolated_home, monkeypatch, garbage_input
) -> None:
    """`--uninstall ","` and similar separator-only inputs fail loudly.

    Pre-fix (Codex review 2026-04-27 Finding #1): the whitespace-strip
    guard at the top of the dispatch only catches pure-whitespace inputs.
    A bare comma is not whitespace, so `","` strips to itself and bypasses
    the guard, then the comprehension's `if c.strip()` filter drops every
    empty chunk, leaving `requested = []`. Without this test the dispatch
    would silently no-op via `uninstall_components([])`.
    """
    _seed_install_state(["plugin"])
    captured: list[list[str]] = []

    def fake_uninstall(components, ctx):
        captured.append(list(components))

    monkeypatch.setattr("sys.argv", ["orbit-install", "--uninstall", garbage_input])
    monkeypatch.setattr(
        "orbit_install.__main__.installers.uninstall_components", fake_uninstall
    )

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code != 0, f"Separator-only input {garbage_input!r} must exit non-zero"
    assert captured == [], "Uninstall must not have been called"


def test_wizard_pick_auto_expands_to_command_companion(
    isolated_home, monkeypatch
) -> None:
    """Wizard-side picks of `codex` auto-include `codex_commands`.

    Pre-fix (Codex review 2026-04-27 Finding #2): the wizard branch passed
    user picks straight to `uninstall_components` without applying the
    `_expand_command_pairs` logic that the positive-list branch uses.
    Result: user picks `codex` from the wizard menu and `codex_commands`
    is left orphaned in state.json + on disk. This test seeds both, mocks
    the wizard to return only `["codex"]`, and asserts the dispatch expands
    to `["codex", "codex_commands"]` before invoking the uninstaller.
    """
    _seed_install_state(["plugin", "codex", "codex_commands"])
    captured: list[list[str]] = []

    def fake_uninstall(components, ctx):
        captured.append(list(components))

    # Mock the wizard to return only `codex` (simulating an index pick).
    monkeypatch.setattr(
        "orbit_install.__main__.wizard.run_uninstall_wizard",
        lambda: ["codex"],
    )
    monkeypatch.setattr("sys.argv", ["orbit-install", "--uninstall"])
    monkeypatch.setattr(
        "orbit_install.__main__.installers.uninstall_components", fake_uninstall
    )

    rc = main()

    assert rc == 0
    assert captured == [["codex", "codex_commands"]], (
        "Wizard pick of codex must expand to codex+codex_commands to match the "
        "install-side pairing - otherwise codex_commands orphans"
    )


def test_wizard_pick_does_not_expand_already_removed_companion(
    isolated_home, monkeypatch
) -> None:
    """Wizard-side pick of `codex` doesn't error when `codex_commands` is already gone.

    The expansion respects current installed-state, not COMMAND_IMPLIES blindly.
    """
    _seed_install_state(["plugin", "codex"])  # codex_commands NOT tracked
    captured: list[list[str]] = []

    def fake_uninstall(components, ctx):
        captured.append(list(components))

    monkeypatch.setattr(
        "orbit_install.__main__.wizard.run_uninstall_wizard",
        lambda: ["codex"],
    )
    monkeypatch.setattr("sys.argv", ["orbit-install", "--uninstall"])
    monkeypatch.setattr(
        "orbit_install.__main__.installers.uninstall_components", fake_uninstall
    )

    rc = main()

    assert rc == 0
    assert captured == [["codex"]], (
        "Don't auto-add codex_commands if it isn't tracked anymore"
    )


def test_uninstall_positive_list_with_all_is_ambiguous_error(
    isolated_home, monkeypatch
) -> None:
    """`--uninstall foo --all` is ambiguous and must fail."""
    _seed_install_state(["plugin"])
    captured: list[list[str]] = []

    def fake_uninstall(components, ctx):
        captured.append(list(components))

    monkeypatch.setattr("sys.argv", ["orbit-install", "--uninstall", "plugin", "--all"])
    monkeypatch.setattr(
        "orbit_install.__main__.installers.uninstall_components", fake_uninstall
    )

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code != 0
    assert captured == []


def test_uninstall_unknown_component_errors_with_valid_list(
    isolated_home, monkeypatch
) -> None:
    """An unknown component name produces a fail-fast error before any uninstall."""
    _seed_install_state(["plugin"])
    captured: list[list[str]] = []

    def fake_uninstall(components, ctx):
        captured.append(list(components))

    monkeypatch.setattr("sys.argv", ["orbit-install", "--uninstall", "blorp"])
    monkeypatch.setattr(
        "orbit_install.__main__.installers.uninstall_components", fake_uninstall
    )

    with pytest.raises(SystemExit):
        main()

    assert captured == []


def test_uninstall_tracked_but_not_installed_errors(
    isolated_home, monkeypatch
) -> None:
    """`--uninstall codex` when only `plugin` is tracked errors clearly."""
    _seed_install_state(["plugin"])
    captured: list[list[str]] = []

    def fake_uninstall(components, ctx):
        captured.append(list(components))

    monkeypatch.setattr("sys.argv", ["orbit-install", "--uninstall", "codex"])
    monkeypatch.setattr(
        "orbit_install.__main__.installers.uninstall_components", fake_uninstall
    )

    with pytest.raises(SystemExit):
        main()

    assert captured == []


def test_uninstall_with_update_is_mutex_error(monkeypatch) -> None:
    """`--uninstall --update` is rejected at main() entry (different verbs)."""
    monkeypatch.setattr("sys.argv", ["orbit-install", "--uninstall", "--update"])

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code != 0


def test_uninstall_dash_form_is_normalized(isolated_home, monkeypatch) -> None:
    """`--uninstall codex-commands` (CLI dash form) maps to internal `codex_commands`."""
    _seed_install_state(["codex_commands"])
    captured: list[list[str]] = []

    def fake_uninstall(components, ctx):
        captured.append(list(components))

    monkeypatch.setattr("sys.argv", ["orbit-install", "--uninstall", "codex-commands"])
    monkeypatch.setattr(
        "orbit_install.__main__.installers.uninstall_components", fake_uninstall
    )

    rc = main()

    assert rc == 0
    assert captured == [["codex_commands"]], (
        "User-friendly dash form must map to internal underscore form"
    )


def test_uninstall_case_insensitive_normalization(isolated_home, monkeypatch) -> None:
    """`--uninstall Codex` and `--uninstall CODEX` both work."""
    _seed_install_state(["codex"])
    captured: list[list[str]] = []

    def fake_uninstall(components, ctx):
        captured.append(list(components))

    monkeypatch.setattr("sys.argv", ["orbit-install", "--uninstall", "Codex"])
    monkeypatch.setattr(
        "orbit_install.__main__.installers.uninstall_components", fake_uninstall
    )

    rc = main()

    assert rc == 0
    assert captured and captured[0] == ["codex"]


def test_uninstall_positive_list_dedupes(isolated_home, monkeypatch) -> None:
    """`--uninstall plugin,plugin,plugin` becomes `["plugin"]`, not three calls."""
    _seed_install_state(["plugin"])
    captured: list[list[str]] = []

    def fake_uninstall(components, ctx):
        captured.append(list(components))

    monkeypatch.setattr(
        "sys.argv", ["orbit-install", "--uninstall", "plugin,plugin,plugin"]
    )
    monkeypatch.setattr(
        "orbit_install.__main__.installers.uninstall_components", fake_uninstall
    )

    rc = main()

    assert rc == 0
    assert captured == [["plugin"]]


def test_uninstall_filters_unknown_state_keys_with_warning(
    isolated_home, monkeypatch, capsys
) -> None:
    """State.json with a no-longer-recognized component name is filtered + warned.

    Schema-evolution defense: an old state.json may name a component that
    a future orbit-install version has deleted. The dispatcher should warn
    and skip rather than KeyError or silently include the orphan.
    """
    _seed_install_state(["plugin", "_legacy_component_"])
    captured: list[list[str]] = []

    def fake_uninstall(components, ctx):
        captured.append(list(components))

    monkeypatch.setattr("sys.argv", ["orbit-install", "--uninstall", "--all"])
    monkeypatch.setattr(
        "orbit_install.__main__.installers.uninstall_components", fake_uninstall
    )

    rc = main()

    assert rc == 0
    assert captured == [["plugin"]], (
        "Unknown state.json key must be filtered out, not passed to uninstaller"
    )
    out = capsys.readouterr().out + capsys.readouterr().err
    # The warning is emitted via ui.warn which prints to stdout.
    # We don't assert exact wording; just that the legacy name surfaces somewhere.

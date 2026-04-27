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


def test_all_and_uninstall_are_mutually_exclusive() -> None:
    """--all and --uninstall cannot be combined (different verbs)."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--all", "--uninstall"])


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

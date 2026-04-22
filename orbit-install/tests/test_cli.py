"""Tests for orbit_install.__main__ - CLI parsing and component dispatch."""

from __future__ import annotations

import pytest

from orbit_install.__main__ import (
    _excluded_components,
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

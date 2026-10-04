"""Tests for missioncache_auto.cli.parse_args.

A bare project name runs it: `missioncache-auto my-project` means
`missioncache-auto run my-project`. The global `-v/--visibility` option takes a
value, which may be spaced (`-v minimal`) or joined (`-vminimal`,
`--visibility=minimal`), and every form must reach the same run.
"""

import sys

import pytest

from missioncache_auto.cli import _config_from_args, parse_args


def _parse(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["missioncache-auto", *argv])
    return parse_args()


@pytest.mark.parametrize(
    "argv",
    [
        ("-v", "minimal", "proj"),
        ("--visibility", "minimal", "proj"),
        ("-vminimal", "proj"),
        ("--visibility=minimal", "proj"),
        ("--no-color", "-v", "minimal", "proj"),
    ],
)
def test_every_visibility_form_runs_the_project(monkeypatch, argv):
    args = _parse(monkeypatch, *argv)
    assert args.command == "run"
    assert args.task_name == "proj"
    assert args.visibility == "minimal"


def test_a_bare_project_name_runs_it(monkeypatch):
    args = _parse(monkeypatch, "proj")
    assert (args.command, args.task_name) == ("run", "proj")


@pytest.mark.parametrize("command", ["run", "status"])
def test_an_explicit_command_is_kept(monkeypatch, command):
    args = _parse(monkeypatch, "-v", "none", command, "proj")
    assert (args.command, args.task_name, args.visibility) == (command, "proj", "none")


def test_effort_reaches_the_config(monkeypatch):
    config = _config_from_args(_parse(monkeypatch, "proj", "--effort", "xhigh"))
    assert config.effort == "xhigh"
    assert _config_from_args(_parse(monkeypatch, "proj")).effort is None

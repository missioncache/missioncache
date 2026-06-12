"""Tests for orbit_install.ui - colored output and interactive prompts."""

from __future__ import annotations

import sys

import pytest

from orbit_install import ui


def test_ask_yn_returns_default_when_stdin_not_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-interactive stdin (CI, pipes) returns the default silently.

    This is what makes `orbit-install --all` safe in CI pipelines.
    """
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert ui.ask_yn("Proceed?", default=True) is True
    assert ui.ask_yn("Proceed?", default=False) is False


def test_ask_yn_honors_affirmative_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """A TTY user typing 'y' returns True, regardless of default."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "y")
    assert ui.ask_yn("?", default=False) is True
    monkeypatch.setattr("builtins.input", lambda _: "Y")
    assert ui.ask_yn("?", default=False) is True


def test_ask_yn_honors_negative_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """A TTY user typing 'n' returns False, regardless of default."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert ui.ask_yn("?", default=True) is False


def test_ask_yn_empty_input_returns_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pressing Enter on an empty prompt selects the default answer."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "")
    assert ui.ask_yn("?", default=True) is True
    assert ui.ask_yn("?", default=False) is False


def test_ask_yn_handles_eof_as_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ctrl-D at the prompt returns the default instead of crashing."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    def raise_eof(_prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", raise_eof)
    assert ui.ask_yn("?", default=True) is True


def test_fail_exits_with_provided_code(capsys: pytest.CaptureFixture[str]) -> None:
    """ui.fail() exits with the given code and writes to stderr."""
    with pytest.raises(SystemExit) as exc_info:
        ui.fail("something broke", exit_code=3)
    assert exc_info.value.code == 3
    captured = capsys.readouterr()
    assert "something broke" in captured.err


def test_helper_functions_do_not_raise(capsys: pytest.CaptureFixture[str]) -> None:
    """Smoke test: info/success/warn/detail/step all print without raising."""
    ui.banner()
    ui.step(1, "hello")
    ui.info("info line")
    ui.success("success line")
    ui.warn("warn line")
    ui.detail("detail line")
    ui.success_banner()

    captured = capsys.readouterr()
    assert "hello" in captured.out
    assert "info line" in captured.out
    assert "success line" in captured.out
    assert "warn line" in captured.out

"""Tests for the --update flow: failure retry, untracked reporting, cache
invalidation, and the update-time statusline guard.

Spec source: the update contract established with the 2026-07-28 fixes -
`uvx missioncache-install --update` must (a) retry components whose last
install failed, (b) report-but-never-touch components installed outside the
state file, (c) drop the shared update-check cache so the statusline stops
showing a pre-update answer, and (d) never re-acquire a statusLine the user
has since pointed elsewhere.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from missioncache_install import installers, settings, state
from missioncache_install import subprocess_utils


def _make_ctx(
    mode: str = "pypi",
    *,
    repo_root: Path | None = None,
    assume_yes: bool = False,
    updating: bool = False,
) -> installers.InstallContext:
    return installers.InstallContext(
        mode=mode,  # type: ignore[arg-type]
        repo_root=repo_root,
        skip_service=True,
        port=8787,
        assume_yes=assume_yes,
        updating=updating,
    )


def _fake_installers(
    monkeypatch: pytest.MonkeyPatch, *, failing: frozenset[str] = frozenset()
) -> list[str]:
    """Replace every registered installer with a recording fake.

    Successful fakes honor the real contract (record_component on success);
    failing ones raise CommandFailed before recording, like real installers.
    Returns the shared call log.
    """
    calls: list[str] = []
    fakes = {}
    for comp in installers.ALL_COMPONENTS:
        def fake(ctx, _comp=comp):
            calls.append(_comp)
            if _comp in failing:
                raise subprocess_utils.CommandFailed([_comp], 1, "", "boom")
            state.record_component(_comp, {"mode": ctx.mode})
        fakes[comp] = fake
    monkeypatch.setattr(installers, "_INSTALLERS", fakes)
    return calls


def _cache_file(home: Path) -> Path:
    return home / ".missioncache" / "update-check.json"


# ---------------------------------------------------------------------------
# state.update_failures
# ---------------------------------------------------------------------------

def test_update_failures_records_and_clears(isolated_home: Path) -> None:
    """A failed attempt is recorded; a later successful attempt clears it."""
    state.update_failures(attempted=["dashboard", "codex"], failed=["codex"])
    assert state.failed_components() == ["codex"]

    state.update_failures(attempted=["codex"], failed=[])
    assert state.failed_components() == [], \
        "A successful retry must clear the failure record"


def test_update_failures_subset_keeps_untouched_components(
    isolated_home: Path,
) -> None:
    """A partial install (component subset) must not erase failure records of
    components it did not attempt."""
    state.update_failures(attempted=["codex"], failed=["codex"])
    state.update_failures(attempted=["dashboard"], failed=[])

    assert state.failed_components() == ["codex"], \
        "codex was not attempted, so its failure record must survive"


def test_update_failures_key_absent_when_empty(isolated_home: Path) -> None:
    """No failures means no failed_components key lingering in the file."""
    state.update_failures(attempted=["codex"], failed=["codex"])
    state.update_failures(attempted=["codex"], failed=[])

    on_disk = json.loads(state.STATE_FILE.read_text())
    assert "failed_components" not in on_disk


# ---------------------------------------------------------------------------
# install_components: failure recording + cache invalidation
# ---------------------------------------------------------------------------

def test_install_components_records_failures_for_retry(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A component that fails is recorded in state so --update can retry it -
    otherwise the printed 're-run --update' advice can never work."""
    _fake_installers(monkeypatch, failing=frozenset({"codex"}))

    failed = installers.install_components(["dashboard", "codex"], _make_ctx())

    assert failed == ["codex"]
    assert state.failed_components() == ["codex"]
    assert "dashboard" in state.load()["components"]
    assert "codex" not in state.load()["components"]


def test_install_components_invalidates_update_cache(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Installing anything drops the shared update-check cache, so the
    statusline recomputes instead of serving a pre-update answer for up to
    the cache TTL."""
    cache = _cache_file(isolated_home)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text('{"update_available": true, "checked_at": 1}')
    _fake_installers(monkeypatch)

    installers.install_components(["dashboard"], _make_ctx())

    assert not cache.exists(), \
        "update-check cache must be dropped after an install run"


def test_uninstall_components_clears_failures_and_cache(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Uninstalling a component removes its failure record (nothing left to
    retry) and drops the update cache."""
    state.update_failures(attempted=["codex"], failed=["codex"])
    cache = _cache_file(isolated_home)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("{}")
    monkeypatch.setattr(
        installers, "_UNINSTALLERS", {c: lambda ctx: None for c in installers.ALL_COMPONENTS}
    )

    installers.uninstall_components(["codex"], _make_ctx())

    assert state.failed_components() == []
    assert not cache.exists()


# ---------------------------------------------------------------------------
# update_all: retry + report
# ---------------------------------------------------------------------------

def test_update_all_retries_previously_failed_components(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--update attempts tracked components AND previously failed ones; a
    failed component that now succeeds is promoted to tracked and its failure
    record cleared."""
    state.record_component("dashboard", {"mode": "pypi"})
    state.update_failures(attempted=["codex"], failed=["codex"])
    calls = _fake_installers(monkeypatch)

    installers.update_all(_make_ctx())

    assert set(calls) == {"dashboard", "codex"}, \
        "Both the tracked and the previously failed component must be attempted"
    assert "codex" in state.load()["components"], \
        "A successful retry must promote the component to tracked"
    assert state.failed_components() == []


def test_update_all_reports_untracked_without_acting(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Components present on the machine but absent from state are reported
    and NOT installed - acting on them could clobber a maintainer's editable
    setup or config the user manages elsewhere."""
    state.record_component("codex", {"command": "mcp-missioncache"})
    calls = _fake_installers(monkeypatch)
    monkeypatch.setattr(
        installers, "_UNTRACKED_PROBES", {"dashboard": lambda: True}
    )
    warnings: list[str] = []
    monkeypatch.setattr(installers.ui, "warn", lambda msg, **kw: warnings.append(msg))

    installers.update_all(_make_ctx())

    assert calls == ["codex"], \
        "Only the tracked component may be installed; the probed one is report-only"
    assert any("dashboard" in w for w in warnings), \
        "The untracked-but-installed component must be named in a warning"


def test_update_all_empty_state_reports_untracked(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reset/corrupt state file must not strand the user at a bare 'Nothing
    to update' when components are clearly installed - the report tells them
    how to re-adopt."""
    calls = _fake_installers(monkeypatch)
    monkeypatch.setattr(
        installers, "_UNTRACKED_PROBES", {"dashboard": lambda: True}
    )
    warnings: list[str] = []
    monkeypatch.setattr(installers.ui, "warn", lambda msg, **kw: warnings.append(msg))

    installers.update_all(_make_ctx())

    assert calls == []
    assert any("dashboard" in w for w in warnings)


def test_update_all_sets_updating_flag(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """update_all marks the context as updating so installers can apply
    update-specific consent rules (statusline guard)."""
    state.record_component("statusline", {"command": "missioncache-statusline"})
    seen: list[bool] = []
    monkeypatch.setattr(
        installers,
        "_INSTALLERS",
        {"statusline": lambda ctx: seen.append(ctx.updating)},
    )

    installers.update_all(_make_ctx())

    assert seen == [True]


# ---------------------------------------------------------------------------
# install_statusline: update-time guard
# ---------------------------------------------------------------------------

def test_update_never_replaces_foreign_statusline(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """During --update, a statusLine pointing at something that is not
    MissionCache's is left untouched - no prompt (which would stall a
    non-interactive update) and no overwrite, even with assume_yes."""
    settings.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    settings.SETTINGS_FILE.write_text(json.dumps({
        "statusLine": {"type": "command", "command": "my-own-statusline"}
    }))

    def _no_prompt(*a, **k):
        raise AssertionError("update must not prompt about the statusline")

    monkeypatch.setattr(installers.ui, "ask_yn", _no_prompt)

    result = installers.install_statusline(_make_ctx(assume_yes=True, updating=True))

    assert result is False
    preserved = json.loads(settings.SETTINGS_FILE.read_text())["statusLine"]["command"]
    assert preserved == "my-own-statusline", \
        "The user's statusline must survive an update untouched"


def test_update_refreshes_own_statusline(isolated_home: Path) -> None:
    """When statusLine already points at missioncache-statusline, an update
    keeps it wired (idempotent re-set, no backup)."""
    settings.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    settings.SETTINGS_FILE.write_text(json.dumps({
        "statusLine": {"type": "command", "command": "missioncache-statusline"}
    }))

    result = installers.install_statusline(_make_ctx(updating=True))

    assert result is True
    cmd = json.loads(settings.SETTINGS_FILE.read_text())["statusLine"]["command"]
    assert cmd == "missioncache-statusline"


# ---------------------------------------------------------------------------
# _install_plugin_pypi: update must refresh marketplace + plugin
# ---------------------------------------------------------------------------

def _record_claude_calls(
    monkeypatch: pytest.MonkeyPatch, *, failing_prefixes: tuple[tuple[str, ...], ...] = ()
) -> list[list[str]]:
    """Fake subprocess runs of the claude CLI, recording every command."""
    import subprocess

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        for prefix in failing_prefixes:
            if tuple(cmd[: len(prefix)]) == prefix:
                raise subprocess_utils.CommandFailed(list(cmd), 1, "", "boom")
        return subprocess.CompletedProcess(list(cmd), 0, "", "")

    monkeypatch.setattr(installers.subprocess_utils, "run", fake_run)
    monkeypatch.setattr(installers.shutil, "which", lambda _n: "/usr/bin/claude")
    return calls


def test_plugin_update_refreshes_marketplace_and_updates(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """During --update the pypi plugin path must run `marketplace update` and
    `plugins update` - `marketplace add` does not re-fetch an existing
    marketplace and `plugins install` no-ops on an installed plugin, so
    without these the plugin stays at install-time version forever."""
    calls = _record_claude_calls(monkeypatch)

    installers._install_plugin_pypi(updating=True)

    assert ["claude", "plugins", "marketplace", "update",
            installers.PLUGIN_MARKETPLACE_NAME] in calls
    assert ["claude", "plugins", "update", installers.PLUGIN_ID_PYPI] in calls
    assert not any(c[:3] == ["claude", "plugins", "install"] for c in calls), \
        "A successful plugin update must not fall through to install"


def test_plugin_update_falls_back_to_install(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`plugins update` failing (e.g. plugin not actually installed) falls
    back to the plain install path instead of aborting the update."""
    calls = _record_claude_calls(
        monkeypatch, failing_prefixes=(("claude", "plugins", "update"),)
    )

    installers._install_plugin_pypi(updating=True)

    assert ["claude", "plugins", "install", installers.PLUGIN_ID_PYPI] in calls


def test_plugin_fresh_install_does_not_run_update_commands(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh install keeps the original add+install shape - no update
    subcommands."""
    calls = _record_claude_calls(monkeypatch)

    installers._install_plugin_pypi(updating=False)

    assert ["claude", "plugins", "install", installers.PLUGIN_ID_PYPI] in calls
    assert not any("update" in c for c in calls)


def test_plugin_marketplace_refresh_failure_propagates(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed marketplace refresh must NOT proceed to `plugins update`:
    updating against stale metadata would no-op "already latest" and record a
    false success - the stuck-plugin bug this path exists to fix. The failure
    propagates so the component is recorded failed and retried next update."""
    calls = _record_claude_calls(
        monkeypatch,
        failing_prefixes=(("claude", "plugins", "marketplace", "update"),),
    )

    with pytest.raises(subprocess_utils.CommandFailed):
        installers._install_plugin_pypi(updating=True)

    assert not any(
        c[:3] == ["claude", "plugins", "update"] for c in calls
    ), "plugins update must not run against a stale marketplace"
    assert not any(
        c[:3] == ["claude", "plugins", "install"] for c in calls
    ), "the install fallback must not mask the refresh failure either"


def test_plugin_refresh_failure_is_recorded_for_retry(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through install_components: the refresh failure lands in
    failed_components, so the next --update retries the plugin."""
    _record_claude_calls(
        monkeypatch,
        failing_prefixes=(("claude", "plugins", "marketplace", "update"),),
    )

    failed = installers.install_components(["plugin"], _make_ctx(updating=True))

    assert failed == ["plugin"]
    assert state.failed_components() == ["plugin"]
    assert "plugin" not in state.load().get("components", {})

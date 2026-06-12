"""Tests for MISSIONCACHE_DASHBOARD_URL env var read at statusline import time.

The statusline module reads MISSIONCACHE_DASHBOARD_URL exactly once - at import -
into a module-level ``_DASHBOARD_URL`` binding. The whole statusline's
OSC 8 hyperlinks point at whatever value that binding ends up holding.
If the env-var name or the default URL is silently renamed (e.g. by a
mechanical rename sweep) and no consumer is updated, the statusline's
clickable links break with zero test signal. These tests pin the
contract: the env-var name, the binding name, and the default URL.

The binding is initialized at import time, so monkeypatching the env
var after import has no effect on the live module. To verify the
import-time read, each test reloads the module under a controlled env
state and restores it in teardown so other tests see the original
binding.
"""

import importlib
import os

import pytest

import missioncache_dashboard.statusline as mod


@pytest.fixture
def reload_statusline():
    """Reload the statusline module and restore it after the test.

    Yields a callable that triggers a reload against the current
    environment. After the test, reloads one final time under the
    original MISSIONCACHE_DASHBOARD_URL value so module-level state matches
    what every other test in the suite expects.
    """
    original_url_env = os.environ.get("MISSIONCACHE_DASHBOARD_URL")

    def _reload():
        return importlib.reload(mod)

    try:
        yield _reload
    finally:
        # Restore the original env state, then reload one final time
        # so the module-level _DASHBOARD_URL binding matches what the
        # rest of the suite saw at original import.
        if original_url_env is None:
            os.environ.pop("MISSIONCACHE_DASHBOARD_URL", None)
        else:
            os.environ["MISSIONCACHE_DASHBOARD_URL"] = original_url_env
        importlib.reload(mod)


class TestDashboardUrlEnvVar:
    def test_env_var_overrides_default(self, monkeypatch, reload_statusline):
        """When MISSIONCACHE_DASHBOARD_URL is set, the module-level binding picks it up."""
        monkeypatch.setenv("MISSIONCACHE_DASHBOARD_URL", "http://from-env:9999")
        reloaded = reload_statusline()
        assert reloaded._DASHBOARD_URL == "http://from-env:9999"

    def test_default_when_env_var_absent(self, monkeypatch, reload_statusline):
        """When MISSIONCACHE_DASHBOARD_URL is unset, the default localhost URL is used."""
        monkeypatch.delenv("MISSIONCACHE_DASHBOARD_URL", raising=False)
        reloaded = reload_statusline()
        assert reloaded._DASHBOARD_URL == "http://localhost:8787"

    def test_env_var_exact_name(self, monkeypatch, reload_statusline):
        """A near-miss env var name must NOT be read.

        Pins the exact env var spelling. If the rename sweep renames the
        env-var lookup but leaves consumers exporting the old name, or
        vice-versa, this test catches it.
        """
        monkeypatch.delenv("MISSIONCACHE_DASHBOARD_URL", raising=False)
        # Set near-miss names that must NOT be picked up.
        monkeypatch.setenv("MISSIONCACHE_DASHBOARD_URI", "http://wrong-var:1111")
        monkeypatch.setenv("DASHBOARD_URL", "http://wrong-var:2222")
        monkeypatch.setenv("ORBIT_DASHBOARD_URL", "http://wrong-var:3333")
        reloaded = reload_statusline()
        assert reloaded._DASHBOARD_URL == "http://localhost:8787"

    def test_default_url_exact_value(self, monkeypatch, reload_statusline):
        """Pins the default URL string exactly.

        Both the host (localhost) and port (8787) are load-bearing -
        the dashboard launchd service listens on 8787, and changing
        either silently breaks every clickable link the statusline
        renders. This test asserts the full string, not a substring
        match.
        """
        monkeypatch.delenv("MISSIONCACHE_DASHBOARD_URL", raising=False)
        reloaded = reload_statusline()
        assert reloaded._DASHBOARD_URL == "http://localhost:8787"

    def test_empty_string_env_var_overrides_default(self, monkeypatch, reload_statusline):
        """An empty-string env value overrides the default.

        ``os.environ.get(name, default)`` returns "" when name is set
        to the empty string, not the default. This is the documented
        Python behavior and the statusline relies on it; pin it here
        so a "helpful" rename that switches to ``or default`` fallback
        semantics gets flagged.
        """
        monkeypatch.setenv("MISSIONCACHE_DASHBOARD_URL", "")
        reloaded = reload_statusline()
        assert reloaded._DASHBOARD_URL == ""

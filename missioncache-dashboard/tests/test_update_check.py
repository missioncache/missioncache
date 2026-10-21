"""Tests for MissionCache update discovery.

Spec source: the update-discovery contract - installed sentinel packages
are compared against PyPI, the answer is cached with a TTL at
``~/.missioncache/update-check.json``, a fresh cache short-circuits the
fetch, fetch failure returns the previous answer (stale beats none) while
refreshing the timestamp so offline machines do not re-fetch per render,
a newer LOCAL version (maintainer machine) is not an update, and an
editable (pip install -e) package never nags - its metadata version is
frozen at install time, so comparing it to PyPI is meaningless and the
update command could never clear the nag (2026-07-28 fix).
"""

import json
import time

import pytest

import missioncache_dashboard.update_check as uc


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    cache = tmp_path / "update-check.json"
    monkeypatch.setattr(uc, "CACHE_PATH", cache)
    # Keep this machine's own plugin and mcp-missioncache installs out.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setattr(uc.shutil, "which", lambda name: None)
    return cache


class _FakeDist:
    """Distribution stub controlling what direct_url.json says."""

    def __init__(self, direct_url: str | None) -> None:
        self._direct_url = direct_url

    def read_text(self, name):
        assert name == "direct_url.json"
        return self._direct_url


def _install_fakes(monkeypatch, installed: dict, latest: dict, editable: set | None = None):
    """Fake version metadata, PyPI, and distribution records.

    Faking metadata.distribution matters even for tests that don't care
    about editable installs: without it, the real environment's own install
    kind (editable on a maintainer clone) would leak into the comparison
    and flip results per machine.
    """
    editable = editable or set()

    def fake_version(pkg):
        if pkg in installed:
            return installed[pkg]
        raise uc.metadata.PackageNotFoundError(pkg)

    def fake_distribution(pkg):
        if pkg in editable:
            return _FakeDist(json.dumps(
                {"dir_info": {"editable": True}, "url": "file:///clone/pkg"}
            ))
        return _FakeDist(None)

    monkeypatch.setattr(uc.metadata, "version", fake_version)
    monkeypatch.setattr(uc.metadata, "distribution", fake_distribution)
    monkeypatch.setattr(uc, "_fetch_latest", lambda pkg, timeout: latest.get(pkg))


class TestParseVersion:
    def test_orders_numerically_not_lexically(self):
        assert uc._parse_version("1.0.10") > uc._parse_version("1.0.9")

    def test_malformed_segment_collapses_to_zero(self):
        assert uc._parse_version("1.x.2") == (1, 0, 2)


class TestGetUpdateStatus:
    def test_outdated_package_flags_update(self, sandbox, monkeypatch):
        _install_fakes(
            monkeypatch,
            {"missioncache-db": "1.0.13", "missioncache-dashboard": "1.0.7"},
            {"missioncache-db": "1.0.13", "missioncache-dashboard": "1.0.8"},
        )
        status = uc.get_update_status()
        assert status["update_available"] is True
        assert status["packages"]["missioncache-dashboard"]["outdated"] is True
        assert status["packages"]["missioncache-db"]["outdated"] is False
        assert status["command"] == uc.UPDATE_COMMAND

    def test_all_current_means_no_update(self, sandbox, monkeypatch):
        _install_fakes(
            monkeypatch,
            {"missioncache-db": "1.0.13"},
            {"missioncache-db": "1.0.13"},
        )
        assert uc.get_update_status()["update_available"] is False

    def test_newer_local_is_not_an_update(self, sandbox, monkeypatch):
        """Maintainer machine: local 1.0.8 vs published 1.0.7."""
        _install_fakes(
            monkeypatch,
            {"missioncache-dashboard": "1.0.8"},
            {"missioncache-dashboard": "1.0.7"},
        )
        status = uc.get_update_status()
        assert status["update_available"] is False

    def test_missing_package_skipped(self, sandbox, monkeypatch):
        _install_fakes(monkeypatch, {}, {"missioncache-db": "9.9.9"})
        status = uc.get_update_status()
        assert status["packages"] == {}
        assert status["update_available"] is False

    def test_fresh_cache_short_circuits_fetch(self, sandbox, monkeypatch):
        sandbox.write_text(json.dumps({
            "checked_at": time.time(),
            "update_available": True,
            "packages": {"x": {}},
            "command": uc.UPDATE_COMMAND,
        }))

        def boom(pkg, timeout):
            raise AssertionError("fetch must not run on a fresh cache")

        monkeypatch.setattr(uc, "_fetch_latest", boom)
        assert uc.get_update_status()["update_available"] is True

    def test_stale_cache_refetches_and_rewrites(self, sandbox, monkeypatch):
        sandbox.write_text(json.dumps({
            "checked_at": time.time() - uc.CACHE_TTL - 10,
            "update_available": True,
            "packages": {},
            "command": uc.UPDATE_COMMAND,
        }))
        _install_fakes(
            monkeypatch,
            {"missioncache-db": "1.0.13"},
            {"missioncache-db": "1.0.13"},
        )
        status = uc.get_update_status()
        assert status["update_available"] is False
        on_disk = json.loads(sandbox.read_text())
        assert on_disk["update_available"] is False

    def test_fetch_failure_keeps_stale_answer_but_stamps_time(self, sandbox, monkeypatch):
        old = time.time() - uc.CACHE_TTL - 10
        sandbox.write_text(json.dumps({
            "checked_at": old,
            "update_available": True,
            "packages": {"missioncache-db": {"outdated": True}},
            "command": uc.UPDATE_COMMAND,
        }))
        monkeypatch.setattr(
            uc.metadata, "version", lambda pkg: "1.0.0"
        )

        def boom(pkg, timeout):
            raise OSError("offline")

        monkeypatch.setattr(uc, "_fetch_latest", boom)
        status = uc.get_update_status()
        assert status["update_available"] is True  # stale beats none
        assert status["checked_at"] > old  # no per-render refetch storm
        assert status["error"] == "fetch failed"
        assert json.loads(sandbox.read_text())["checked_at"] > old

    def test_fetch_failure_with_no_cache_is_neutral(self, sandbox, monkeypatch):
        monkeypatch.setattr(uc.metadata, "version", lambda pkg: "1.0.0")

        def boom(pkg, timeout):
            raise OSError("offline")

        monkeypatch.setattr(uc, "_fetch_latest", boom)
        status = uc.get_update_status()
        assert status["update_available"] is False
        assert status["error"] == "fetch failed"


class TestEditableInstalls:
    def test_editable_behind_pypi_never_nags(self, sandbox, monkeypatch):
        """A contributor's editable install looks behind (frozen metadata)
        but must not produce a nag - no update command can clear it."""
        _install_fakes(
            monkeypatch,
            {"missioncache-db": "1.0.14"},
            {"missioncache-db": "1.0.15"},
            editable={"missioncache-db"},
        )
        status = uc.get_update_status()
        assert status["update_available"] is False
        entry = status["packages"]["missioncache-db"]
        assert entry["outdated"] is False
        assert entry["editable"] is True, \
            "The entry must be labeled so UIs can explain why no update applies"

    def test_regular_package_still_nags_beside_editable(self, sandbox, monkeypatch):
        """Editable exclusion is per-package: a genuinely outdated regular
        install must keep nagging."""
        _install_fakes(
            monkeypatch,
            {"missioncache-db": "1.0.14", "missioncache-dashboard": "1.0.7"},
            {"missioncache-db": "1.0.15", "missioncache-dashboard": "1.0.8"},
            editable={"missioncache-db"},
        )
        status = uc.get_update_status()
        assert status["update_available"] is True
        assert status["packages"]["missioncache-dashboard"]["outdated"] is True
        assert status["packages"]["missioncache-db"]["outdated"] is False

    def test_regular_install_carries_no_editable_key(self, sandbox, monkeypatch):
        """Regular installs keep the pre-fix entry shape."""
        _install_fakes(
            monkeypatch,
            {"missioncache-db": "1.0.14"},
            {"missioncache-db": "1.0.15"},
        )
        entry = uc.get_update_status()["packages"]["missioncache-db"]
        assert "editable" not in entry

    def test_unreadable_direct_url_falls_back_to_comparison(
        self, sandbox, monkeypatch
    ):
        """Editable detection must never take down the check - a distribution
        whose direct_url.json read blows up is treated as a regular install."""

        class _ExplodingDist:
            def read_text(self, name):
                raise OSError("unreadable metadata")

        monkeypatch.setattr(uc.metadata, "version", lambda pkg: "1.0.0")
        monkeypatch.setattr(uc.metadata, "distribution", lambda pkg: _ExplodingDist())
        monkeypatch.setattr(uc, "_fetch_latest", lambda pkg, timeout: "1.0.1")
        status = uc.get_update_status()
        assert status["update_available"] is True


class TestPluginAndMcp:
    """The two installs the PyPI loop cannot import: a release that bumps only
    the plugin or only mcp-missioncache must still show as an update."""

    def _record_plugin(self, tmp_path, plugin_id, version):
        plugins = tmp_path / "claude" / "plugins"
        plugins.mkdir(parents=True)
        (plugins / "installed_plugins.json").write_text(json.dumps(
            {"version": 2, "plugins": {plugin_id: [{"scope": "user", "version": version}]}}
        ))

    def _mcp_on_path(self, tmp_path, monkeypatch, version):
        venv = tmp_path / "tools" / "mcp-missioncache"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "mcp-missioncache").write_text("#!/bin/sh\n")
        dist = venv / "lib" / "python3.13" / "site-packages" / f"mcp_missioncache-{version}.dist-info"
        dist.mkdir(parents=True)
        (dist / "METADATA").write_text(f"Metadata-Version: 2.1\nName: mcp-missioncache\nVersion: {version}\n")
        link = tmp_path / "bin" / "mcp-missioncache"
        link.parent.mkdir()
        link.symlink_to(venv / "bin" / "mcp-missioncache")
        monkeypatch.setattr(uc.shutil, "which", lambda name: str(link))

    def test_plugin_behind_its_manifest_flags_update(self, sandbox, tmp_path, monkeypatch):
        self._record_plugin(tmp_path, uc.PLUGIN_ID, "1.0.40")
        _install_fakes(monkeypatch, {}, {uc.PLUGIN_KEY: "1.0.41"})
        status = uc.get_update_status()
        assert status["update_available"] is True
        assert status["packages"][uc.PLUGIN_KEY] == {
            "installed": "1.0.40", "latest": "1.0.41", "outdated": True,
        }

    def test_local_marketplace_plugin_is_not_checked(self, sandbox, tmp_path, monkeypatch):
        """missioncache@local is a maintainer checkout: no update applies."""
        self._record_plugin(tmp_path, "missioncache@local", "1.0.40")
        _install_fakes(monkeypatch, {}, {uc.PLUGIN_KEY: "1.0.41"})
        assert uc.get_update_status()["packages"] == {}

    def test_mcp_on_path_behind_pypi_flags_update(self, sandbox, tmp_path, monkeypatch):
        self._mcp_on_path(tmp_path, monkeypatch, "1.0.42")
        _install_fakes(monkeypatch, {}, {uc.MCP_DIST: "1.0.43"})
        status = uc.get_update_status()
        assert status["update_available"] is True
        assert status["packages"][uc.MCP_DIST]["installed"] == "1.0.42"
        assert status["packages"][uc.MCP_DIST]["outdated"] is True

    def test_one_unreachable_host_spares_the_others(self, sandbox, tmp_path, monkeypatch):
        """GitHub down must not freeze the PyPI answers, and the plugin keeps
        its last known answer instead of vanishing."""
        sandbox.write_text(json.dumps({
            "checked_at": time.time() - uc.CACHE_TTL - 10,
            "update_available": False,
            "packages": {uc.PLUGIN_KEY: {"installed": "1.0.40", "latest": "1.0.40", "outdated": False}},
            "command": uc.UPDATE_COMMAND,
        }))
        self._record_plugin(tmp_path, uc.PLUGIN_ID, "1.0.40")
        _install_fakes(monkeypatch, {"missioncache-db": "1.0.36"}, {})

        def fetch(pkg, timeout):
            if pkg == uc.PLUGIN_KEY:
                raise OSError("github unreachable")
            return "1.0.37"

        monkeypatch.setattr(uc, "_fetch_latest", fetch)
        status = uc.get_update_status()
        assert "error" not in status
        assert status["update_available"] is True
        assert status["packages"]["missioncache-db"]["latest"] == "1.0.37"
        assert status["packages"][uc.PLUGIN_KEY]["latest"] == "1.0.40"

    def test_no_mcp_on_path_adds_nothing(self, sandbox, monkeypatch):
        _install_fakes(monkeypatch, {}, {uc.MCP_DIST: "1.0.43"})
        assert uc.get_update_status()["packages"] == {}


class TestEndpoint:
    def test_endpoint_returns_the_checker_answer(self, monkeypatch):
        """Endpoint function called directly, mirroring test_version_endpoint."""
        from missioncache_dashboard import server

        sentinel = {"update_available": True, "packages": {}, "command": uc.UPDATE_COMMAND}
        monkeypatch.setattr(server.update_check, "get_update_status", lambda: sentinel)
        assert server.api_update_check() == sentinel

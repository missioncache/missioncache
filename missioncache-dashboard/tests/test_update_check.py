"""Tests for MissionCache update discovery.

Spec source: the update-discovery contract - installed sentinel packages
are compared against PyPI, the answer is cached with a TTL at
``~/.missioncache/update-check.json``, a fresh cache short-circuits the
fetch, fetch failure returns the previous answer (stale beats none) while
refreshing the timestamp so offline machines do not re-fetch per render,
and a newer LOCAL version (maintainer machine) is not an update.
"""

import json
import time

import pytest

import missioncache_dashboard.update_check as uc


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    cache = tmp_path / "update-check.json"
    monkeypatch.setattr(uc, "CACHE_PATH", cache)
    return cache


def _install_fakes(monkeypatch, installed: dict, latest: dict):
    def fake_version(pkg):
        if pkg in installed:
            return installed[pkg]
        raise uc.metadata.PackageNotFoundError(pkg)

    monkeypatch.setattr(uc.metadata, "version", fake_version)
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


class TestEndpoint:
    def test_endpoint_returns_the_checker_answer(self, monkeypatch):
        """Endpoint function called directly, mirroring test_version_endpoint."""
        from missioncache_dashboard import server

        sentinel = {"update_available": True, "packages": {}, "command": uc.UPDATE_COMMAND}
        monkeypatch.setattr(server.update_check, "get_update_status", lambda: sentinel)
        assert server.api_update_check() == sentinel

"""Tests for orbit-dashboard/lib/config.py."""

import json

import pytest

from lib import config


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    """Redirect CONFIG_FILE to a temp dir and clear env vars for each test."""
    tmp_file = tmp_path / "orbit-dashboard-config.json"
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_file)
    monkeypatch.delenv("ORBIT_DASHBOARD_URL", raising=False)
    return tmp_file


class TestDefaults:
    def test_missing_file_returns_defaults(self, tmp_config):
        assert config.get_jira_urls() == {}
        assert config.get_author_emails() == []
        assert config.get_repo_overrides() == {}
        assert config.get_dashboard_url() == "http://localhost:8787"

    def test_empty_file_returns_defaults(self, tmp_config):
        tmp_config.write_text("{}")
        assert config.get_jira_urls() == {}
        assert config.get_author_emails() == []

    def test_corrupt_file_returns_defaults(self, tmp_config):
        tmp_config.write_text("{not: valid, json,,,")
        assert config.get_jira_urls() == {}
        assert config.get_dashboard_url() == "http://localhost:8787"

    def test_non_dict_root_returns_defaults(self, tmp_config):
        tmp_config.write_text("[1, 2, 3]")
        assert config.get_jira_urls() == {}


class TestJiraUrls:
    def test_set_and_get(self, tmp_config):
        config.set_jira_urls({"PROJ-": "https://example.com/browse/"})
        assert config.get_jira_urls() == {"PROJ-": "https://example.com/browse/"}

    def test_set_persists_to_disk(self, tmp_config):
        config.set_jira_urls({"GC-": "https://x/y/"})
        on_disk = json.loads(tmp_config.read_text())
        assert on_disk["jira_urls"] == {"GC-": "https://x/y/"}

    def test_set_overwrites_previous(self, tmp_config):
        config.set_jira_urls({"A-": "https://a/"})
        config.set_jira_urls({"B-": "https://b/"})
        assert config.get_jira_urls() == {"B-": "https://b/"}

    def test_get_returns_copy(self, tmp_config):
        config.set_jira_urls({"X-": "https://x/"})
        result = config.get_jira_urls()
        result["Y-"] = "https://y/"
        assert config.get_jira_urls() == {"X-": "https://x/"}


class TestAuthorEmails:
    def test_set_and_get(self, tmp_config):
        config.set_author_emails(["a@b.com", "c@d.com"])
        assert config.get_author_emails() == ["a@b.com", "c@d.com"]

    def test_set_empty(self, tmp_config):
        config.set_author_emails(["a@b.com"])
        config.set_author_emails([])
        assert config.get_author_emails() == []


class TestRepoOverrides:
    def test_set_and_get(self, tmp_config):
        overrides = {
            "/path/to/repo": {"display_name": "My Repo", "hidden": False},
            "/path/to/other": {"display_name": None, "hidden": True},
        }
        config.set_repo_overrides(overrides)
        assert config.get_repo_overrides() == overrides


class TestDashboardUrl:
    def test_default(self, tmp_config):
        assert config.get_dashboard_url() == "http://localhost:8787"

    def test_file_value(self, tmp_config):
        tmp_config.write_text('{"dashboard_url": "http://localhost:9999"}')
        assert config.get_dashboard_url() == "http://localhost:9999"

    def test_env_var_overrides_file(self, tmp_config, monkeypatch):
        tmp_config.write_text('{"dashboard_url": "http://localhost:9999"}')
        monkeypatch.setenv("ORBIT_DASHBOARD_URL", "http://from-env:1234")
        assert config.get_dashboard_url() == "http://from-env:1234"

    def test_env_var_overrides_default(self, tmp_config, monkeypatch):
        monkeypatch.setenv("ORBIT_DASHBOARD_URL", "http://from-env:1234")
        assert config.get_dashboard_url() == "http://from-env:1234"


class TestStatuslineConfig:
    def test_default(self, tmp_config):
        cfg = config.get_statusline_config()
        assert cfg["codex"] is True
        assert cfg["subscription_usage"] is True
        assert cfg["subscription_type"] is True
        assert cfg["claude_status"] is True
        assert cfg["claude_status_services"] == ["Code", "Claude API"]

    def test_set_and_get(self, tmp_config):
        config.set_statusline_config({
            "codex": False,
            "subscription_usage": True,
            "subscription_type": False,
            "claude_status": True,
            "claude_status_services": ["Code"],
        })
        cfg = config.get_statusline_config()
        assert cfg["codex"] is False
        assert cfg["subscription_type"] is False
        assert cfg["claude_status_services"] == ["Code"]

    def test_partial_config_fills_defaults(self, tmp_config):
        """An older dashboard may have written only a subset of keys."""
        tmp_config.write_text(json.dumps({"statusline": {"codex": False}}))
        cfg = config.get_statusline_config()
        assert cfg["codex"] is False
        assert cfg["subscription_usage"] is True  # filled from default
        assert cfg["claude_status_services"] == ["Code", "Claude API"]

    def test_non_dict_statusline_returns_all_defaults(self, tmp_config):
        tmp_config.write_text(json.dumps({"statusline": "not-a-dict"}))
        cfg = config.get_statusline_config()
        assert cfg == {
            "codex": True,
            "subscription_usage": True,
            "subscription_type": True,
            "claude_status": True,
            "claude_status_services": ["Code", "Claude API"],
        }


class TestAtomicWrite:
    def test_concurrent_updates_preserve_both(self, tmp_config):
        """Sequential set_X and set_Y should not clobber each other."""
        config.set_jira_urls({"A-": "https://a/"})
        config.set_author_emails(["me@x.com"])
        assert config.get_jira_urls() == {"A-": "https://a/"}
        assert config.get_author_emails() == ["me@x.com"]

    def test_no_leftover_tempfiles(self, tmp_config):
        config.set_jira_urls({"A-": "https://a/"})
        parent = tmp_config.parent
        tempfiles = list(parent.glob(".orbit-dashboard-config.*.tmp"))
        assert tempfiles == []

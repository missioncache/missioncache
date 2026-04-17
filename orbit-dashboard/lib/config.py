"""Dashboard configuration loaded from ~/.claude/orbit-dashboard-config.json.

Precedence: environment variable > config file > hardcoded default.
Read on every call. The file is tiny and sits in the OS page cache, and
settings are accessed rarely enough that a stale-cache failure mode would
cost more than the cache would save.

Schema (all keys optional; missing keys use defaults):

    {
      "jira_urls":     {"PROJ-": "https://example.com/jira/browse/", ...},
      "author_emails": ["me@example.com", "work@example.com"],
      "repos":         {"/abs/path": {"display_name": "My Project", "hidden": false}},
      "dashboard_url": "http://localhost:8787",
      "statusline": {
          "codex":                  true,
          "subscription_usage":     true,
          "subscription_type":      true,
          "claude_status":          true,
          "claude_status_services": ["Code", "Claude API"]
      }
    }

Writes go through an atomic tempfile + os.replace pattern so a crash
mid-save cannot leave a half-written config on disk.
"""

import json
import os
import tempfile
from pathlib import Path
from typing import Any

CONFIG_FILE = Path.home() / ".claude" / "orbit-dashboard-config.json"

_DEFAULT_STATUSLINE: dict[str, Any] = {
    "codex": True,
    "subscription_usage": True,
    "subscription_type": True,
    "claude_status": True,
    "claude_status_services": ["Code", "Claude API"],
}

DEFAULTS: dict[str, Any] = {
    "jira_urls": {},
    "author_emails": [],
    "repos": {},
    "dashboard_url": "http://localhost:8787",
    "statusline": _DEFAULT_STATUSLINE,
}


def _read() -> dict[str, Any]:
    """Return the config file contents merged over defaults.

    A missing file, a file with invalid JSON, or a read error all return
    the defaults - the dashboard must keep running even if the config is
    temporarily broken.
    """
    if not CONFIG_FILE.exists():
        return dict(DEFAULTS)
    try:
        with open(CONFIG_FILE) as f:
            file_data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULTS)
    if not isinstance(file_data, dict):
        return dict(DEFAULTS)
    return {**DEFAULTS, **file_data}


def _write(data: dict[str, Any]) -> None:
    """Atomically write to CONFIG_FILE via tempfile + os.replace.

    Writes a sorted, 2-space-indented JSON file with a trailing newline
    so `cat` output and diff-friendliness are maximized.
    """
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=CONFIG_FILE.parent,
        prefix=".orbit-dashboard-config.",
        suffix=".tmp",
        delete=False,
    ) as tf:
        json.dump(data, tf, indent=2, sort_keys=True)
        tf.write("\n")
        tempname = tf.name
    os.replace(tempname, CONFIG_FILE)


def _update(key: str, value: Any) -> None:
    """Read-modify-write a single top-level key."""
    data = _read()
    data[key] = value
    _write(data)


# ============ Tier 1 getters / setters ============


def get_jira_urls() -> dict[str, str]:
    """Return the JIRA prefix-to-base-URL mapping."""
    return dict(_read()["jira_urls"])


def set_jira_urls(mapping: dict[str, str]) -> None:
    """Replace the JIRA URL mapping and persist to disk."""
    _update("jira_urls", dict(mapping))


def get_author_emails() -> list[str]:
    """Return the configured author email allowlist.

    When this list is empty, callers should fall back to per-repo
    `git config user.email` for backwards compatibility.
    """
    return list(_read()["author_emails"])


def set_author_emails(emails: list[str]) -> None:
    """Replace the author email allowlist and persist to disk."""
    _update("author_emails", list(emails))


def get_repo_overrides() -> dict[str, dict[str, Any]]:
    """Return per-repo display overrides keyed by absolute path.

    Each value is a dict with `display_name` (str | None) and `hidden` (bool).
    Repos not in the map use their default short_name and are visible.
    """
    return dict(_read()["repos"])


def set_repo_overrides(overrides: dict[str, dict[str, Any]]) -> None:
    """Replace the repo overrides map and persist to disk."""
    _update("repos", dict(overrides))


def get_dashboard_url() -> str:
    """Return the dashboard URL.

    Precedence: ORBIT_DASHBOARD_URL env var, config file, hardcoded default.
    """
    return os.environ.get("ORBIT_DASHBOARD_URL") or _read()["dashboard_url"]


def get_statusline_config() -> dict[str, Any]:
    """Return the statusline visibility + status-services config.

    Any keys missing from the on-disk config fall back to per-key defaults,
    so partial configs written by older dashboards stay valid.
    """
    raw = _read().get("statusline")
    if not isinstance(raw, dict):
        return dict(_DEFAULT_STATUSLINE)
    merged = dict(_DEFAULT_STATUSLINE)
    for k in _DEFAULT_STATUSLINE:
        if k in raw:
            merged[k] = raw[k]
    return merged


def set_statusline_config(cfg: dict[str, Any]) -> None:
    """Replace the statusline config and persist to disk."""
    _update("statusline", dict(cfg))

"""Tests for server startup on a fresh machine.

Spec source: the install contract - the dashboard must start on a machine
where nothing else has run yet. A host that never ran Claude Code has no
~/.claude directory, and sqlite cannot create a database file inside a
missing directory; startup crash-looped the service until the parent
mkdir landed (found by the CI installer smoke test).
"""

import missioncache_dashboard.server as srv


def test_init_hooks_state_db_creates_missing_parent_dir(tmp_path, monkeypatch):
    db_path = tmp_path / "never-created" / "hooks-state.db"
    monkeypatch.setattr(srv, "HOOKS_STATE_DB", db_path)

    srv._init_hooks_state_db()

    assert db_path.is_file()


def test_init_hooks_state_db_idempotent_on_existing_dir(tmp_path, monkeypatch):
    db_path = tmp_path / "hooks-state.db"
    monkeypatch.setattr(srv, "HOOKS_STATE_DB", db_path)

    srv._init_hooks_state_db()
    srv._init_hooks_state_db()

    assert db_path.is_file()

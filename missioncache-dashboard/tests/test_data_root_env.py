"""Tests that the dashboard resolves the data root through missioncache_db.

Spec source: the data-root ownership rule stated at
``missioncache-db/missioncache_db/__init__.py`` (the MISSIONCACHE_ROOT /
DB_PATH block). ``missioncache_db`` owns the data root and the task DB path;
every consumer resolves through it, and a module may keep its own constant only
for a path that extends the root into an artifact it owns.

The dashboard broke that rule in three different ways at once, inside one file:
``server.py`` held its own ``Path.home() / ".missioncache"`` constant, two
functions rebuilt ``tasks.db`` from scratch, and ``get_today`` lazily imported
the real constant so ``/api/today`` honored MISSIONCACHE_ROOT while the
endpoints beside it did not.

The env case runs in a subprocess: these constants resolve at import, so
setting the variable afterwards proves nothing.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import missioncache_dashboard.server as server

_PROBE = (
    "import json; "
    "import missioncache_dashboard.server as sv; "
    "import missioncache_dashboard.update_check as uc; "
    "from missioncache_dashboard.lib import analytics_db as adb; "
    "print(json.dumps({"
    "'server_root': str(sv._mc_root()), "
    "'duckdb': str(adb.DUCKDB_PATH), "
    "'sqlite': str(adb.SQLITE_PATH), "
    "'update_cache': str(uc.CACHE_PATH)}))"
)


def _probe(**env_overrides):
    """Every dashboard-owned data path, as resolved by a fresh interpreter.

    HOME and USERPROFILE are both pinned because ``ntpath.expanduser`` consults
    USERPROFILE first and CI runs a windows-latest job.
    """
    env = {**os.environ}
    for key, value in env_overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    result = subprocess.run(
        [sys.executable, "-c", _PROBE], capture_output=True, text=True, env=env
    )
    assert result.returncode == 0, result.stderr
    return {k: Path(v) for k, v in json.loads(result.stdout).items()}


class TestServerHasNoRootConstantOfItsOwn:
    def test_the_module_does_not_bind_the_root_at_import(self):
        """``server.MISSIONCACHE_ROOT`` must stay absent.

        Mirrors the rule enforced for missioncache-auto in
        ``test_task_paths_init.py``. Reintroducing the name, whether as a
        literal or as ``from missioncache_db import MISSIONCACHE_ROOT``, would
        snapshot the real home at import and put this file back to needing a
        second monkeypatch in every fixture. If someone adds it, this fails,
        which is the signal we want.
        """
        assert not hasattr(server, "MISSIONCACHE_ROOT")


class TestEveryDashboardPathFollowsTheRoot:
    def test_the_override_moves_all_of_them(self, tmp_path):
        root = tmp_path / "override"
        paths = _probe(MISSIONCACHE_ROOT=str(root))

        assert paths["server_root"] == root
        assert paths["duckdb"] == root / "tasks.duckdb"
        assert paths["sqlite"] == root / "tasks.db"
        assert paths["update_cache"] == root / "update-check.json"

    def test_set_but_empty_falls_back_to_home_not_cwd(self, tmp_path):
        """An empty value must not resolve to the working directory, which is
        what ``Path("")`` gives and what the ``or`` in missioncache_db's own
        resolution exists to prevent."""
        home = tmp_path / "home"
        expected = home / ".missioncache"
        paths = _probe(
            MISSIONCACHE_ROOT="", HOME=str(home), USERPROFILE=str(home)
        )

        assert paths["server_root"] == expected
        assert paths["duckdb"] == expected / "tasks.duckdb"
        assert paths["sqlite"] == expected / "tasks.db"
        assert paths["update_cache"] == expected / "update-check.json"

    def test_unset_resolves_under_home(self, tmp_path):
        """Positive control: the default path still works, so the tests above
        are not passing merely because everything moved."""
        home = tmp_path / "home"
        paths = _probe(
            MISSIONCACHE_ROOT=None, HOME=str(home), USERPROFILE=str(home)
        )

        assert paths["server_root"] == home / ".missioncache"
        assert paths["sqlite"] == home / ".missioncache" / "tasks.db"

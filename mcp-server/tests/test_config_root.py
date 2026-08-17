"""Tests for the MCP server's data-root resolution.

Spec source: the data-root ownership rule stated at
``missioncache-db/missioncache_db/__init__.py`` (the MISSIONCACHE_ROOT /
DB_PATH block) - ``missioncache_db`` is the single owner of the data root and
the task DB path, and every consumer resolves through it.

What that rule means for this module: ``settings.root`` and
``settings.db_path`` must describe ONE location. They did not. ``root`` and
``db_path`` were independent fields, each with its own
``Path.home() / ".missioncache"`` literal, and ``env_prefix`` wired
MISSIONCACHE_ROOT onto only the first. Measured before the fix:

* ``MISSIONCACHE_ROOT=/tmp/x`` gave ``root=/tmp/x`` and
  ``db_path=~/.missioncache/tasks.db``, so one process wrote project files
  under the override and DB rows into the real home.
* ``MISSIONCACHE_ROOT=""`` gave ``root=Path(".")``, the server's working
  directory, which is the failure the ``or`` idiom in ``missioncache_db``
  exists to prevent.

The env cases run in a subprocess because ``Settings`` is instantiated at
import; setting the variable afterwards proves nothing.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from mcp_missioncache.config import Settings

_PROBE = (
    "import json; from mcp_missioncache.config import settings as s; "
    "print(json.dumps([str(s.root), str(s.db_path)]))"
)


def _probe(**env_overrides):
    """``(root, db_path)`` as resolved by a fresh interpreter.

    A ``None`` value removes the variable. HOME and USERPROFILE are both pinned
    by the callers that care: ``ntpath.expanduser`` consults USERPROFILE first,
    and CI runs a windows-latest job.
    """
    env = {**os.environ}
    for key, value in env_overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    root, db_path = json.loads(result.stdout)
    return Path(root), Path(db_path)


class TestDbPathFollowsRoot:
    """The invariant the original bug violated."""

    def test_db_path_lands_inside_an_explicit_root(self, tmp_path):
        """A caller that sets only the root gets a DB inside it.

        No env, no subprocess - this is the pure statement of the contract, and
        it fails on the pre-fix code.
        """
        settings = Settings(root=tmp_path)
        assert settings.db_path == tmp_path / "tasks.db"

    def test_an_explicit_db_path_still_wins(self, tmp_path):
        """Setting both keeps both, DB outside the root included.

        This is not a curiosity: four ``isolated_orbit`` fixtures deliberately
        put the DB outside the root, so a refactor that turned ``db_path`` into
        a computed property would break them. This test says that shape is
        supported on purpose.
        """
        elsewhere = tmp_path / "elsewhere" / "tasks.db"
        settings = Settings(root=tmp_path / "root", db_path=elsewhere)
        assert settings.root == tmp_path / "root"
        assert settings.db_path == elsewhere


class TestEnvOverride:
    """MISSIONCACHE_ROOT is an internal mechanism (tests, import into a fresh
    root). It is not documented for users, but it must be coherent: it moves
    everything or nothing."""

    def test_the_override_moves_both_paths(self, tmp_path):
        root = tmp_path / "override"
        got_root, got_db = _probe(MISSIONCACHE_ROOT=str(root))
        assert got_root == root
        assert got_db == root / "tasks.db", (
            "db_path stayed behind in the real home - the split this fixed"
        )

    def test_set_but_empty_falls_back_to_home_not_cwd(self, tmp_path):
        """An empty value must not resolve to the working directory.

        ``Path("")`` equals ``Path(".")`` and is truthy, so this can only be
        caught before pydantic coerces the value. Before the fix this returned
        ``.``, meaning the server wrote project files wherever it happened to
        be started from.
        """
        home = tmp_path / "home"
        got_root, got_db = _probe(
            MISSIONCACHE_ROOT="", HOME=str(home), USERPROFILE=str(home)
        )
        assert got_root == home / ".missioncache"
        assert got_db == home / ".missioncache" / "tasks.db"

    def test_an_explicit_db_path_env_var_is_honored(self, tmp_path):
        """MISSIONCACHE_DB_PATH still wins over the follow-root default.

        The env twin of the kwarg case above, and the assertion that pins the
        `model_fields_set` semantics the follow-root validator relies on: an
        env-sourced value counts as explicitly set. A future pydantic-settings
        change to that would otherwise silently start overwriting a DB path the
        operator chose.
        """
        root = tmp_path / "override"
        explicit = tmp_path / "elsewhere" / "chosen.db"
        got_root, got_db = _probe(
            MISSIONCACHE_ROOT=str(root), MISSIONCACHE_DB_PATH=str(explicit)
        )
        assert got_root == root
        assert got_db == explicit

    def test_an_empty_value_falls_back_on_every_field(self):
        """``env_ignore_empty`` covers the whole class, not just the root.

        ``MISSIONCACHE_ACTIVE_DIR_NAME=""`` used to collapse
        ``root/active/<name>`` to ``root/<name>``, which is the same failure as
        the empty root one field over. Asserting it here is what keeps the
        setting from being dropped as redundant with a per-field guard.
        """
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from mcp_missioncache.config import settings as s; "
                "print(s.active_dir_name)",
            ],
            capture_output=True,
            text=True,
            env={**os.environ, "MISSIONCACHE_ACTIVE_DIR_NAME": ""},
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "active"

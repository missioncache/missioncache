"""Tests for how the data root resolves from MISSIONCACHE_ROOT.

Spec source: the ownership block above ``MISSIONCACHE_ROOT`` in
``missioncache_db/__init__.py``. It states that this module is the single owner
of the data root, and that the override must be absolute.

Why absolute is a contract and not a preference: the consumers do not share a
working directory. Measured on a real install, the launchd-started dashboard
runs from ``/`` while the MCP server runs from the user's repo. A relative
override therefore names a different physical directory in each process, which
puts project files, the SQLite DB, the DuckDB mirror and the caches in two
places off one env value - the same split the ownership block exists to prevent,
one level up.

Every case runs in a subprocess: the root resolves once, at import.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

_PROBE = (
    "import json; import missioncache_db as m; "
    "from missioncache_db import machine_map as mm; "
    "print(json.dumps([str(m.MISSIONCACHE_ROOT), str(m.DB_PATH), "
    "str(mm.MACHINE_FILE)]))"
)


def _probe(root_value, cwd=None, home=None):
    """Resolve the root in a fresh interpreter, optionally from a given cwd."""
    env = {k: v for k, v in os.environ.items() if k != "MISSIONCACHE_ROOT"}
    if root_value is not None:
        env["MISSIONCACHE_ROOT"] = root_value
    if home is not None:
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
    )
    assert result.returncode == 0, result.stderr
    root, db_path, machine_file = json.loads(result.stdout)
    return Path(root), Path(db_path), Path(machine_file)


class TestAbsoluteOverride:
    def test_an_absolute_override_is_honored(self, tmp_path):
        root, db_path, machine_file = _probe(str(tmp_path / "elsewhere"))

        assert root == tmp_path / "elsewhere"
        assert db_path == root / "tasks.db"
        assert machine_file == root / "machine.json"

    def test_unset_resolves_under_home(self, tmp_path):
        """Positive control, so the tests below cannot pass merely because
        everything falls back."""
        home = tmp_path / "home"
        root, db_path, _ = _probe(None, home=home)

        assert root == home / ".missioncache"
        assert db_path == home / ".missioncache" / "tasks.db"


class TestRelativeOverrideIsRefused:
    """A relative value is refused rather than resolved, because resolving it
    would silently mean a different directory in each consumer process."""

    def test_the_same_relative_value_agrees_across_working_directories(
        self, tmp_path
    ):
        """The contract, stated the way it actually bites.

        Run the same override from two different working directories, the way
        the dashboard (cwd ``/``) and the MCP server (cwd = the repo) really do.
        Before the guard these produced ``<cwd>/data`` in each process, two
        different real directories. They must now agree.
        """
        home = tmp_path / "home"
        cwd_a = tmp_path / "one"
        cwd_b = tmp_path / "two" / "deeper"
        cwd_a.mkdir(parents=True)
        cwd_b.mkdir(parents=True)

        root_a, db_a, _ = _probe("data", cwd=cwd_a, home=home)
        root_b, db_b, _ = _probe("data", cwd=cwd_b, home=home)

        assert root_a == root_b
        assert db_a == db_b
        assert root_a == home / ".missioncache", "must fall back, not resolve"

    def test_a_bare_dot_does_not_become_the_working_directory(self, tmp_path):
        """``.`` is the shape that reads as harmless and is not: it silently
        makes the data root wherever the process happened to start."""
        home = tmp_path / "home"
        cwd = tmp_path / "somewhere"
        cwd.mkdir()

        root, _, _ = _probe(".", cwd=cwd, home=home)

        assert root != cwd
        assert root == home / ".missioncache"

    def test_an_empty_value_falls_back(self, tmp_path):
        """``Path("")`` equals ``Path(".")``, so empty is the same failure
        wearing a different spelling."""
        home = tmp_path / "home"
        cwd = tmp_path / "somewhere"
        cwd.mkdir()

        root, _, _ = _probe("", cwd=cwd, home=home)

        assert root == home / ".missioncache"


class TestMachineMapAgrees:
    """``machine_map`` deliberately copies the resolution instead of importing
    this package, so the absolute-only rule has to be copied with it. If the two
    ever disagree, a relative override splits the machine map away from the data
    it describes."""

    def test_it_refuses_the_same_values(self, tmp_path):
        home = tmp_path / "home"
        for value in ("data", ".", ""):
            root, _, machine_file = _probe(value, home=home)
            assert machine_file == root / "machine.json", value
            assert root == home / ".missioncache", value

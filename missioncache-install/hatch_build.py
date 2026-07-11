"""Build hook that vendors the repo-root rules/, user-commands/, and commands/
dirs into missioncache_install/bundled/ so they ship in BOTH the wheel and the
sdist.

The old approach force-included the ``../`` dirs into the wheel only. That
resolved when building the wheel straight from source, but broke uv's default
sdist-then-wheel build: the isolated sdist has no ``../`` siblings, so the wheel
step failed with "Forced include not found". Copying into the package tree here
(before file collection, for every target) makes the files real package data
that flows through the sdist. When building from an extracted sdist the ``../``
dirs are absent, so the copy is skipped and the bundled/ dir already present in
the sdist is used as-is.
"""

import shutil
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

# repo-root dir name -> destination name under missioncache_install/bundled/
_BUNDLE = {
    "rules": "rules",
    "user-commands": "user_commands",
    "commands": "commands",
}


class BundleBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version, build_data):
        repo_root = Path(self.root).parent
        bundled = Path(self.root) / "missioncache_install" / "bundled"
        for src_name, dst_name in _BUNDLE.items():
            src = repo_root / src_name
            if not src.is_dir():
                # Building from an extracted sdist: bundled/ is already present.
                continue
            dst = bundled / dst_name
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)

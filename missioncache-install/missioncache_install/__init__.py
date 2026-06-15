"""MissionCache installer - bootstrap package for MissionCache on Claude Code."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("missioncache-install")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0+unknown"

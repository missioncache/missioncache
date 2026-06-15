"""MissionCache Dashboard - Task analytics and autonomous execution monitoring."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("missioncache-dashboard")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0+unknown"

"""
Entry point for running missioncache-auto as a module.

Usage:
    python -m missioncache_auto <task-name> [options]
"""

import sys

from missioncache_auto.cli import main

if __name__ == "__main__":
    sys.exit(main())

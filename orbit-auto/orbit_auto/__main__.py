"""
Entry point for running orbit-auto as a module.

Usage:
    python -m orbit_auto <task-name> [options]
"""

import sys

from orbit_auto.cli import main

if __name__ == "__main__":
    sys.exit(main())

"""
MissionCache Auto - Autonomous AI Development Tool

A Python implementation of the MissionCache Auto technique for autonomous
AI-assisted development. Supports both sequential and parallel execution
with MissionCache integration.

Usage:
    missioncache-auto <task-name>              # Parallel (default, 8 workers)
    missioncache-auto <task-name> -w 12        # Parallel with 12 workers
    missioncache-auto <task-name> --sequential # Sequential mode
    missioncache-auto <task-name> --dry-run    # Show execution plan
    missioncache-auto init <task-name> "desc"  # Initialize task
    missioncache-auto status <task-name>       # Show task status
"""

__version__ = "1.0.0"
__author__ = "Tom Brami"

from missioncache_auto.models import Task, State, Config, ExecutionResult
from missioncache_auto.dag import DAG
from missioncache_auto.state import StateManager

__all__ = [
    "Task",
    "State",
    "Config",
    "ExecutionResult",
    "DAG",
    "StateManager",
    "__version__",
]

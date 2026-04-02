"""
Orbit Auto - Autonomous AI Development Tool

A Python implementation of the Orbit Auto technique for autonomous
AI-assisted development. Supports both sequential and parallel execution
with orbit integration.

Usage:
    orbit-auto <task-name>              # Parallel (default, 8 workers)
    orbit-auto <task-name> -w 12        # Parallel with 12 workers
    orbit-auto <task-name> --sequential # Sequential mode
    orbit-auto <task-name> --dry-run    # Show execution plan
    orbit-auto init <task-name> "desc"  # Initialize task
    orbit-auto status <task-name>       # Show task status
"""

__version__ = "3.0.0"
__author__ = "Tom Brami"

from orbit_auto.models import Task, State, Config, ExecutionResult
from orbit_auto.dag import DAG
from orbit_auto.state import StateManager

__all__ = [
    "Task",
    "State",
    "Config",
    "ExecutionResult",
    "DAG",
    "StateManager",
    "__version__",
]

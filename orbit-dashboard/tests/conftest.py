"""Shared fixtures for orbit-dashboard tests."""

import sys
from pathlib import Path

# Add orbit-dashboard root to sys.path so `lib.*` imports work
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

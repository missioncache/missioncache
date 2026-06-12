"""Tests for parse_tasks_md and parse_dependency_graph from analytics_db."""

import pytest

from orbit_dashboard.lib.analytics_db import ParsedAgent, parse_dependency_graph, parse_tasks_md


# --- parse_tasks_md ---


class TestParseTasksMd:
    def test_basic_parsing(self):
        content = """\
# Tasks

- [ ] 1. Set up project
- [x] 2. Write config
- [ ] 3. Add tests
"""
        agents = parse_tasks_md(content)
        assert len(agents) == 3
        assert agents[0].agent_id == "01"
        assert agents[0].agent_name == "Set up project"
        assert agents[0].completed is False
        assert agents[1].completed is True
        # Sequential dependencies: second depends on first
        assert agents[1].dependencies == ["01"]
        assert agents[2].dependencies == ["02"]
        # First task has no dependencies
        assert agents[0].dependencies == []

    def test_empty_content(self):
        agents = parse_tasks_md("")
        assert agents == []

    def test_no_matching_lines(self):
        content = "# Just a heading\n\nSome text without tasks.\n"
        agents = parse_tasks_md(content)
        assert agents == []


# --- parse_dependency_graph ---


class TestParseDependencyGraph:
    def test_single_phase(self):
        content = """\
## Task Dependencies
```
Phase 1: [1-3] → [4-6] → [7]
```
"""
        deps = parse_dependency_graph(content)
        # Tasks 4-6 depend on tasks 1-3
        assert deps["04"] == ["01", "02", "03"]
        assert deps["05"] == ["01", "02", "03"]
        assert deps["06"] == ["01", "02", "03"]
        # Task 7 depends on tasks 4-6
        assert deps["07"] == ["04", "05", "06"]
        # Tasks 1-3 have no entries (no dependencies)
        assert "01" not in deps

    def test_multi_phase(self):
        content = """\
## Task Dependencies
```
Phase 1: [1-2] → [3]
Phase 2: [4] → [5-6]
```
"""
        deps = parse_dependency_graph(content)
        assert deps["03"] == ["01", "02"]
        assert deps["05"] == ["04"]
        assert deps["06"] == ["04"]

    def test_no_dependency_section(self):
        content = "# Tasks\n\n- [ ] 1. Do something\n"
        deps = parse_dependency_graph(content)
        assert deps == {}

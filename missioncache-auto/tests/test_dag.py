"""Tests for orbit_auto.dag module."""

import pytest

from orbit_auto.dag import DAG, CycleDetectedError


class TestAddTaskAndProperties:
    def test_add_task_stores_task(self):
        dag = DAG()
        dag.add_task("01", ["02"], title="First task")
        assert "01" in dag.tasks
        assert dag.task_count == 1

    def test_tasks_returns_sorted(self):
        dag = DAG()
        dag.add_task("03", [])
        dag.add_task("01", [])
        dag.add_task("02", [])
        assert dag.tasks == ["01", "02", "03"]

    def test_get_dependencies(self):
        dag = DAG()
        dag.add_task("01", ["02", "03"])
        assert dag.get_dependencies("01") == ["02", "03"]

    def test_get_dependencies_unknown_task(self):
        dag = DAG()
        assert dag.get_dependencies("99") == []

    def test_get_title_returns_stored_title(self):
        dag = DAG()
        dag.add_task("01", [], title="My Task")
        assert dag.get_title("01") == "My Task"

    def test_get_title_fallback(self):
        dag = DAG()
        dag.add_task("01", [])
        assert dag.get_title("01") == "Task 01"


class TestBuildFromAdjacencyList:
    def test_builds_correct_dag(self):
        dag = DAG.build_from_adjacency_list({"a": [], "b": ["a"], "c": ["a", "b"]})
        assert dag.task_count == 3
        assert dag.get_dependencies("b") == ["a"]
        assert dag.get_dependencies("c") == ["a", "b"]


class TestDetectCycles:
    def test_no_cycle(self, linear_dag):
        assert linear_dag.detect_cycles() is True

    def test_with_cycle(self):
        dag = DAG.build_from_adjacency_list(
            {"01": ["03"], "02": ["01"], "03": ["02"]}
        )
        with pytest.raises(CycleDetectedError, match="Cycle detected"):
            dag.detect_cycles()


class TestTopologicalSort:
    def test_linear_chain(self, linear_dag):
        order = linear_dag.topological_sort()
        assert order == ["01", "02", "03"]

    def test_diamond(self, diamond_dag):
        order = diamond_dag.topological_sort()
        # 01 must come first, 04 must come last, 02/03 in the middle
        assert order[0] == "01"
        assert order[-1] == "04"
        assert set(order[1:3]) == {"02", "03"}


class TestGetWaves:
    def test_independent_tasks_single_wave(self, independent_dag):
        waves = independent_dag.get_waves()
        assert len(waves) == 1
        assert waves[0]["wave"] == 1
        assert sorted(waves[0]["tasks"]) == ["01", "02", "03"]

    def test_linear_chain_separate_waves(self, linear_dag):
        waves = linear_dag.get_waves()
        assert len(waves) == 3
        assert waves[0]["tasks"] == ["01"]
        assert waves[1]["tasks"] == ["02"]
        assert waves[2]["tasks"] == ["03"]

    def test_parallel_with_deps(self, diamond_dag):
        waves = diamond_dag.get_waves()
        assert len(waves) == 3
        assert waves[0]["tasks"] == ["01"]
        assert sorted(waves[1]["tasks"]) == ["02", "03"]
        assert waves[2]["tasks"] == ["04"]


class TestCriticalPath:
    def test_linear_chain(self, linear_dag):
        length, path = linear_dag.get_critical_path()
        assert length == 3
        assert path == ["01", "02", "03"]

    def test_diamond_critical_path(self, diamond_dag):
        length, path = diamond_dag.get_critical_path()
        assert length == 3
        # Path goes through one of the middle nodes
        assert path[0] == "01"
        assert path[-1] == "04"


class TestGetReadyTasks:
    def test_initial_state(self, diamond_dag):
        ready = diamond_dag.get_ready_tasks(completed=set(), in_progress=set())
        assert ready == ["01"]

    def test_after_completing_root(self, diamond_dag):
        ready = diamond_dag.get_ready_tasks(completed={"01"}, in_progress=set())
        assert sorted(ready) == ["02", "03"]

    def test_excludes_in_progress(self, diamond_dag):
        ready = diamond_dag.get_ready_tasks(completed={"01"}, in_progress={"02"})
        assert ready == ["03"]


class TestDepsSatisfied:
    def test_no_deps(self, independent_dag):
        assert independent_dag.deps_satisfied("01", completed=set()) is True

    def test_deps_not_met(self, linear_dag):
        assert linear_dag.deps_satisfied("02", completed=set()) is False

    def test_deps_met(self, linear_dag):
        assert linear_dag.deps_satisfied("02", completed={"01"}) is True

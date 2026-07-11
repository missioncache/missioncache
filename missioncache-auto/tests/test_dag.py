"""Tests for missioncache_auto.dag module."""

import pytest

from missioncache_auto.dag import DAG, CycleDetectedError


def _write_prompt(directory, task_id, deps_block, title="Task"):
    """Write a prompt file with the given (already-formatted) deps block."""
    body = f'---\ntask_id: "{task_id}"\ntask_title: "{title}"\n{deps_block}---\nBody\n'
    (directory / f"task-{task_id}-prompt.md").write_text(body)


class TestBuildFromPromptsDependencies:
    def test_inline_array_deps(self, tmp_path):
        _write_prompt(tmp_path, "01", "dependencies: []\n")
        _write_prompt(tmp_path, "02", 'dependencies: ["01"]\n')
        dag = DAG.build_from_prompts(tmp_path)
        assert dag.get_dependencies("02") == ["01"]

    def test_block_list_deps_are_read_not_dropped(self, tmp_path):
        # Regression: the DAG previously only matched the inline [..] form and
        # silently replaced block-list deps with an implicit previous-task dep.
        _write_prompt(tmp_path, "01", "dependencies: []\n")
        _write_prompt(tmp_path, "02", "dependencies:\n  - \"01\"\n")
        _write_prompt(tmp_path, "03", "dependencies:\n  - \"01\"\n  - \"02\"\n")
        dag = DAG.build_from_prompts(tmp_path)
        assert dag.get_dependencies("02") == ["01"]
        # The bug would have yielded ["02"] (implicit prev) here.
        assert dag.get_dependencies("03") == ["01", "02"]

    def test_empty_deps_field_overrides_implicit_prev(self, tmp_path):
        # An explicit (empty) dependencies field is authoritative - no implicit
        # previous-task dependency is fabricated.
        _write_prompt(tmp_path, "01", "dependencies: []\n")
        _write_prompt(tmp_path, "02", "dependencies: []\n")
        dag = DAG.build_from_prompts(tmp_path)
        assert dag.get_dependencies("02") == []

    def test_missing_field_uses_implicit_prev_when_it_exists(self, tmp_path):
        _write_prompt(tmp_path, "01", "")
        _write_prompt(tmp_path, "02", "")
        dag = DAG.build_from_prompts(tmp_path)
        assert dag.get_dependencies("01") == []
        assert dag.get_dependencies("02") == ["01"]

    def test_missing_implicit_predecessor_raises(self, tmp_path):
        # task-04 has no deps field and task-03 does not exist (numbering gap).
        _write_prompt(tmp_path, "01", "dependencies: []\n")
        _write_prompt(tmp_path, "02", 'dependencies: ["01"]\n')
        _write_prompt(tmp_path, "04", "")
        with pytest.raises(ValueError, match="task-04"):
            DAG.build_from_prompts(tmp_path)


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

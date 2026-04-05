"""Tests for orbit_auto.runnable module."""

from orbit_auto.runnable import (
    TaskModeInfo,
    _get_sequential_dependencies,
    get_runnable_tasks,
    parse_task_modes,
)


class TestParseTaskModes:
    def test_auto_marker(self, tmp_path):
        f = tmp_path / "tasks.md"
        f.write_text("- [ ] 1. Setup project `[auto]`\n")
        result = parse_task_modes(f)
        assert len(result) == 1
        assert result[0].mode == "auto"
        assert result[0].task_id == "1"
        assert result[0].title == "Setup project"

    def test_inter_marker(self, tmp_path):
        f = tmp_path / "tasks.md"
        f.write_text("- [ ] 2. Review code `[inter]`\n")
        result = parse_task_modes(f)
        assert len(result) == 1
        assert result[0].mode == "inter"

    def test_auto_with_depends(self, tmp_path):
        f = tmp_path / "tasks.md"
        f.write_text("- [ ] 4. Deploy `[auto:depends=3,5]`\n")
        result = parse_task_modes(f)
        assert len(result) == 1
        assert result[0].mode == "auto"
        assert result[0].dependencies == ["3", "5"]

    def test_no_mode_marker(self, tmp_path):
        f = tmp_path / "tasks.md"
        f.write_text("- [ ] 1. Plain task\n")
        result = parse_task_modes(f)
        assert len(result) == 1
        assert result[0].mode is None

    def test_completed_task(self, tmp_path):
        f = tmp_path / "tasks.md"
        f.write_text("- [x] 1. Done task `[auto]`\n")
        result = parse_task_modes(f)
        assert result[0].completed is True


class TestFilterRunnableTasks:
    def test_only_auto_tasks_returned(self, tmp_path):
        f = tmp_path / "tasks.md"
        f.write_text(
            "- [ ] 1. Auto task `[auto]`\n"
            "- [ ] 2. Inter task `[inter]`\n"
            "- [ ] 3. No mode task\n"
        )
        result = get_runnable_tasks(f)
        # Only auto tasks appear in runnable
        runnable_ids = [t.task_id for t in result.runnable]
        assert "1" in runnable_ids
        assert "2" not in runnable_ids
        assert "3" not in runnable_ids


class TestSequentialDependencies:
    def test_implicit_ordering(self):
        tasks = [
            TaskModeInfo(task_id="1", title="A", mode="auto", completed=False, dependencies=[]),
            TaskModeInfo(task_id="2", title="B", mode="auto", completed=False, dependencies=[]),
        ]
        deps = _get_sequential_dependencies("2", tasks)
        assert deps == ["1"]

    def test_first_task_no_implicit_dep(self):
        tasks = [
            TaskModeInfo(task_id="1", title="A", mode="auto", completed=False, dependencies=[]),
        ]
        deps = _get_sequential_dependencies("1", tasks)
        assert deps == []

    def test_explicit_deps_skip_implicit(self):
        tasks = [
            TaskModeInfo(task_id="1", title="A", mode="auto", completed=False, dependencies=[]),
            TaskModeInfo(
                task_id="2", title="B", mode="auto", completed=False, dependencies=["1"]
            ),
        ]
        # Task 2 has explicit deps, so no implicit sequential dep
        deps = _get_sequential_dependencies("2", tasks)
        assert deps == []


class TestBlockingByInteractiveTasks:
    def test_blocked_by_inter(self, tmp_path):
        f = tmp_path / "tasks.md"
        f.write_text(
            "- [ ] 1. Interactive review `[inter]`\n"
            "- [ ] 2. Auto deploy `[auto]`\n"
        )
        result = get_runnable_tasks(f)
        # Task 2 is auto but blocked by task 1 (inter, not completed)
        assert len(result.blocked_by_inter) == 1
        assert result.blocked_by_inter[0].task_id == "2"

    def test_blocked_by_inter_summary(self, tmp_path):
        f = tmp_path / "tasks.md"
        f.write_text(
            "- [ ] 1. Manual review `[inter]`\n"
            "- [ ] 2. Auto build `[auto]`\n"
            "- [ ] 3. Auto test `[auto]`\n"
        )
        result = get_runnable_tasks(f)
        # Task 2 is directly blocked by inter task 1.
        # Task 3 is blocked by task 2 (auto), so it lands in blocked but NOT blocked_by_inter.
        assert len(result.blocked_by_inter) == 1
        assert result.blocked_by_inter[0].task_id == "2"
        assert result.blocked_by_inter[0].blocked_by == "1"
        # Both are in the general blocked list
        assert len(result.blocked) == 2

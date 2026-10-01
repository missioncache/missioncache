"""Tests for missioncache_auto.sequential result handling."""

from unittest.mock import MagicMock

from missioncache_auto.models import Config, ExecutionResult, TaskPaths
from missioncache_auto.sequential import SequentialRunner


def _bare_runner(tmp_path):
    """Build a SequentialRunner without running its __init__ side effects.

    _handle_result only touches paths, config, display, logger and the missioncache-db
    flags, so a hand-assembled instance exercises the checkbox-marking path
    without spinning up loggers or the task DB.
    """
    runner = SequentialRunner.__new__(SequentialRunner)

    tasks_file = tmp_path / "t-tasks.md"
    tasks_file.write_text("- [ ] 1. First\n- [ ] 2. Second\n")
    context_file = tmp_path / "t-context.md"
    context_file.write_text("**Last Updated:** old\n")
    auto_log = tmp_path / "t-auto-log.md"
    auto_log.write_text("# log\n")

    runner.paths = TaskPaths(
        task_dir=tmp_path,
        tasks_file=tasks_file,
        context_file=context_file,
        auto_log=auto_log,
        prompts_dir=tmp_path / "prompts",
        state_dir=tmp_path / "state",
        logs_dir=tmp_path / "logs",
    )
    runner.config = Config(auto_commit=False)
    runner.display = MagicMock()
    runner.logger = MagicMock()
    runner.use_prompts = False
    runner.project_root = tmp_path
    runner.task_name = "t"
    runner.current_task_attempts = 1
    runner.total_iterations = 1
    runner.missioncache_db_enabled = False
    runner.missioncache_db_cli = None
    return runner, tasks_file


def test_success_marks_task_checkbox(tmp_path):
    runner, tasks_file = _bare_runner(tmp_path)

    result = ExecutionResult(
        task_id="1",
        success=True,
        output="ok",
        duration=1.0,
        what_worked="did it",
    )
    ret = runner._handle_result(result, "1", "First")

    assert ret is None  # loop continues
    updated = tasks_file.read_text()
    assert "- [x] 1." in updated  # first task now completed
    assert "- [ ] 2." in updated  # second task untouched


def test_failure_does_not_mark_checkbox(tmp_path):
    runner, tasks_file = _bare_runner(tmp_path)

    result = ExecutionResult(
        task_id="1",
        success=False,
        output="",
        duration=1.0,
        what_failed="broke",
    )
    ret = runner._handle_result(result, "1", "First")

    assert ret is None
    assert "- [ ] 1." in tasks_file.read_text()  # still uncompleted


def test_dry_run_runs_nothing(tmp_path, monkeypatch):
    """--sequential --dry-run shows the plan and exits 0 without starting a run.

    No Claude call, no auto log, no dashboard execution row, no checkbox ticked.
    """
    runner, tasks_file = _bare_runner(tmp_path)
    runner.config = Config(auto_commit=False, dry_run=True)
    runner.iteration = runner.total_iterations = 0
    runner.validate = lambda: []
    runner.paths.auto_log.unlink()
    calls = []
    monkeypatch.setattr(runner, "_init_auto_log", lambda: calls.append("auto_log"))

    def no_claude(*a, **k):
        raise AssertionError("dry run started a Claude call")

    monkeypatch.setattr("missioncache_auto.sequential.ClaudeRunner", no_claude)

    assert runner.run() == 0

    assert calls == []
    runner.logger.start.assert_not_called()
    assert tasks_file.read_text() == "- [ ] 1. First\n- [ ] 2. Second\n"
    shown = " ".join(str(c) for c in runner.display.method_calls)
    assert "1" in shown and "First" in shown and "Second" in shown

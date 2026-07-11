"""Tests for parallel-mode worktree defaulting, git-repo detection, and the
pre-flight refusals that stop misconfigurations from silently losing work."""

import subprocess
import sys
from unittest.mock import MagicMock

from missioncache_auto import cli as cli_module
from missioncache_auto.cli import _config_from_args
from missioncache_auto.dag import DAG
from missioncache_auto.models import Config, TaskPaths
from missioncache_auto.parallel import ParallelRunner
from missioncache_auto.state import StateManager


def test_use_worktrees_default_on():
    # Parallel workers share one checkout otherwise and race on commit, so
    # worktree isolation is the default.
    assert Config().use_worktrees is True


def _bare_runner(project_root):
    runner = ParallelRunner.__new__(ParallelRunner)
    runner.project_root = project_root
    return runner


def _guard_runner(project_root, **config_kwargs):
    """A ParallelRunner with just the attributes the guard helpers touch."""
    runner = ParallelRunner.__new__(ParallelRunner)
    runner.project_root = project_root
    runner.config = Config(**config_kwargs)
    runner.display = MagicMock()
    return runner


def _init_git_repo(path):
    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@e.com"], cwd=path, capture_output=True, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "test"], cwd=path, capture_output=True, check=True
    )
    (path / "app.py").write_text("v1\n")
    subprocess.run(["git", "add", "app.py"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, capture_output=True, check=True)


def test_is_git_repo_true(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    assert _bare_runner(tmp_path)._is_git_repo() is True


def test_is_git_repo_false(tmp_path):
    assert _bare_runner(tmp_path)._is_git_repo() is False


def _parse(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", list(argv))
    return cli_module.parse_args()


def test_no_worktree_flag_builds_config_without_worktrees(monkeypatch):
    # Assert on the real args -> Config wiring, not a re-derivation of it.
    args = _parse(monkeypatch, ["missioncache-auto", "my-task", "--no-worktree"])
    assert args.no_worktree is True
    assert _config_from_args(args).use_worktrees is False


def test_default_args_build_config_with_worktrees(monkeypatch):
    args = _parse(monkeypatch, ["missioncache-auto", "my-task"])
    assert args.no_worktree is False
    assert args.worktree is False
    assert _config_from_args(args).use_worktrees is True


# --- Non-git-repo fallback -------------------------------------------------


def test_worktree_fallback_disables_on_non_git(tmp_path):
    runner = _guard_runner(tmp_path, use_worktrees=True)
    runner._apply_worktree_fallback(is_git_repo=False)
    assert runner.config.use_worktrees is False
    runner.display.warning.assert_called_once()


def test_worktree_fallback_noop_on_git_repo(tmp_path):
    runner = _guard_runner(tmp_path, use_worktrees=True)
    runner._apply_worktree_fallback(is_git_repo=True)
    assert runner.config.use_worktrees is True
    runner.display.warning.assert_not_called()


# --- Pre-flight refusals (exit code 3) -------------------------------------


def test_preflight_refuses_worktrees_with_no_commit(tmp_path):
    # Worktrees + --no-commit => nothing committed, worktrees force-removed,
    # every worker's output discarded.
    runner = _guard_runner(tmp_path, use_worktrees=True, auto_commit=False)
    assert runner._preflight_refusals(is_git_repo=True) == 3
    runner.display.error.assert_called_once()
    msg = runner.display.error.call_args[0][0]
    assert "--no-commit" in msg
    assert "--no-worktree" in msg


def test_preflight_refuses_shared_checkout_autocommit_multiworker(tmp_path):
    # Multiple auto-committing workers in one shared checkout race on commit.
    runner = _guard_runner(
        tmp_path, use_worktrees=False, auto_commit=True, max_workers=4
    )
    assert runner._preflight_refusals(is_git_repo=True) == 3
    runner.display.error.assert_called_once()


def test_preflight_allows_single_shared_worker(tmp_path):
    runner = _guard_runner(
        tmp_path, use_worktrees=False, auto_commit=True, max_workers=1
    )
    assert runner._preflight_refusals(is_git_repo=True) is None
    runner.display.error.assert_not_called()


def test_preflight_refuses_dirty_tracked_main_checkout(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / "app.py").write_text("uncommitted edit\n")  # tracked change
    runner = _guard_runner(tmp_path, use_worktrees=True, auto_commit=True)
    assert runner._preflight_refusals(is_git_repo=True) == 3
    runner.display.error.assert_called_once()


def test_preflight_warns_on_untracked_only_and_proceeds(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / "brand-new.txt").write_text("scratch\n")  # untracked only
    runner = _guard_runner(tmp_path, use_worktrees=True, auto_commit=True)
    assert runner._preflight_refusals(is_git_repo=True) is None
    runner.display.warning.assert_called_once()
    runner.display.error.assert_not_called()


def test_preflight_clean_checkout_proceeds(tmp_path):
    _init_git_repo(tmp_path)
    runner = _guard_runner(tmp_path, use_worktrees=True, auto_commit=True)
    assert runner._preflight_refusals(is_git_repo=True) is None
    runner.display.error.assert_not_called()
    runner.display.warning.assert_not_called()


# --- Final-state re-read ----------------------------------------------------


class _DeadProcess:
    """A stand-in worker process that never runs and reports itself dead."""

    def __init__(self, target=None, **kwargs):
        pass

    def start(self):
        pass

    def is_alive(self):
        return False

    def terminate(self):
        pass

    def kill(self):
        pass

    def join(self, timeout=None):
        pass


def test_run_workers_reports_failure_when_tasks_left_pending(tmp_path, monkeypatch):
    # Workers die immediately leaving every task PENDING; the run must report
    # failure (exit 1) from the authoritative final state, not success.
    from missioncache_auto import parallel as parallel_module

    runner = ParallelRunner.__new__(ParallelRunner)
    runner.config = Config(max_workers=1, auto_commit=False, use_worktrees=False)
    runner.display = MagicMock()
    runner.logger = MagicMock()
    runner.logger.execution_id = None  # keep Worker off the task DB
    runner.worktree_manager = None
    runner.start_time = 0.0
    runner.task_name = "t"
    runner.project_root = tmp_path

    state_dir = tmp_path / "state"
    runner.paths = TaskPaths(
        task_dir=tmp_path,
        tasks_file=tmp_path / "t-tasks.md",  # absent => sync_to_tasks_md is a no-op
        context_file=tmp_path / "t-context.md",
        auto_log=tmp_path / "t-auto-log.md",
        prompts_dir=tmp_path / "prompts",
        state_dir=state_dir,
        logs_dir=tmp_path / "logs",
    )
    runner.dag = DAG.build_from_adjacency_list({"01": [], "02": ["01"]})
    runner.state_manager = StateManager(state_dir)
    runner.state_manager.init(runner.dag.tasks)  # both PENDING, never executed

    monkeypatch.setattr(parallel_module.multiprocessing, "Process", _DeadProcess)

    rc = runner._run_workers(None)

    assert rc == 1
    runner.logger.finish.assert_called_once()
    assert runner.logger.finish.call_args.kwargs.get("status") == "failed"

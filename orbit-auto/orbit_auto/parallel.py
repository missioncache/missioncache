"""
Parallel execution mode for Orbit Auto.

Orchestrates multiple workers executing tasks in parallel
while respecting dependencies between tasks.
"""

from __future__ import annotations

import multiprocessing
import time
from pathlib import Path

from orbit_auto.dag import DAG
from orbit_auto.plan_validator import Severity, validate_plan, has_errors
from orbit_auto.db_logger import create_logger
from orbit_auto.display import Display, create_display
from orbit_auto.models import Config, TaskPaths, TaskStatus
from orbit_auto.runnable import get_runnable_tasks, get_blocking_summary
from orbit_auto.state import StateManager
from orbit_auto.task_parser import parse_tasks_md
from orbit_auto.worker import Worker
from orbit_auto.worktree import WorktreeManager


class ParallelRunner:
    """
    Runs orbit-auto in parallel mode with multiple workers.

    This mode:
    - Parses dependencies from prompt YAML frontmatter
    - Builds a DAG and computes parallel execution waves
    - Spawns worker processes (up to 12)
    - Workers claim tasks atomically respecting dependencies
    - State is synced to tasks.md periodically
    """

    def __init__(
        self,
        task_name: str,
        project_root: Path,
        config: Config | None = None,
        display: Display | None = None,
    ) -> None:
        self.task_name = task_name
        self.project_root = project_root
        self.config = config or Config()
        self.display = display or create_display()

        self.paths = TaskPaths.from_task_name(task_name)
        self.dag: DAG | None = None
        self.state_manager: StateManager | None = None
        self.worktree_manager: WorktreeManager | None = None
        self.start_time = time.time()

        # Database logging for dashboard
        self.logger = create_logger(task_name, config, mode="parallel")

    def validate(self) -> list[str]:
        """Validate the task setup. Returns list of errors."""
        errors = self.paths.validate()

        if not errors:
            # Check for prompts directory
            if not self.paths.prompts_dir.exists():
                errors.append(
                    f"Parallel mode requires prompts/ directory: {self.paths.prompts_dir}"
                )
            else:
                prompt_files = list(self.paths.prompts_dir.glob("task-*-prompt.md"))
                if not prompt_files:
                    errors.append(f"No prompt files found in {self.paths.prompts_dir}")

        return errors

    def run(self, dry_run: bool = False) -> int:
        """
        Run the parallel execution.

        Args:
            dry_run: If True, only show execution plan without running

        Returns:
            0 = All tasks completed
            1 = One or more tasks failed
            2 = Blocked on [WAIT] task
            3 = Configuration/setup error
        """
        # Validate setup
        errors = self.validate()
        if errors:
            for error in errors:
                self.display.error(error)
            return 3

        # Build DAG from prompts
        try:
            self.dag = DAG.build_from_prompts(self.paths.prompts_dir)
            self.dag.detect_cycles()
        except Exception as e:
            self.display.error(f"Failed to build dependency graph: {e}")
            return 3

        # Validate plan before proceeding
        validation_issues = validate_plan(
            self.paths.prompts_dir,
            self.paths.tasks_file,
            self.dag,
        )
        if validation_issues:
            for issue in validation_issues:
                if issue.severity == Severity.ERROR:
                    self.display.error(f"[VALIDATION] {issue.message}")
                else:
                    self.display.warning(f"[VALIDATION] {issue.message}")
            if has_errors(validation_issues):
                self.display.error("Plan validation failed - fix errors before running")
                return 3

        # Check for already-completed tasks in tasks.md
        # This allows orbit-auto to resume after interruption
        pre_completed = self._get_pre_completed_tasks()

        # Check for tasks blocked by interactive tasks
        runnable_result = get_runnable_tasks(self.paths.tasks_file)
        blocking_summary = get_blocking_summary(self.paths.tasks_file)

        # Track which tasks are blocked by interactive
        blocked_by_inter_ids = {t.task_id for t in runnable_result.blocked_by_inter}
        # Convert to padded format used by DAG (e.g., "1" -> "01")
        blocked_by_inter_padded = {
            f"{int(tid):02d}" for tid in blocked_by_inter_ids if tid.isdigit()
        }

        # Calculate how many tasks remain (excluding blocked-by-inter)
        remaining_count = self.dag.task_count - len(pre_completed)

        # Display header and plan
        self.display.header("ORBIT AUTO [PARALLEL]")
        info = {
            "Directory": str(self.paths.task_dir),
            "Tasks": self.dag.task_count,
            "Workers": self.config.max_workers,
        }
        if pre_completed:
            info["Already completed"] = len(pre_completed)
            info["Remaining"] = remaining_count
        if blocking_summary["blocked_by_inter_count"] > 0:
            info["Blocked by interactive"] = blocking_summary["blocked_by_inter_count"]
        if self.config.use_worktrees:
            info["Worktrees"] = "Enabled"
        if not self.config.auto_commit:
            info["Auto-commit"] = "Disabled"
        if self.config.enable_review:
            info["Code review"] = "Spec + Quality"
        elif self.config.spec_review_only:
            info["Code review"] = "Spec only"
        self.display.project_info(self.task_name, info)

        self.display.execution_plan(self.dag, self.config.max_workers)

        # Show blocking summary if tasks are blocked by interactive
        if blocking_summary["blocked_by_inter_count"] > 0:
            self.display.runnable_status(
                blocking_summary["runnable_count"],
                blocking_summary["blocked_count"],
                blocking_summary["blocked_by_inter_count"],
            )

        if dry_run:
            self.display.info("Dry run - not executing")
            return 0

        # If no runnable tasks but some are blocked by interactive, exit early
        if (
            blocking_summary["runnable_count"] == 0
            and blocking_summary["blocked_by_inter_count"] > 0
        ):
            self.display.blocked_tasks_warning(
                completed_count=0,
                blocked_by_inter=[
                    {"task_id": t.task_id, "title": t.title}
                    for t in runnable_result.blocked_by_inter
                ],
                first_blocker=blocking_summary.get("first_inter_blocker"),
            )
            return 2  # Exit with blocked status

        # Check if all tasks are already completed
        if remaining_count == 0:
            self.display.info("All tasks already completed.")

            # Interactive prompt to reset
            try:
                response = input("\n  Reset project state and run again? [y/N]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                response = ""

            if response == "y":
                # Reset state and re-calculate remaining
                temp_state_manager = StateManager(self.paths.state_dir)
                temp_state_manager.reset()
                pre_completed = set()
                remaining_count = self.dag.task_count
                self.display.info("Project state reset. Proceeding with execution...")
            else:
                self.display.info("Nothing to do. Exiting.")
                return 0

        # Ask for confirmation before proceeding
        if not self._confirm_execution():
            self.display.info("Aborted by user")
            return 0

        # Initialize state with pre-completed tasks
        self.state_manager = StateManager(self.paths.state_dir)
        self.state_manager.init(self.dag.tasks, pre_completed)

        # Create logs directory for worker output
        self.paths.logs_dir.mkdir(parents=True, exist_ok=True)

        # Start database logging for dashboard
        self.logger.start(total_subtasks=remaining_count)

        # Set up worktrees if enabled
        worktree_paths: dict[int, Path] | None = None
        if self.config.use_worktrees:
            self.display.info("Creating git worktrees for worker isolation...")
            try:
                self.worktree_manager = WorktreeManager(
                    self.project_root,
                    self.task_name,
                    self.config.max_workers,
                )
                worktree_paths = self.worktree_manager.create_worktrees()
                self.display.info(f"Created {len(worktree_paths)} worktrees")
            except Exception as e:
                self.display.error(f"Failed to create worktrees: {e}")
                return 3

        # Spawn workers
        return self._run_workers(worktree_paths)

    def _confirm_execution(self) -> bool:
        """Ask user for confirmation before starting execution."""
        self.display._print()
        try:
            response = input("  Proceed? [Y/n] ").strip().lower()
            return response in ("", "y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False

    def _get_pre_completed_tasks(self) -> set[str]:
        """
        Get task IDs that are already completed in tasks.md.

        Parses tasks.md for completed checkboxes and converts task numbers
        to the padded format used by the DAG (e.g., "1" -> "01").

        Returns:
            Set of task IDs (padded format) that are already completed.
        """
        if not self.paths.tasks_file.exists():
            return set()

        tasks = parse_tasks_md(self.paths.tasks_file)
        completed = set()

        for task in tasks:
            if task.is_completed:
                # Convert to padded format: "1" -> "01"
                padded_id = f"{int(task.number):02d}"
                completed.add(padded_id)

        return completed

    def _run_workers(self, worktree_paths: dict[int, Path] | None = None) -> int:
        """Run worker processes and monitor progress."""
        assert self.dag is not None
        assert self.state_manager is not None

        workers: list[multiprocessing.Process] = []
        completed = set()
        in_progress = set()
        failed = set()

        # Create worker processes
        for worker_id in range(self.config.max_workers):
            # Use worktree path if available, otherwise project_root
            working_dir = (
                worktree_paths[worker_id]
                if worktree_paths and worker_id in worktree_paths
                else self.project_root
            )
            worker = Worker(
                worker_id=worker_id,
                task_name=self.task_name,
                project_root=working_dir,
                state_dir=self.paths.state_dir,
                prompts_dir=self.paths.prompts_dir,
                adjacency_file=self.paths.state_dir / "adjacency.txt",
                logs_dir=self.paths.logs_dir,
                max_retries=self.config.max_retries,
                task_timeout=self.config.task_timeout,
                visibility=self.config.visibility,
                execution_id=self.logger.execution_id,
                enable_review=self.config.enable_review,
                spec_review_only=self.config.spec_review_only,
                auto_commit=self.config.auto_commit,
                tdd_mode=self.config.tdd_mode,
            )

            # Write adjacency file for workers
            self.dag.to_adjacency_file(self.paths.state_dir / "adjacency.txt")

            # Start worker in separate process
            p = multiprocessing.Process(target=worker.run)
            p.start()
            workers.append(p)

        # Monitor progress
        try:
            while not self.state_manager.is_complete():
                # Get current state
                state = self.state_manager.read()

                # Update tracking sets
                completed = {
                    tid for tid, task in state.tasks.items() if task.status == TaskStatus.COMPLETED
                }
                in_progress = {
                    tid
                    for tid, task in state.tasks.items()
                    if task.status == TaskStatus.IN_PROGRESS
                }
                failed = {
                    tid for tid, task in state.tasks.items() if task.status == TaskStatus.FAILED
                }

                # Display progress
                self.display.parallel_progress(
                    completed=len(completed),
                    total=self.dag.task_count,
                    in_progress=list(in_progress),
                    failed=list(failed),
                )

                # Update database progress
                self.logger.update_progress(
                    completed=len(completed),
                    failed=len(failed),
                )

                # Check for fail-fast
                if self.config.fail_fast and failed:
                    self.display.error("\nFail-fast triggered - stopping workers")
                    for p in workers:
                        p.terminate()
                    break

                # Recover orphaned tasks from individually dead workers
                dead_ids = {i for i, p in enumerate(workers) if not p.is_alive()}
                if dead_ids:
                    released = self.state_manager.release_orphaned_tasks(
                        dead_ids, self.config.max_retries
                    )
                    if released:
                        self.display.warning(
                            f"Recovered orphaned tasks from dead workers: {', '.join(released)}"
                        )

                # Check if any workers are still alive
                alive = [p for p in workers if p.is_alive()]
                if not alive and not self.state_manager.is_complete():
                    # All workers died but work isn't done
                    break

                time.sleep(0.5)

        except KeyboardInterrupt:
            self.display.warning("\nInterrupted - stopping workers")
            for p in workers:
                p.terminate()
            self.logger.finish(status="cancelled")
            return 1

        finally:
            # Ensure all workers are stopped
            for p in workers:
                if p.is_alive():
                    p.terminate()
                    p.join(timeout=5)

            # Sync final state to tasks.md
            self.state_manager.sync_to_tasks_md(self.paths.tasks_file)

            # Merge worktrees back if enabled
            if self.worktree_manager is not None:
                self.display.info("Merging worktree changes...")
                merge_results = self.worktree_manager.merge_all()
                self._display_merge_results(merge_results)
                self.worktree_manager.cleanup_with_results(merge_results)

        # Print summary
        self._print_summary(completed, failed)

        # Finish database logging
        if failed:
            self.logger.finish(
                status="failed",
                error_message=f"Failed tasks: {', '.join(sorted(failed))}",
            )
        else:
            self.logger.finish(status="completed")

        return 0 if not failed else 1

    def _display_merge_results(self, results: list[dict]) -> None:
        """Display merge results from worktree integration."""
        for r in results:
            status = r["status"]
            if status == "merged":
                self.display.info(f"Worker {r['worker_id']}: {r['message']}")
            elif status == "no_changes":
                pass  # Don't clutter output with no-op workers
            elif status == "conflict":
                self.display.warning(f"Worker {r['worker_id']}: CONFLICT - {r['message']}")

    def _print_summary(self, completed: set, failed: set) -> None:
        """Print final execution summary."""
        duration = time.time() - self.start_time

        self.display._print()  # Newline after progress bar
        self.display._print()

        if not failed:
            self.display.completion_summary(
                total_iterations=len(completed),
                total_duration=duration,
                completed=len(completed),
                total=self.dag.task_count if self.dag else 0,
            )
        else:
            self.display.error(f"Failed tasks: {', '.join(sorted(failed))}")
            self.display.failure_summary(
                iteration=len(completed) + len(failed),
                max_iterations=self.dag.task_count if self.dag else 0,
                failed_task=", ".join(sorted(failed)),
            )


def run_parallel(
    task_name: str,
    project_root: Path,
    config: Config | None = None,
    dry_run: bool = False,
) -> int:
    """
    Convenience function to run orbit-auto in parallel mode.

    Returns exit code (0=success, 1=failed, 2=blocked, 3=error).
    """
    runner = ParallelRunner(task_name, project_root, config)
    return runner.run(dry_run=dry_run)

"""
Sequential execution mode for Orbit Auto.

Runs tasks one at a time in order, with retry logic and
full orbit integration including log management.
"""

from __future__ import annotations

import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

from orbit_auto.claude_runner import ClaudeRunner, build_generic_prompt
from orbit_auto.db_logger import create_logger
from orbit_auto.display import Display, create_display
from orbit_auto.models import Config, ExecutionResult, TaskPaths
from orbit_auto.runnable import get_runnable_tasks, get_blocking_summary
from orbit_auto.task_parser import (
    extract_prompt_content,
    get_first_uncompleted_task,
    get_prompt_for_task,
    get_task_progress,
    update_timestamps,
    validate_prompts_exist,
)
from orbit_auto.worker import git_commit_task


class SequentialRunner:
    """
    Runs orbit-auto in sequential mode - one task at a time.

    This is the main execution mode for orbit tasks, handling:
    - Task discovery and ordering
    - Prompt selection (optimized or generic)
    - Claude invocation and output parsing
    - auto-log management
    - Task DB integration
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
        self.use_prompts = False
        self.iteration = 0
        self.current_task_attempts = 0
        self.total_iterations = 0
        self.start_time = time.time()

        # Task DB integration
        self.orbit_db_script = Path.home() / ".claude" / "scripts" / "orbit_db.py"
        self.orbit_db_enabled = self.orbit_db_script.exists()

        # Database logging for dashboard
        self.logger = create_logger(task_name, config, mode="sequential")

    def validate(self) -> list[str]:
        """Validate the task setup. Returns list of errors."""
        errors = self.paths.validate()

        if not errors:
            # Check for prompts directory
            if self.paths.prompts_dir.exists():
                prompt_files = list(self.paths.prompts_dir.glob("task-*-prompt.md"))
                if prompt_files:
                    self.use_prompts = True
                    # Validate all prompts exist
                    missing = validate_prompts_exist(
                        self.paths.prompts_dir,
                        self.paths.tasks_file,
                    )
                    if missing:
                        errors.extend([f"Missing prompt: {m}" for m in missing])

        return errors

    def run(self) -> int:
        """
        Run the sequential loop.

        Returns:
            0 = All tasks completed
            1 = Max retries reached (failed)
            2 = Blocked on [WAIT] task
            3 = Missing prompt file
        """
        # Validate setup
        errors = self.validate()
        if errors:
            for error in errors:
                self.display.error(error)
            return 3

        # Initialize auto log
        self._init_auto_log()

        # Get total tasks for progress tracking
        completed, total = get_task_progress(self.paths.tasks_file)
        remaining = total - completed

        # Start database logging for dashboard
        self.logger.start(total_subtasks=remaining)

        # Display header
        self.display.header()
        self.display.task_info(
            self.task_name,
            {
                "Directory": str(self.paths.task_dir),
                "Prompts": "optimized" if self.use_prompts else "generic",
                "Max retries/task": self.config.max_retries,
            },
        )

        if self.use_prompts:
            self.display.info(f"Using optimized prompts from prompts/")

        # Check for blocked-by-interactive situation
        # (sequential mode respects mode markers when prompts are used)
        if self.use_prompts:
            runnable_result = get_runnable_tasks(self.paths.tasks_file)
            summary = get_blocking_summary(self.paths.tasks_file)

            # Show runnable status
            if summary["blocked_by_inter_count"] > 0:
                self.display.runnable_status(
                    summary["runnable_count"],
                    summary["blocked_count"],
                    summary["blocked_by_inter_count"],
                )

            # If no runnable tasks but some are blocked by interactive
            if summary["runnable_count"] == 0 and summary["blocked_by_inter_count"] > 0:
                self.display.blocked_tasks_warning(
                    completed_count=0,
                    blocked_by_inter=[
                        {"task_id": t.task_id, "title": t.title}
                        for t in runnable_result.blocked_by_inter
                    ],
                    first_blocker=summary.get("first_inter_blocker"),
                )
                return 2  # Exit with blocked status

        # Main loop
        while True:
            self.iteration += 1
            self.total_iterations += 1

            # Check if we've hit max retries for current task
            if self.current_task_attempts >= self.config.max_retries:
                task = get_first_uncompleted_task(self.paths.tasks_file)
                if task:
                    self.display.failure_summary(
                        self.iteration - 1,
                        self.config.max_retries,
                        f"{task.number}: {task.title}",
                    )
                    self.logger.finish(
                        status="failed",
                        error_message=f"Task {task.number} failed after {self.config.max_retries} attempts",
                    )
                return 1

            # Get next task
            task = get_first_uncompleted_task(self.paths.tasks_file)
            if task is None:
                # All tasks completed
                self._handle_completion()
                return 0

            # Check for [WAIT] marker
            if task.is_wait:
                self.display.blocked_summary(task.number, task.title)
                self.logger.log(
                    f"Blocked on task {task.number}: {task.title} (requires human input)",
                    level="warn",
                    subtask_id=task.number,
                )
                self.logger.finish(status="cancelled", error_message="Blocked on [WAIT] task")
                return 2

            # Get progress
            completed, total = get_task_progress(self.paths.tasks_file)

            # Display iteration header
            self.display.iteration_header(
                iteration=self.current_task_attempts + 1,
                max_iterations=self.config.max_retries,
                task_num=task.number,
                total_tasks=total,
                completed_tasks=completed,
                task_title=task.title,
            )

            # Build prompt
            prompt = self._build_prompt(task.number, task.title)

            # Run Claude
            result = self._run_claude(prompt, task.number)
            result.task_id = task.number

            # Handle result
            exit_code = self._handle_result(result, task.number, task.title)
            if exit_code is not None:
                return exit_code

            # Pause between iterations
            if self.config.pause_seconds > 0:
                time.sleep(self.config.pause_seconds)

    def _build_prompt(self, task_number: str, task_title: str) -> str:
        """Build the prompt for the current task."""
        if self.use_prompts:
            prompt_file = get_prompt_for_task(self.paths.prompts_dir, task_number)
            if prompt_file:
                return extract_prompt_content(prompt_file)

        # Fall back to generic prompt
        return build_generic_prompt(
            task_number=task_number,
            task_title=task_title,
            tasks_file=self.paths.tasks_file,
            context_file=self.paths.context_file,
            auto_log=self.paths.auto_log,
        )

    def _run_claude(self, prompt: str, task_number: str = "") -> ExecutionResult:
        """Run Claude with the prompt."""
        runner = ClaudeRunner(
            visibility=self.config.visibility,
            on_tool_use=self.display.tool_use,
        )

        self.display.working()
        session_name = f"{self.task_name}/t{task_number}" if task_number else self.task_name
        return runner.run(prompt, self.project_root, session_name=session_name)

    def _handle_result(
        self,
        result: ExecutionResult,
        task_number: str,
        task_title: str,
    ) -> int | None:
        """
        Handle the result of a Claude invocation.

        Returns exit code if loop should end, None to continue.
        """
        # Write to auto log
        self._write_iteration_log(result, task_number, task_title)

        if result.is_complete:
            # All tasks completed
            self.display.iteration_result(
                "SUCCESS",
                int(result.duration),
                result.tools_used,
                result.learnings,
            )
            self._handle_completion(result.output)
            return 0

        if result.is_blocked:
            self.display.iteration_result(
                "BLOCKED",
                int(result.duration),
                result.tools_used,
            )
            self.display.blocked_summary(task_number, task_title)
            self.logger.log(
                f"Task {task_number} blocked (requires human input)",
                level="warn",
                subtask_id=task_number,
            )
            self.logger.finish(status="cancelled", error_message="Blocked on task")
            return 2

        if result.success:
            # TDD review (blocking) - if enabled and fails, treat as task failure
            if self.config.tdd_mode and self.use_prompts:
                prompt_file = get_prompt_for_task(self.paths.prompts_dir, task_number)
                if prompt_file:
                    from orbit_auto.code_reviewer import run_tdd_review

                    try:
                        tdd_passed, tdd_summary = run_tdd_review(
                            task_number, prompt_file, self.project_root, self.paths.logs_dir
                        )
                    except Exception as e:
                        tdd_passed, tdd_summary = False, str(e)

                    self.logger.log(
                        f"TDD review: {tdd_summary}",
                        level="success" if tdd_passed else "error",
                        subtask_id=task_number,
                    )
                    if not tdd_passed:
                        self.display.iteration_result(
                            "TDD FAILED",
                            int(result.duration),
                            result.tools_used,
                            tdd_summary,
                        )
                        self.current_task_attempts += 1
                        self.logger.log_task_failed(
                            task_id=task_number,
                            error=f"TDD review failed: {tdd_summary}",
                            attempt=self.current_task_attempts,
                            max_attempts=self.config.max_retries,
                        )
                        return None

            # Task completed, reset retry counter
            self.display.iteration_result(
                "SUCCESS",
                int(result.duration),
                result.tools_used,
                result.what_worked or result.learnings,
            )
            self.current_task_attempts = 0

            # Log task completion
            self.logger.log_task_completed(
                task_id=task_number,
                duration=result.duration,
                summary=result.what_worked or result.learnings,
            )

            # Update progress in database
            completed, total = get_task_progress(self.paths.tasks_file)
            self.logger.update_progress(completed=completed)

            # Auto-commit changes for this task
            if self.config.auto_commit and self.use_prompts:
                prompt_file = get_prompt_for_task(self.paths.prompts_dir, task_number)
                if prompt_file:
                    committed, msg = git_commit_task(task_number, prompt_file, self.project_root)
                    if committed:
                        self.logger.log(msg, subtask_id=task_number)

            # Update timestamps and task DB
            update_timestamps(self.paths.tasks_file, self.paths.context_file)
            self._process_heartbeats()
            self._update_task_progress()

            # Handle patterns and gotchas
            if result.pattern_discovered:
                self._add_to_codebase_knowledge("pattern", result.pattern_discovered)
            if result.gotcha:
                self._add_to_codebase_knowledge("gotcha", result.gotcha)

        else:
            # Task failed, increment retry counter
            self.display.iteration_result(
                "FAILED",
                int(result.duration),
                result.tools_used,
                result.what_failed,
            )
            self.current_task_attempts += 1

            # Log task failure
            self.logger.log_task_failed(
                task_id=task_number,
                error=result.what_failed or "Unknown error",
                attempt=self.current_task_attempts,
                max_attempts=self.config.max_retries,
            )

        return None

    def _init_auto_log(self) -> None:
        """Initialize the auto log file if it doesn't exist."""
        if self.paths.auto_log.exists():
            return

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        content = f"""# {self.task_name} - Auto Log

**Started:** {timestamp}
**Last Updated:** {timestamp}

## Codebase Knowledge

### Patterns Discovered
(none yet)

### Gotchas
(none yet)

---

"""
        self.paths.auto_log.write_text(content)

    def _write_iteration_log(
        self,
        result: ExecutionResult,
        task_number: str,
        task_title: str,
    ) -> None:
        """Write an iteration entry to the auto log."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        status = "SUCCESS" if result.success else "FAILED"
        if result.is_blocked:
            status = "BLOCKED"
        if result.is_complete:
            status = "COMPLETE"

        entry = f"""
---

## Task {task_number}: {task_title} - Attempt {self.current_task_attempts + 1}
**Status:** {status}
**Time:** {timestamp}
**Duration:** {int(result.duration)}s | **Tools:** {result.tools_used}

"""

        if result.files_modified:
            entry += "### Files Modified\n"
            for f in result.files_modified:
                # Make path relative to project root
                rel_path = f.replace(str(self.project_root) + "/", "")
                entry += f"- `{rel_path}`\n"
            entry += "\n"

        if result.learnings:
            entry += f"### Learnings\n{result.learnings}\n\n"

        if result.what_worked:
            entry += f"### What Worked\n{result.what_worked}\n\n"

        if result.what_failed:
            entry += f"### What Failed\n{result.what_failed}\n\n"

        if result.dont_retry:
            entry += f"### Don't Retry\n{result.dont_retry}\n\n"

        if result.try_next:
            entry += f"### Try Next\n{result.try_next}\n\n"

        # Append to log
        with open(self.paths.auto_log, "a") as f:
            f.write(entry)

    def _add_to_codebase_knowledge(self, kind: str, content: str) -> None:
        """Add a pattern or gotcha to the Codebase Knowledge section."""
        if not self.paths.auto_log.exists():
            return

        log_content = self.paths.auto_log.read_text()

        # Format the content
        if ":" in content:
            name, desc = content.split(":", 1)
            formatted = f"- **{name.strip()}**: {desc.strip()}"
        else:
            formatted = f"- {content}"

        # Find the right section and add
        if kind == "pattern":
            section = "### Patterns Discovered"
        else:
            section = "### Gotchas"

        # Remove "(none yet)" placeholder if present
        log_content = re.sub(
            rf"({section})\n\(none yet\)",
            rf"\1",
            log_content,
        )

        # Insert after section header
        log_content = re.sub(
            rf"({section})\n",
            rf"\1\n{formatted}\n",
            log_content,
        )

        self.paths.auto_log.write_text(log_content)

    def _handle_completion(self, run_summary: str | None = None) -> None:
        """Handle completion of all tasks."""
        duration = time.time() - self.start_time
        completed, total = get_task_progress(self.paths.tasks_file)

        # Write completion to auto log
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        completion_entry = f"""
---

# COMPLETED
**Finished:** {timestamp}
**Total iterations:** {self.total_iterations}
**Duration:** {int(duration)}s

## Run Summary
{run_summary or "All tasks completed successfully."}
"""
        with open(self.paths.auto_log, "a") as f:
            f.write(completion_entry)

        # Update timestamps
        update_timestamps(self.paths.tasks_file, self.paths.context_file)

        # Complete task in DB
        self._complete_task_in_db()

        # Finish database logging
        self.logger.finish(status="completed")

        # Display summary
        self.display.completion_summary(
            total_iterations=self.total_iterations,
            total_duration=duration,
            completed=completed,
            total=total,
            run_summary=run_summary,
        )

    def _process_heartbeats(self) -> None:
        """Process heartbeats for task DB time tracking."""
        if not self.orbit_db_enabled:
            return
        try:
            subprocess.run(
                ["python3", str(self.orbit_db_script), "process-heartbeats"],
                capture_output=True,
                check=False,
            )
        except Exception:
            pass

    def _update_task_progress(self) -> None:
        """Update task progress in task DB."""
        if not self.orbit_db_enabled:
            return

        completed, total = get_task_progress(self.paths.tasks_file)
        percent = int(completed * 100 / total) if total > 0 else 0

        try:
            subprocess.run(
                [
                    "python3",
                    str(self.orbit_db_script),
                    "add-update",
                    self.task_name,
                    f"[PROGRESS] {completed}/{total} ({percent}%)",
                ],
                capture_output=True,
                check=False,
            )
        except Exception:
            pass

    def _complete_task_in_db(self) -> None:
        """Mark task as completed in task DB."""
        if not self.orbit_db_enabled:
            return
        try:
            subprocess.run(
                ["python3", str(self.orbit_db_script), "complete-task", self.task_name],
                capture_output=True,
                check=False,
            )
        except Exception:
            pass


def run_sequential(
    task_name: str,
    project_root: Path,
    config: Config | None = None,
) -> int:
    """
    Convenience function to run orbit-auto in sequential mode.

    Returns exit code (0=success, 1=failed, 2=blocked, 3=error).
    """
    runner = SequentialRunner(task_name, project_root, config)
    return runner.run()

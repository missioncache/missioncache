"""
Display utilities for Orbit Auto.

Provides colored terminal output, progress visualization,
and formatted display of execution plans and results.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime
from typing import TextIO

from orbit_auto.dag import DAG


# ANSI Color codes
class Colors:
    """ANSI color codes for terminal output."""

    RED = "\033[0;31m"
    GREEN = "\033[0;32m"
    YELLOW = "\033[1;33m"
    BLUE = "\033[0;34m"
    PURPLE = "\033[0;35m"
    CYAN = "\033[0;36m"
    WHITE = "\033[1;37m"
    GRAY = "\033[0;90m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    NC = "\033[0m"  # No Color


class Icons:
    """Status indicator icons."""

    SUCCESS = "+"
    FAILED = "x"
    BLOCKED = "||"
    WORKING = "*"
    TASK = ">"
    WAVE = "="
    BRANCH = "|_"


@dataclass
class DisplayConfig:
    """Configuration for display output."""

    use_color: bool = True
    output: TextIO = sys.stderr


class Display:
    """
    Handles all terminal output for Orbit Auto.

    Provides methods for displaying:
    - Execution plans with waves and dependencies
    - Live progress during task execution
    - Completion summaries with statistics
    """

    def __init__(self, config: DisplayConfig | None = None) -> None:
        self.config = config or DisplayConfig()
        self.c = Colors if self.config.use_color else _NoColors()

    def _print(self, *args, **kwargs) -> None:
        """Print to configured output stream."""
        print(*args, file=self.config.output, **kwargs)

    def header(self, title: str = "ORBIT AUTO") -> None:
        """Print the orbit-auto header."""
        self._print()
        self._print(f"  {self.c.CYAN}{self.c.BOLD}=== {title} ==={self.c.NC}")
        self._print()

    def info(self, message: str) -> None:
        """Print an info message."""
        self._print(f"  {self.c.GREEN}{message}{self.c.NC}")

    def warning(self, message: str) -> None:
        """Print a warning message."""
        self._print(f"  {self.c.YELLOW}{message}{self.c.NC}")

    def error(self, message: str) -> None:
        """Print an error message."""
        self._print(f"  {self.c.RED}{message}{self.c.NC}")

    def task_info(self, task_name: str, details: dict) -> None:
        """Display task information (used for individual sub-tasks)."""
        self._print(f"  {self.c.WHITE}Task:{self.c.NC} {task_name}")
        for key, value in details.items():
            self._print(f"  {self.c.GRAY}{key}:{self.c.NC} {value}")
        self._print()

    def project_info(self, project_name: str, details: dict) -> None:
        """Display project information (top-level orbit project)."""
        self._print(f"  {self.c.WHITE}Project:{self.c.NC} {project_name}")
        for key, value in details.items():
            self._print(f"  {self.c.GRAY}{key}:{self.c.NC} {value}")
        self._print()

    def execution_plan(self, dag: DAG, max_workers: int) -> None:
        """Display the execution plan with waves and dependencies."""
        waves = dag.get_waves()
        critical_length, critical_path = dag.get_critical_path()

        self._print()
        self._print(f"  {self.c.CYAN}{self.c.BOLD}EXECUTION PLAN{self.c.NC}")
        self._print(f"  {self.c.GRAY}{'-' * 50}{self.c.NC}")
        self._print()

        # Summary
        self._print(f"  {self.c.WHITE}Total tasks:{self.c.NC} {dag.task_count}")
        self._print(f"  {self.c.WHITE}Max workers:{self.c.NC} {max_workers}")
        self._print(f"  {self.c.WHITE}Waves:{self.c.NC} {len(waves)}")
        self._print(f"  {self.c.WHITE}Critical path:{self.c.NC} {critical_length} tasks")
        self._print()

        # Waves detail
        for wave_info in waves:
            wave_num = wave_info["wave"]
            tasks = wave_info["tasks"]
            self._print(f"  {self.c.YELLOW}Wave {wave_num}{self.c.NC} ({len(tasks)} tasks)")

            for task_id in tasks:
                deps = dag.get_dependencies(task_id)
                title = dag.get_title(task_id)
                deps_str = f" -> deps: [{', '.join(deps)}]" if deps else ""

                # Highlight critical path tasks
                if task_id in critical_path:
                    marker = f"{self.c.RED}*{self.c.NC}"
                else:
                    marker = " "

                self._print(
                    f"    {marker}{self.c.CYAN}{task_id}{self.c.NC}: "
                    f"{title[:40]}{self.c.DIM}{deps_str}{self.c.NC}"
                )
            self._print()

        # Critical path
        self._print(f"  {self.c.RED}Critical path:{self.c.NC} {' -> '.join(critical_path)}")
        self._print()

    def iteration_header(
        self,
        iteration: int,
        max_iterations: int,
        task_num: str,
        total_tasks: int,
        completed_tasks: int,
        task_title: str,
    ) -> None:
        """Display the iteration header."""
        percent = int(completed_tasks * 100 / total_tasks) if total_tasks > 0 else 0
        progress_bar = self._progress_bar(completed_tasks, total_tasks, width=12)

        self._print()
        self._print(f"  {self.c.GRAY}{'-' * 60}{self.c.NC}")
        self._print(
            f"  {self.c.CYAN}{Icons.WORKING} ITERATION {iteration}/{max_iterations}{self.c.NC}  |  "
            f"Task {task_num}/{total_tasks}  |  {progress_bar} {percent}%"
        )
        self._print(f"  {Icons.TASK} {task_title}")
        self._print(f"  {self.c.GRAY}{'-' * 60}{self.c.NC}")
        self._print()

    def working(self) -> None:
        """Display working indicator."""
        self._print(f"  {self.c.DIM}{Icons.WORKING} Working...{self.c.NC}")

    def tool_use(self, tool_name: str, display_info: str = "") -> None:
        """Display a tool use event."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        if display_info:
            self._print(
                f"  {self.c.GRAY}{timestamp}{self.c.NC} "
                f"{self.c.CYAN}{tool_name}{self.c.NC} "
                f"{self.c.DIM}{display_info}{self.c.NC}"
            )
        else:
            self._print(
                f"  {self.c.GRAY}{timestamp}{self.c.NC} {self.c.CYAN}{tool_name}{self.c.NC}"
            )

    def done(self, duration: int, tool_count: int) -> None:
        """Display completion of an iteration."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self._print(
            f"  {self.c.GRAY}{timestamp}{self.c.NC} "
            f"{self.c.DIM}Done ({duration}s, {tool_count} tools){self.c.NC}"
        )

    def iteration_result(
        self,
        status: str,
        duration: int,
        tool_count: int,
        summary: str | None = None,
    ) -> None:
        """Display the result of an iteration."""
        self._print()

        if status == "SUCCESS":
            self._print(
                f"  {self.c.GREEN}{Icons.SUCCESS} SUCCESS{self.c.NC}  |  "
                f"{duration}s  |  {tool_count} tools"
            )
        elif status == "FAILED":
            self._print(
                f"  {self.c.RED}{Icons.FAILED} FAILED{self.c.NC}  |  "
                f"{duration}s  |  {tool_count} tools"
            )
        elif status == "BLOCKED":
            self._print(
                f"  {self.c.YELLOW}{Icons.BLOCKED} BLOCKED{self.c.NC}  |  "
                f"{duration}s  |  Waiting for human input"
            )

        if summary:
            self._print(f"  {Icons.BRANCH} {summary}")

    def completion_summary(
        self,
        total_iterations: int,
        total_duration: float,
        completed: int,
        total: int,
        run_summary: str | None = None,
    ) -> None:
        """Display the final completion summary."""
        self._print()
        self._print(f"  {self.c.GREEN}{self.c.BOLD}=== COMPLETED ==={self.c.NC}")
        self._print()
        self._print(f"  {self.c.WHITE}Total iterations:{self.c.NC} {total_iterations}")
        self._print(f"  {self.c.WHITE}Duration:{self.c.NC} {int(total_duration)}s")
        self._print(f"  {self.c.WHITE}Tasks completed:{self.c.NC} {completed}/{total}")

        if run_summary:
            self._print()
            self._print(f"  {self.c.CYAN}Summary:{self.c.NC}")
            self._print(f"  {run_summary}")

        self._print()

    def failure_summary(
        self,
        iteration: int,
        max_iterations: int,
        failed_task: str,
        reason: str | None = None,
    ) -> None:
        """Display failure summary when max retries reached."""
        self._print()
        self._print(f"  {self.c.RED}{self.c.BOLD}=== FAILED ==={self.c.NC}")
        self._print()
        self._print(f"  {self.c.WHITE}Iterations:{self.c.NC} {iteration}/{max_iterations}")
        self._print(f"  {self.c.WHITE}Failed on:{self.c.NC} Task {failed_task}")

        if reason:
            self._print()
            self._print(f"  {self.c.RED}Reason:{self.c.NC}")
            self._print(f"  {reason}")

        self._print()

    def blocked_summary(self, task_num: str, task_title: str) -> None:
        """Display blocked summary for WAIT tasks."""
        self._print()
        self._print(f"  {self.c.YELLOW}{self.c.BOLD}=== BLOCKED ==={self.c.NC}")
        self._print()
        self._print(f"  {self.c.WHITE}Waiting on:{self.c.NC} Task {task_num}: {task_title}")
        self._print()
        self._print(
            f"  {self.c.DIM}Remove [WAIT] marker or complete task manually to continue{self.c.NC}"
        )
        self._print()

    def parallel_progress(
        self,
        completed: int,
        total: int,
        in_progress: list[str],
        failed: list[str],
    ) -> None:
        """Display parallel execution progress."""
        progress_bar = self._progress_bar(completed, total, width=20)
        percent = int(completed * 100 / total) if total > 0 else 0

        self._print(
            f"\r  {progress_bar} {percent}%  |  "
            f"{self.c.GREEN}{completed}{self.c.NC} done  |  "
            f"{self.c.CYAN}{len(in_progress)}{self.c.NC} running  |  "
            f"{self.c.RED}{len(failed)}{self.c.NC} failed",
            end="",
        )

    def _progress_bar(self, current: int, total: int, width: int = 20) -> str:
        """Create a text progress bar."""
        if total == 0:
            return "#" * width

        filled = int(width * current / total)
        empty = width - filled
        return f"{'#' * filled}{'.' * empty}"

    def blocked_tasks_warning(
        self,
        completed_count: int,
        blocked_by_inter: list[dict],
        first_blocker: dict | None = None,
    ) -> None:
        """Display warning about tasks blocked by interactive tasks.

        Args:
            completed_count: Number of tasks completed this run
            blocked_by_inter: List of auto tasks waiting for interactive tasks
            first_blocker: Info about the first interactive blocker (task_id, title)
        """
        self._print()
        self._print(f"  {self.c.YELLOW}{self.c.BOLD}=== BLOCKED BY INTERACTIVE ==={self.c.NC}")
        self._print()

        if completed_count > 0:
            self._print(
                f"  {self.c.GREEN}{Icons.SUCCESS} Completed:{self.c.NC} {completed_count} tasks"
            )

        blocked_count = len(blocked_by_inter)
        self._print(
            f"  {self.c.YELLOW}{Icons.BLOCKED} Blocked:{self.c.NC} {blocked_count} tasks waiting for interactive"
        )

        if first_blocker:
            self._print()
            self._print(
                f"  {self.c.WHITE}Waiting on:{self.c.NC} Task {first_blocker.get('task_id')}: "
                f"{first_blocker.get('title', 'Interactive task')}"
            )

        self._print()
        self._print(
            f"  {self.c.DIM}Complete the interactive task(s) to unblock remaining work.{self.c.NC}"
        )
        self._print()

    def runnable_status(
        self,
        runnable_count: int,
        blocked_count: int,
        blocked_by_inter_count: int,
    ) -> None:
        """Display current runnable task status."""
        self._print()
        self._print(f"  {self.c.WHITE}Runnable status:{self.c.NC}")
        self._print(f"    {self.c.GREEN}{runnable_count}{self.c.NC} tasks ready to run")

        if blocked_count > 0:
            self._print(f"    {self.c.YELLOW}{blocked_count}{self.c.NC} tasks blocked")

        if blocked_by_inter_count > 0:
            self._print(
                f"    {self.c.YELLOW}{blocked_by_inter_count}{self.c.NC} blocked by interactive"
            )

        self._print()


class _NoColors:
    """Stub class for when colors are disabled."""

    def __getattr__(self, name: str) -> str:
        return ""


def create_display(use_color: bool = True, output: TextIO = sys.stderr) -> Display:
    """Factory function to create a Display instance."""
    return Display(DisplayConfig(use_color=use_color, output=output))

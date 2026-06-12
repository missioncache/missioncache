"""
Command-line interface for Orbit Auto.

Provides the main entry point and argument parsing for all orbit-auto commands.
"""

import argparse
import os
import sys
from pathlib import Path

from orbit_auto.display import create_display
from orbit_auto.init_task import init_task
from orbit_auto.models import Config, Visibility
from orbit_auto.parallel import run_parallel
from orbit_auto.sequential import run_sequential


def find_project_root() -> Path:
    """Find project root by looking for dev/ directory."""
    current = Path.cwd()
    while current != current.parent:
        if (current / "dev").is_dir():
            return current
        current = current.parent

    # If no dev/ found, use current directory
    return Path.cwd()


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    # Pre-process argv: if first positional arg isn't a known command, insert "run"
    known_commands = {"run", "init", "status", "-h", "--help"}

    # Find first positional arg (skip options that start with -)
    argv = sys.argv[1:]
    first_positional_idx = None
    for i, arg in enumerate(argv):
        if not arg.startswith("-"):
            first_positional_idx = i
            break
        # Skip option values (e.g., -v verbose)
        if arg in ("-v", "--visibility"):
            continue

    # If first positional is not a known command, insert "run"
    if first_positional_idx is not None:
        first_positional = argv[first_positional_idx]
        if first_positional not in known_commands:
            sys.argv.insert(first_positional_idx + 1, "run")

    parser = argparse.ArgumentParser(
        prog="orbit-auto",
        description="Orbit Auto - Autonomous AI Development for Orbit Projects",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  orbit-auto my-feature                    # Parallel (default, 8 workers)
  orbit-auto my-feature -w 12              # Parallel with 12 workers
  orbit-auto my-feature --sequential       # Sequential mode
  orbit-auto my-feature --dry-run          # Show execution plan
  orbit-auto init my-feature "description" # Initialize project
  orbit-auto status my-feature             # Show project status

Exit Codes:
  0  All tasks completed successfully
  1  Max retries reached (failed)
  2  Blocked on [WAIT] task
  3  Missing prompt or configuration error
""",
    )

    # Global options
    parser.add_argument(
        "-v",
        "--visibility",
        choices=["verbose", "minimal", "none"],
        default=os.environ.get("ORBIT_AUTO_VISIBILITY", "verbose"),
        help="Output visibility level (env: ORBIT_AUTO_VISIBILITY)",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable colored output",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="command")

    # Run command (default when just task name given)
    run_parser = subparsers.add_parser("run", help="Run orbit-auto on a project")
    _add_run_arguments(run_parser)

    # Init command
    init_parser = subparsers.add_parser("init", help="Initialize a new project")
    init_parser.add_argument("task_name", help="Name of the project")
    init_parser.add_argument(
        "description",
        nargs="?",
        default="",
        help="Project description",
    )

    # Status command
    status_parser = subparsers.add_parser("status", help="Show project status")
    status_parser.add_argument("task_name", help="Name of the project")

    return parser.parse_args()


def _add_run_arguments(parser: argparse.ArgumentParser) -> None:
    """Add arguments for the run command."""
    parser.add_argument("task_name", help="Name of task in ~/.orbit/active/")

    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--sequential",
        "-s",
        action="store_true",
        help="Run in sequential mode (one task at a time)",
    )
    mode_group.add_argument(
        "--parallel",
        "-p",
        action="store_true",
        default=True,
        help="Run in parallel mode (default)",
    )

    parser.add_argument(
        "-w",
        "--workers",
        type=int,
        default=8,
        help="Number of parallel workers (default: 8, max: 12)",
    )
    parser.add_argument(
        "-r",
        "--retries",
        type=int,
        default=3,
        help="Max retries per task (default: 3)",
    )
    parser.add_argument(
        "--pause",
        type=int,
        default=3,
        help="Pause between iterations in seconds (default: 3)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=1800,
        help="Timeout per task in seconds (default: 1800 = 30 min, 0 = no timeout)",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop all workers on first failure (parallel mode)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show execution plan without running",
    )
    parser.add_argument(
        "--worktree",
        action="store_true",
        help="Isolate each worker in its own git worktree (prevents file conflicts)",
    )

    parser.add_argument(
        "--no-commit",
        action="store_true",
        help="Disable automatic git commit after each task",
    )

    review_group = parser.add_mutually_exclusive_group()
    review_group.add_argument(
        "--enable-review",
        action="store_true",
        help="Run two-stage code review after each task (spec compliance + code quality)",
    )
    review_group.add_argument(
        "--spec-review-only",
        action="store_true",
        help="Run only spec compliance review after each task (cheaper, ~$0.15/task)",
    )

    parser.add_argument(
        "--tdd",
        action="store_true",
        help="Enforce TDD: wrap prompts with RED-GREEN-REFACTOR, block tasks without tests",
    )


def cmd_run(args: argparse.Namespace) -> int:
    """Run orbit-auto on a task."""
    project_root = find_project_root()
    display = create_display(use_color=not args.no_color)

    config = Config(
        max_workers=args.workers,
        max_retries=args.retries,
        pause_seconds=args.pause,
        task_timeout=args.timeout,
        fail_fast=args.fail_fast,
        visibility=Visibility(args.visibility),
        dry_run=args.dry_run,
        use_worktrees=getattr(args, "worktree", False),
        enable_review=getattr(args, "enable_review", False),
        spec_review_only=getattr(args, "spec_review_only", False),
        auto_commit=not getattr(args, "no_commit", False),
        tdd_mode=getattr(args, "tdd", False),
    )

    if args.sequential:
        return run_sequential(args.task_name, project_root, config)
    else:
        return run_parallel(args.task_name, project_root, config, args.dry_run)


def cmd_init(args: argparse.Namespace) -> int:
    """Initialize a new task."""
    project_root = find_project_root()
    display = create_display(use_color=not args.no_color)

    try:
        task_dir = init_task(args.task_name, args.description, project_root)
        display.info(f"Created project: {args.task_name}")
        display.info(f"Directory: {task_dir}")
        return 0
    except Exception as e:
        display.error(f"Failed to create project: {e}")
        return 1


def cmd_status(args: argparse.Namespace) -> int:
    """Show task status."""
    from orbit_auto.models import TaskPaths
    from orbit_auto.runnable import get_blocking_summary, get_runnable_tasks
    from orbit_auto.task_parser import get_task_progress, parse_tasks_md

    display = create_display(use_color=not args.no_color)
    paths = TaskPaths.from_task_name(args.task_name)

    errors = paths.validate()
    if errors:
        for error in errors:
            display.error(error)
        return 1

    display.header(f"PROJECT: {args.task_name}")

    completed, total = get_task_progress(paths.tasks_file)
    percent = int(completed * 100 / total) if total > 0 else 0

    # Get blocking summary
    blocking = get_blocking_summary(paths.tasks_file)

    info = {
        "Progress": f"{completed}/{total} ({percent}%)",
        "Tasks file": str(paths.tasks_file),
        "Prompts": "Yes" if paths.prompts_dir.exists() else "No",
    }

    # Add blocking info if relevant
    if blocking["runnable_count"] > 0 or blocking["blocked_by_inter_count"] > 0:
        info["Ready to run"] = blocking["runnable_count"]
        if blocking["blocked_by_inter_count"] > 0:
            info["Blocked by interactive"] = blocking["blocked_by_inter_count"]

    display.project_info(args.task_name, info)

    # Show individual tasks with blocking status
    runnable_result = get_runnable_tasks(paths.tasks_file)
    task_info_map = {t.task_id: t for t in runnable_result.all_tasks}

    tasks = parse_tasks_md(paths.tasks_file)
    display._print("  Tasks:")
    for task in tasks:
        status = "+" if task.is_completed else "o"
        wait = " [WAIT]" if task.is_wait else ""

        # Check blocking status from runnable analysis
        extra = ""
        task_mode = task_info_map.get(task.number)
        if task_mode and task_mode.mode == "auto":
            if task_mode.is_blocked:
                if task_mode.blocker_mode == "inter":
                    extra = f" [blocked by #{task_mode.blocked_by}]"
                else:
                    extra = f" [waiting on #{task_mode.blocked_by}]"
            elif not task_mode.completed:
                extra = " [ready]"

        display._print(f"    {status} {task.number}. {task.title}{wait}{extra}")

    display._print()
    return 0


def main() -> int:
    """Main entry point for orbit-auto CLI."""
    args = parse_args()

    # Handle commands using match statement (Python 3.10+)
    match args.command:
        case "run" | None:
            if not hasattr(args, "task_name"):
                print("Error: project_name required")
                print("Usage: orbit-auto <project-name> [options]")
                return 1
            return cmd_run(args)
        case "init":
            return cmd_init(args)
        case "status":
            return cmd_status(args)
        case _:
            print(f"Unknown command: {args.command}")
            return 1


if __name__ == "__main__":
    sys.exit(main())

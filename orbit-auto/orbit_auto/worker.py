"""
Worker process for parallel orbit-auto execution.

Each worker claims and executes tasks atomically,
respecting dependencies via the shared state manager.
"""

import subprocess
import time
from pathlib import Path
from typing import Optional

from orbit_auto.claude_runner import ClaudeRunner
from orbit_auto.dag import DAG
from orbit_auto.db_logger import ExecutionLogger
from orbit_auto.models import Visibility
from orbit_auto.state import StateManager
from orbit_auto.task_parser import extract_prompt_content, parse_prompt_yaml


def _extract_task_title(prompt_file: Path) -> str:
    """Extract task title from prompt YAML frontmatter or first heading."""
    if prompt_file.exists():
        info = parse_prompt_yaml(prompt_file)
        if info and info.task_title:
            return info.task_title

        # Fallback: first markdown heading
        content = prompt_file.read_text()
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                # Strip "# Task XX: " prefix if present
                heading = stripped.lstrip("# ").strip()
                if ": " in heading:
                    return heading.split(": ", 1)[1]
                return heading

    return f"task {prompt_file.stem}"


def git_commit_task(task_id: str, prompt_file: Path, project_root: Path) -> tuple[bool, str]:
    """Commit changes for a completed task.

    Returns (committed, message) where committed=True if a commit was made,
    and message is a log-friendly string (commit msg or warning).
    """
    title = _extract_task_title(prompt_file)
    commit_msg = f"feat({task_id}): {title}"
    cwd = str(project_root)

    try:
        # Check for unstaged or staged changes
        unstaged = subprocess.run(["git", "diff", "--quiet"], cwd=cwd, capture_output=True)
        staged = subprocess.run(
            ["git", "diff", "--cached", "--quiet"], cwd=cwd, capture_output=True
        )

        if unstaged.returncode == 0 and staged.returncode == 0:
            return False, ""

        subprocess.run(["git", "add", "-A"], cwd=cwd, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=cwd,
            capture_output=True,
            check=True,
        )
        return True, f"Committed: {commit_msg}"

    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode(errors="replace").strip() if e.stderr else ""
        return False, f"Git commit failed for task {task_id}: {stderr}"
    except Exception as e:
        return False, f"Git commit error for task {task_id}: {e}"


class Worker:
    """
    A worker process that claims and executes tasks.

    Workers:
    - Atomically claim available tasks via StateManager
    - Execute tasks by invoking Claude with the prompt
    - Report success/failure back to state
    - Respect max_retries for failed tasks
    """

    def __init__(
        self,
        worker_id: int,
        task_name: str,
        project_root: Path,
        state_dir: Path,
        prompts_dir: Path,
        adjacency_file: Path,
        logs_dir: Path | None = None,
        max_retries: int = 3,
        task_timeout: int = 1800,
        visibility: Visibility = Visibility.NONE,
        execution_id: Optional[int] = None,
        enable_review: bool = False,
        spec_review_only: bool = False,
        auto_commit: bool = True,
        tdd_mode: bool = False,
    ) -> None:
        self.worker_id = worker_id
        self.task_name = task_name
        self.project_root = project_root
        self.state_dir = state_dir
        self.prompts_dir = prompts_dir
        self.adjacency_file = adjacency_file
        self.logs_dir = logs_dir
        self.max_retries = max_retries
        self.task_timeout = task_timeout
        self.visibility = visibility
        self.execution_id = execution_id
        self.enable_review = enable_review
        self.spec_review_only = spec_review_only
        self.auto_commit = auto_commit
        self.tdd_mode = tdd_mode

        self.state_manager = StateManager(state_dir)
        self.dag: DAG | None = None

        # Initialize database logger if execution_id provided
        self._db_logger: Optional[ExecutionLogger] = None
        if execution_id is not None:
            self._init_db_logger()

    def _init_db_logger(self) -> None:
        """Initialize the database logger for this worker."""
        try:
            from orbit_db import DB_PATH, TaskDB

            db_path = DB_PATH
            if db_path.exists() and self.execution_id is not None:
                # Create a lightweight logger that wraps TaskDB
                self._db_logger = _WorkerDBLogger(
                    TaskDB(str(db_path)),
                    self.execution_id,
                    self.worker_id,
                )
        except ImportError:
            pass
        except Exception as e:
            # Match db_logger.py: surface to stderr instead of silent disable.
            # First line only - migration errors carry multi-line shell snippets.
            import sys

            first_line = str(e).splitlines()[0] if str(e) else ""
            print(
                f"orbit-auto worker {self.worker_id}: db logger disabled ({type(e).__name__}: {first_line})",
                file=sys.stderr,
            )

    def _log(self, message: str, level: str = "info", subtask_id: Optional[str] = None) -> None:
        """Log a message to the database if logging is enabled."""
        if self._db_logger is not None:
            self._db_logger.log(message, level, subtask_id)

    def run(self) -> None:
        """Main worker loop - claim and execute tasks until none available."""
        # Load DAG from adjacency file
        if self.adjacency_file.exists():
            self.dag = DAG.from_adjacency_file(self.adjacency_file)
        else:
            self.dag = DAG()

        while True:
            # Try to claim a task
            task_id = self.state_manager.claim_task(self.worker_id, self.dag)

            if task_id is None:
                # No tasks available - check if we should wait or exit
                if self._should_wait():
                    time.sleep(0.5)
                    continue
                else:
                    break

            # Get task state to check for previous errors and attempt number
            state = self.state_manager.read()
            task = state.tasks.get(task_id)
            attempt = task.attempts if task else 1
            previous_error = task.error_message if task else None

            # Log task claimed
            self._log(f"Claimed task {task_id}", level="info", subtask_id=task_id)

            # Log retry info if this is a retry
            if attempt > 1 and previous_error:
                self._log_retry(task_id, attempt, previous_error)

            # Execute the task with error context if retrying
            start_time = time.time()
            success, error_message = self._execute_task(task_id, previous_error)
            duration = time.time() - start_time

            if success:
                # Run reviews (TDD is blocking, spec/quality are advisory)
                review_ok = True
                if self.enable_review or self.spec_review_only or self.tdd_mode:
                    review_ok = self._run_review(task_id)

                if self.tdd_mode and not review_ok:
                    error_message = "TDD review failed: insufficient test coverage"
                    result = self.state_manager.release_task(
                        task_id, self.max_retries, error_message
                    )
                    if result == "max_retries_reached":
                        self._log_failure(task_id, error_message)
                    continue

                # Auto-commit changes for this task
                if self.auto_commit:
                    self._git_commit(task_id)

                self.state_manager.complete_task(task_id)
                self._log(
                    f"Task {task_id} completed ({int(duration)}s)",
                    level="success",
                    subtask_id=task_id,
                )
            else:
                # Release for retry or mark as failed, with error message
                result = self.state_manager.release_task(task_id, self.max_retries, error_message)
                if result == "max_retries_reached":
                    # Task failed permanently
                    self._log_failure(task_id, error_message)

    def _log_retry(self, task_id: str, attempt: int, previous_error: str) -> None:
        """Log when a task is being retried."""
        print(
            f"[Worker {self.worker_id}] Task {task_id}: Retry {attempt}/{self.max_retries} (previous: {previous_error})"
        )
        self._log(
            f"Retrying (attempt {attempt}/{self.max_retries}): {previous_error}",
            level="warn",
            subtask_id=task_id,
        )

    def _log_failure(self, task_id: str, error_message: str | None) -> None:
        """Log when a task has permanently failed."""
        msg = error_message or "Unknown error"
        print(
            f"[Worker {self.worker_id}] Task {task_id}: FAILED after {self.max_retries} attempts ({msg})"
        )
        self._log(
            f"Failed after {self.max_retries} attempts: {msg}",
            level="error",
            subtask_id=task_id,
        )

    def _run_review(self, task_id: str) -> bool:
        """Run code reviews after task completion.

        Returns True if all blocking reviews passed, False if TDD review failed.
        Spec and quality reviews are advisory (logged but don't block).
        """
        prompt_file = self.prompts_dir / f"task-{task_id}-prompt.md"

        # Stage 0: TDD compliance review (BLOCKING when tdd_mode enabled)
        if self.tdd_mode:
            from orbit_auto.code_reviewer import run_tdd_review

            try:
                passed, summary = run_tdd_review(
                    task_id,
                    prompt_file,
                    self.project_root,
                    self.logs_dir,
                )
                level = "success" if passed else "error"
                self._log(f"TDD review: {summary}", level=level, subtask_id=task_id)
                if not passed:
                    return False
            except Exception as e:
                self._log(f"TDD review failed: {e}", level="error", subtask_id=task_id)
                return False

        # Stage 1: Spec compliance review (advisory)
        if self.enable_review or self.spec_review_only:
            from orbit_auto.code_reviewer import run_spec_review

            try:
                passed, summary = run_spec_review(
                    task_id,
                    prompt_file,
                    self.project_root,
                    self.logs_dir,
                )
                level = "success" if passed else "warn"
                self._log(f"Spec review: {summary}", level=level, subtask_id=task_id)
            except Exception as e:
                self._log(f"Spec review failed: {e}", level="warn", subtask_id=task_id)

        # Stage 2: Code quality review (advisory, unless spec-only mode)
        if self.enable_review:
            from orbit_auto.code_reviewer import run_quality_review

            try:
                passed, summary = run_quality_review(
                    task_id,
                    self.project_root,
                    self.logs_dir,
                )
                level = "success" if passed else "warn"
                self._log(f"Quality review: {summary}", level=level, subtask_id=task_id)
            except Exception as e:
                self._log(f"Quality review failed: {e}", level="warn", subtask_id=task_id)

        return True

    def _git_commit(self, task_id: str) -> None:
        """Auto-commit changes after a successful task."""
        prompt_file = self.prompts_dir / f"task-{task_id}-prompt.md"
        committed, msg = git_commit_task(task_id, prompt_file, self.project_root)
        if committed:
            self._log(msg, subtask_id=task_id)
        elif msg:
            self._log(msg, level="warn", subtask_id=task_id)

    def _should_wait(self) -> bool:
        """Check if worker should wait for more tasks to become available."""
        # Get current state
        state = self.state_manager.read()

        # If there are pending tasks but none we can claim (due to deps), wait
        pending_count = sum(1 for task in state.tasks.values() if task.status.value == "pending")
        in_progress_count = sum(
            1 for task in state.tasks.values() if task.status.value == "in_progress"
        )

        # Wait if there are pending tasks AND other workers are making progress
        return pending_count > 0 and in_progress_count > 0

    def _check_tdd_override(self, prompt_file: Path) -> bool | None:
        """Check for per-task TDD override in prompt YAML frontmatter.

        Returns True (force enable), False (force disable), or None (use global).
        """
        info = parse_prompt_yaml(prompt_file)
        if info and info.tdd is not None:
            return info.tdd
        return None

    def _wrap_tdd_prompt(self, prompt: str) -> str:
        """Wrap task prompt with TDD enforcement instructions."""
        tdd_wrapper = """<tdd-enforcement>
You MUST follow RED-GREEN-REFACTOR for this task:

1. RED: Write failing tests FIRST that define the expected behavior
2. GREEN: Write minimum code to make tests pass
3. REFACTOR: Clean up while keeping tests green

Rules:
- Do NOT write implementation code before tests exist
- Run tests after each phase to verify state
- If tests don't exist for this area, write them before changing code
</tdd-enforcement>

"""
        return tdd_wrapper + prompt

    def _execute_task(
        self, task_id: str, previous_error: str | None = None
    ) -> tuple[bool, str | None]:
        """
        Execute a single task.

        Args:
            task_id: The task to execute
            previous_error: Error message from previous attempt (for smart retry)

        Returns:
            Tuple of (success, error_message). error_message is None on success.
        """
        # Find prompt file
        prompt_file = self.prompts_dir / f"task-{task_id}-prompt.md"
        if not prompt_file.exists():
            return False, f"Prompt file not found: {prompt_file.name}"

        # Extract prompt content
        prompt = extract_prompt_content(prompt_file)
        if not prompt:
            return False, "Failed to extract prompt content from file"

        # Wrap with TDD instructions if enabled (respects per-task override)
        if self.tdd_mode:
            tdd_override = self._check_tdd_override(prompt_file)
            if tdd_override is not False:
                prompt = self._wrap_tdd_prompt(prompt)

        # If retrying, prepend error context to help Claude fix the issue
        if previous_error:
            prompt = self._build_retry_prompt(prompt, previous_error)

        # Construct log file path if logs_dir is set
        # Include timestamp to preserve logs from failed attempts
        log_file = None
        if self.logs_dir:
            from datetime import datetime

            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            log_file = self.logs_dir / f"worker-{self.worker_id:02d}-task-{task_id}-{timestamp}.log"

        # Run Claude
        runner = ClaudeRunner(visibility=self.visibility)
        timeout = self.task_timeout if self.task_timeout > 0 else None
        session_name = f"{self.task_name}/t{task_id}/w{self.worker_id:02d}"
        result = runner.run(
            prompt, self.project_root, print_output=False, log_file=log_file,
            timeout=timeout, session_name=session_name,
        )

        if result.is_blocked:
            return False, "Task blocked: waiting for human input"

        if not result.success:
            # Build informative error message
            error_parts = []

            # CLI errors take priority (e.g., rate limits, auth issues, flag errors)
            if result.cli_error:
                error_parts.append(f"CLI error: {result.cli_error}")
            elif result.what_failed:
                error_parts.append(result.what_failed)
            elif not result.output:
                error_parts.append("No output from Claude (possible CLI error or rate limit)")
            else:
                # No <what_worked> tag means failure
                error_parts.append("Missing <what_worked> tag in response")

            return False, "; ".join(error_parts) if error_parts else "Unknown error"

        return True, None

    def _build_retry_prompt(self, original_prompt: str, previous_error: str) -> str:
        """
        Build a retry prompt that includes context about the previous failure.

        This helps Claude understand what went wrong and fix it.
        """
        retry_context = f"""<retry-context>
This is a RETRY. The previous attempt failed with this error:
{previous_error}

Please fix the issue and try again. Common fixes:
- If "Missing <what_worked> tag": Make sure to include <what_worked>...</what_worked> at the end
- If "CLI error": The issue may be transient, try the same approach
- If specific error mentioned: Address that specific issue

Now proceed with the original task:
</retry-context>

"""
        return retry_context + original_prompt


class _WorkerDBLogger:
    """
    Lightweight logger wrapper for worker processes.

    Wraps TaskDB to add execution logs with worker context.
    """

    def __init__(self, db, execution_id: int, worker_id: int) -> None:
        self._db = db
        self._execution_id = execution_id
        self._worker_id = worker_id

    def log(
        self,
        message: str,
        level: str = "info",
        subtask_id: Optional[str] = None,
    ) -> None:
        """Add a log entry to the execution."""
        try:
            self._db.add_auto_execution_log(
                execution_id=self._execution_id,
                message=message,
                level=level,
                worker_id=self._worker_id,
                subtask_id=subtask_id,
            )
        except Exception:
            pass

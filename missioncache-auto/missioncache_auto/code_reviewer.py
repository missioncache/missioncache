"""
Optional two-stage code review for orbit-auto tasks.

Runs after a task completes successfully:
- Stage 1 (spec compliance): Checks implementation matches the prompt spec
- Stage 2 (code quality): Checks for code quality issues

Off by default. Enable via --enable-review or --spec-review-only.
"""

from __future__ import annotations

from pathlib import Path

from orbit_auto.claude_runner import ClaudeRunner
from orbit_auto.models import Visibility


def run_tdd_review(
    task_id: str,
    prompt_file: Path,
    project_root: Path,
    logs_dir: Path | None = None,
) -> tuple[bool, str]:
    """
    Run TDD compliance review on a completed task.

    Checks that tests were written/modified and cover the task's requirements.
    This is a BLOCKING review - failure means the task should be retried.

    Returns (passed, summary).
    """
    prompt_content = prompt_file.read_text() if prompt_file.exists() else ""

    review_prompt = f"""You are reviewing code changes for TDD compliance.

A developer completed a task. Check whether they followed test-driven development.

<original_spec>
{prompt_content}
</original_spec>

Steps:
1. Run `git diff HEAD~1 --name-only` to see which files changed
2. Check if any test files were added or modified (files matching *test*, *spec*, tests/)
3. If tests exist, verify they cover the task's acceptance criteria
4. Check that tests actually pass by running the project's test command if identifiable

Respond with:
<review_result>
<passed>true or false</passed>
<summary>One-line summary of TDD compliance</summary>
<issues>List any TDD issues found (empty if passed)</issues>
</review_result>

Pass if: test files were added/modified AND they cover the task requirements.
Fail if: no test changes, or tests don't cover the acceptance criteria.

<what_worked>TDD compliance review completed</what_worked>
"""

    runner = ClaudeRunner(visibility=Visibility.NONE)
    result = runner.run(
        review_prompt,
        project_root,
        print_output=False,
        log_file=_log_path(logs_dir, task_id, "tdd"),
    )

    passed = "<passed>true</passed>" in (result.output or "")
    summary = _extract_tag(result.output or "", "summary") or "TDD review completed"
    return passed, summary


def run_spec_review(
    task_id: str,
    prompt_file: Path,
    project_root: Path,
    logs_dir: Path | None = None,
) -> tuple[bool, str]:
    """
    Run spec compliance review on a completed task.

    Compares git diff against the original prompt to verify
    the implementation matches the spec.

    Returns (passed, summary).
    """
    prompt_content = prompt_file.read_text() if prompt_file.exists() else ""

    review_prompt = f"""You are reviewing code changes for spec compliance.

A developer completed a task. Review the git diff against the original spec below.

<original_spec>
{prompt_content}
</original_spec>

Steps:
1. Run `git diff HEAD~1` to see what changed (if no diff, check `git diff` for unstaged changes)
2. Compare changes against the acceptance criteria in the spec
3. Check that all requirements are addressed

Respond with:
<review_result>
<passed>true or false</passed>
<summary>One-line summary of findings</summary>
<issues>List any spec compliance issues found (empty if passed)</issues>
</review_result>

<what_worked>Spec compliance review completed</what_worked>
"""

    runner = ClaudeRunner(visibility=Visibility.NONE)
    result = runner.run(
        review_prompt,
        project_root,
        print_output=False,
        log_file=_log_path(logs_dir, task_id, "spec"),
    )

    passed = "<passed>true</passed>" in (result.output or "")
    summary = _extract_tag(result.output or "", "summary") or "Review completed"
    return passed, summary


def run_quality_review(
    task_id: str,
    project_root: Path,
    logs_dir: Path | None = None,
) -> tuple[bool, str]:
    """
    Run code quality review on a completed task.

    Checks for code quality issues in the recent changes.

    Returns (passed, summary).
    """
    review_prompt = """You are reviewing code changes for quality.

Steps:
1. Run `git diff HEAD~1` to see what changed (if no diff, check `git diff` for unstaged changes)
2. Review for:
   - Code style and readability
   - Error handling
   - Edge cases
   - Unused imports or dead code introduced

Respond with:
<review_result>
<passed>true or false</passed>
<summary>One-line summary of findings</summary>
<issues>List any quality issues found (empty if passed)</issues>
</review_result>

<what_worked>Code quality review completed</what_worked>
"""

    runner = ClaudeRunner(visibility=Visibility.NONE)
    result = runner.run(
        review_prompt,
        project_root,
        print_output=False,
        log_file=_log_path(logs_dir, task_id, "quality"),
    )

    passed = "<passed>true</passed>" in (result.output or "")
    summary = _extract_tag(result.output or "", "summary") or "Review completed"
    return passed, summary


def _log_path(logs_dir: Path | None, task_id: str, stage: str) -> Path | None:
    """Build log file path for review output."""
    if not logs_dir:
        return None
    from datetime import datetime

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return logs_dir / f"review-{stage}-task-{task_id}-{timestamp}.log"


def _extract_tag(text: str, tag: str) -> str | None:
    """Extract content between XML tags."""
    import re

    match = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.DOTALL)
    return match.group(1).strip() if match else None

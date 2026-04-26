#!/usr/bin/env python3
"""
UserPromptSubmit hook - Detect orbit task tracking divergence.

Runs on every user prompt and checks whether the active orbit project's
context file has findings recorded for tasks that are still unchecked in
the tasks file. If divergence is detected, prints a reminder to stdout so
Claude sees it at the moment it's about to move on to the next task.

This exists because Claude instances tend to treat the context file as the
live progress ledger (appending findings under `### Task N` headings) but
forget to flip the corresponding checkbox in the tasks file. The statusline
progress display `[X/Y]` shows the user this divergence, but Claude can't
see its own statusline - so this hook injects the same signal into Claude's
context.
"""

import json
import os
import re
import sys
from pathlib import Path

# Bundled orbit-db path for marketplace installs (no system pip install).
_BUNDLED_ORBIT_DB = Path(__file__).resolve().parent.parent / "orbit-db"
if _BUNDLED_ORBIT_DB.is_dir() and str(_BUNDLED_ORBIT_DB) not in sys.path:
    sys.path.insert(0, str(_BUNDLED_ORBIT_DB))

# Skip patterns - do not check for divergence on these prompts (match
# activity_tracker.py:16-25 behavior for consistency).
SKIP_PATTERNS = [
    re.compile(r"^/\w+"),        # Slash commands
    re.compile(r"^!\w+"),        # Shell commands
    re.compile(r"^exit$", re.I),
    re.compile(r"^clear$", re.I),
    re.compile(r"^help$", re.I),
    re.compile(r"^y(es)?$", re.I),
    re.compile(r"^n(o)?$", re.I),
    re.compile(r"^\s*$"),        # Empty prompts
]

# Tasks file pattern - capture "- [ ] N. description" with top-level
# numbering only (matches the orbit template format).
PENDING_RE = re.compile(
    r"^\s*-\s*\[\s*\]\s+(\d+)\.\s+(.+?)\s*$", re.MULTILINE
)

# Context file heading pattern - captures "### Task N" or "### Task N: description"
HEADING_RE = re.compile(r"^###\s+Task\s+(\d+)", re.MULTILINE | re.IGNORECASE)


def should_skip(prompt: str) -> bool:
    """Return True if this prompt shouldn't trigger divergence checks."""
    trimmed = prompt.strip()
    return any(p.search(trimmed) for p in SKIP_PATTERNS)


def parse_pending_tasks(tasks_content: str) -> dict[int, str]:
    """Return {task_num: description} for tasks still marked `[ ]`."""
    return {int(num): desc for num, desc in PENDING_RE.findall(tasks_content)}


def parse_context_headings(context_content: str) -> set[int]:
    """Return set of task numbers that have `### Task N` headings."""
    return {int(num) for num in HEADING_RE.findall(context_content)}


def build_reminder(
    divergent_tasks: dict[int, str], tasks_file_path: str
) -> str:
    """Format the divergence reminder for stdout injection."""
    lines = [
        "",
        "## \u26a0\ufe0f Orbit task tracking divergence",
        "",
        "The context file has findings recorded for tasks that are still "
        "unchecked in the tasks file:",
        "",
    ]
    for num in sorted(divergent_tasks):
        lines.append(f"- Task {num}: {divergent_tasks[num]}")
    lines += [
        "",
        "If any of these are actually complete, mark them NOW before continuing:",
        "",
        "  mcp__plugin_orbit_pm__update_tasks_file(",
        f'    tasks_file="{tasks_file_path}",',
        '    completed_tasks=["task description", ...]',
        "  )",
        "",
        "Or run /orbit:save to update both files in one step.",
        "",
        "If a task is still in progress, ignore this warning and continue - "
        "it will clear once the checkbox flips or the heading is removed from "
        "the context file.",
        "",
        "Important: the built-in TaskCreate tool and any system reminders "
        "about \"task tools\" refer to Claude Code's in-conversation todo "
        "list, NOT the orbit tasks file. Use "
        "`mcp__plugin_orbit_pm__update_tasks_file` for orbit work.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    """Entry point - read stdin, check for divergence, print reminder if any."""
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        return

    # Skip in subagent context - subagents have their own context and
    # shouldn't be distracted by the parent session's task tracking.
    if data.get("agent_id"):
        return

    raw_prompt = data.get("prompt", "")
    if isinstance(raw_prompt, list):
        raw_prompt = " ".join(
            b.get("text", "") for b in raw_prompt if isinstance(b, dict) and b.get("type") == "text"
        )
    prompt = raw_prompt if isinstance(raw_prompt, str) else ""
    if should_skip(prompt):
        return

    cwd = data.get("cwd", "") or os.getcwd()
    session_id = data.get("session_id", "")

    try:
        from orbit_db import TaskDB  # type: ignore[import-not-found]

        db = TaskDB()
        task = db.find_task_for_cwd(cwd, session_id)
        if not task or not task.full_path or not task.name:
            return

        # Orbit files live under ~/.orbit/<full_path>/, not under the
        # repo path. `task.full_path` already includes the "active/<name>"
        # segment. This matches settings.orbit_root in the MCP server
        # (mcp_orbit/config.py:15) and the helpers in mcp_orbit/helpers.py.
        orbit_root = Path.home() / ".orbit"
        orbit_dir = orbit_root / task.full_path

        # Two supported filename layouts:
        # - Top-level tasks: `{task.name}-tasks.md` / `{task.name}-context.md`
        # - Subtasks (nested under a parent task dir): `tasks.md` / `context.md`
        # Mirrors the candidate lists in mcp-server/src/mcp_orbit/helpers.py
        # and hooks/stop.py.
        tasks_file = next(
            (
                f
                for f in (
                    orbit_dir / f"{task.name}-tasks.md",
                    orbit_dir / "tasks.md",
                )
                if f.exists()
            ),
            None,
        )
        context_file = next(
            (
                f
                for f in (
                    orbit_dir / f"{task.name}-context.md",
                    orbit_dir / "context.md",
                )
                if f.exists()
            ),
            None,
        )

        if tasks_file is None or context_file is None:
            return

        tasks_content = tasks_file.read_text()
        context_content = context_file.read_text()

        pending = parse_pending_tasks(tasks_content)
        if not pending:
            return

        heading_nums = parse_context_headings(context_content)
        if not heading_nums:
            return

        divergent_nums = heading_nums & set(pending.keys())
        if not divergent_nums:
            return

        divergent_tasks = {num: pending[num] for num in divergent_nums}
        print(build_reminder(divergent_tasks, str(tasks_file)))

    except ImportError:
        # orbit_db not available, skip silently
        pass
    except Exception as e:
        # Don't fail the prompt submission
        print(f"<!-- orbit task_tracker: {e} -->", file=sys.stderr)


if __name__ == "__main__":
    main()

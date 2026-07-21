#!/usr/bin/env python3
"""
UserPromptSubmit hook - Detect MissionCache task tracking divergence.

Runs on every user prompt and checks two signals, in priority order:

1. Precise divergence: the context file has `### Task N` headings for tasks
   that are still unchecked in the tasks file.
2. Staleness: the context file's `**Last Updated:**` is newer than the tasks
   file's while unchecked items remain. Every context save path stamps that
   header (update_context_file, the PreCompact snapshot hook), so this catches
   real progress recorded in context - including hook auto-saves - without the
   corresponding checkbox flips.

If either fires, prints a reminder to stdout so Claude sees it at the moment
it's about to move on to the next task.

This exists because Claude instances tend to treat the context file as the
live progress ledger but forget to flip the corresponding checkbox in the
tasks file. The statusline progress display `[X/Y]` is parsed from the tasks
file's checkboxes, so the user watches it sit stale while real progress
happens - and Claude can't see its own statusline, so this hook injects the
same signal into Claude's context.
"""

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

# Bundled missioncache-db path for marketplace installs (no system pip install).
# Path segment tracks the in-repo package dir.
_BUNDLED_MISSIONCACHE_DB = Path(__file__).resolve().parent.parent / "missioncache-db"
if _BUNDLED_MISSIONCACHE_DB.is_dir() and str(_BUNDLED_MISSIONCACHE_DB) not in sys.path:
    sys.path.insert(0, str(_BUNDLED_MISSIONCACHE_DB))

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

# Tasks file pattern - capture "- [ ] N. description" with flat ("1.") or
# hierarchical ("1.2.") numbering, matching the MissionCache template format.
PENDING_RE = re.compile(
    r"^\s*-\s*\[\s*\]\s+(\d+(?:\.\d+)*)\.\s+(.+?)\s*$", re.MULTILINE
)

# Any unchecked checklist item, numbered or not - same flat count the
# statusline's progress parser uses for the [X/Y] denominator.
ANY_PENDING_RE = re.compile(r"^\s*[-*]\s*\[\s*\]", re.MULTILINE)

# Context file heading pattern - captures "### Task N" or "### Task N: description"
HEADING_RE = re.compile(
    r"^###\s+Task\s+(\d+(?:\.\d+)*)", re.MULTILINE | re.IGNORECASE
)

# The `**Last Updated:**` header every MissionCache file carries. Stamped by
# update_context_file / update_tasks_file (mcp-server project_files.py) and by
# the PreCompact snapshot hook, so it reflects the last write through any of
# the managed paths. First match wins - a snapshot body quoting the header
# sits below the real one.
_LAST_UPDATED_RE = re.compile(
    r"^\*\*Last Updated:\*\*\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})", re.MULTILINE
)

# Session id charset (UUID-shaped) - guards the dedup filename against path
# traversal. Similar to session_start's guard (`[A-Za-z0-9_-]`, capped) but
# not identical across files: mcp-server/helpers.py also allows `.` and caps
# at 128. Do not assume the three are interchangeable.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")

# Cap the pending-task listing in the staleness reminder so a large backlog
# does not flood the prompt context.
_STALE_LIST_CAP = 10


def should_skip(prompt: str) -> bool:
    """Return True if this prompt shouldn't trigger divergence checks."""
    trimmed = prompt.strip()
    return any(p.search(trimmed) for p in SKIP_PATTERNS)


def parse_pending_tasks(tasks_content: str) -> dict[str, str]:
    """Return {task_num: description} for numbered tasks still marked `[ ]`."""
    return dict(PENDING_RE.findall(tasks_content))


def parse_context_headings(context_content: str) -> set[str]:
    """Return set of task numbers that have `### Task N` headings."""
    return set(HEADING_RE.findall(context_content))


def _num_sort_key(num: str) -> tuple[int, ...]:
    """Numeric sort key for flat/hierarchical task numbers ("2" < "10")."""
    return tuple(int(p) for p in num.split("."))


def _freshness(content: str, path: Path) -> tuple[float, str] | None:
    """Return ``(epoch_seconds, display)`` freshness for a MissionCache file.

    Prefers the ``**Last Updated:**`` header (minute resolution, local time -
    what every managed write path stamps); falls back to file mtime when the
    header is missing or unparseable. None only when both are unavailable.
    """
    m = _LAST_UPDATED_RE.search(content)
    if m:
        try:
            dt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M")
            return dt.timestamp(), m.group(1)
        except ValueError:
            pass
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return mtime, datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")


def _mtime(path: Path) -> float:
    """File mtime, or 0.0 when unstattable."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _save_marker(context_file: Path, ctx_fresh) -> str | None:
    """Identity of the context file's current save, for dedup.

    mtime_ns tells apart saves that share a minute-resolution header stamp
    (two saves in the same minute must each get their reminder chance); the
    header display string is the fallback when stat fails.
    """
    try:
        return str(context_file.stat().st_mtime_ns)
    except OSError:
        return ctx_fresh[1] if ctx_fresh else None


def _divergence_state_file(session_id: str):
    """Path to the per-session dedup state file, or None if session_id unusable.

    Keyed by session_id so each reminder is surfaced once per divergence/
    staleness state instead of re-injected on every prompt. Computed from
    Path.home() at call time so a patched home (tests) is honored.
    """
    if not _SESSION_ID_RE.match(session_id or ""):
        return None
    return (
        Path.home()
        / ".claude"
        / "hooks"
        / "state"
        / f"divergence-{session_id}.json"
    )


def _load_state(state_file) -> dict:
    """Return the per-session dedup state dict.

    Tolerates the legacy shape (a bare JSON list of divergent task numbers,
    written before the staleness signal existed) by lifting it into the dict
    form. Any unreadable state reads as empty.
    """
    if state_file is None:
        return {}
    try:
        data = json.loads(state_file.read_text())
    except Exception:
        return {}
    if isinstance(data, list):
        return {"divergent": [str(n) for n in data]}
    if isinstance(data, dict):
        return data
    return {}


def _store_state(state_file, state: dict) -> None:
    """Persist the dedup state just emitted; best-effort."""
    if state_file is None:
        return
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(state))
    except Exception:
        pass


def _clear_divergence_state(state_file) -> None:
    """Drop the per-session dedup state so a later recurrence re-fires."""
    if state_file is None:
        return
    try:
        state_file.unlink(missing_ok=True)
    except OSError:
        pass


_TASKCREATE_NOTE = (
    "Important: the built-in TaskCreate tool and any system reminders "
    'about "task tools" refer to Claude Code\'s in-conversation todo '
    "list, NOT the MissionCache tasks file. Use "
    "`mcp__plugin_missioncache_pm__update_tasks_file` for MissionCache work."
)


def build_reminder(
    divergent_tasks: dict[str, str], tasks_file_path: str
) -> str:
    """Format the precise-divergence reminder for stdout injection."""
    lines = [
        "",
        "## ⚠️ MissionCache task tracking divergence",
        "",
        "The context file has findings recorded for tasks that are still "
        "unchecked in the tasks file:",
        "",
    ]
    for num in sorted(divergent_tasks, key=_num_sort_key):
        lines.append(f"- Task {num}: {divergent_tasks[num]}")
    lines += [
        "",
        "If any of these are actually complete, mark them NOW before continuing:",
        "",
        "  mcp__plugin_missioncache_pm__update_tasks_file(",
        f'    tasks_file="{tasks_file_path}",',
        '    completed_tasks=["task description", ...]',
        "  )",
        "",
        "Or run /missioncache:save to update both files in one step.",
        "",
        "If a task is still in progress, ignore this warning and continue - "
        "it will clear once the checkbox flips or the heading is removed from "
        "the context file.",
        "",
        _TASKCREATE_NOTE,
        "",
    ]
    return "\n".join(lines)


def build_stale_reminder(
    pending: dict[str, str],
    total_pending: int,
    ctx_display: str,
    tasks_display: str,
    tasks_file_path: str,
) -> str:
    """Format the tasks-file-staleness reminder for stdout injection."""
    lines = [
        "",
        "## ⚠️ MissionCache tasks file may be stale",
        "",
        f"The context file was saved at {ctx_display} but the tasks file has "
        f"not changed since {tasks_display}, and {total_pending} checklist "
        "item(s) are still unchecked. The statusline progress counter reads "
        "the tasks file, so it stays stale until the boxes flip.",
        "",
        "If any of these were completed, mark them NOW before continuing:",
        "",
    ]
    numbered = sorted(pending, key=_num_sort_key)
    for num in numbered[:_STALE_LIST_CAP]:
        lines.append(f"- Task {num}: {pending[num]}")
    unlisted = total_pending - len(numbered[:_STALE_LIST_CAP])
    if unlisted > 0:
        lines.append(f"- ... and {unlisted} more unchecked item(s)")
    lines += [
        "",
        "  mcp__plugin_missioncache_pm__update_tasks_file(",
        f'    tasks_file="{tasks_file_path}",',
        '    completed_tasks=["task description", ...]',
        "  )",
        "",
        "Or run /missioncache:save to update both files in one step.",
        "",
        "If nothing on the checklist is actually finished yet, ignore this "
        "and continue - it re-fires only after the next context save.",
        "",
        _TASKCREATE_NOTE,
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
        from missioncache_db import TaskDB  # type: ignore[import-not-found]

        db = TaskDB()
        task = db.find_task_for_cwd(cwd, session_id)
        if not task or not task.full_path or not task.name:
            return

        # MissionCache files live under ~/.missioncache/<full_path>/, not under the
        # repo path. `task.full_path` already includes the "active/<name>"
        # segment. This matches settings.root in the MCP server
        # (mcp_missioncache/config.py:15) and the helpers in mcp_missioncache/helpers.py.
        missioncache_root = Path.home() / ".missioncache"
        missioncache_dir = missioncache_root / task.full_path

        # Two supported filename layouts:
        # - Top-level tasks: `{task.name}-tasks.md` / `{task.name}-context.md`
        # - Subtasks (nested under a parent task dir): `tasks.md` / `context.md`
        # Mirrors the candidate lists in mcp-server/src/mcp_missioncache/helpers.py
        # and hooks/stop.py.
        tasks_file = next(
            (
                f
                for f in (
                    missioncache_dir / f"{task.name}-tasks.md",
                    missioncache_dir / "tasks.md",
                )
                if f.exists()
            ),
            None,
        )
        context_file = next(
            (
                f
                for f in (
                    missioncache_dir / f"{task.name}-context.md",
                    missioncache_dir / "context.md",
                )
                if f.exists()
            ),
            None,
        )

        if tasks_file is None or context_file is None:
            return

        state_file = _divergence_state_file(session_id)

        # Read tasks.md first and bail before touching the (larger) context.md
        # when nothing is pending - no signal can fire in that case. Clear
        # any stored dedup state on the way out so a later recurrence re-fires.
        tasks_content = tasks_file.read_text()
        pending = parse_pending_tasks(tasks_content)
        total_pending = len(ANY_PENDING_RE.findall(tasks_content))
        if total_pending == 0:
            _clear_divergence_state(state_file)
            return

        context_content = context_file.read_text()

        state = _load_state(state_file)
        new_state = dict(state)

        ctx_fresh = _freshness(context_content, context_file)
        tasks_fresh = _freshness(tasks_content, tasks_file)
        save_marker = _save_marker(context_file, ctx_fresh)

        # Signal 1 (precise, wins when present): `### Task N` headings in the
        # context for tasks still unchecked. Per-session dedup: the reminder
        # surfaces when the divergent set changes. Firing also stamps the
        # current save generation so the broader staleness signal below stays
        # quiet until the NEXT context save instead of re-nagging this one.
        divergent_nums = parse_context_headings(context_content) & set(pending)
        current = sorted(divergent_nums, key=_num_sort_key)
        if current and state.get("divergent") != current:
            divergent_tasks = {num: pending[num] for num in divergent_nums}
            print(build_reminder(divergent_tasks, str(tasks_file)))
            new_state["divergent"] = current
            if save_marker:
                new_state["stale_marker"] = save_marker
            _store_state(state_file, new_state)
            return
        if not current:
            new_state.pop("divergent", None)
        # An unchanged divergent set deliberately does NOT return here: a
        # later context save recording progress on OTHER (unheaded) tasks
        # must still be able to fire the staleness signal below.

        # Signal 2 (staleness): the context file was saved after the tasks
        # file last changed. Catches progress recorded via update_context_file
        # or the PreCompact snapshot hook without checkbox flips - the case
        # that leaves the statusline counter stale. Deduped per context save
        # (mtime_ns identity), so an intentional "nothing completed" save is
        # nagged at most once even when two saves share a header minute.
        # Header stamps are minute-resolution, so a save flow that wrote both
        # files lands equal; the mtime tie-break lets a context save AFTER a
        # same-minute tasks write still read as stale.
        if (
            ctx_fresh
            and tasks_fresh
            and (
                ctx_fresh[0] > tasks_fresh[0]
                or (
                    ctx_fresh[0] == tasks_fresh[0]
                    and _mtime(context_file) > _mtime(tasks_file)
                )
            )
        ):
            if save_marker and state.get("stale_marker") != save_marker:
                print(
                    build_stale_reminder(
                        pending,
                        total_pending,
                        ctx_fresh[1],
                        tasks_fresh[1],
                        str(tasks_file),
                    )
                )
                new_state["stale_marker"] = save_marker
        else:
            new_state.pop("stale_marker", None)

        if new_state != state:
            _store_state(state_file, new_state)

    except ImportError:
        # missioncache_db not available, skip silently
        pass
    except Exception as e:
        # Don't fail the prompt submission
        print(f"<!-- missioncache task_tracker: {e} -->", file=sys.stderr)


if __name__ == "__main__":
    main()

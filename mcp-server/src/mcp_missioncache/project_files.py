"""MissionCache file operations."""

import contextlib
import fcntl
import os
import re
from collections.abc import Callable, Iterator
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Any

from missioncache_db import context_health
from missioncache_db import validate_task_name as _missioncache_db_validate_task_name

from .config import settings
from .errors import ErrorCode, MissionCacheError, MissionCacheFileNotFoundError, ValidationError
from .models import MissionCacheFiles, TaskProgress
from .tasks_parse import parse_tasks_md


# NOTE: ``_file_lock`` and ``_atomic_update_text`` below are duplicated in
# ``hooks/pre_compact.py`` to keep the PreCompact hook self-contained
# (avoids dragging mcp_missioncache's transitive imports into the hook hot path).
# If you change locking semantics here, mirror the change in the hook.


@contextlib.contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive lock on a sidecar lockfile next to ``path``.

    The lockfile (``<path>.lock``) is a long-lived sidecar; we never delete
    it because creation/deletion under contention is racy.
    """
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lockfd:
        fcntl.flock(lockfd.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lockfd.fileno(), fcntl.LOCK_UN)


def _atomic_update_text(path: Path, transform: Callable[[str], str]) -> str:
    """Atomically update a text file under exclusive lock.

    Acquires a flock on a sidecar lockfile, reads current content, applies the
    transform, writes the result to ``<path>.tmp``, and atomically replaces
    the target via ``os.replace``. A crash mid-write leaves the original file
    intact; concurrent callers serialize on the lockfile so their
    read-modify-write cycles do not interleave.
    """
    with _file_lock(path):
        content = path.read_text()
        new_content = transform(content)
        tmp_path = path.with_name(path.name + ".tmp")
        tmp_path.write_text(new_content)
        os.replace(tmp_path, path)
        return new_content


def _atomic_update_context_with_journal(
    context_path: Path,
    journal_path: Path,
    transform: Callable[[str], tuple[str, str | None]],
) -> str:
    """Like ``_atomic_update_text`` but the transform may emit journal text.

    The transform returns ``(new_content, journal_append_or_None)``. Both
    writes happen under the CONTEXT file's sidecar lock - the journal is only
    ever written while that lock is held, so it needs no lock of its own.
    Write order is journal first, context second: a crash between the two
    replaces duplicates the rolled-over entries into the journal (they are
    still in the context, so the next rollover re-moves them) rather than
    losing them. The window is one ``os.replace`` wide.
    """
    with _file_lock(context_path):
        content = context_path.read_text()
        new_content, journal_append = transform(content)
        if journal_append:
            if journal_path.exists():
                journal_content = journal_path.read_text().rstrip("\n") + "\n\n"
            else:
                journal_content = (
                    context_health.journal_header(context_path.parent.name) + "\n"
                )
            journal_content += journal_append
            journal_tmp = journal_path.with_name(journal_path.name + ".tmp")
            journal_tmp.write_text(journal_content)
            os.replace(journal_tmp, journal_path)
        tmp_path = context_path.with_name(context_path.name + ".tmp")
        tmp_path.write_text(new_content)
        os.replace(tmp_path, context_path)
        return new_content


def validate_task_name(name: str) -> None:
    """Validate task name is safe for filesystem and git branch use.

    Delegates to ``missioncache_db.validate_task_name`` (the single source of
    truth for the regex and per-branch error messages) and re-raises
    its ``ValueError`` as the structured ``ValidationError`` that mcp
    callers and tests already expect. Keeping the wrap thin here means
    a future tightening of the rule lands in one place (missioncache-db) and
    propagates to every surface.
    """
    try:
        _missioncache_db_validate_task_name(name)
    except ValueError as e:
        raise ValidationError(str(e), field="task_name") from e


def get_timestamp() -> str:
    """Get current local timestamp."""
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def format_tasks_markdown(tasks: list) -> tuple[str, int]:
    """Format tasks list into numbered markdown.

    Supports two formats:
    1. Flat list: ["task1", "task2"] -> numbered tasks
    2. Hierarchical: [{"title": "Parent", "subtasks": ["sub1", "sub2"]}, ...] -> parent.child numbering

    Args:
        tasks: List of task strings or dicts with title/subtasks

    Returns:
        Tuple of (markdown string, total task count)
    """
    if not tasks:
        return "- [ ] TBD", 0

    lines = []
    total_count = 0

    for i, task in enumerate(tasks, start=1):
        if isinstance(task, dict):
            # Hierarchical: {"title": "Parent task", "subtasks": ["sub1", "sub2"]}
            title = task.get("title", "")
            subtasks = task.get("subtasks", [])

            if subtasks:
                # Parent task with subtasks
                lines.append(f"- [ ] {i}. {title}")
                for j, subtask in enumerate(subtasks, start=1):
                    lines.append(f"  - [ ] {i}.{j}. {subtask}")
                    total_count += 1
            else:
                # Just a parent without subtasks (treat as flat)
                lines.append(f"- [ ] {i}. {title}")
                total_count += 1
        else:
            # Flat: just a string
            lines.append(f"- [ ] {i}. {task}")
            total_count += 1

    return "\n".join(lines), total_count


def get_task_dir(task_name: str, active: bool = True) -> Path:
    """Get the task directory path under MISSIONCACHE_ROOT."""
    subdir = settings.active_dir_name if active else settings.completed_dir_name
    return settings.root / subdir / task_name


def get_missioncache_files(task_name: str, full_path: str | None = None) -> MissionCacheFiles:
    """Get paths to all MissionCache files for a task.

    When ``full_path`` is given (e.g. ``active/parent/subtask`` for nested
    subtasks), it is authoritative. Otherwise, search the active directory
    first, then the completed directory. This lets ``/missioncache:load`` and the
    /missioncache:save flow find archived projects without prompting the user to
    "create files" - which would otherwise overwrite the archived content.
    """
    if full_path:
        candidate_dirs = [settings.root / full_path]
    else:
        candidate_dirs = [
            get_task_dir(task_name, active=True),
            get_task_dir(task_name, active=False),
        ]

    def find_file(candidates: list[Path]) -> str | None:
        for c in candidates:
            if c.exists():
                return str(c)
        return None

    chosen_dir = candidate_dirs[0]
    plan_file = context_file = tasks_file = None
    for task_dir in candidate_dirs:
        p = find_file([task_dir / f"{task_name}-plan.md", task_dir / "plan.md"])
        c = find_file(
            [task_dir / f"{task_name}-context.md", task_dir / "context.md"]
        )
        t = find_file([task_dir / f"{task_name}-tasks.md", task_dir / "tasks.md"])
        if p or c or t:
            chosen_dir = task_dir
            plan_file, context_file, tasks_file = p, c, t
            break

    prompts_dir = chosen_dir / "prompts"

    return MissionCacheFiles(
        task_dir=str(chosen_dir),
        plan_file=plan_file,
        context_file=context_file,
        tasks_file=tasks_file,
        prompts_dir=str(prompts_dir) if prompts_dir.exists() else None,
    )


def create_missioncache_files(
    task_name: str,
    description: str = "TBD",
    jira_key: str | None = None,
    branch: str | None = None,
    tasks: list[str] | None = None,
    plan_content: dict[str, str] | None = None,
    force: bool = False,
) -> MissionCacheFiles:
    """Create MissionCache files for a task under MISSIONCACHE_ROOT.

    Args:
        task_name: Task name (kebab-case)
        description: Short description for context.md
        jira_key: Optional JIRA ticket
        branch: Optional git branch
        tasks: List of task descriptions for tasks.md
        plan_content: Optional dict with plan sections (summary, goals, etc.)
        force: If True, overwrite existing files. If False (default), raise
            MissionCacheError(ALREADY_EXISTS) when any of plan/context/tasks already
            exist on disk for this task. Prevents silent data loss when the
            same name is reused.

    Returns:
        MissionCacheFiles with paths to created files
    """
    validate_task_name(task_name)
    task_dir = get_task_dir(task_name)

    if not force:
        # Include both prefixed AND legacy unprefixed filenames - get_missioncache_files
        # accepts both, so the guard must too. Otherwise creating a task whose
        # dir already has only legacy files would write fresh prefixed files,
        # and the legacy content would be hidden by the read-time precedence.
        existing = [
            p
            for p in (
                task_dir / f"{task_name}-plan.md",
                task_dir / f"{task_name}-context.md",
                task_dir / f"{task_name}-tasks.md",
                task_dir / "plan.md",
                task_dir / "context.md",
                task_dir / "tasks.md",
            )
            if p.exists()
        ]
        if existing:
            raise MissionCacheError(
                ErrorCode.ALREADY_EXISTS,
                f"MissionCache files for '{task_name}' already exist. "
                f"Pass force=True to overwrite, or pick a different name.",
                {
                    "task_name": task_name,
                    "task_dir": str(task_dir),
                    "existing_files": [str(p) for p in existing],
                },
            )

    task_dir.mkdir(parents=True, exist_ok=True)

    timestamp = get_timestamp()
    templates = resources.files("mcp_missioncache.templates")

    # Create context.md
    context_template = templates.joinpath("context.md").read_text()
    context_content = context_template.replace(
        "{{task_name}}", task_name.replace("-", " ").title()
    )
    context_content = context_content.replace("{{timestamp}}", timestamp)
    context_content = context_content.replace("{{description}}", description)

    context_file = task_dir / f"{task_name}-context.md"
    context_file.write_text(context_content)

    # Create tasks.md
    tasks_template = templates.joinpath("tasks.md").read_text()
    tasks_content = tasks_template.replace(
        "{{task_name}}", task_name.replace("-", " ").title()
    )
    tasks_content = tasks_content.replace("{{timestamp}}", timestamp)

    if tasks:
        tasks_md, total_count = format_tasks_markdown(tasks)
        tasks_content = tasks_content.replace("{{tasks}}", tasks_md)
        remaining = f"{total_count} tasks pending"
    else:
        tasks_content = tasks_content.replace("{{tasks}}", "- [ ] TBD")
        remaining = "TBD"

    tasks_content = tasks_content.replace("{{remaining}}", remaining)

    tasks_file = task_dir / f"{task_name}-tasks.md"
    tasks_file.write_text(tasks_content)

    # Create plan.md
    plan_template = templates.joinpath("plan.md").read_text()
    plan_content = plan_content or {}

    plan_md = plan_template.replace(
        "{{task_name}}", task_name.replace("-", " ").title()
    )
    plan_md = plan_md.replace("{{timestamp}}", timestamp)
    plan_md = plan_md.replace("{{jira_key}}", jira_key or "")
    plan_md = plan_md.replace("{{branch}}", branch or f"feature/{task_name}")
    plan_md = plan_md.replace("{{summary}}", plan_content.get("summary", "TBD"))
    plan_md = plan_md.replace(
        "{{research_findings}}",
        plan_content.get("research_findings", "N/A - research phase skipped"),
    )
    plan_md = plan_md.replace("{{goals}}", plan_content.get("goals", "TBD"))
    plan_md = plan_md.replace(
        "{{success_criteria}}", plan_content.get("success_criteria", "TBD")
    )
    plan_md = plan_md.replace("{{approach}}", plan_content.get("approach", "TBD"))
    plan_md = plan_md.replace("{{files}}", plan_content.get("files", "TBD"))
    plan_md = plan_md.replace(
        "{{dependencies}}", plan_content.get("dependencies", "None")
    )
    plan_md = plan_md.replace("{{risks}}", plan_content.get("risks", "None"))

    plan_file = task_dir / f"{task_name}-plan.md"
    plan_file.write_text(plan_md)

    return MissionCacheFiles(
        task_dir=str(task_dir),
        plan_file=str(plan_file),
        context_file=str(context_file),
        tasks_file=str(tasks_file),
        prompts_dir=None,
    )


def _apply_waiting_on(
    content: str,
    timestamp: str,
    waiting_on_add: list[dict[str, str]] | None,
    waiting_on_resolve: list[dict[str, str]] | None,
) -> tuple[str, list[str], list[str]]:
    """Apply waiting-on resolves then adds; pure function.

    Returns ``(content, resolved_changes, unmatched)``. ``resolved_changes``
    are the "Resolved (was waiting on ...)" bullets destined for today's
    Recent Changes subsection; ``unmatched`` are resolve ``match`` values
    that hit no row (surfaced to the caller, never dropped).
    """
    resolved_changes: list[str] = []
    unmatched: list[str] = []

    if waiting_on_resolve:
        rows = context_health.parse_waiting_on(content)
        remaining = list(rows)
        for item in waiting_on_resolve:
            match_text = (item.get("match") or "").strip()
            outcome = (item.get("outcome") or "").strip()
            found = next(
                (r for r in remaining if match_text and match_text in r["what"]),
                None,
            )
            if found is None:
                unmatched.append(match_text)
                continue
            remaining.remove(found)
            note = f"Resolved (was waiting on {found['who']}): {found['what']}"
            if outcome:
                note += f" - {outcome}"
            resolved_changes.append(note)
        if len(remaining) != len(rows):
            content = context_health.replace_waiting_on_table(content, remaining)

    if waiting_on_add:
        # Self-heal the section if missing so the convention works on
        # not-yet-migrated files.
        if context_health.extract_section(content, "Waiting on") is None:
            content = context_health.insert_waiting_on_before_next_steps(
                content, context_health.build_waiting_on_section([])
            )
        rows = context_health.parse_waiting_on(content)
        today = timestamp.split(" ")[0]
        for row in waiting_on_add:
            rows.append(
                {
                    "what": (row.get("what") or "").strip(),
                    "who": (row.get("who") or "").strip(),
                    "since": (row.get("since") or today).strip(),
                    "gates": (row.get("gates") or "").strip(),
                }
            )
        content = context_health.replace_waiting_on_table(content, rows)

    return content, resolved_changes, unmatched


def update_context_file(
    context_file: str | Path,
    next_steps: list[str] | None = None,
    recent_changes: list[str] | None = None,
    key_decisions: list[str] | None = None,
    gotchas: list[str] | None = None,
    key_files: dict[str, str] | None = None,
    waiting_on_add: list[dict[str, str]] | None = None,
    waiting_on_resolve: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Update sections in a context.md file atomically.

    Args:
        context_file: Path to context.md
        next_steps: List of next steps to add/replace
        recent_changes: List of recent changes to add
        key_decisions: List of decisions to add
        gotchas: List of gotchas to add
        key_files: Dict of file paths to descriptions
        waiting_on_add: Waiting-on rows to append, each
            ``{"what", "who", "since", "gates"}`` (``since`` defaults to
            today). Creates the section before Next Steps if missing.
        waiting_on_resolve: Rows to resolve, each ``{"match", "outcome"}``.
            Removes the first row whose What cell contains ``match`` and
            records the resolution in today's Recent Changes subsection.

    Returns:
        Dict with ``content`` (updated file text), ``waiting_on_unmatched``
        (``match`` values that resolved to no row - never silently dropped,
        mirroring ``update_tasks_file``'s ``unmatched`` contract), and
        ``journal_rolled_over`` (Recent Changes subsections moved to the
        per-project journal by the cap).
    """
    path = Path(context_file)
    if not path.exists():
        raise MissionCacheFileNotFoundError(str(path))

    journal_path = context_health.derive_journal_path(path)
    waiting_on_unmatched: list[str] = []
    rolled_over = 0

    def _transform(content: str) -> tuple[str, str | None]:
        nonlocal rolled_over
        # Stamp inside the lock so serialized writers each get a fresh
        # timestamp instead of all sharing the function-entry value.
        timestamp = get_timestamp()

        # Update Last Updated timestamp
        content = re.sub(
            r"\*\*Last Updated:\*\* .+",
            f"**Last Updated:** {timestamp}",
            content,
        )

        # Update Next Steps section. (Replacement stops at the next `## `
        # heading, so a Waiting on section placed before Next Steps is
        # untouched by this.)
        if next_steps:
            next_steps_md = "\n".join(
                f"{i + 1}. {step}" for i, step in enumerate(next_steps)
            )
            content = _update_section(content, "Next Steps", next_steps_md)

        # Waiting-on maintenance (resolves BEFORE Recent Changes so the
        # resolutions land in today's subsection alongside recent_changes).
        content, resolved_changes, unmatched = _apply_waiting_on(
            content, timestamp, waiting_on_add, waiting_on_resolve
        )
        waiting_on_unmatched.extend(unmatched)

        # Update Recent Changes - one `## Recent Changes` heading with dated
        # `###` sub-sections prepended newest-first. The prepend shape lives
        # in context_health.prepend_recent_changes (shared with the
        # pre-compact hook; fence-aware and ^-anchored).
        combined_changes = resolved_changes + list(recent_changes or [])
        if combined_changes:
            changes_md = "\n".join(f"- {change}" for change in combined_changes)
            content = context_health.prepend_recent_changes(
                content, timestamp, changes_md
            )

        # Enforce the Recent Changes cap AFTER the prepend, in the same
        # transform under the same lock. Overflow (the oldest subsections)
        # becomes journal text written by the atomic helper.
        content, journal_append, moved = context_health.split_recent_changes_for_cap(
            content, journal_path.name
        )
        rolled_over = moved

        # Update Key Decisions section
        if key_decisions:
            decisions_md = "\n".join(f"- {d}" for d in key_decisions)
            content = _append_to_section(
                content, "Key Architectural Decisions", decisions_md
            )

        # Update Gotchas section
        if gotchas:
            gotchas_md = "\n".join(f"- {g}" for g in gotchas)
            content = _append_to_section(content, "Gotchas", gotchas_md)

        # Update Key Files section
        if key_files:
            files_md = "\n".join(
                f"| `{filename}` | {desc} |"
                for filename, desc in key_files.items()
            )
            content = _append_to_section(content, "Key Files", files_md)

        return content, journal_append

    new_content = _atomic_update_context_with_journal(path, journal_path, _transform)
    return {
        "content": new_content,
        "waiting_on_unmatched": waiting_on_unmatched,
        "journal_rolled_over": rolled_over,
    }


# Pull a checklist number ("7", "54a", "0.1") off the front of a
# completed-task entry, tolerating a leading list marker / checkbox. The
# number must be terminated by a "." or end-of-string so a description that
# merely starts with a digit ("3 tests added") is not mistaken for a number.
_LEADING_NUM_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\[[ xX]?\]\s*)?([0-9]+(?:\.[0-9]+)*[a-z]?)\s*(?:\.|$)"
)


def _leading_task_number(entry: str) -> str | None:
    """Return the checklist number at the front of ``entry``, or None."""
    m = _LEADING_NUM_RE.match(entry)
    return m.group(1) if m else None


def _mark_task_checked_by_number(content: str, number: str) -> str:
    """Flip the unchecked checklist line for ``number`` to ``[x]``.

    Idempotent: an already-checked ``[x]`` line is left untouched, so
    re-completing a done item is a safe no-op (and stays out of the
    pre/post transition diff).
    """
    pattern = re.compile(
        rf"^(\s*[-*]\s*)\[\s*\](\s*{re.escape(number)}\.)",
        re.MULTILINE,
    )
    return pattern.sub(r"\1[x]\2", content)


def update_tasks_file(
    tasks_file: str | Path,
    completed_tasks: list[str] | None = None,
    new_tasks: list[str] | None = None,
    remaining_summary: str | None = None,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    """Update a tasks.md file.

    Args:
        tasks_file: Path to tasks.md
        completed_tasks: Task identifiers to mark as [x]. Each entry may
            lead with the checklist number ("7", "54a", "0.1", optionally
            with trailing prose) which is matched by number; otherwise it
            falls back to a literal substring match against a task line.
        new_tasks: List of new tasks to add
        remaining_summary: New summary for Remaining field
        notes: Notes to add

    Returns:
        Dict with update summary including ``completed_numbers``: the
        checklist numbers (e.g. ``["54a", "56"]``) of items that were
        unchecked before this call and are now checked. Used by callers
        to drive cross-cutting cleanup like clearing active-task pointers.
        Also includes ``unmatched``: ``completed_tasks`` entries that
        resolved to no checklist item, so callers can surface a dropped
        completion instead of leaving a box silently unticked.
    """
    path = Path(tasks_file)
    if not path.exists():
        raise MissionCacheFileNotFoundError(str(path))

    updates_made: list[str] = []
    completed_numbers_seen: list[str] = []
    unmatched: list[str] = []

    def _transform(content: str) -> str:
        # Stamp inside the lock so serialized writers each get a fresh
        # timestamp instead of all sharing the function-entry value.
        timestamp = get_timestamp()

        # Snapshot pre-transform unchecked items so we can diff after
        # marking completions and report the actual numbers transitioned.
        pre_unchecked = {
            item.number for item in parse_tasks_md(content) if not item.checked
        }

        # Update Last Updated timestamp
        content = re.sub(
            r"\*\*Last Updated:\*\* .+",
            f"**Last Updated:** {timestamp}",
            content,
        )

        # Mark tasks as completed. Prefer the stable checklist NUMBER
        # ("7", "54a", "0.1") parsed from the front of each entry, so a
        # completion lands even when the caller's string carries extra
        # prose ("7. Foo - DONE: shipped in PR #312"). Fall back to a
        # literal substring match only when no leading number resolves to
        # a real item. Entries that match nothing go to ``unmatched`` and
        # are returned, so a dropped completion is never silent.
        if completed_tasks:
            present_numbers = {item.number for item in parse_tasks_md(content)}
            for task_desc in completed_tasks:
                number = _leading_task_number(task_desc)
                if number and number in present_numbers:
                    content = _mark_task_checked_by_number(content, number)
                    updates_made.append(f"Completed: {task_desc[:50]}...")
                    continue
                # Fallback: literal substring of an unchecked line.
                escaped = re.escape(task_desc)
                pattern = rf"- \[\s*\]([^\n]*{escaped}[^\n]*)"
                if re.search(pattern, content, re.IGNORECASE):
                    content = re.sub(
                        pattern, r"- [x]\1", content, flags=re.IGNORECASE
                    )
                    updates_made.append(f"Completed: {task_desc[:50]}...")
                else:
                    unmatched.append(task_desc)

        # Diff post-transform: any number that was [ ] before and is [x]
        # now is a real transition. This catches edits regardless of how
        # the caller phrased ``completed_tasks`` (description, fragment,
        # etc.) and ignores items that were already checked beforehand.
        post_checked = {
            item.number for item in parse_tasks_md(content) if item.checked
        }
        completed_numbers_seen.extend(sorted(pre_unchecked & post_checked))

        # Add new tasks (before Phase 2/Validation section)
        if new_tasks:
            # Find the highest existing task number to continue numbering
            existing_numbers = re.findall(
                r"^\s*[-*]\s*\[[x\s]\]\s*(\d+)\.", content, re.MULTILINE
            )
            next_num = max([int(n) for n in existing_numbers], default=0) + 1

            new_tasks_lines = []
            for i, task in enumerate(new_tasks):
                new_tasks_lines.append(f"- [ ] {next_num + i}. {task}")
            new_tasks_md = "\n".join(new_tasks_lines)

            # Find a good insertion point (before Phase 2 or Validation)
            insertion_patterns = [
                r"(## Phase 2)",
                r"(## Validation)",
                r"(## Notes)",
            ]
            inserted = False
            for pattern in insertion_patterns:
                if re.search(pattern, content):
                    content = re.sub(pattern, f"{new_tasks_md}\n\n\\1", content)
                    inserted = True
                    break

            if not inserted:
                content += f"\n{new_tasks_md}\n"

            updates_made.append(f"Added {len(new_tasks)} new tasks")

        # Update Remaining summary
        if remaining_summary:
            content = re.sub(
                r"\*\*Remaining:\*\* .+",
                f"**Remaining:** {remaining_summary}",
                content,
            )
            updates_made.append(f"Updated remaining: {remaining_summary}")

        # Add notes
        if notes:
            notes_md = "\n".join(f"- {n}" for n in notes)
            content = _append_to_section(content, "Notes", notes_md)
            updates_made.append(f"Added {len(notes)} notes")

        return content

    new_content = _atomic_update_text(path, _transform)

    # Calculate progress from the just-written content
    progress = parse_task_progress(new_content)

    return {
        "file": str(path),
        "updates_made": updates_made,
        "progress": progress.model_dump() if progress else None,
        "completed_numbers": completed_numbers_seen,
        "unmatched": unmatched,
    }


def parse_task_progress(content: str) -> TaskProgress:
    """Parse progress from tasks.md content."""
    # Match markdown checklist items: - [ ] or - [x]
    completed_pattern = r"^\s*[-*]\s*\[x\]"
    pending_pattern = r"^\s*[-*]\s*\[\s*\]"

    completed = len(
        re.findall(completed_pattern, content, re.MULTILINE | re.IGNORECASE)
    )
    pending = len(re.findall(pending_pattern, content, re.MULTILINE))

    total = completed + pending
    pct = int((completed / total * 100) if total > 0 else 0)

    # Extract remaining items as summary (first few pending items)
    remaining_items = re.findall(r"^\s*[-*]\s*\[\s*\]\s*(.+)$", content, re.MULTILINE)
    remaining_summary = None
    if remaining_items:
        # Take first 2-3 items as summary
        summary_items = remaining_items[:3]
        remaining_summary = "; ".join(item.strip() for item in summary_items)
        if len(remaining_items) > 3:
            remaining_summary += f" (+{len(remaining_items) - 3} more)"

    return TaskProgress(
        completion_pct=pct,
        total_items=total,
        completed_items=completed,
        remaining_summary=remaining_summary,
    )


def _update_section(content: str, section_name: str, new_content: str) -> str:
    """Replace a whole section's body, fence-aware.

    Delegates to the context-file structure owner (``context_health``) so
    heading location uses the same fence-masking as Recent Changes and
    Waiting on: a column-0 ``## <name>`` inside a fenced code block can
    never be mistaken for the section. This closes, for the sibling
    sections (Next Steps etc.), the same bug class the 2026-07-11 anchored
    fix closed for Recent Changes.
    """
    return context_health.replace_section_body(content, section_name, new_content)


def _append_to_section(content: str, section_name: str, new_content: str) -> str:
    """Append content to a section's body, fence-aware.

    Strips template placeholders (lines that are exactly ``- TBD`` or
    ``1. TBD``) so the first real write replaces the template rather than
    sitting alongside it. Fence-aware for the same reason as
    ``_update_section``.
    """
    return context_health.append_to_section_body(
        content, section_name, new_content, drop_lines=("- TBD", "1. TBD")
    )

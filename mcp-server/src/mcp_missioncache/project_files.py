"""MissionCache file operations."""

import contextlib
import os
import re
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Any

from missioncache_db import context_health
from missioncache_db import filelock
from missioncache_db import replace_with_retry
from missioncache_db import validate_task_name as _missioncache_db_validate_task_name

from .config import settings
from .errors import (
    ErrorCode,
    InvalidStateError,
    MissionCacheError,
    MissionCacheFileNotFoundError,
    ValidationError,
)
from .models import MissionCacheFiles, TaskProgress
from .tasks_parse import parse_tasks_md


# NOTE: the portable lock lives in ``missioncache_db.filelock`` (fcntl on
# POSIX, msvcrt on Windows); ``_atomic_update_text`` is still mirrored in
# ``hooks/pre_compact.py`` to keep the PreCompact hook self-contained.
# If you change locking semantics, change ``filelock.py`` and the hook
# mirror together.


def _file_lock(path: Path) -> "AbstractContextManager[None]":
    """Exclusive lock on the ``<path>.lock`` sidecar (never deleted)."""
    return filelock.sidecar_lock(path)


# The lock-taking wrappers below each pair with an ``_unlocked_*`` core.
# Every single-file caller uses the wrapper. ``move_to_project`` is the one
# operation that spans two projects: it takes all the locks itself, in a
# fixed order, and then calls the cores. It MUST NOT call the wrappers -
# POSIX ``flock`` blocks a second acquisition of the same lockfile from the
# same process, so a wrapper called under an outer lock deadlocks against
# itself with no timeout and no error.


def _unlocked_write_text(path: Path, new_content: str) -> None:
    """tmp-write + atomic replace. Caller must already hold ``path``'s lock."""
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(new_content, encoding="utf-8")
    replace_with_retry(tmp_path, path)


def _unlocked_append_journal(
    context_path: Path, journal_path: Path, journal_append: str
) -> None:
    """Append rolled-over entries to the journal. Caller holds the context lock."""
    if journal_path.exists():
        journal_content = journal_path.read_text(encoding="utf-8").rstrip("\n") + "\n\n"
    else:
        journal_content = context_health.journal_header(context_path.parent.name) + "\n"
    journal_content += journal_append
    journal_tmp = journal_path.with_name(journal_path.name + ".tmp")
    journal_tmp.write_text(journal_content, encoding="utf-8")
    replace_with_retry(journal_tmp, journal_path)


@contextlib.contextmanager
def _structured_ambiguity_error(path: Path) -> "Iterator[None]":
    """Translate context_health's AmbiguousSectionError into a coded error.

    ``AmbiguousSectionError`` subclasses ``ValueError`` and lives in
    missioncache-db, which cannot import ``MissionCacheError`` from up here
    (mcp-server imports missioncache_db, never the reverse). Left alone it
    escapes as a plain ValueError, so the MCP layer logs a stack trace and
    returns ``{"error": True, "message": ...}`` with no ``code`` and no
    ``field`` - nothing ``commands/save.md`` can branch on, even though the
    remedy is a specific command.
    """
    try:
        yield
    except context_health.AmbiguousSectionError as e:
        raise InvalidStateError(
            f"{path.name}: {e}",
            current_state="duplicate sections",
            expected_state="one section per name",
        ) from e


def _atomic_update_text(path: Path, transform: Callable[[str], str]) -> str:
    """Atomically update a text file under exclusive lock.

    Acquires a flock on a sidecar lockfile, reads current content, applies the
    transform, writes the result to ``<path>.tmp``, and atomically replaces
    the target via ``os.replace``. A crash mid-write leaves the original file
    intact; concurrent callers serialize on the lockfile so their
    read-modify-write cycles do not interleave.
    """
    with _file_lock(path):
        content = path.read_text(encoding="utf-8")
        new_content = transform(content)
        _unlocked_write_text(path, new_content)
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
        content = context_path.read_text(encoding="utf-8")
        new_content, journal_append = transform(content)
        if journal_append:
            _unlocked_append_journal(context_path, journal_path, journal_append)
        _unlocked_write_text(context_path, new_content)
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


def _inject_fork_header(content: str, parent: str) -> str:
    """Insert a ``**Fork of:** <parent>`` line into the context header region,
    right after the ``**Last Updated:**`` line (falling back to right after
    the H1 for custom templates), so the digest and scan reconcile find it."""
    lines = content.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.startswith("**Last Updated:**"):
            lines.insert(i + 1, f"**Fork of:** {parent}\n")
            return "".join(lines)
    lines.insert(1, f"\n**Fork of:** {parent}\n")
    return "".join(lines)


def create_missioncache_files(
    task_name: str,
    description: str = "TBD",
    jira_key: str | None = None,
    branch: str | None = None,
    tasks: list[str] | None = None,
    plan_content: dict[str, object] | None = None,
    force: bool = False,
    fork_of: str | None = None,
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
        fork_of: Optional parent project name. Writes a ``**Fork of:**``
            header line into the new context file, which the DB scan
            reconciles into the parent link. The parent's context file is the
            fork's shared knowledge layer.

    Returns:
        MissionCacheFiles with paths to created files
    """
    validate_task_name(task_name)
    if fork_of is not None:
        validate_task_name(fork_of)
        if fork_of == task_name:
            raise MissionCacheError(
                ErrorCode.VALIDATION_ERROR,
                "A project cannot fork itself.",
                {"task_name": task_name},
            )
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
    context_template = templates.joinpath("context.md").read_text(encoding="utf-8")
    context_content = context_template.replace(
        "{{task_name}}", task_name.replace("-", " ").title()
    )
    context_content = context_content.replace("{{timestamp}}", timestamp)
    context_content = context_content.replace("{{description}}", description)
    if fork_of is not None:
        context_content = _inject_fork_header(context_content, fork_of)

    context_file = task_dir / f"{task_name}-context.md"
    context_file.write_text(context_content, encoding="utf-8")

    # Create tasks.md
    tasks_template = templates.joinpath("tasks.md").read_text(encoding="utf-8")
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
    tasks_file.write_text(tasks_content, encoding="utf-8")

    # Create plan.md
    plan_template = templates.joinpath("plan.md").read_text(encoding="utf-8")
    plan_content = plan_content or {}

    def _section(key: str, default: str) -> str:
        # Agents routinely pass lists (goals, risks) or nested dicts despite
        # the declared dict[str, str] - render them as markdown instead of
        # crashing str.replace with a non-string.
        value = plan_content.get(key)
        if value is None or value == "":
            return default
        if isinstance(value, str):
            return value
        if isinstance(value, (list, tuple)):
            return "\n".join(f"- {item}" for item in value)
        if isinstance(value, dict):
            return "\n".join(f"- **{k}**: {v}" for k, v in value.items())
        return str(value)

    plan_md = plan_template.replace(
        "{{task_name}}", task_name.replace("-", " ").title()
    )
    plan_md = plan_md.replace("{{timestamp}}", timestamp)
    plan_md = plan_md.replace("{{jira_key}}", jira_key or "")
    plan_md = plan_md.replace("{{branch}}", branch or f"feature/{task_name}")
    plan_md = plan_md.replace("{{summary}}", _section("summary", "TBD"))
    plan_md = plan_md.replace(
        "{{research_findings}}",
        _section("research_findings", "N/A - research phase skipped"),
    )
    plan_md = plan_md.replace("{{goals}}", _section("goals", "TBD"))
    plan_md = plan_md.replace(
        "{{success_criteria}}", _section("success_criteria", "TBD")
    )
    plan_md = plan_md.replace("{{approach}}", _section("approach", "TBD"))
    plan_md = plan_md.replace("{{files}}", _section("files", "TBD"))
    plan_md = plan_md.replace("{{dependencies}}", _section("dependencies", "None"))
    plan_md = plan_md.replace("{{risks}}", _section("risks", "None"))

    plan_file = task_dir / f"{task_name}-plan.md"
    plan_file.write_text(plan_md, encoding="utf-8")

    return MissionCacheFiles(
        task_dir=str(task_dir),
        plan_file=str(plan_file),
        context_file=str(context_file),
        tasks_file=str(tasks_file),
        prompts_dir=None,
    )


# Recent Changes wording per waiting_on_resolve `kind`. The keys are the
# validated enum; `resolved` is the default and the historical behavior.
WAITING_ON_KINDS = {
    "resolved": "Resolved",
    "moved": "Moved out",
    "dropped": "Dropped",
}

# Sections update_context_file refuses to delete. The core five are what
# every reader (digest, health check, resume) depends on; the DB-managed
# mirrors are rendered from SQLite by the PM layer, so removing the markdown
# would only have it reappear on the next sync.
PROTECTED_SECTIONS = frozenset(
    context_health.CORE_SECTIONS
    + ["Action Items", "Stakeholders", "Tickets"]
)

def _reject_multiline(value: str, field: str) -> None:
    """Refuse a section name or match string that spans lines.

    Every guard here is exact set-membership on the stripped name, while the
    consumer builds its pattern with ``re.escape(name)`` under ``re.MULTILINE``
    - and ``re.escape`` renders a newline as a literal newline match, so a
    "name" carrying the file's own text across several lines passes the set
    test and still matches. Measured: a ``sections_remove`` entry of
    ``"Waiting on\\n\\n<the real table>\\n\\n## Next Steps"`` is not in
    PROTECTED_SECTIONS, matches, and deletes BOTH protected sections.

    Same control ``_validate_imported_event`` applies to its heading, for the
    same reason: these strings are interpolated into markdown structure the
    digest parses.
    """
    if "\n" in value or "\r" in value:
        raise ValidationError(
            f"{field} must be a single line - it is matched against markdown "
            "structure, and a newline lets it span sections",
            field=field,
        )


# Sections whose list items must not be removed by `bullets_remove`.
# Recent Changes is prepend-only history, and Waiting on has its own
# removal primitive that records the outcome.
BULLETS_REMOVE_FORBIDDEN = frozenset(
    ["Recent Changes", "Waiting on"] + ["Action Items", "Stakeholders", "Tickets"]
)


def _apply_waiting_on(
    content: str,
    timestamp: str,
    waiting_on_add: list[dict[str, str]] | None,
    waiting_on_resolve: list[dict[str, str]] | None,
) -> tuple[str, list[str], list[str]]:
    """Apply waiting-on resolves then adds; pure function.

    Returns ``(content, resolved_changes, unmatched)``. ``resolved_changes``
    are the bullets destined for today's Recent Changes subsection;
    ``unmatched`` are resolve ``match`` values that hit no row (surfaced to
    the caller, never dropped).

    A resolve carries a ``kind``: ``resolved`` (default), ``moved`` or
    ``dropped``, which only picks the wording. Without it every removal was
    written up as "Resolved", so a row that moved to another project left a
    false record of an answer that never came.
    """
    resolved_changes: list[str] = []
    unmatched: list[str] = []

    if waiting_on_resolve:
        rows = context_health.parse_waiting_on(content)
        remaining = list(rows)
        for item in waiting_on_resolve:
            match_text = (item.get("match") or "").strip()
            outcome = (item.get("outcome") or "").strip()
            kind = (item.get("kind") or "resolved").strip()
            found = next(
                (r for r in remaining if match_text and match_text in r["what"]),
                None,
            )
            if found is None:
                unmatched.append(match_text)
                continue
            remaining.remove(found)
            note = (
                f"{WAITING_ON_KINDS[kind]} (was waiting on {found['who']}): "
                f"{found['what']}"
            )
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


def _apply_imported_event(
    content: str, timestamp: str, imported_event: dict[str, str]
) -> tuple[str, bool]:
    """Write a self-contained imported-event section above ``## Waiting on``.

    This is the only locked writer for the "Cross-project events" convention; a
    direct Edit skips the sidecar lock the parallel-session discipline requires.

    Returns ``(content, applied)``. ``applied`` is False when a section with this
    exact heading is already present, which makes a retried call a no-op instead
    of stacking duplicate sections - the receiving side is told to "read the
    section it names", and two sections with one name makes that ambiguous.

    Both caller-supplied strings are constrained before they reach the file.
    ``heading`` is validated single-line by the MCP layer; ``body`` has its
    headings demoted here so a pasted summary containing ``## Next Steps``
    becomes a subsection of this event instead of shadowing the project's real
    one. The date is appended unless the heading already ends in a parenthesized
    one, so an id like ``ABCD-1234-56-7890`` is not mistaken for a date.
    """
    heading = " ".join((imported_event.get("heading") or "").split())
    body = (imported_event.get("body") or "").strip()
    if not heading or not body:
        return content, False

    today = timestamp.split(" ")[0]
    if not re.search(r"\(\d{4}-\d{2}-\d{2}\)\s*$", heading):
        heading = f"{heading} ({today})"

    if context_health.extract_section(content, heading) is not None:
        return content, False

    # sanitize_bullet, not demote_headings alone. Demotion turns a pasted
    # `## 2026-08-14 sync` into a column-0 `### 2026-08-14 sync`, which is a
    # perfectly good fake Recent Changes boundary sitting inside this event's
    # body, and it leaves a truncated code fence open. The body is
    # caller-supplied text from another project, the same untrusted shape the
    # PreCompact snapshot turned out to be.
    content = context_health.insert_section_before(
        content,
        f"## {heading}\n\n{context_health.sanitize_bullet(body)}",
        ("Waiting on", "Next Steps", "Recent Changes"),
    )

    related = (imported_event.get("related_project") or "").strip()
    if related:
        content = context_health.upsert_related_projects(
            content, related, imported_event.get("related_note", "") or ""
        )
    return content, True


def update_context_file(
    context_file: str | Path,
    next_steps: list[str] | None = None,
    recent_changes: list[str] | None = None,
    key_decisions: list[str] | None = None,
    gotchas: list[str] | None = None,
    key_files: dict[str, str] | None = None,
    waiting_on_add: list[dict[str, str]] | None = None,
    waiting_on_resolve: list[dict[str, str]] | None = None,
    imported_event: dict[str, str] | None = None,
    sections_remove: list[str] | None = None,
    bullets_remove: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Update sections in a context.md file atomically.

    Every free-form string a caller supplies (``recent_changes``,
    ``key_decisions``, ``gotchas``) goes through
    ``context_health.sanitize_bullet`` first, so a pasted block carrying its
    own ``## `` headings or a truncated code fence cannot end a section early
    or forge a Recent Changes boundary.

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
        waiting_on_resolve: Rows to resolve, each
            ``{"match", "outcome", "kind"}``. Removes the first row whose
            What cell contains ``match`` and records it in today's Recent
            Changes subsection. ``kind`` is ``resolved`` (default), ``moved``
            or ``dropped`` and only selects the wording.
        imported_event: A cross-project event as
            ``{"heading", "body", "related_project", "related_note"}``. Writes
            ``## <heading>`` above Waiting on and records the link on the
            ``**Related projects:**`` header line. Ignored unless both
            ``heading`` and ``body`` are given.
        sections_remove: Section headings to delete whole, matched EXACTLY
            (``section_index`` gives the verbatim text). ``PROTECTED_SECTIONS``
            are refused.
        bullets_remove: Items to delete, each ``{"section", "match"}``. Removes
            the first list item or table row in that section containing
            ``match``, with its continuation lines. Table header rows are never
            matched. ``BULLETS_REMOVE_FORBIDDEN`` sections are refused.

    Returns:
        Dict with ``content`` (updated file text), ``waiting_on_unmatched``
        (``match`` values that resolved to no row - never silently dropped,
        mirroring ``update_tasks_file``'s ``unmatched`` contract),
        ``journal_rolled_over`` (Recent Changes subsections moved to the
        per-project journal by the cap), and the removal results
        ``sections_removed`` / ``sections_unmatched`` / ``bullets_removed`` /
        ``bullets_unmatched``.
    """
    path = Path(context_file)
    if not path.exists():
        raise MissionCacheFileNotFoundError(str(path))

    for name in sections_remove or []:
        _reject_multiline(name, "sections_remove")
        if name.strip() in PROTECTED_SECTIONS:
            raise ValidationError(
                f"'## {name.strip()}' is a protected section and cannot be removed",
                field="sections_remove",
            )
    for item in bullets_remove or []:
        section = (item.get("section") or "").strip()
        _reject_multiline(item.get("section") or "", "bullets_remove.section")
        if not section or not (item.get("match") or "").strip():
            raise ValidationError(
                "each bullets_remove entry needs a non-empty 'section' and 'match'",
                field="bullets_remove",
            )
        if section in BULLETS_REMOVE_FORBIDDEN:
            raise ValidationError(
                f"items in '## {section}' cannot be removed this way "
                "(Recent Changes is prepend-only history; Waiting on uses "
                "waiting_on_resolve; the rest are rendered from the database)",
                field="bullets_remove",
            )
    for item in waiting_on_resolve or []:
        kind = (item.get("kind") or "resolved").strip()
        if kind not in WAITING_ON_KINDS:
            raise ValidationError(
                f"unknown waiting_on_resolve kind '{kind}' "
                f"(expected one of {', '.join(sorted(WAITING_ON_KINDS))})",
                field="waiting_on_resolve",
            )

    journal_path = context_health.derive_journal_path(path)
    waiting_on_unmatched: list[str] = []
    sections_removed: list[str] = []
    sections_unmatched: list[str] = []
    bullets_removed: list[str] = []
    bullets_unmatched: list[str] = []
    rolled_over = 0
    # Carries results out of the transform, which runs under the lock and may be
    # retried; a plain closure variable would be rebound per attempt.
    nonlocal_state: dict[str, Any] = {"imported_event_applied": False}

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

        # Removals run FIRST, before anything is added. A caller splitting a
        # project passes a removal and an addition in the same call, and
        # doing it in this order means a section can be removed and a
        # replacement written under the same name without the removal eating
        # the new one. Results are collected per attempt so a miss surfaces
        # instead of being silently dropped.
        for name in sections_remove or []:
            name = name.strip()
            content, body = context_health.remove_section(content, name)
            # `is None`, not truthiness: remove_section returns the body it
            # cut, and a section with an empty body returns "". Treating that
            # as a miss reports a removal that actually happened as unmatched,
            # which is the never-silently-drop contract failing the other way
            # round - the caller retries or tells the user it did not work.
            (sections_unmatched if body is None else sections_removed).append(name)

        for item in bullets_remove or []:
            section = (item.get("section") or "").strip()
            match_text = (item.get("match") or "").strip()
            content, removed_item = context_health.remove_list_item(
                content, section, match_text
            )
            if removed_item is None:
                bullets_unmatched.append(f"{section}: {match_text}")
            else:
                bullets_removed.append(removed_item)

        # Update Next Steps section. (Replacement stops at the next `## `
        # heading, so a Waiting on section placed before Next Steps is
        # untouched by this.)
        if next_steps:
            next_steps_md = "\n".join(
                f"{i + 1}. {context_health.sanitize_bullet(step)}"
                for i, step in enumerate(next_steps)
            )
            content = _update_section(content, "Next Steps", next_steps_md)

        # Waiting-on maintenance (resolves BEFORE Recent Changes so the
        # resolutions land in today's subsection alongside recent_changes).
        content, resolved_changes, unmatched = _apply_waiting_on(
            content, timestamp, waiting_on_add, waiting_on_resolve
        )
        waiting_on_unmatched.extend(unmatched)

        # Imported cross-project event, after waiting-on maintenance so the
        # `## Waiting on` anchor exists even when this call self-healed it.
        if imported_event:
            content, applied = _apply_imported_event(
                content, timestamp, imported_event
            )
            nonlocal_state["imported_event_applied"] = applied

        # Update Recent Changes - one `## Recent Changes` heading with dated
        # `###` sub-sections prepended newest-first. The prepend shape lives
        # in context_health.prepend_recent_changes (shared with the
        # pre-compact hook; fence-aware and ^-anchored).
        combined_changes = resolved_changes + list(recent_changes or [])
        if combined_changes:
            changes_md = "\n".join(
                f"- {context_health.sanitize_bullet(change)}"
                for change in combined_changes
            )
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
            decisions_md = "\n".join(
                f"- {context_health.sanitize_bullet(d)}" for d in key_decisions
            )
            content = _append_to_section(
                content, "Key Architectural Decisions", decisions_md
            )

        # Update Gotchas section
        if gotchas:
            gotchas_md = "\n".join(
                f"- {context_health.sanitize_bullet(g)}" for g in gotchas
            )
            content = _append_to_section(content, "Gotchas", gotchas_md)

        # Update Key Files section
        if key_files:
            # Table cells, so collapse to one line rather than indent:
            # a newline in either half breaks the row into forged markdown.
            # _escape_cell also neutralises an unescaped pipe.
            files_md = "\n".join(
                f"| `{context_health.escape_cell(filename)}` "
                f"| {context_health.escape_cell(desc)} |"
                for filename, desc in key_files.items()
            )
            content = _append_to_section(content, "Key Files", files_md)

        return content, journal_append

    with _structured_ambiguity_error(path):
        new_content = _atomic_update_context_with_journal(
            path, journal_path, _transform
        )
    return {
        "content": new_content,
        "waiting_on_unmatched": waiting_on_unmatched,
        "journal_rolled_over": rolled_over,
        "imported_event_applied": nonlocal_state["imported_event_applied"],
        "sections_removed": sections_removed,
        "sections_unmatched": sections_unmatched,
        "bullets_removed": bullets_removed,
        "bullets_unmatched": bullets_unmatched,
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
    # The trailing (?!\d) disqualifies a following digit so completing
    # parent "1" matches "- [ ] 1. Parent" but not the "1." prefix of a
    # subtask line "- [ ] 1.2. Child" (and completing "1.2" won't match
    # "1.2.3."). Without it, sub() would flip every child too.
    pattern = re.compile(
        rf"^(\s*[-*]\s*)\[\s*\](\s*{re.escape(number)}\.)(?!\d)",
        re.MULTILINE,
    )
    return pattern.sub(r"\1[x]\2", content)


REMOVED_SECTION = "Removed"

# A struck-through removal record: `- ~~54a. text~~ (removed ...)`. Carries
# no checkbox on purpose, so parse_tasks_md skips it and the progress
# counter stops counting work nobody will do. The number is still parsed
# back out of here to stop `new_tasks` from reusing it.
_REMOVED_RECORD_RE = re.compile(
    r"^\s*[-*]\s*~~([0-9]+(?:\.[0-9]+)*[a-z]?)\.", re.MULTILINE
)


def _remove_task_line(content: str, number: str) -> tuple[str, str | None]:
    """Cut the checklist line for ``number`` out. Returns ``(content, text)``.

    ``text`` is the line verbatim, or None when no such line exists. Same
    ``(?!\\d)`` guard as ``_mark_task_checked_by_number``: removing "1" must
    not take "1.2" with it.
    """
    pattern = re.compile(
        rf"^[ \t]*[-*][ \t]*\[[ xX]?\][ \t]*{re.escape(number)}\.(?!\d)[^\n]*\n?",
        re.MULTILINE,
    )
    match = pattern.search(content)
    if match is None:
        return content, None
    return content[: match.start()] + content[match.end() :], match.group(0).rstrip("\n")


def update_tasks_file(
    tasks_file: str | Path,
    completed_tasks: list[str] | None = None,
    new_tasks: list[str] | None = None,
    remaining_summary: str | None = None,
    notes: list[str] | None = None,
    tasks_remove: list[dict[str, str]] | None = None,
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
        tasks_remove: Items to delete, each ``{"match", "reason"}``. ``match``
            resolves number-first then by substring, exactly like
            ``completed_tasks``. ``reason`` is required. The line leaves the
            checklist and a struck-through record lands under ``## Removed``,
            which keeps the history without letting a task nobody will do
            skew the progress counter. Refused when the item still has
            children.

    Returns:
        Dict with update summary including ``completed_numbers``: the
        checklist numbers (e.g. ``["54a", "56"]``) of items that were
        unchecked before this call and are now checked. Used by callers
        to drive cross-cutting cleanup like clearing active-task pointers.
        Also includes ``unmatched``: ``completed_tasks`` entries that
        resolved to no checklist item, so callers can surface a dropped
        completion instead of leaving a box silently unticked, plus
        ``removed_numbers`` and ``remove_unmatched`` for ``tasks_remove``.
    """
    path = Path(tasks_file)
    if not path.exists():
        raise MissionCacheFileNotFoundError(str(path))

    for item in tasks_remove or []:
        if not (item.get("match") or "").strip():
            raise ValidationError(
                "each tasks_remove entry needs a non-empty 'match'",
                field="tasks_remove",
            )
        if not (item.get("reason") or "").strip():
            raise ValidationError(
                "each tasks_remove entry needs a non-empty 'reason' - the "
                "record under '## Removed' is the only trace the task leaves",
                field="tasks_remove",
            )

    updates_made: list[str] = []
    completed_numbers_seen: list[str] = []
    unmatched: list[str] = []
    removed_numbers: list[str] = []
    remove_unmatched: list[str] = []

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
                # Fallback: literal substring of an unchecked line. Only
                # the FIRST match is flipped (count=1) - a bare word can
                # appear in several task lines, and marking every one of
                # them done would silently complete unfinished siblings.
                escaped = re.escape(task_desc)
                pattern = rf"- \[\s*\]([^\n]*{escaped}[^\n]*)"
                if re.search(pattern, content, re.IGNORECASE):
                    content = re.sub(
                        pattern, r"- [x]\1", content, count=1, flags=re.IGNORECASE
                    )
                    updates_made.append(f"Completed: {task_desc[:50]}...")
                else:
                    unmatched.append(task_desc)

        # Remove tasks. Runs after completions so a call that both completes
        # and removes behaves the same whatever order the caller listed them.
        if tasks_remove:
            for item in tasks_remove:
                match_text = (item.get("match") or "").strip()
                reason = (item.get("reason") or "").strip()
                items = parse_tasks_md(content)
                by_number = {i.number: i for i in items}
                number = _leading_task_number(match_text)
                target = by_number.get(number) if number else None
                if target is None:
                    target = next(
                        (
                            i
                            for i in items
                            if match_text.lower() in i.text.lower()
                        ),
                        None,
                    )
                if target is None:
                    remove_unmatched.append(match_text)
                    continue
                # Removing a parent would orphan its children in the file
                # and leave them counted under a number with no owner.
                children = [
                    i.number
                    for i in items
                    if i.number.startswith(target.number + ".")
                ]
                if children:
                    raise ValidationError(
                        f"task {target.number} still has children "
                        f"({', '.join(children)}) - remove them first",
                        field="tasks_remove",
                    )
                content, line = _remove_task_line(content, target.number)
                if line is None:
                    remove_unmatched.append(match_text)
                    continue
                today = timestamp.split(" ")[0]
                # Collapse the reason to one line, the same normalization
                # _apply_imported_event does on its heading. The record is a
                # single list item, and a reason carrying newlines writes
                # column-0 text into a checklist file: measured, a reason of
                # "superseded\n- [x] 3. fake\n- [ ] 4. fake" invented two
                # tasks that parse_tasks_md then counted, in the one file
                # where the number is how every surface addresses an item.
                record = (
                    f"- ~~{target.number}. {target.text}~~ "
                    f"(removed {today}: {' '.join(reason.split())})"
                )
                content = _append_to_section(content, REMOVED_SECTION, record)
                removed_numbers.append(target.number)
                updates_made.append(f"Removed: {target.number}. {target.text[:50]}")

        # Diff post-transform: any number that was [ ] before and is [x]
        # now is a real transition. This catches edits regardless of how
        # the caller phrased ``completed_tasks`` (description, fragment,
        # etc.) and ignores items that were already checked beforehand.
        post_checked = {
            item.number for item in parse_tasks_md(content) if item.checked
        }
        completed_numbers_seen.extend(sorted(pre_unchecked & post_checked))

        if new_tasks:
            # Numbering comes from the canonical parser, not a local regex. The
            # old one matched `[x\s]` and a bare `\d+`, so a hand-edited `[X]`
            # or a letter-suffixed `54a` did not raise the max and the next task
            # collided with an existing number.
            # parse_tasks_md only yields numbers matching [0-9]+(\.[0-9]+)*[a-z]?,
            # so the leading integer is always there to take.
            # Removed records count too. They carry no checkbox, so
            # parse_tasks_md does not see them, and without this a task
            # removed at the top of the range frees its number for the next
            # addition - two different items sharing one id, in a file where
            # the number is how every other surface addresses a task.
            tops = [
                int(re.match(r"\d+", item.number).group())
                for item in parse_tasks_md(content)
            ] + [
                int(re.match(r"\d+", number).group())
                for number in _REMOVED_RECORD_RE.findall(content)
            ]
            next_num = max(tops, default=0) + 1

            new_tasks_md = "\n".join(
                f"- [ ] {next_num + i}. {task}" for i, task in enumerate(new_tasks)
            )

            # Append to a dated section instead of hunting for an anchor. The
            # old code ran an UNANCHORED re.sub with the default count=0 against
            # `## Phase 2` / `## Validation` / `## Notes`, so it inserted at every
            # occurrence including one inside an existing task's description,
            # splitting that line in half and creating the new task twice. It
            # also landed mid-file (the first anchor is usually near the top),
            # which is why physical order drifted away from numbering over time.
            # A dated section keeps same-day additions together and keeps the
            # file's order matching its numbers.
            content = _append_to_section(
                content, f"Additions ({datetime.now().strftime('%Y-%m-%d')})", new_tasks_md
            )

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
            # One line per note. sanitize_bullet neutralises headings, but a
            # checklist line is what matters in a TASKS file and indenting
            # one does not hide it (parse_tasks_md allows leading whitespace,
            # since nested subtasks are indented). Collapsing is the only
            # thing that stops a note inventing a task.
            notes_md = "\n".join(f"- {' '.join(n.split())}" for n in notes)
            content = _append_to_section(content, "Notes", notes_md)
            updates_made.append(f"Added {len(notes)} notes")

        return content

    with _structured_ambiguity_error(path):
        new_content = _atomic_update_text(path, _transform)

    # Calculate progress from the just-written content
    progress = parse_task_progress(new_content)

    return {
        "file": str(path),
        "updates_made": updates_made,
        "progress": progress.model_dump() if progress else None,
        "completed_numbers": completed_numbers_seen,
        "unmatched": unmatched,
        "removed_numbers": removed_numbers,
        "remove_unmatched": remove_unmatched,
    }


def parse_task_progress(content: str) -> TaskProgress:
    """Parse progress from tasks.md content.

    Only leaf checklist items count toward the totals. A parent line (a
    numbered item that has dotted children, e.g. ``1.`` when ``1.1``/``1.2``
    exist) is a structural heading, not a work item - counting it would keep
    a project with subtasks from ever reaching 100% and disagree with the
    ``format_tasks_markdown`` count that seeds the "N tasks pending" header.
    """
    # Match markdown checklist items: - [ ] or - [x]
    completed_pattern = r"^\s*[-*]\s*\[x\]"
    pending_pattern = r"^\s*[-*]\s*\[\s*\]"

    completed = len(
        re.findall(completed_pattern, content, re.MULTILINE | re.IGNORECASE)
    )
    pending = len(re.findall(pending_pattern, content, re.MULTILINE))

    # Drop parent lines from the counts: a number is a parent iff another
    # item's number extends it with a "." (so "1" is a parent of "1.1").
    # max(0, ...) guards against a hand-edited checkbox the line regex and
    # parse_tasks_md classify differently (e.g. "- [ x ]").
    items = parse_tasks_md(content)
    numbers = [item.number for item in items]
    for item in items:
        if any(other.startswith(item.number + ".") for other in numbers):
            if item.checked:
                completed = max(0, completed - 1)
            else:
                pending = max(0, pending - 1)

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


# ── cross-project move ───────────────────────────────────────────────────


def _plan_source_context(
    content: str,
    sections: list[str],
    bullets: list[dict[str, str]],
    waiting_on: list[str],
) -> tuple[str, dict[str, Any]]:
    """Cut the moved pieces out of the source context. Pure.

    Returns ``(new_content, taken)`` where ``taken`` carries the verbatim
    text of everything removed, which is what gets planted in the target.
    Anything that matched nothing is reported rather than assumed moved -
    a move that silently drops half its payload is worse than one that
    refuses.
    """
    taken: dict[str, Any] = {
        "sections": [],
        "bullets": [],
        "waiting_on": [],
        "unmatched": [],
    }

    for heading in sections:
        # One lookup, not two: remove_section returns the body of the section
        # it actually cut. Reading with extract_section (prefix-tolerant) and
        # deleting with remove_section (exact) could resolve to two different
        # sections, moving the body of `## Notes (old)` while deleting
        # `## Notes`.
        content, body = context_health.remove_section(content, heading)
        if body is None:
            taken["unmatched"].append(f"section: {heading}")
            continue
        # Normalize the heading the same way _apply_imported_event does
        # before it is interpolated into a `## ` line on the target. The
        # caller's string reaches the target file, not the source file's own
        # heading text, so anything structural in it is written verbatim.
        taken["sections"].append(
            {"heading": " ".join(heading.split()), "body": body.strip()}
        )

    for item in bullets:
        section = (item.get("section") or "").strip()
        match_text = (item.get("match") or "").strip()
        content, removed = context_health.remove_list_item(
            content, section, match_text
        )
        if removed is None:
            taken["unmatched"].append(f"bullet: {section}: {match_text}")
        else:
            taken["bullets"].append({"section": section, "item": removed})

    if waiting_on:
        rows = context_health.parse_waiting_on(content)
        remaining = list(rows)
        for match_text in waiting_on:
            found = next(
                (r for r in remaining if match_text and match_text in r["what"]),
                None,
            )
            if found is None:
                taken["unmatched"].append(f"waiting_on: {match_text}")
                continue
            remaining.remove(found)
            taken["waiting_on"].append(found)
        if len(remaining) != len(rows):
            content = context_health.replace_waiting_on_table(content, remaining)

    return content, taken


def _plan_target_context(content: str, taken: dict[str, Any]) -> str:
    """Plant the moved pieces into the target context. Pure."""
    for section in taken["sections"]:
        # Above Waiting on, the same placement and anchor order the
        # cross-project imported_event convention already uses.
        content = context_health.insert_section_before(
            content,
            f"## {section['heading']}\n\n{section['body']}",
            ("Waiting on", "Next Steps", "Recent Changes"),
        )

    for bullet in taken["bullets"]:
        # Create the section in canonical position when the target lacks it.
        # append_to_section_body's own fallback puts a new section at EOF,
        # which on a normal file is BELOW Recent Changes and breaks the
        # section order rules/missioncache.md defines - and Recent Changes is
        # the one section that must stay last-ish for the cap to find its
        # entries. The waiting-on branch below already self-heals this way.
        if context_health.extract_section(content, bullet["section"]) is None:
            content = context_health.insert_section_before(
                content,
                f"## {bullet['section']}\n",
                ("Waiting on", "Next Steps", "Recent Changes"),
            )
        content = context_health.append_to_section_body(
            content, bullet["section"], bullet["item"],
            drop_lines=("- TBD", "1. TBD"),
        )

    if taken["waiting_on"]:
        if context_health.extract_section(content, "Waiting on") is None:
            content = context_health.insert_waiting_on_before_next_steps(
                content, context_health.build_waiting_on_section([])
            )
        rows = context_health.parse_waiting_on(content) + taken["waiting_on"]
        content = context_health.replace_waiting_on_table(content, rows)

    return content


def move_to_project(
    source_project: str,
    target_project: str,
    sections: list[str] | None = None,
    bullets: list[dict[str, str]] | None = None,
    tasks: list[dict[str, str]] | None = None,
    waiting_on: list[str] | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Move context sections, items, waiting-on rows and tasks between projects.

    Splitting a project is a MOVE, not a delete plus an add. Done as two
    calls it can half-fail and leave a section in both files or neither.
    This holds every affected file's lock for the whole operation.

    Locking: all four project files are locked in sorted path order. Two
    sessions moving in opposite directions between the same pair would
    otherwise take the same two locks in opposite orders and deadlock. This
    is the only operation in the codebase that holds more than one project
    lock, which is why it calls the ``_unlocked_*`` cores rather than the
    lock-taking wrappers (see the note above those functions).

    Write order: the target side is written fully, then the source side.
    Up to six writes happen (two journals if either Recent Changes trips the
    cap, plus the two contexts and two tasks files), and a crash partway
    leaves the content in BOTH files rather than neither - the same
    duplicate-rather-than-lose reasoning the journal write already uses.
    Recovery is to remove the leftovers from the source with
    ``sections_remove`` / ``bullets_remove`` / ``tasks_remove``.
    """
    # Both names are interpolated straight into a path under settings.root
    # by get_missioncache_files, so a name like "../../x" would walk out of
    # the data root. Same control rename_task applies, and the same reason
    # the file-taking tools run _validate_path(must_be_under=settings.root).
    validate_task_name(source_project)
    validate_task_name(target_project)
    if source_project == target_project:
        raise ValidationError(
            "source_project and target_project are the same",
            field="target_project",
        )
    if not any([sections, bullets, tasks, waiting_on]):
        raise ValidationError(
            "nothing to move - pass at least one of sections, bullets, "
            "tasks or waiting_on",
            field="sections",
        )
    for item in tasks or []:
        if not (item.get("match") or "").strip():
            raise ValidationError(
                "each tasks entry needs a non-empty 'match'", field="tasks"
            )
    # Same guard update_context_file applies to bullets_remove, and it matters
    # more here. remove_list_item does a substring test, so a blank match is
    # contained in EVERY item: it would silently take the first item of the
    # named section and move it to the other project, reported as a success.
    for item in bullets or []:
        section = (item.get("section") or "").strip()
        _reject_multiline(item.get("section") or "", "bullets.section")
        if not section or not (item.get("match") or "").strip():
            raise ValidationError(
                "each bullets entry needs a non-empty 'section' and 'match' "
                "(a blank match would move the section's first item)",
                field="bullets",
            )
        # Moving out of these is removing from them, so the same refusals
        # update_context_file applies have to hold here. Without this,
        # bullets=[{"section": "Recent Changes", ...}] takes an entry out of
        # prepend-only history through the move instead of the remove.
        if section in BULLETS_REMOVE_FORBIDDEN:
            raise ValidationError(
                f"items in '## {section}' cannot be moved "
                "(Recent Changes is prepend-only history; Waiting on moves via "
                "the waiting_on parameter; the rest are rendered from the database)",
                field="bullets",
            )
    for heading in sections or []:
        _reject_multiline(heading, "sections")
        if not heading.strip():
            raise ValidationError(
                "sections entries must be non-empty heading text",
                field="sections",
            )
        if heading.strip() in PROTECTED_SECTIONS:
            raise ValidationError(
                f"'## {heading.strip()}' is a protected section and cannot be moved",
                field="sections",
            )

    src_files = get_missioncache_files(source_project)
    dst_files = get_missioncache_files(target_project)
    if not src_files.context_file:
        raise MissionCacheFileNotFoundError(
            f"source project '{source_project}' has no context file"
        )
    if not dst_files.context_file:
        raise MissionCacheFileNotFoundError(
            f"target project '{target_project}' has no context file"
        )

    src_ctx = Path(src_files.context_file)
    dst_ctx = Path(dst_files.context_file)
    src_tasks = Path(src_files.tasks_file) if src_files.tasks_file else None
    dst_tasks = Path(dst_files.tasks_file) if dst_files.tasks_file else None
    if tasks and (src_tasks is None or dst_tasks is None):
        raise MissionCacheFileNotFoundError(
            "moving tasks needs a tasks file on both sides"
        )

    src_journal = context_health.derive_journal_path(src_ctx)
    dst_journal = context_health.derive_journal_path(dst_ctx)
    lock_paths = [p for p in (src_ctx, dst_ctx, src_tasks, dst_tasks) if p is not None]

    # The dedup-and-sort ordering rule lives in filelock, next to the lock it
    # protects: it is a locking primitive, not a move-specific one, and the
    # next multi-lock caller must not have to re-derive it (getting it wrong
    # hangs with no timeout and no error).
    with filelock.sidecar_locks(lock_paths):
        timestamp = get_timestamp()
        today = timestamp.split(" ")[0]
        src_content = src_ctx.read_text(encoding="utf-8")
        dst_content = dst_ctx.read_text(encoding="utf-8")

        # A section name already present in the target makes the result
        # ambiguous for every reader, the same rule imported_event applies.
        # Checked INSIDE the lock against the content just read: done before
        # acquiring it, another writer could add the same section in the gap,
        # and this would then insert a second copy and delete the source's -
        # exactly the state that makes every later write to that section fail.
        conflicts = [
            heading
            for heading in (sections or [])
            if context_health.extract_section(dst_content, heading) is not None
        ]
        if conflicts:
            raise ValidationError(
                f"target '{target_project}' already has these sections: "
                f"{', '.join(conflicts)}",
                field="sections",
            )

        src_content, taken = _plan_source_context(
            src_content,
            list(sections or []),
            list(bullets or []),
            list(waiting_on or []),
        )
        # The target may itself be damaged; surface that as a coded error
        # naming the target rather than a bare ValueError.
        with _structured_ambiguity_error(dst_ctx):
            dst_content = _plan_target_context(dst_content, taken)

        moved_tasks: list[dict[str, str]] = []
        src_tasks_content = dst_tasks_content = None
        if tasks and src_tasks is not None and dst_tasks is not None:
            src_tasks_content = src_tasks.read_text(encoding="utf-8")
            dst_tasks_content = dst_tasks.read_text(encoding="utf-8")
            src_tasks_content, dst_tasks_content, moved_tasks, unmatched = (
                _plan_task_move(
                    src_tasks_content,
                    dst_tasks_content,
                    list(tasks),
                    source_project,
                    target_project,
                    today,
                )
            )
            taken["unmatched"].extend(unmatched)

        if not (
            taken["sections"] or taken["bullets"] or taken["waiting_on"] or moved_tasks
        ):
            # Nothing matched, so nothing moved: writing anyway stamped both
            # files with a "Moved to <x>: nothing matched" entry and a
            # permanent **Related projects:** link between two projects that
            # never exchanged anything.
            return {
                "source_project": source_project,
                "target_project": target_project,
                "sections_moved": [],
                "bullets_moved": [],
                "waiting_on_moved": [],
                "tasks_moved": [],
                "unmatched": taken["unmatched"],
                "summary": "nothing matched - no files were written",
            }

        summary = _describe_move(taken, moved_tasks)
        if note:
            summary = f"{summary} - {note}"
        src_content, src_journal_append = _record_move(
            src_content,
            timestamp,
            f"Moved to {target_project}: {summary}",
            src_journal.name,
        )
        dst_content, dst_journal_append = _record_move(
            dst_content,
            timestamp,
            f"Moved in from {source_project}: {summary}",
            dst_journal.name,
        )
        src_content = context_health.upsert_related_projects(
            src_content, target_project, "received part of this project"
        )
        dst_content = context_health.upsert_related_projects(
            dst_content, source_project, "moved content here"
        )

        # Target side first, source side last (see the docstring).
        if dst_journal_append:
            _unlocked_append_journal(dst_ctx, dst_journal, dst_journal_append)
        _unlocked_write_text(dst_ctx, dst_content)
        if dst_tasks is not None and dst_tasks_content is not None:
            _unlocked_write_text(dst_tasks, dst_tasks_content)
        if src_journal_append:
            _unlocked_append_journal(src_ctx, src_journal, src_journal_append)
        _unlocked_write_text(src_ctx, src_content)
        if src_tasks is not None and src_tasks_content is not None:
            _unlocked_write_text(src_tasks, src_tasks_content)

    return {
        "source_project": source_project,
        "target_project": target_project,
        "sections_moved": [s["heading"] for s in taken["sections"]],
        "bullets_moved": [b["item"] for b in taken["bullets"]],
        "waiting_on_moved": [r["what"] for r in taken["waiting_on"]],
        "tasks_moved": moved_tasks,
        "unmatched": taken["unmatched"],
        "summary": summary,
    }


def _describe_move(taken: dict[str, Any], moved_tasks: list[dict[str, str]]) -> str:
    """One-line description of what moved, for both Recent Changes entries."""
    parts = []
    if taken["sections"]:
        parts.append(
            ", ".join(f"'{s['heading']}'" for s in taken["sections"])
        )
    if taken["bullets"]:
        parts.append(f"{len(taken['bullets'])} items")
    if taken["waiting_on"]:
        parts.append(
            "waiting on "
            + ", ".join(f"'{r['what']}'" for r in taken["waiting_on"])
        )
    if moved_tasks:
        parts.append(
            "tasks "
            + ", ".join(f"{t['from']} -> {t['to']}" for t in moved_tasks)
        )
    return "; ".join(parts) if parts else "nothing matched"


def _record_move(
    content: str, timestamp: str, line: str, journal_name: str
) -> tuple[str, str | None]:
    """Prepend the move to Recent Changes and re-enforce the cap.

    Returns ``(content, journal_append_or_None)`` so the caller writes the
    journal under the lock it already holds, exactly like the single-file
    ``update_context_file`` path.
    """
    # Same anchored, count=1 stamp update_context_file does: both files
    # genuinely changed, and a stale Last Updated makes the health check
    # call an actively-edited project stale.
    content = re.sub(
        r"^\*\*Last Updated:\*\* .+",
        f"**Last Updated:** {timestamp}",
        content,
        count=1,
        flags=re.MULTILINE,
    )
    content = context_health.prepend_recent_changes(
        content, timestamp, f"- {context_health.sanitize_bullet(line)}"
    )
    content, journal_append, _ = context_health.split_recent_changes_for_cap(
        content, journal_name
    )
    return content, journal_append


def _plan_task_move(
    src_content: str,
    dst_content: str,
    tasks: list[dict[str, str]],
    source_project: str,
    target_project: str,
    today: str,
) -> tuple[str, str, list[dict[str, str]], list[str]]:
    """Move checklist items between two tasks files. Pure.

    The task keeps its text but takes a NEW number on the target side, since
    numbers are per-file. The source keeps a struck-through record naming
    where it went, so a number referenced in an old note still resolves to
    an explanation rather than to nothing.
    """
    moved: list[dict[str, str]] = []
    unmatched: list[str] = []

    for entry in tasks:
        match_text = (entry.get("match") or "").strip()
        items = parse_tasks_md(src_content)
        by_number = {i.number: i for i in items}
        number = _leading_task_number(match_text)
        target = by_number.get(number) if number else None
        if target is None:
            target = next(
                (i for i in items if match_text.lower() in i.text.lower()), None
            )
        if target is None:
            unmatched.append(f"task: {match_text}")
            continue
        children = [
            i.number for i in items if i.number.startswith(target.number + ".")
        ]
        if children:
            raise ValidationError(
                f"task {target.number} still has children "
                f"({', '.join(children)}) - move them first",
                field="tasks",
            )

        src_content, line = _remove_task_line(src_content, target.number)
        if line is None:
            unmatched.append(f"task: {match_text}")
            continue

        new_number = _next_task_number(dst_content)
        checkbox = "x" if target.checked else " "
        dst_content = _append_to_section(
            dst_content,
            f"Additions ({today})",
            f"- [{checkbox}] {new_number}. {target.text} "
            f"(moved from {source_project} task {target.number})",
        )
        src_content = _append_to_section(
            src_content,
            REMOVED_SECTION,
            f"- ~~{target.number}. {target.text}~~ "
            f"(removed {today}: moved to {target_project} as {new_number})",
        )
        moved.append({"from": target.number, "to": str(new_number)})

    return src_content, dst_content, moved, unmatched


def _next_task_number(content: str) -> int:
    """Next free top-level checklist number, counting removed records too."""
    tops = [
        int(re.match(r"\d+", item.number).group())
        for item in parse_tasks_md(content)
    ] + [
        int(re.match(r"\d+", number).group())
        for number in _REMOVED_RECORD_RE.findall(content)
    ]
    return max(tops, default=0) + 1

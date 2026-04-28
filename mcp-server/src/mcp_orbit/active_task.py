"""Per-session active orbit task pointer.

Tracks which orbit checklist task numbers (e.g. ``"54a"``, ``"56"``) the
caller is currently focused on. The statusline reads this to render the
``Task:`` field, replacing the previous read of Claude Code's internal
TodoList (which duplicated information Claude already prints in chat).

Identifier shape: orbit's DB tracks projects, not checklist items. The
items the user picks from (``54a``, ``8``, ``0.1``) are markdown lines in
``<project>-tasks.md`` parsed by their numbering, not rows in ``tasks.db``.
So the active-task pointer keys by ``(project_name, task_numbers)``.

State file: ``~/.claude/hooks/state/active-orbit-task/<session-id>.json``::

    {
      "project_name": "orbit-public-release",
      "task_numbers": ["54a"],
      "updated": "2026-04-28T12:34:56+03:00"
    }

Per-session keying matches the existing ``hooks/state/projects/<sid>.json``
pattern. Concurrent sessions on the same project don't clobber each other.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

# Lives under ``~/.claude/`` (Claude Code's state dir), not ``~/.orbit/``,
# because session-scoped state is keyed by Claude Code session ids and
# parallels the existing hook pointers.
STATE_DIR = Path.home() / ".claude" / "hooks" / "state" / "active-orbit-task"

# Conservative session-id shape: alphanumeric + ``._-`` only, bounded length.
# Covers Claude Code's UUIDs and Codex/OpenCode-style ids; rejects path
# separators, ``..`` components, and null bytes that could escape STATE_DIR
# when joined into a filename.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _safe_session_id(session_id: str) -> bool:
    """Return True iff session_id is safe to use as a filename component.

    Defense in depth - the MCP layer already validates non-empty session ids,
    but accepting any string here would let a misbehaving caller traverse
    out of ``STATE_DIR`` via ``../foo`` or ``/etc/passwd``.
    """
    if not session_id or ".." in session_id:
        return False
    return bool(_SESSION_ID_RE.match(session_id))


def _pointer_path(session_id: str) -> Path:
    return STATE_DIR / f"{session_id}.json"


def read_pointer(session_id: str) -> dict | None:
    """Return the pointer dict for this session, or None if unset/unreadable."""
    if not _safe_session_id(session_id):
        return None
    path = _pointer_path(session_id)
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def write_pointer(
    session_id: str, project_name: str, task_numbers: list[str]
) -> Path:
    """Write or replace the active-task pointer for this session.

    Atomic via tmp-then-rename. Returns the written path. Raises
    ValueError on session ids that would escape ``STATE_DIR`` - callers
    above the MCP boundary should have already validated, but we
    re-check here as defense in depth.
    """
    if not _safe_session_id(session_id):
        raise ValueError(f"unsafe session_id: {session_id!r}")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = _pointer_path(session_id)
    payload = {
        "project_name": project_name,
        "task_numbers": list(task_numbers),
        "updated": datetime.now().astimezone().isoformat(),
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, path)
    return path


def clear_pointer(session_id: str) -> bool:
    """Delete the pointer for this session. Returns True if a file was removed."""
    if not _safe_session_id(session_id):
        return False
    path = _pointer_path(session_id)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def remove_task_numbers_everywhere(
    project_name: str, completed_numbers: list[str]
) -> list[str]:
    """Remove ``completed_numbers`` from every session's pointer for ``project_name``.

    Used as the auto-clear hook on ``update_tasks_file``: when items get
    marked ``[x]``, they should disappear from any active-task pointer that
    referenced them. Empty pointers are removed.

    Returns the list of session ids that were modified (for logging/tests).
    """
    if not completed_numbers:
        return []
    if not STATE_DIR.is_dir():
        return []

    completed_set = set(completed_numbers)
    affected: list[str] = []

    for path in STATE_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("project_name") != project_name:
            continue
        existing = data.get("task_numbers") or []
        remaining = [n for n in existing if n not in completed_set]
        if remaining == existing:
            continue
        session_id = path.stem
        if not _safe_session_id(session_id):
            # Filename inside STATE_DIR but not a shape we'd write - leave alone.
            continue
        affected.append(session_id)
        # Write empty pointer first when the set drains, then best-effort
        # unlink. If unlink fails (permissions, FS error), the empty
        # pointer is inert (statusline treats empty task_numbers as
        # "hide field"), avoiding a stale-data race where the original
        # file with the now-completed numbers would otherwise persist.
        if remaining:
            write_pointer(session_id, project_name, remaining)
        else:
            write_pointer(session_id, project_name, [])
            try:
                path.unlink()
            except OSError:
                pass

    return affected

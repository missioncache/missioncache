#!/usr/bin/env python3
"""
PreCompact hook - capture session state before compaction.

Snapshots the last few user prompts and assistant text turns into context.md
under a `### <timestamp>` Pre-Compact Snapshot subsection so something
state-bearing actually survives compaction. Uses an atomic flock-protected
read-modify-write so concurrent saves do not race.

DB calls (find_task_for_cwd, get_repo, process_heartbeats) are wrapped in
bounded retry-with-backoff because under active MCP server load the hook
can collide with other writers and silently fail (sqlite3 OperationalError:
database is locked). On terminal failure the hook writes a sticky error
file at ~/.claude/hooks/state/last-precompact-error.json that /orbit:go
surfaces on next resume.
"""

import contextlib
import fcntl
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

# Bundled orbit-db path for marketplace installs (no system pip install).
_BUNDLED_ORBIT_DB = Path(__file__).resolve().parent.parent / "orbit-db"
if _BUNDLED_ORBIT_DB.is_dir() and str(_BUNDLED_ORBIT_DB) not in sys.path:
    sys.path.insert(0, str(_BUNDLED_ORBIT_DB))

ERROR_FILE = (
    Path.home() / ".claude" / "hooks" / "state" / "last-precompact-error.json"
)
SNAPSHOT_PREFIX = "Pre-Compact Snapshot"
MAX_TURNS = 5
MAX_TURN_CHARS = 800
RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY = 0.4  # exponential backoff between retries


# ── Atomic write helpers (duplicated from mcp_orbit.orbit; keeping the hook
#    self-contained avoids dragging the full mcp_orbit transitive imports
#    into the PreCompact hot path) ────────────────────────────────────────


@contextlib.contextmanager
def _file_lock(path):
    """Hold an exclusive lock on a sidecar lockfile next to ``path``."""
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lockfd:
        fcntl.flock(lockfd.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lockfd.fileno(), fcntl.LOCK_UN)


def _atomic_update_text(path, transform):
    """Read-modify-write under flock with os.replace for crash safety."""
    with _file_lock(path):
        content = path.read_text()
        new_content = transform(content)
        tmp_path = path.with_name(path.name + ".tmp")
        tmp_path.write_text(new_content)
        os.replace(tmp_path, path)
        return new_content


# ── DB retry + sticky error ──────────────────────────────────────────────


def _retry_db(fn):
    """Run fn with bounded retry on 'database is locked' OperationalErrors.

    Other OperationalErrors (e.g. malformed schema) bubble up immediately.
    """
    last_err = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "database is locked" not in str(e).lower():
                raise
            last_err = e
            if attempt < RETRY_ATTEMPTS - 1:
                time.sleep(RETRY_BASE_DELAY * (2**attempt))
    if last_err is not None:
        raise last_err
    return None


def _write_sticky_error(reason, task_name=None):
    """Write a sticky error file that /orbit:go surfaces on next resume."""
    try:
        ERROR_FILE.parent.mkdir(parents=True, exist_ok=True)
        ERROR_FILE.write_text(
            json.dumps(
                {
                    "timestamp": datetime.now().isoformat(),
                    "task_name": task_name,
                    "reason": reason,
                }
            )
        )
    except Exception:
        # Recursion-safe fallback: if the sticky-error write itself fails
        # there is nowhere left to log to. Swallow.
        pass


def _clear_sticky_error():
    """Remove sticky error after a successful PreCompact run."""
    if ERROR_FILE.exists():
        try:
            ERROR_FILE.unlink()
        except Exception:
            pass


def _safe_db_call(label, fn, task_name=None, sticky=True):
    """Run ``fn`` through ``_retry_db`` and absorb terminal failures.

    Returns ``fn()`` on success, ``None`` if the retry budget was exhausted
    or any other exception escaped. By default the failure is recorded in the
    sticky error file so /orbit:go can surface it; pass ``sticky=False`` for
    benign deferrals (heartbeat aggregation, where the work just stays queued).
    """
    try:
        return _retry_db(fn)
    except Exception as e:
        reason = f"{label}: {e}"
        if sticky:
            _write_sticky_error(reason, task_name=task_name)
        print(f"orbit pre_compact: {reason}", file=sys.stderr)
        return None


# ── Transcript snapshot extraction ───────────────────────────────────────


def _extract_text(content):
    """Pull plain text out of a Claude message ``content`` field.

    Content can be a bare string OR a list of {type, text/thinking/...}
    blocks (the same shape the activity_tracker hook handles for image
    attachments). Only ``text`` blocks contribute to the snapshot;
    thinking, tool_use, and tool_result blocks are skipped.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    texts = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text")
        if text:
            texts.append(text)
    return "\n".join(texts)


def _read_recent_turns(transcript_path):
    """Return (user_prompts, assistant_replies), each capped at MAX_TURNS."""
    user_prompts: list[str] = []
    assistant_replies: list[str] = []

    if not transcript_path:
        return user_prompts, assistant_replies
    p = Path(transcript_path)
    if not p.exists():
        return user_prompts, assistant_replies

    try:
        with p.open() as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rtype = rec.get("type")
                if rtype == "user" and not rec.get("isMeta"):
                    text = _extract_text(rec.get("message", {}).get("content", ""))
                    if text.strip():
                        user_prompts.append(text)
                elif rtype == "assistant":
                    text = _extract_text(rec.get("message", {}).get("content", ""))
                    if text.strip():
                        assistant_replies.append(text)
    except Exception:
        # A truncated or corrupted transcript should not break the hook.
        pass

    return user_prompts[-MAX_TURNS:], assistant_replies[-MAX_TURNS:]


def _truncate(text):
    if len(text) <= MAX_TURN_CHARS:
        return text
    return text[:MAX_TURN_CHARS] + "..."


def _build_snapshot_body(user_prompts, assistant_replies):
    """Format the snapshot subsection body (everything below the ### heading)."""
    lines = [f"**{SNAPSHOT_PREFIX}** (auto-saved before compaction)", ""]

    if user_prompts:
        lines.append("Recent user prompts (oldest first):")
        lines.append("")
        for i, prompt in enumerate(user_prompts):
            lines.append(_truncate(prompt))
            if i < len(user_prompts) - 1:
                lines.append("")
                lines.append("---")
                lines.append("")
        lines.append("")

    if assistant_replies:
        lines.append("Recent assistant responses (oldest first):")
        lines.append("")
        for i, reply in enumerate(assistant_replies):
            lines.append(_truncate(reply))
            if i < len(assistant_replies) - 1:
                lines.append("")
                lines.append("---")
                lines.append("")
        lines.append("")

    if not user_prompts and not assistant_replies:
        lines.append(
            "(no recent turns captured - transcript empty or unreadable)"
        )
        lines.append("")

    return "\n".join(lines)


# ── context.md transform ─────────────────────────────────────────────────


def _make_transform(timestamp, snapshot_body):
    """Build the in-memory transform fn for ``_atomic_update_text``.

    Updates **Last Updated** and prepends a ``### <timestamp>`` Pre-Compact
    Snapshot subsection under the existing ``## Recent Changes`` heading
    (newest-first, same shape as manual /orbit:save entries). Tolerates the
    legacy ``## Recent Changes (timestamp)`` heading by only matching the
    prefix.
    """

    def transform(content):
        content = re.sub(
            r"\*\*Last Updated:\*\* .+",
            f"**Last Updated:** {timestamp}",
            content,
        )
        new_subsection = f"### {timestamp}\n\n{snapshot_body}\n"
        match = re.search(r"(## Recent Changes[^\n]*\n)", content)
        if match:
            heading_end = match.end()
            content = (
                content[:heading_end]
                + f"\n{new_subsection}\n"
                + content[heading_end:]
            )
        else:
            content = content + f"\n## Recent Changes\n\n{new_subsection}"
        return content

    return transform


# ── main ─────────────────────────────────────────────────────────────────


def _read_stdin_payload():
    """Best-effort parse of the hook stdin JSON. Returns dict (possibly empty)."""
    try:
        if sys.stdin.isatty():
            return {}
        raw = sys.stdin.read()
        if not raw:
            return {}
        return json.loads(raw)
    except Exception:
        return {}


def main():
    """Save context before compaction."""
    payload = _read_stdin_payload()
    transcript_path = payload.get("transcript_path")
    cwd = payload.get("cwd") or os.getcwd()
    session_id = payload.get("session_id") or os.environ.get("CLAUDE_SESSION_ID")

    try:
        from orbit_db import TaskDB  # type: ignore[import-not-found]
    except ImportError:
        # No orbit-db on sys.path - cannot resolve task. Bail quietly.
        return

    try:
        db = TaskDB()
    except Exception as e:
        _write_sticky_error(f"TaskDB init failed: {e}")
        print(f"orbit pre_compact: TaskDB init failed: {e}", file=sys.stderr)
        return

    task = _safe_db_call(
        "find_task_for_cwd", lambda: db.find_task_for_cwd(cwd, session_id)
    )
    if not task or not task.repo_id:
        return

    repo = _safe_db_call(
        "get_repo", lambda: db.get_repo(task.repo_id), task_name=task.name
    )
    if not repo:
        return

    task_dir = Path(repo.path) / task.full_path
    if not task_dir.exists():
        return

    context_file = None
    for cf in [task_dir / f"{task.name}-context.md", task_dir / "context.md"]:
        if cf.exists():
            context_file = cf
            break
    if not context_file:
        return

    # Build the snapshot from transcript
    user_prompts, assistant_replies = _read_recent_turns(transcript_path)
    snapshot_body = _build_snapshot_body(user_prompts, assistant_replies)

    # Atomic write to context.md. Stamp the timestamp at the actual write
    # site so we record the moment the snapshot landed, not whenever the
    # hook started.
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        _atomic_update_text(
            context_file, _make_transform(timestamp, snapshot_body)
        )
    except Exception as e:
        _write_sticky_error(
            f"context.md write failed: {e}", task_name=task.name
        )
        print(f"orbit pre_compact write error: {e}", file=sys.stderr)
        return

    # Aggregate heartbeats. Failure here is benign (heartbeats stay queued
    # for the next aggregation pass) so we suppress the sticky error - the
    # snapshot is the load-bearing thing and it already landed.
    _safe_db_call(
        "process_heartbeats",
        lambda: db.process_heartbeats(),
        task_name=task.name,
        sticky=False,
    )

    # Clear any prior sticky error - this run succeeded
    _clear_sticky_error()

    print(f"Pre-compact snapshot saved for task: {task.name}", file=sys.stderr)


if __name__ == "__main__":
    main()

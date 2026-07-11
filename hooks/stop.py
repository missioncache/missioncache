#!/usr/bin/env python3
"""
Stop hook - Remind about MissionCache updates if files were modified.

Checks if code files were edited during the session and reminds
to update MissionCache files if working on an active project.
"""

import json
import os
import sys
from pathlib import Path

# Bundled missioncache-db path for marketplace installs (no system pip install).
_BUNDLED_MISSIONCACHE_DB = Path(__file__).resolve().parent.parent / "missioncache-db"
if _BUNDLED_MISSIONCACHE_DB.is_dir() and str(_BUNDLED_MISSIONCACHE_DB) not in sys.path:
    sys.path.insert(0, str(_BUNDLED_MISSIONCACHE_DB))

# File-editing tools whose presence in the transcript should trigger the
# /missioncache:save reminder.
EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


def _transcript_has_edits(transcript):
    """Return True if the session transcript contains a file-editing tool_use.

    Streams the JSONL transcript and inspects each assistant message's content
    blocks for a ``tool_use`` whose name is a Write/Edit-family tool. Claude
    Code writes transcripts as compact JSON (no space after the colon), so a
    substring match on spaced literals never fires; parsing is spacing-agnostic
    and stops at the first hit, so large transcripts are not read fully into
    memory.
    """
    try:
        with transcript.open() as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                content = (rec.get("message") or {}).get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "tool_use"
                        and block.get("name") in EDIT_TOOLS
                    ):
                        return True
    except Exception:
        return False
    return False


def main():
    """Check if MissionCache update reminder is needed."""
    try:
        # Read the hook input from stdin
        input_data = json.loads(sys.stdin.read())

        # Check if any code files were edited
        transcript_path = input_data.get("transcript_path")
        if not transcript_path:
            return

        transcript = Path(transcript_path)
        if not transcript.exists():
            return

        # Check the transcript for Write/Edit tool uses.
        if not _transcript_has_edits(transcript):
            return

        # Check for active task
        from missioncache_db import TaskDB  # type: ignore[import-not-found]

        db = TaskDB()
        cwd = input_data.get("cwd", os.getcwd())
        session_id = input_data.get("session_id")

        task = db.find_task_for_cwd(cwd, session_id)

        if not task:
            return

        # Check if MissionCache files exist under centralized location
        if not task.full_path:
            return

        from missioncache_db import MISSIONCACHE_ROOT

        task_dir = MISSIONCACHE_ROOT / task.full_path
        has_missioncache_files = task_dir.exists() and any(
            (task_dir / f).exists()
            for f in [
                f"{task.name}-context.md",
                f"{task.name}-tasks.md",
                "context.md",
                "tasks.md",
            ]
        )

        if has_missioncache_files:
            # Output reminder (stderr shows to user)
            print(
                f"""
---
**MissionCache Reminder:** You made file edits while working on **{task.name}**.
Consider running `/missioncache:save` to save context before ending your session.
---
""",
                file=sys.stderr,
            )

    except Exception as e:
        # Don't fail the stop event
        pass


if __name__ == "__main__":
    main()

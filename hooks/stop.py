#!/usr/bin/env python3
"""
Stop hook - Remind about orbit updates if files were modified.

Checks if code files were edited during the session and reminds
to update orbit files if working on an active project.
"""

import json
import os
import sys
from pathlib import Path


def main():
    """Check if orbit update reminder is needed."""
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

        # Read transcript to check for Write/Edit tool uses
        transcript_content = transcript.read_text()

        # Simple check for file modifications (Write or Edit tools)
        has_edits = '"tool_use"' in transcript_content and (
            '"name": "Write"' in transcript_content
            or '"name": "Edit"' in transcript_content
        )

        if not has_edits:
            return

        # Check for active task
        from orbit_db import TaskDB

        db = TaskDB()
        cwd = input_data.get("cwd", os.getcwd())
        session_id = input_data.get("session_id")

        task = db.find_task_for_cwd(cwd, session_id)

        if not task:
            return

        # Check if orbit files exist under centralized location
        if not task.full_path:
            return

        orbit_root = Path.home() / ".claude" / "orbit"
        task_dir = orbit_root / task.full_path
        has_orbit_files = task_dir.exists() and any(
            (task_dir / f).exists()
            for f in [
                f"{task.name}-context.md",
                f"{task.name}-tasks.md",
                "context.md",
                "tasks.md",
            ]
        )

        if has_orbit_files:
            # Output reminder (stderr shows to user)
            print(
                f"""
---
**Orbit Reminder:** You made file edits while working on **{task.name}**.
Consider running `/orbit:save` to save context before ending your session.
---
""",
                file=sys.stderr,
            )

    except Exception as e:
        # Don't fail the stop event
        pass


if __name__ == "__main__":
    main()

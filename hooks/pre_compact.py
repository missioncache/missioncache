#!/usr/bin/env python3
"""
PreCompact hook - Auto-save context before compaction.

Attempts to preserve task context by writing to context.md before
automatic compaction occurs.
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path


def main():
    """Save context before compaction."""
    try:
        from orbit_db import TaskDB

        db = TaskDB()
        cwd = os.getcwd()
        session_id = os.environ.get("CLAUDE_SESSION_ID")

        # Find task for current directory
        task = db.find_task_for_cwd(cwd, session_id)

        if not task:
            return

        # Get repo path
        if not task.repo_id:
            return

        repo = db.get_repo(task.repo_id)
        if not repo:
            return

        task_dir = Path(repo.path) / task.full_path
        if not task_dir.exists():
            return

        # Find context file
        context_files = [
            task_dir / f"{task.name}-context.md",
            task_dir / "context.md",
        ]

        context_file = None
        for cf in context_files:
            if cf.exists():
                context_file = cf
                break

        if not context_file:
            return

        # Read current content
        content = context_file.read_text()

        # Update timestamp
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        import re

        content = re.sub(
            r"\*\*Last Updated:\*\* .+",
            f"**Last Updated:** {timestamp}",
            content,
        )

        # Add compaction note if Recent Changes section exists
        compaction_note = f"- Auto-saved before compaction ({timestamp})"

        if "## Recent Changes" in content:
            # Add to existing section
            content = re.sub(
                r"(## Recent Changes[^\n]*\n)",
                f"\\1{compaction_note}\n",
                content,
            )
        else:
            # Add section at end
            content += f"\n## Recent Changes ({timestamp})\n\n{compaction_note}\n"

        # Write updated content
        context_file.write_text(content)

        # Process heartbeats to aggregate time
        db.process_heartbeats()

        # Output confirmation
        print(f"Auto-saved context for task: {task.name}", file=sys.stderr)

    except ImportError:
        pass
    except Exception as e:
        print(f"orbit pre_compact error: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()

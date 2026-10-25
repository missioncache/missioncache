#!/usr/bin/env python3
"""
SessionEnd hook - close the session's row in the resume lists.

It refreshes the row first, while Claude Code's own record of the process
still exists, then closes it, so the session shows up in Saved or Closed
straight away. Claude Code gives every SessionEnd hook together 1.5 s by
default (the user can raise it, a plugin cannot), and nothing promises the
event fires when a tab is closed, so this is only the fast path: the next read of the lists closes any
session that ended without it (missioncache_db.resume_sessions.reconcile).
"""

import json
import sys
from pathlib import Path

# Bundled missioncache-db path for marketplace installs (no system pip install).
_BUNDLED_MISSIONCACHE_DB = Path(__file__).resolve().parent.parent / "missioncache-db"
if _BUNDLED_MISSIONCACHE_DB.is_dir() and str(_BUNDLED_MISSIONCACHE_DB) not in sys.path:
    sys.path.insert(0, str(_BUNDLED_MISSIONCACHE_DB))


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError, OSError):
        return
    if not isinstance(data, dict) or data.get("agent_id"):
        return
    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return
    reason = data.get("reason")
    try:
        from missioncache_db import resume_sessions  # type: ignore[import-not-found]

        resume_sessions.hook_record(
            session_id,
            data.get("cwd"),
            data.get("transcript_path"),
            close_reason=reason if isinstance(reason, str) and reason else "other",
        )
    except Exception:
        # Never raise across the hook boundary: reconcile closes the row later.
        pass


if __name__ == "__main__":
    main()

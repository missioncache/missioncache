#!/usr/bin/env python3
"""
UserPromptSubmit hook - name the Claude Code session after its MissionCache project.

Claude Code's cross-session `SendMessage` addresses a peer by its session title,
so the title is the address. Left to Claude Code, the title is derived from the
session's first prompt, which means a session working on `avc-in-house-testing`
can be called anything at all. Nothing can then reliably message the session that
owns a given project.

This hook sets the title from the project the session is explicitly bound to, so
`missioncache_db.live_sessions_for_project` can hand a caller a name that
`SendMessage` will actually resolve.

Three behaviours worth knowing:

1. It reads the binding from `project_state`, the same store the live-session
   lookup reads, so "titled", "addressable" and "notified" describe one set of
   sessions. The `projects/<sid>.json` pointer is the wrong source here - it is
   also written from cwd auto-resolution at SessionStart, so merely opening a
   project's repo would claim the project's title.
2. It re-emits only when the computed address changes: on a rebind, on a new
   collision, or when a stale suffix can drop back to the plain name after a
   peer dies. In the steady state it stays silent, which is what lets a manual
   `/rename` survive day to day (see `resolve_title` for the accepted narrow
   clobber case).
3. It runs on every prompt, including slash commands. It deliberately does NOT
   copy the `SKIP_PATTERNS` guard from the other two UserPromptSubmit hooks -
   that guard keeps `activity_tracker`'s heartbeat time honest, and copying it
   here would delay every retitle to the first non-slash prompt.
"""

import json
import sys
from pathlib import Path

# Bundled missioncache-db path for marketplace installs (no system pip install).
# Path segment tracks the in-repo package dir.
_BUNDLED_MISSIONCACHE_DB = Path(__file__).resolve().parent.parent / "missioncache-db"
if _BUNDLED_MISSIONCACHE_DB.is_dir() and str(_BUNDLED_MISSIONCACHE_DB) not in sys.path:
    sys.path.insert(0, str(_BUNDLED_MISSIONCACHE_DB))


def resolve_title(session_id: str) -> tuple[str, str] | None:
    """``(title, project_name)`` to apply to ``session_id``, or None for nothing.

    The title is recomputed every prompt and emitted only when it DIFFERS from
    what this hook last applied. That one comparison carries three behaviours:

    * Steady state: same project, same peers -> the computed title equals the
      recorded one -> silent. A manual ``/rename`` therefore survives, because
      nothing is re-asserted while the environment is unchanged.
    * Rebind: ``/missioncache:load other-project`` changes the computation ->
      re-title.
    * Self-heal: a session left holding ``<project>-2`` after the peer that
      owned the plain name died re-titles to the plain name on its next prompt.
      Without this, suffixes only ever accumulate (a real machine reached ``-3``
      with zero live peers) and the recorded address stops matching any
      ``ListAgents`` row, which the send protocol reads as "session gone".

    The narrow cost: a manual ``/rename`` is re-overwritten when the peer set
    changes (a collision appears or a suffix frees up), not only on rebind.
    That trade was accepted because an unaddressable session fails every notify,
    while a clobbered rename costs one repeated ``/rename``.
    """
    from missioncache_db import (  # type: ignore[import-not-found]
        bound_project_for_session,
        choose_session_title,
        read_session_title,
    )

    project_name = bound_project_for_session(session_id)
    if not project_name:
        return None

    computed = choose_session_title(project_name, session_id)
    applied = read_session_title(session_id) or {}
    if (
        applied.get("projectName") == project_name
        and applied.get("title") == computed
    ):
        return None

    return computed, project_name


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        return
    if not isinstance(data, dict):
        # Valid JSON that is not an object (`[]`, `"x"`, `null`). Without this
        # the `.get` below raises AttributeError straight across the hook
        # boundary this file promises never to cross.
        return

    # Skip in subagent context - a subagent is not a session anyone addresses,
    # and it would otherwise rename the parent it runs under.
    if data.get("agent_id"):
        return

    session_id = data.get("session_id", "")
    if not session_id or not isinstance(session_id, str):
        return

    try:
        from missioncache_db import write_session_title  # type: ignore[import-not-found]

        resolved = resolve_title(session_id)
        if resolved is None:
            return
        title, project_name = resolved

        write_session_title(session_id, title, project_name)
        # Record before emitting: a title we announced but did not record would
        # be re-picked next prompt, and a second live session would meanwhile
        # see the name as free and collide with it.
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "UserPromptSubmit",
                        "sessionTitle": title,
                    }
                }
            )
        )
    except Exception:
        # Never raise across the hook boundary. A session that keeps its
        # first-prompt title is only unaddressable by name; the caller falls
        # back to asking the user which session to message.
        pass


if __name__ == "__main__":
    main()

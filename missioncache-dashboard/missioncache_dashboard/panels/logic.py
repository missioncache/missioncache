"""The parts of the terminal panels that need no terminal: when a session counts
as changed, what a row is called, the row order and stepping through it.

Activity in a Claude Code session is a change in its status (working,
waiting, done, error, idle): on iTerm2 the one its Claude Code integration
keeps, on Windows the one the MissionCache mod writes to ``agent_status``,
and on both a busy/idle change in Claude Code's own
``~/.claude/sessions/<pid>.json``.
Activity in a plain shell is the last input it read (the tty's atime) or,
while a command runs, its last output (the tty's mtime). Focusing a tab only
marks it as seen. A session that finished, needs input or failed after its
last focus is unseen.
"""

import re
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from typing import Any

UNSEEN_STATUSES = {"done", "waiting", "error"}
SHELLS = {"zsh", "bash", "fish", "sh", "login"}
# Full-screen programs that redraw on their own, so their output is not activity.
SELF_REDRAWING = {"k9s", "htop", "top", "btop", "watch", "tmux", "less", "man"}
TITLE_GLYPH_RE = re.compile(r"^[^\w~./-]+\s+")  # Claude Code's "✳ " / "◑ " title prefix
CLAUDE_DEFAULT_TITLE = "Claude Code"  # the tab title before the first prompt
LEAD_NAME = "missioncache-lead"
# The mod's states in the panels' words. Every idle the mod writes ends a turn.
AGENT_STATUS = {"working": "working", "compacting": "working", "needs_you": "waiting", "idle": "done"}


def bare_title(title: str) -> str:
    return TITLE_GLYPH_RE.sub("", title)


def is_claude_title(title: str) -> bool:
    return bool(TITLE_GLYPH_RE.match(title)) or title == CLAUDE_DEFAULT_TITLE


def shell_activity(atime: float, mtime: float, job: str | None) -> float:
    """A plain shell's last activity. At its prompt the only output is the
    shell redrawing the prompt, and full-screen programs redraw on their own,
    so only output from other commands counts."""
    quiet = job in SHELLS or job in SELF_REDRAWING
    return atime if quiet else max(atime, mtime)


def effective_status(iterm_status: str | None, own: dict[str, Any] | None) -> str | None:
    """iTerm2 sometimes keeps "working" after a turn ends. When Claude Code's
    own record says idle, the session is done."""
    if iterm_status == "working" and own and own.get("status") == "idle":
        return "done"
    return iterm_status


def claude_status(agent: dict[str, Any] | None, own: dict[str, Any]) -> tuple[str, float]:
    """A session's status and when it changed, from the mod's ``agent_status``
    row and Claude Code's own record. The mod's is the precise one. Claude
    Code's busy/idle wins when it is newer: busy is working, and idle after
    working or waiting means the turn ended without the mod seeing it."""
    own_at = (own.get("statusUpdatedAt") or 0) / 1000
    status = AGENT_STATUS.get(agent["state"]) if agent else None
    if agent:
        # The format missioncache_db.lead_board writes (local time).
        at = datetime.strptime(agent["updated_at"], "%Y-%m-%d %H:%M:%S").timestamp()
        # The row keeps whole seconds, the record milliseconds.
        if status and at >= own_at - 1:
            return status, at
    if own.get("status") == "busy":
        return "working", own_at
    if status in ("working", "waiting"):
        return "done", own_at
    return status or "idle", own_at


def join_claude(tabs: list[tuple[str, str]], claude: list[dict[str, Any]],
                mc_titles: dict[str, str],
                transcript_title: Callable[[dict[str, Any]], str | None]) -> dict[str, dict[str, Any]]:
    """row id -> the Claude Code session in that tab, from ``(row id, tab
    title)`` pairs: the tab titled with the session's name (its MissionCache
    title or its own), then the tab with the title from its transcript, then,
    if exactly one Claude tab and one session without a transcript title are
    left, those two. Where nothing ties a process to a tab, the title is the
    only join, so every step joins only when the match is the only one: a
    wrong join would focus, star or resume the other session."""
    joined, sessions = {}, list(claude)
    free = list(tabs)

    def take(match: str, c: dict[str, Any]) -> None:
        nonlocal free
        joined[match] = c
        sessions.remove(c)
        free = [f for f in free if f[0] != match]

    # A tab Open made keeps its title after Claude Code exits, so a plain
    # shell can carry the same name: a tab with Claude Code's glyph wins.
    wanted: dict[str, list[str]] = {}
    for c in claude:
        names = {c.get("name"), mc_titles.get(c["sessionId"])} - {None, ""}
        named = [(rid, title) for rid, title in free if bare_title(title) in names]
        wanted[c["sessionId"]] = [rid for rid, title in named if is_claude_title(title)] or [rid for rid, _ in named]
    claims = Counter(rid for rids in wanted.values() for rid in rids)
    for c in claude:
        rids = wanted[c["sessionId"]]
        if len(rids) == 1 and claims[rids[0]] == 1:
            take(rids[0], c)
    # The transcript is read only for the sessions their names did not join.
    # Two sessions can get the same generated title.
    titled = [(c, title) for c in sessions if (title := transcript_title(c))]
    title_counts = Counter(title for _, title in titled)
    for c, title in titled:
        same_tabs = [rid for rid, t in free if is_claude_title(t) and bare_title(t) == title]
        if len(same_tabs) == 1 and title_counts[title] == 1:
            take(same_tabs[0], c)
    # A session with a title that matched no tab is somewhere else (another
    # terminal, an editor), so only one with no title yet is left to join.
    untitled = [c for c in sessions if all(c is not t for t, _ in titled)]
    left = [rid for rid, title in free if is_claude_title(title)]
    if len(sessions) == 1 and len(untitled) == 1 and len(left) == 1:
        joined[left[0]] = sessions[0]
    return joined


def advance(entry: dict[str, Any] | None, status: str | None, activity: float,
            own_change: float | None, now: float) -> dict[str, Any]:
    """The entry after one poll. A new entry counts as seen, since whether it
    was is unknown."""
    if entry is None:
        return {"status": status, "changed": activity, "focused": activity}
    if entry["status"] != status:
        # iTerm2 sometimes resets several finished sessions to idle at once.
        # Going idle is not something that happened in the session.
        if status != "idle":
            entry["changed"] = now
        entry["status"] = status
    else:
        # Same status, but a shell had input or output, or Claude Code went
        # busy and idle again between two polls.
        entry["changed"] = max(entry["changed"], activity if status is None else own_change or 0)
    return entry


def row_name(tab_title: str, own: dict[str, Any] | None, mc_title: str | None) -> str:
    """The MissionCache session title when the session has one, else the tab
    title, where Claude Code shows its session title after the first prompt
    and after /rename. Before the first prompt the tab only says "Claude
    Code", so Claude Code's own generated name stands in until then."""
    name = bare_title(tab_title)
    if own and name == CLAUDE_DEFAULT_TITLE:
        name = own.get("name") or name
    return mc_title or name


def lead_mark(name: str | None, live_lead_title: str | None) -> str | None:
    """"live" for the session MissionCache counts as its live lead, "off" for
    a session named missioncache-lead that is not receiving updates."""
    if live_lead_title and name == live_lead_title:
        return "live"
    return "off" if name == LEAD_NAME else None


def is_unseen(entry: dict[str, Any]) -> bool:
    return entry["changed"] > entry["focused"] and entry["status"] in UNSEEN_STATUSES


def order_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Newest change first, with the lead pinned on top: it updates in a loop,
    so sorting it by time would only push the sessions that changed down."""
    return sorted(rows, key=lambda r: (r.get("lead") is not None, r["changed"]), reverse=True)


def step_target(order: list[str], current: str | None, offset: int) -> str | None:
    """The session ``offset`` rows from the current one, wrapping at the ends.
    From a session not in the list, the first row."""
    if not order:
        return None
    index = order.index(current) + offset if current in order else 0
    return order[index % len(order)]

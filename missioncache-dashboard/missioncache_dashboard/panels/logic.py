"""The parts of the terminal panels that need no terminal: when a session counts
as changed, what a row is called, the row order and stepping through it.

Activity in a Claude Code session is a change in the status iTerm2's Claude
Code integration keeps per session (working, waiting, done, error, idle), or
a busy/idle change in Claude Code's own ``~/.claude/sessions/<pid>.json``.
Activity in a plain shell is the last input it read (the tty's atime) or,
while a command runs, its last output (the tty's mtime). Focusing a tab only
marks it as seen. A session that finished, needs input or failed after its
last focus is unseen.
"""

import re
from typing import Any

UNSEEN_STATUSES = {"done", "waiting", "error"}
SHELLS = {"zsh", "bash", "fish", "sh", "login"}
# Full-screen programs that redraw on their own, so their output is not activity.
SELF_REDRAWING = {"k9s", "htop", "top", "btop", "watch", "tmux", "less", "man"}
TITLE_GLYPH_RE = re.compile(r"^[^\w~./-]+\s+")  # Claude Code's "✳ " / "◑ " title prefix
CLAUDE_DEFAULT_TITLE = "Claude Code"  # the tab title before the first prompt
LEAD_NAME = "missioncache-lead"


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
    name = TITLE_GLYPH_RE.sub("", tab_title)
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

#!/usr/bin/env python3
"""Claude Code Status Line.

Reads JSON from stdin (Claude Code session data) and outputs
a multi-line ANSI-colored status display.

Layout:
  Line 1: Project    - [project name] (only if active orbit project)
  Line 2: Location   - [dir] [git branch+status]
  Line 3: Metrics    - [model] [tokens] [ctx%]
  Line 4: Session    - [elapsed] [edits]
  Line 5: K8s/Ver    - [k8s context] [version] [health status]
  Line 6: Usage      - [mode] [session%] [weekly%] [opus%]
  Line 7: Codex      - [plan] [5h%] [weekly%] (only if codex installed)

Configuration:
  All visibility toggles (Codex line, Claude subscription usage/type, Claude
  status, status service filter) are managed through the orbit dashboard
  Settings screen. The statusline reads them from
  ~/.claude/orbit-dashboard-config.json on each invocation. Defaults apply
  when the file or its `statusline` section is missing.
"""

import base64
import json
import os
import platform
import re
import sqlite3
import subprocess
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path
from typing import NamedTuple

IS_MACOS = platform.system() == "Darwin"

# ============ STDERR SUPPRESSION ============
try:
    _devnull_fd = os.open(os.devnull, os.O_WRONLY)
    os.dup2(_devnull_fd, 2)
    os.close(_devnull_fd)
except OSError:
    pass

# ============ CONSTANTS ============

ESC = "\033"
RESET = f"{ESC}[0m"

COLORS = {
    "dir": f"{ESC}[38;2;180;140;100m",
    "git_clean": f"{ESC}[38;2;80;200;120m",
    "git_dirty": f"{ESC}[38;2;220;180;50m",
    "project": f"{ESC}[38;2;80;200;120m",
    "k8s": f"{ESC}[38;2;150;120;180m",
    "model": f"{ESC}[38;2;180;130;200m",
    "tokens": f"{ESC}[38;2;100;200;200m",
    "ctx": f"{ESC}[38;2;160;170;190m",
    "ctx_warn": f"{ESC}[38;2;220;180;50m",
    "ctx_urgent": f"{ESC}[38;2;255;109;0m",
    "ctx_est": f"{ESC}[38;2;100;150;220m",
    "time": f"{ESC}[38;2;100;180;180m",
    "edit": f"{ESC}[38;2;200;160;120m",
    "datetime": f"{ESC}[38;2;160;160;180m",
    "version": f"{ESC}[38;2;130;180;220m",
    "pipe": f"{ESC}[38;2;100;100;110m",
    "session_usage": f"{ESC}[38;2;100;160;200m",
    "weekly_usage": f"{ESC}[38;2;160;130;190m",
    "opus_usage": f"{ESC}[38;2;200;160;120m",
    "reset_time": f"{ESC}[38;2;120;120;130m",
    "mode_personal": f"{ESC}[38;2;80;200;120m",
    "mode_work": f"{ESC}[38;2;100;150;220m",
    "mode_free": f"{ESC}[38;2;140;140;150m",
    "health_ok": f"{ESC}[38;2;0;200;83m",
    "health_degraded": f"{ESC}[38;2;255;214;0m",
    "health_partial": f"{ESC}[38;2;255;109;0m",
    "health_resolved": f"{ESC}[38;2;100;180;100m",
    "codex_label": f"{ESC}[38;2;16;163;127m",
    "codex_session": f"{ESC}[38;2;100;200;170m",
    "codex_weekly": f"{ESC}[38;2;160;130;190m",
    "extra_usage": f"{ESC}[38;2;220;170;80m",
    "fast_mode": f"{ESC}[38;2;255;120;20m",
    "upgrade": f"{ESC}[38;2;255;180;60m",
}

ICONS = {
    "dir": "\U0001f4c1",
    "git": "\U0001f500",
    "project": "\U0001f4cb",
    "k8s": "\u2638\ufe0f",
    "model": "\U0001f916",
    "tokens": "\U0001f522",
    "context": "\U0001f4ca",
    "duration": "\u23f1\ufe0f",
    "edit": "\u270f\ufe0f",
    "datetime": "\U0001f550",
    "week": "\U0001f4c5",
    "reset": "\U0001f504",
    "version": "\U0001f4e6",
    "health_ok": "\u2705",
    "health_degraded": "\u26a0\ufe0f",
    "health_partial": "\U0001f7e1",
    "extra": "\U0001f4b3",
}

PIPE = f"  {COLORS['pipe']}\u2502{RESET}  "


SYSTEM_OVERHEAD_PERCENT = 19
CELL_WIDTH = 24

STATE_DIR = Path.home() / ".claude" / "hooks" / "state"
HOOKS_STATE_DB = Path.home() / ".claude" / "hooks-state.db"
SCRIPTS_DIR = Path.home() / ".claude" / "scripts"
SETTINGS_FILE = Path.home() / ".claude" / "settings.json"
ORBIT_ACTIVE = Path.home() / ".claude" / "orbit" / "active"


def _get_hooks_db() -> sqlite3.Connection | None:
    """Get hooks-state DB connection. Returns None if DB doesn't exist."""
    if not HOOKS_STATE_DB.exists():
        return None
    try:
        db = sqlite3.connect(str(HOOKS_STATE_DB), timeout=1)
        db.row_factory = sqlite3.Row
        return db
    except sqlite3.Error:
        return None

HEALTH_CACHE = SCRIPTS_DIR / "health-cache.json"
HEALTH_TTL = 180
HEALTH_URL = "https://status.claude.com/api/v2/incidents.json"

_ALL_HEALTH_COMPONENTS = {
    "yyzkbfz2thpt": "Code",
    "rwppv331jlwc": "claude.ai",
    "k8w3r06qmzrp": "Claude API",
    "0qbwn08sd68x": "platform.claude.com",
    "0scnb50nvy53": "Claude for Government",
    "bpp5gb3hpjcl": "Claude Cowork",
}

_DASHBOARD_CONFIG_FILE = Path.home() / ".claude" / "orbit-dashboard-config.json"
_DEFAULT_STATUSLINE_CONFIG = {
    "codex": True,
    "subscription_usage": True,
    "subscription_type": True,
    "claude_status": True,
    "claude_status_services": ["Code", "Claude API"],
}


def _load_statusline_config() -> dict:
    """Read statusline visibility config from the dashboard config file.

    A missing file, bad JSON, or missing `statusline` section all fall back
    to defaults - the statusline must keep rendering even without a dashboard.
    """
    try:
        data = json.loads(_DASHBOARD_CONFIG_FILE.read_text())
    except Exception:
        return dict(_DEFAULT_STATUSLINE_CONFIG)
    section = data.get("statusline")
    if not isinstance(section, dict):
        return dict(_DEFAULT_STATUSLINE_CONFIG)
    merged = dict(_DEFAULT_STATUSLINE_CONFIG)
    for k in _DEFAULT_STATUSLINE_CONFIG:
        if k in section:
            merged[k] = section[k]
    return merged


STATUSLINE_CONFIG = _load_statusline_config()

HEALTH_COMPONENTS = {
    cid: name
    for cid, name in _ALL_HEALTH_COMPONENTS.items()
    if name in set(STATUSLINE_CONFIG["claude_status_services"])
}

USAGE_CACHE = SCRIPTS_DIR / "usage-cache.json"
USAGE_TTL = 300
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

CODEX_USAGE_CACHE = SCRIPTS_DIR / "codex-usage-cache.json"
CODEX_USAGE_TTL = 300
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CODEX_AUTH_FILE = Path.home() / ".codex" / "auth.json"
CODEX_ENABLED = STATUSLINE_CONFIG["codex"]


# ============ DISPLAY WIDTH ============

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mK]|\x1b\][^\x07]*\x07|\x1b\]8;[^\x1b]*\x1b\\")

_EMOJI_RANGES = [
    (0x1F300, 0x1F9FF),
    (0x2600, 0x26FF),
    (0x2700, 0x27BF),
    (0x1F600, 0x1F64F),
    (0x1F680, 0x1F6FF),
    (0x1F1E0, 0x1F1FF),
]

_EMOJI_SINGLES = frozenset({
    0x231A, 0x231B, 0x23E9, 0x23EA, 0x23EB, 0x23EC, 0x23F0, 0x23F3,
    0x25AA, 0x25AB, 0x25B6, 0x25C0, 0x25FB, 0x25FC, 0x25FD, 0x25FE,
    0x2614, 0x2615, 0x2648, 0x2649, 0x264A, 0x264B, 0x264C, 0x264D,
    0x264E, 0x264F, 0x2650, 0x2651, 0x2652, 0x2653, 0x267F, 0x2693,
    0x26A1, 0x26AA, 0x26AB, 0x26BD, 0x26BE, 0x26C4, 0x26C5, 0x26CE,
    0x26D4, 0x26EA, 0x26F2, 0x26F3, 0x26F5, 0x26FA, 0x26FD, 0x2702,
    0x2705, 0x2708, 0x2709, 0x270A, 0x270B, 0x270C, 0x270D, 0x270F,
    0x2712, 0x2714, 0x2716, 0x271D, 0x2721, 0x2728, 0x2733, 0x2734,
    0x2744, 0x2747, 0x274C, 0x274E, 0x2753, 0x2754, 0x2755, 0x2757,
    0x2763, 0x2764, 0x2795, 0x2796, 0x2797, 0x27A1, 0x27B0, 0x27BF,
    0x2934, 0x2935, 0x2B05, 0x2B06, 0x2B07, 0x2B1B, 0x2B1C, 0x2B50,
    0x2B55, 0x3030, 0x303D, 0x3297, 0x3299,
})


def display_width(s: str) -> int:
    """Calculate display width accounting for ANSI codes, emoji, and CJK."""
    s = _ANSI_RE.sub("", s)
    width = 0
    i = 0
    n = len(s)
    while i < n:
        cp = ord(s[i])
        if cp == 0x200D:
            i += 1
            continue
        has_vs16 = i + 1 < n and ord(s[i + 1]) == 0xFE0F
        if cp in (0xFE0E, 0xFE0F):
            i += 1
            continue
        is_emoji = (
            any(lo <= cp <= hi for lo, hi in _EMOJI_RANGES)
            or has_vs16
            or cp in _EMOJI_SINGLES
        )
        if is_emoji:
            width += 2
        elif unicodedata.east_asian_width(s[i]) in ("W", "F"):
            width += 2
        else:
            width += 1
        i += 1
    return width


# ============ HELPERS ============

def run_cmd(cmd: list[str], timeout: int = 5) -> str | None:
    """Run a command, return stdout stripped or None on failure."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def _relative_time(iso_ts: str) -> str:
    """Convert ISO timestamp to relative time string."""
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        secs = int((datetime.now(timezone.utc) - dt).total_seconds())
        if secs < 60:
            return f"{secs}s ago"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"
    except Exception:
        return ""


def _format_reset_time(iso_ts: str) -> str:
    """Format ISO timestamp to compact 'thu 11am' format."""
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%a %-I%p").lower()
    except Exception:
        return "?"


def _format_unix_reset(ts) -> str:
    """Format unix timestamp to compact 'thu 11am' format."""
    try:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.astimezone().strftime("%a %-I%p").lower()
    except Exception:
        return "?"


def _parse_extra_usage(extra: dict | None) -> dict | None:
    """Parse extra_usage block into display values. Returns None if disabled."""
    if not extra or not extra.get("is_enabled"):
        return None
    monthly_limit = extra.get("monthly_limit", 0)
    if monthly_limit <= 0:
        return None
    used_credits = extra.get("used_credits") or 0.0
    used_dollars = used_credits / 100
    limit_dollars = monthly_limit / 100

    utilization = extra.get("utilization")
    if utilization is not None:
        used_pct = int(utilization)
    elif used_credits == 0:
        used_pct = 0
    else:
        used_pct = min(int((used_credits / monthly_limit) * 100), 100)

    today = date.today()
    if today.month == 12:
        reset_date = date(today.year + 1, 1, 1)
    else:
        reset_date = date(today.year, today.month + 1, 1)
    reset_str = reset_date.strftime("%b %-d").lower()

    fmt = lambda d: f"${d:.0f}" if d == int(d) else f"${d:.2f}"
    return {
        "extra_spent": fmt(used_dollars),
        "extra_limit": fmt(limit_dollars),
        "extra_pct": str(used_pct),
        "extra_reset": reset_str,
    }


def _parse_stdin_rate_limits(rate_limits: dict) -> dict:
    """Parse rate_limits from statusline stdin JSON (different field names than API)."""
    if not rate_limits:
        return {"is_max": True}

    result: dict = {}
    if rate_limits.get("five_hour") is not None:
        fh = rate_limits["five_hour"]
        result["session_pct"] = str(int(fh.get("used_percentage", 0)))
        result["session_reset"] = _format_unix_reset(fh.get("resets_at"))
    if rate_limits.get("seven_day") is not None:
        sd = rate_limits["seven_day"]
        result["weekly_pct"] = str(int(sd.get("used_percentage", 0)))
        result["weekly_reset"] = _format_unix_reset(sd.get("resets_at"))
    if rate_limits.get("seven_day_opus") is not None:
        opus_pct = int(rate_limits["seven_day_opus"].get("used_percentage", 0))
        if opus_pct > 0:
            result["opus_pct"] = str(opus_pct)
    return result


# ============ INPUT PARSING ============

def _fmt_token_count(n: int) -> str:
    """Format a token count with K/M suffixes to match the legacy tokens_str style."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def parse_input(raw: str) -> dict:
    """Parse Claude Code JSON input and extract display values."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        data = {}

    model_name = data.get("model", {}).get("display_name", "Claude")

    ctx = data.get("context_window", {})
    ctx_size = ctx.get("context_window_size", 200000)

    # Debug log (matches existing bash behavior)
    try:
        debug_file = STATE_DIR / "statusline-ctx-debug.log"
        debug_file.write_text(
            f"context_window keys: {list(ctx.keys())}\n"
            f"context_window: {json.dumps(ctx, indent=2)}\n"
            f"\nmodel object: {json.dumps(data.get('model', {}), indent=2)}\n"
            f"Full data keys: {list(data.keys())}\n"
            f"\ncost object: {json.dumps(data.get('cost', {}), indent=2)}\n"
        )
    except OSError:
        pass

    ctx_estimated = False
    if ctx.get("used_percentage") is not None:
        ctx_percent = min(int(ctx["used_percentage"]) + SYSTEM_OVERHEAD_PERCENT, 100)
    else:
        ctx_estimated = True
        cur = ctx.get("current_usage") or {}
        base = (cur.get("input_tokens", 0) + cur.get("cache_creation_input_tokens", 0)
                + cur.get("cache_read_input_tokens", 0) + cur.get("output_tokens", 0))
        current_context = base + int(ctx_size * 0.19)
        ctx_percent = min(int((current_context / ctx_size) * 100) if ctx_size > 0 else 0, 100)

    input_total = ctx.get("total_input_tokens", 0)
    output_total = ctx.get("total_output_tokens", 0)
    tokens_str = f"\u2191{_fmt_token_count(input_total)}/\u2193{_fmt_token_count(output_total)}"

    cost_data = data.get("cost", {})
    duration_ms = cost_data.get("total_duration_ms", 0)
    duration_min = duration_ms // 60000
    duration_sec_rem = (duration_ms % 60000) // 1000
    if duration_min >= 60:
        duration_str = f"{duration_min // 60}h {duration_min % 60}m"
    else:
        duration_str = f"{duration_min}m {duration_sec_rem}s"

    session_cost = cost_data.get("total_cost_usd", 0)

    return {
        "model_name": model_name,
        "tokens_str": tokens_str,
        "ctx_percent": ctx_percent,
        "ctx_estimated": ctx_estimated,
        "duration_str": duration_str,
        "duration_sec": duration_ms // 1000,
        "session_id": data.get("session_id", ""),
        "cost_str": f"${session_cost:.2f}",
        "worktree": (data.get("workspace") or {}).get("git_worktree"),
        "rate_limits": data.get("rate_limits"),
        "running_version": data.get("version", "") or "",
    }


# ============ SESSION STATE ============

def update_session_state(session_id: str, ctx_percent: int, tokens_str: str) -> int:
    """Update session state in hooks-state DB. Returns edit count."""
    if not session_id:
        return 0
    edit_count = 0
    db = _get_hooks_db()
    if db:
        try:
            db.execute(
                """INSERT INTO session_state (session_id, context_percent, context_tokens, updated_at)
                   VALUES (?, ?, ?, datetime('now', 'localtime'))
                   ON CONFLICT(session_id) DO UPDATE SET
                     context_percent = ?,
                     context_tokens = ?,
                     updated_at = datetime('now', 'localtime')""",
                (session_id, ctx_percent, tokens_str, ctx_percent, tokens_str),
            )
            db.commit()
            row = db.execute(
                "SELECT edit_count FROM session_state WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row:
                edit_count = row["edit_count"] or 0
            db.close()
        except sqlite3.Error:
            pass
    return edit_count


def update_term_session(session_id: str) -> None:
    """Update terminal-to-session mapping."""
    term_id = os.environ.get("TERM_SESSION_ID") or os.environ.get("WT_SESSION", "")
    if not session_id or not term_id:
        return
    db = _get_hooks_db()
    if db:
        try:
            db.execute(
                """INSERT INTO term_sessions (term_session_id, session_id, updated_at)
                   VALUES (?, ?, datetime('now', 'localtime'))
                   ON CONFLICT(term_session_id) DO UPDATE SET
                     session_id = ?,
                     updated_at = datetime('now', 'localtime')""",
                (term_id, session_id, session_id),
            )
            db.commit()
            db.close()
        except sqlite3.Error:
            pass


# ============ PROJECT INFO ============

def _parse_task_progress(tasks_content: str) -> str:
    """Parse task progress from tasks.md content.

    Returns a bracket string to append to the project name:
      "[3/22]"  - normal fraction (completed / total checklist items)
      "[TBD]"   - no real tasks defined yet (empty file or only template placeholder)

    Counts ALL checklist items flatly, including nested subtasks, matching
    the reference implementation in mcp-server/src/mcp_orbit/orbit.py:407.
    """
    completed = len(
        re.findall(r"^\s*[-*]\s*\[x\]", tasks_content, re.MULTILINE | re.IGNORECASE)
    )
    pending_items = re.findall(
        r"^\s*[-*]\s*\[\s*\]\s*(.*)$", tasks_content, re.MULTILINE
    )
    pending = len(pending_items)
    total = completed + pending

    # Empty file or no checklists at all - defensive handling.
    if total == 0:
        return "[TBD]"

    # Template placeholder: single pending item with text exactly "TBD".
    if completed == 0 and pending == 1:
        if re.match(r"^\s*TBD\s*$", pending_items[0], re.IGNORECASE):
            return "[TBD]"

    return f"[{completed}/{total}]"


def _get_project_progress(project_dir: Path, project_name: str) -> str:
    """Read the tasks file in project_dir and return the progress bracket.

    Returns a leading-space-prefixed bracket ready for concatenation, e.g.
    " [3/22]" or " [TBD]". Returns "" if the tasks file is missing or
    unreadable (statusline falls back to showing just the project name).
    """
    tasks_file = project_dir / f"{project_name}-tasks.md"
    try:
        content = tasks_file.read_text()
    except OSError:
        return ""
    return f" {_parse_task_progress(content)}"


class ProjectInfo(NamedTuple):
    name: str = ""
    display: str = ""
    progress: str = ""


def get_project_info(session_id: str, duration_sec: int) -> ProjectInfo:
    """Return ProjectInfo(name, display, progress)."""
    if not session_id:
        return ProjectInfo()
    name = ""
    max_age = max(duration_sec + 60, 60)
    db = _get_hooks_db()
    if db:
        try:
            row = db.execute(
                "SELECT project_name, updated_at FROM project_state WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            db.close()
            if row and row["project_name"]:
                updated = datetime.fromisoformat(row["updated_at"])
                age = int((datetime.now() - updated).total_seconds())
                if age < 30 or age < max_age:
                    name = row["project_name"]
        except (sqlite3.Error, ValueError):
            pass
    if not name:
        return ProjectInfo()

    display = name
    project_dir = ORBIT_ACTIVE / name
    if ORBIT_ACTIVE.is_dir():
        if not project_dir.is_dir():
            for parent in ORBIT_ACTIVE.iterdir():
                nested = parent / name
                if parent.is_dir() and nested.is_dir():
                    display = f"{parent.name}/{name}"
                    project_dir = nested
                    break
    progress = _get_project_progress(project_dir, name)
    return ProjectInfo(name, display, progress)


# ============ LAST ACTION TIME ============

def get_last_action_time(session_id: str) -> str:
    """Return formatted time of last prompt for this session."""
    if not session_id:
        return ""
    db = _get_hooks_db()
    if db:
        try:
            row = db.execute(
                "SELECT last_prompt_at FROM session_state WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            db.close()
            if row and row["last_prompt_at"]:
                dt = datetime.fromisoformat(row["last_prompt_at"])
                return dt.strftime("%b %-d %H:%M")
        except (sqlite3.Error, KeyError, ValueError):
            pass
    return ""


# ============ GIT INFO ============

def get_git_info() -> tuple[str, str, bool]:
    """Return (repo_name, branch, is_dirty)."""
    if run_cmd(["git", "rev-parse", "--git-dir"]) is None:
        return "", "", False
    toplevel = run_cmd(["git", "rev-parse", "--show-toplevel"])
    repo_name = Path(toplevel).name if toplevel else ""
    branch = run_cmd(["git", "branch", "--show-current"]) or ""
    if not branch:
        branch = run_cmd(["git", "rev-parse", "--short", "HEAD"]) or ""
    porcelain = run_cmd(["git", "status", "--porcelain"])
    return repo_name, branch, bool(porcelain)


# ============ K8S CONTEXT ============

def get_k8s_context() -> str:
    """Return current K8s context name."""
    ctx = run_cmd(["kubectl", "config", "current-context"])
    return ctx or ""


# ============ VERSION INFO ============

def _parse_semver(version: str) -> tuple[int, ...]:
    """Parse a semver-ish string into a comparable int tuple. Non-numeric
    segments collapse to 0 so malformed input never crashes the comparison."""
    parts = []
    for part in version.split("."):
        try:
            parts.append(int(part))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def is_version_reviewed(version: str) -> bool:
    """Check if /whats-new has been run for this version or a later one.

    /whats-new is cumulative - reviewing the changelog at version N implies
    all prior versions' changelogs have been seen too. So we return True
    whenever the recorded reviewed version is >= `version`.
    """
    reviewed_file = Path.home() / ".claude" / "cache" / "whats-new-version"
    if not reviewed_file.exists():
        return False
    try:
        reviewed = reviewed_file.read_text().strip()
    except OSError:
        return False
    if not reviewed:
        return False
    return _parse_semver(reviewed) >= _parse_semver(version)


_LATEST_RELEASE_TTL = 21600  # 6 hours


def get_version_info(running: str) -> tuple[str, str]:
    """Return (running, latest_if_newer_age).

    - running: the running session's version, passed in from the stdin
      `version` field. This is the version actually executing in the current
      Claude Code process - distinct from `claude --version`, which reports
      the on-disk binary (potentially already auto-updated to a newer tag).
    - latest_if_newer_age: "v2.1.114 (2d)"-style string when a newer release
      exists, otherwise empty. The caller uses its emptiness to decide whether
      to render the upgrade indicator at all.
    """
    if not running:
        return "", ""

    cache_file = STATE_DIR / "version-cache.json"
    cache: dict = {}
    if cache_file.exists():
        try:
            cache = json.loads(cache_file.read_text())
        except (json.JSONDecodeError, OSError):
            cache = {}

    # Latest release lookup - time-bounded cache to avoid hitting GitHub on
    # every prompt.
    latest_version = ""
    latest_date: datetime | None = None
    latest_entry = cache.get("__latest__")
    if isinstance(latest_entry, dict):
        checked_at = latest_entry.get("checked_at", 0)
        if isinstance(checked_at, (int, float)) and time.time() - checked_at < _LATEST_RELEASE_TTL:
            latest_version = latest_entry.get("version", "") or ""
            pub_str = latest_entry.get("published_at", "")
            if isinstance(pub_str, str) and pub_str:
                try:
                    latest_date = datetime.fromisoformat(pub_str)
                except ValueError:
                    latest_version, latest_date = "", None
    if not latest_version:
        try:
            req = urllib.request.Request(
                "https://api.github.com/repos/anthropics/claude-code/releases/latest",
                headers={"User-Agent": "statusline"},
            )
            with urllib.request.urlopen(req, timeout=2) as resp:
                data = json.loads(resp.read())
                tag = data.get("tag_name", "").lstrip("v")
                pub = data.get("published_at", "")
                if tag and pub:
                    latest_version = tag
                    latest_date = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                    cache["__latest__"] = {
                        "version": latest_version,
                        "published_at": latest_date.isoformat(),
                        "checked_at": time.time(),
                    }
                    try:
                        cache_file.parent.mkdir(parents=True, exist_ok=True)
                        cache_file.write_text(json.dumps(cache))
                    except OSError:
                        pass
        except Exception:
            pass

    if latest_version and latest_version != running:
        age = ""
        if latest_date:
            age = f" ({(date.today() - latest_date.astimezone().date()).days}d)"
        return running, f"v{latest_version}{age}"
    return running, ""


# ============ HEALTH STATUS ============

_HEALTH_STATUS_MAP = {
    "investigating": "Investigating",
    "identified": "Identified",
    "monitoring": "Monitoring",
    "resolved": "Resolved",
    "postmortem": "Resolved",
}


def _truncate_name(name: str, limit: int = 55) -> str:
    """Truncate incident name preserving the tail (model names live there)."""
    if len(name) <= limit:
        return name
    tail = limit - 23  # 20 head + "..."
    return name[:20] + "..." + name[-tail:]


def get_health_status() -> list[dict]:
    """Return list of health incident dicts.
    An entry with service='OK' means all clear.
    Returns [] immediately when the Claude status line is disabled in config,
    so the HTTP call to status.claude.com is skipped entirely."""
    if not STATUSLINE_CONFIG["claude_status"]:
        return []
    # Check cache
    if HEALTH_CACHE.exists():
        try:
            cache = json.loads(HEALTH_CACHE.read_text())
            if time.time() - cache.get("timestamp", 0) < HEALTH_TTL and "incidents" in cache:
                return cache["incidents"]
        except (json.JSONDecodeError, OSError):
            pass

    incidents = []
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=3) as r:
            data = json.loads(r.read())
            now = datetime.now(timezone.utc)
            for inc in data.get("incidents", []):
                affected = []
                for comp in inc.get("components", []):
                    cid = comp.get("id")
                    if cid in HEALTH_COMPONENTS and HEALTH_COMPONENTS[cid] not in affected:
                        affected.append(HEALTH_COMPONENTS[cid])
                if not affected:
                    continue

                service = "Both" if len(affected) == 2 else affected[0]
                status = inc.get("status", "")
                resolved_at = inc.get("resolved_at")

                if status not in ("resolved", "postmortem"):
                    updates = inc.get("incident_updates", [])
                    latest = updates[0] if updates else {}
                    raw_status = latest.get("status", status)
                    incidents.append({
                        "service": service,
                        "name": _truncate_name(inc.get("name", "Unknown")),
                        "status": _HEALTH_STATUS_MAP.get(raw_status, raw_status.replace("_", " ").title()),
                        "body": (latest.get("body", "") or "")[:30],
                        "time_ago": _relative_time(
                            latest.get("created_at", inc.get("updated_at", ""))
                        ),
                        "resolved": False,
                    })
                elif resolved_at:
                    try:
                        resolved_dt = datetime.fromisoformat(resolved_at.replace("Z", "+00:00"))
                        if (now - resolved_dt).total_seconds() / 3600 <= 1:
                            incidents.append({
                                "service": service,
                                "name": _truncate_name(inc.get("name", "Unknown")),
                                "status": "Resolved",
                                "body": "",
                                "time_ago": _relative_time(resolved_at),
                                "resolved": True,
                            })
                    except Exception:
                        pass
    except Exception:
        pass

    if not incidents:
        incidents = [{"service": "OK"}]

    # Cache
    try:
        HEALTH_CACHE.parent.mkdir(parents=True, exist_ok=True)
        HEALTH_CACHE.write_text(json.dumps({"timestamp": time.time(), "incidents": incidents}))
    except OSError:
        pass
    return incidents


# ============ USAGE DATA ============

def _get_oauth_token() -> str | None:
    """Read OAuth token from macOS Keychain or CLAUDE_OAUTH_TOKEN env var."""
    if IS_MACOS:
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                capture_output=True, text=True, timeout=2,
            )
            if result.returncode != 0:
                return None
            creds = json.loads(result.stdout.strip())
            return creds.get("claudeAiOauth", {}).get("accessToken")
        except Exception:
            return None
    else:
        return os.environ.get("CLAUDE_OAUTH_TOKEN")


def _parse_usage_response(data: dict) -> dict:
    """Parse API usage response into display values."""
    result: dict = {}
    if (data.get("five_hour") is None
            and data.get("seven_day") is None
            and data.get("seven_day_opus") is None):
        result["is_max"] = True
    else:
        if data.get("five_hour") is not None:
            result["session_pct"] = str(int(data["five_hour"].get("utilization", 0)))
            result["session_reset"] = _format_reset_time(data["five_hour"].get("resets_at", ""))
        if data.get("seven_day") is not None:
            result["weekly_pct"] = str(int(data["seven_day"].get("utilization", 0)))
            result["weekly_reset"] = _format_reset_time(data["seven_day"].get("resets_at", ""))
        if data.get("seven_day_opus") is not None:
            opus_pct = int(data["seven_day_opus"].get("utilization", 0))
            if opus_pct > 0:
                result["opus_pct"] = str(opus_pct)
    extra = _parse_extra_usage(data.get("extra_usage"))
    if extra:
        result.update(extra)
    return result


def get_usage_data() -> dict | None:
    """Return parsed usage data, or None on failure."""
    if os.environ.get("CLAUDE_CODE_USE_FOUNDRY") == "1":
        return {"is_foundry": True}

    # Check cache (read once, reuse for stale fallback)
    cached = None
    if USAGE_CACHE.exists():
        try:
            cached = json.loads(USAGE_CACHE.read_text())
        except Exception:
            pass
    if cached:
        try:
            cached_at = datetime.fromisoformat(cached["cached_at"])
            if (datetime.now(timezone.utc) - cached_at).total_seconds() < USAGE_TTL:
                return _parse_usage_response(cached["data"])
        except Exception:
            pass
    stale_data = cached.get("data") if cached else None

    token = _get_oauth_token()
    if not token:
        return None

    try:
        req = urllib.request.Request(
            USAGE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
                "User-Agent": "claude-statusline/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())
        try:
            USAGE_CACHE.write_text(json.dumps({
                "cached_at": datetime.now(timezone.utc).isoformat(),
                "data": data,
            }))
        except OSError:
            pass
        return _parse_usage_response(data)
    except Exception:
        # API failed (429, timeout, etc.) - fall back to stale cache if available
        if stale_data:
            return _parse_usage_response(stale_data)
        return {"is_oauth": True}


# ============ CODEX USAGE ============


def get_codex_usage() -> dict | None:
    """Return parsed Codex usage data, or None if not installed."""
    if not CODEX_ENABLED or not CODEX_AUTH_FILE.exists():
        return None

    # Check cache
    if CODEX_USAGE_CACHE.exists():
        try:
            cache = json.loads(CODEX_USAGE_CACHE.read_text())
            cached_at = datetime.fromisoformat(cache["cached_at"])
            if (datetime.now(timezone.utc) - cached_at).total_seconds() < CODEX_USAGE_TTL:
                return cache["parsed"]
        except Exception:
            pass

    # Read auth token
    try:
        auth = json.loads(CODEX_AUTH_FILE.read_text())
        token = auth.get("tokens", {}).get("access_token")
        if not token:
            return {"codex_installed": True}
    except Exception:
        return {"codex_installed": True}

    # Fetch from API
    try:
        req = urllib.request.Request(
            CODEX_USAGE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "claude-statusline/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())

        result: dict = {"codex_installed": True}
        plan = data.get("plan_type", "")
        if plan:
            result["plan_type"] = plan.title()

        rl = data.get("rate_limit", {})
        pw = rl.get("primary_window")
        if pw:
            result["session_pct"] = str(int(pw.get("used_percent", 0)))
            result["session_reset"] = _format_unix_reset(pw.get("reset_at"))
        sw = rl.get("secondary_window")
        if sw:
            result["weekly_pct"] = str(int(sw.get("used_percent", 0)))
            result["weekly_reset"] = _format_unix_reset(sw.get("reset_at"))

        # Cache
        try:
            CODEX_USAGE_CACHE.write_text(json.dumps({
                "cached_at": datetime.now(timezone.utc).isoformat(),
                "parsed": result,
            }))
        except OSError:
            pass
        return result
    except Exception:
        # API failed - try returning expired cache
        if CODEX_USAGE_CACHE.exists():
            try:
                cache = json.loads(CODEX_USAGE_CACHE.read_text())
                return cache["parsed"]
            except Exception:
                pass
        return {"codex_installed": True}


# ============ SUBSCRIPTION DETECTION ============


def _is_fast_mode() -> bool:
    """Check if Claude Code fast mode is enabled via settings.json."""
    try:
        settings = json.loads(SETTINGS_FILE.read_text())
        return bool(settings.get("fastMode"))
    except Exception:
        return False


def _detect_subscription(usage: dict | None) -> tuple[str, str, str]:
    """Return (name, icon, color_key) based on Claude Code auth method.

    Follows Claude Code's own auth precedence:
    cloud providers > auth token > API key > OAuth subscription.
    """
    if os.environ.get("CLAUDE_CODE_USE_BEDROCK") == "1":
        return "Bedrock", "\u2601\ufe0f", "mode_work"
    if os.environ.get("CLAUDE_CODE_USE_VERTEX") == "1":
        return "Vertex AI", "\u2601\ufe0f", "mode_work"
    if os.environ.get("CLAUDE_CODE_USE_FOUNDRY") == "1":
        return "Foundry", "\u26a1", "mode_work"
    if os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return "API Gateway", "\U0001f310", "mode_work"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "API Key", "\U0001f511", "mode_work"
    # OAuth login - we know it's a claude.ai subscription but can't
    # reliably distinguish Pro/Max/Team/Enterprise from available data.
    if usage:
        if usage.get("is_oauth"):
            # OAuth token exists but usage API failed - show authenticated state
            return "claude.ai", "\u2728", "mode_personal"
        if usage.get("session_pct") or usage.get("weekly_pct"):
            return "claude.ai", "\u2728", "mode_personal"
        return "claude.ai", "\u2728", "mode_personal"
    return "claude.ai", "\U0001f464", "mode_free"


# ============ LINE BUILDING ============

_HEALTH_LINK_URL = "https://status.claude.com"
_DASHBOARD_URL = os.environ.get("ORBIT_DASHBOARD_URL", "http://localhost:8787")


def _health_link(text: str) -> str:
    """Wrap text in an OSC 8 clickable hyperlink to status.claude.com."""
    return f"\033]8;;{_HEALTH_LINK_URL}\033\\{text}\033]8;;\033\\"


def _osc8_link(url: str, text: str) -> str:
    """Wrap text in an OSC 8 clickable hyperlink."""
    clean_url = url.replace("\033", "").replace("\x07", "")
    clean_text = re.sub(r"[\x00-\x1f\x7f]", "", text)
    return f"\033]8;;{clean_url}\033\\{clean_text}\033]8;;\033\\"


def _item(color: str, icon: str, label: str, value: str) -> str:
    return f"{color}{icon} {label}: {value}{RESET}"


def _join_items(items: list[str], widths: list[int], max_col1: int, max_col2: int) -> str:
    if not items:
        return ""
    parts: list[str] = []
    for i, (item, w) in enumerate(zip(items, widths)):
        if i == 0:
            pad = max_col1 - w
            parts.append(item + (" " * max(pad, 0)))
        else:
            target = max_col2 if i == 1 else CELL_WIDTH
            pad = target - w
            parts.append(PIPE + item + (" " * max(pad, 0)))
    return "".join(parts)


def _pad_line(line: str, line_width: int, max_width: int) -> str:
    pad = max_width - line_width
    return line + (" " * pad) if pad > 0 else line


# ============ iTERM TITLE ============

def set_iterm_title(session_id: str, project_name: str, repo_name: str, branch: str, dir_name: str = "") -> None:
    try:
        tty = open("/dev/tty", "w")
    except OSError:
        return

    action = "Claude Code"
    db = _get_hooks_db()
    if db:
        try:
            row = db.execute(
                "SELECT action FROM session_state WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            db.close()
            if row and row["action"]:
                action = row["action"]
        except sqlite3.Error:
            pass

    prefix = project_name or dir_name
    title = f"{prefix}: {action}" if prefix else action
    tty.write(f"\033]1;{title}\007")

    if os.environ.get("TERM_PROGRAM") == "iTerm.app":
        subtitle = ""
        if project_name:
            subtitle = project_name
        elif repo_name and branch:
            subtitle = f"{repo_name}({branch})"
        elif repo_name:
            subtitle = repo_name
        if subtitle:
            b64 = base64.b64encode(subtitle.encode()).decode()
            tty.write(f"\033]1337;SetUserVar=claudeSubtitle={b64}\007")
    elif os.environ.get("CMUX_WORKSPACE_ID"):
        if project_name:
            cmux_bin = os.environ.get("CMUX_CLAUDE_HOOK_CMUX_BIN", "cmux")
            try:
                subprocess.Popen(
                    [cmux_bin, "workspace-action", "--action", "set-description",
                     "--description", project_name],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except OSError:
                pass
    tty.close()


# ============ MAIN ============

def main() -> None:
    raw = sys.stdin.read()
    if not raw.strip():
        return

    info = parse_input(raw)
    session_id = info["session_id"]
    model_name = info["model_name"]
    tokens_str = info["tokens_str"]

    # Default empty model/tokens (startup or incomplete data)
    model_name = model_name or "Claude"
    tokens_str = tokens_str or "0"

    update_term_session(session_id)
    edit_count = update_session_state(session_id, info["ctx_percent"], tokens_str)

    # Run slow operations (subprocesses + HTTP) concurrently to stay under
    # Claude Code's ~300ms debounce/cancel window on first render.
    rate_limits = info.get("rate_limits")
    pool = ThreadPoolExecutor(max_workers=6)
    f_project = pool.submit(get_project_info, session_id, info["duration_sec"])
    f_git = pool.submit(get_git_info)
    f_k8s = pool.submit(get_k8s_context)
    f_version = pool.submit(get_version_info, info["running_version"])
    f_health = pool.submit(get_health_status)
    f_usage = pool.submit(
        lambda: _parse_stdin_rate_limits(rate_limits) if rate_limits else get_usage_data()
    )
    # Always fetch extra_usage from API (300s cache) - stdin doesn't include it
    f_extra = pool.submit(get_usage_data) if rate_limits else None
    f_codex = pool.submit(get_codex_usage)
    f_last_time = pool.submit(get_last_action_time, session_id)

    _FUTURE_TIMEOUT = 3
    try:
        project_name, project_display, project_progress = f_project.result(timeout=_FUTURE_TIMEOUT)
    except Exception:
        project_name, project_display, project_progress = "", "", ""
    try:
        last_action_time = f_last_time.result(timeout=_FUTURE_TIMEOUT)
    except Exception:
        last_action_time = ""
    try:
        repo_name, branch, git_dirty = f_git.result(timeout=_FUTURE_TIMEOUT)
    except Exception:
        repo_name, branch, git_dirty = "", "", False
    try:
        k8s_name = f_k8s.result(timeout=_FUTURE_TIMEOUT)
    except Exception:
        k8s_name = ""
    try:
        version, version_upgrade = f_version.result(timeout=_FUTURE_TIMEOUT)
    except Exception:
        version, version_upgrade = "", ""
    try:
        health = f_health.result(timeout=_FUTURE_TIMEOUT)
    except Exception:
        health = []
    try:
        usage = f_usage.result(timeout=_FUTURE_TIMEOUT)
    except Exception:
        usage = None
    if f_extra:
        try:
            extra_data = f_extra.result(timeout=_FUTURE_TIMEOUT)
            if extra_data and usage:
                for k in ("extra_spent", "extra_limit", "extra_pct", "extra_reset"):
                    if k in extra_data:
                        usage[k] = extra_data[k]
        except Exception:
            pass
    try:
        codex_usage = f_codex.result(timeout=_FUTURE_TIMEOUT)
    except Exception:
        codex_usage = None

    # Release stragglers without blocking; workers self-terminate
    # via their own internal timeouts (HTTP: 2-3s, subprocess: 2-5s).
    pool.shutdown(wait=False, cancel_futures=True)

    dir_name = Path.cwd().name
    if dir_name == os.environ.get("USER", ""):
        dir_name = "~"

    # --- Build items per line ---

    # Line 1: Location
    line1 = [_item(COLORS["dir"], ICONS["dir"], "Dir", dir_name)]
    if branch:
        c = COLORS["git_dirty"] if git_dirty else COLORS["git_clean"]
        worktree = info.get("worktree")
        branch_display = f"{branch} (worktree)" if worktree else branch
        line1.append(_item(c, ICONS["git"], "Git", branch_display))

    # Line 2: Project + Last Action time
    line2: list[str] = []
    if project_name:
        linked_name = _osc8_link(f"{_DASHBOARD_URL}/#projects", project_display)
        if project_progress:
            progress_url = f"{_DASHBOARD_URL}/#projects?task={urllib.parse.quote(project_name, safe='')}&tab=tasks"
            linked_value = f"{linked_name} {_osc8_link(progress_url, project_progress.strip())}"
        else:
            linked_value = linked_name
        line2.append(_item(COLORS["project"], ICONS["project"], "Project", linked_value))
    if last_action_time:
        line2.append(_item(COLORS["datetime"], ICONS["datetime"], "Last Action", last_action_time))

    # Line 3: Metrics
    line3 = [
        _item(COLORS["model"], ICONS["model"], "Model", model_name),
        _item(COLORS["tokens"], ICONS["tokens"], "Tokens", tokens_str),
    ]
    ctx_pct = info["ctx_percent"]
    if ctx_pct >= 80:
        line3.append(_item(COLORS["ctx_urgent"], "\U0001f534", "Ctx", f"{ctx_pct}% (Compact now!)"))
    elif ctx_pct >= 65:
        line3.append(_item(COLORS["ctx_warn"], "\U0001f7e1", "Ctx", f"{ctx_pct}% (Compact recommended)"))
    elif info["ctx_estimated"]:
        line3.append(_item(COLORS["ctx_est"], ICONS["context"], "Ctx", f"{ctx_pct}% (Estimated)"))
    else:
        line3.append(_item(COLORS["ctx"], ICONS["context"], "Ctx", f"{ctx_pct}%"))
    if _is_fast_mode():
        line3.append(f"{COLORS['fast_mode']}\u26a1 Fast mode activated{RESET}")

    # Line 4: Session
    line4 = [
        _item(COLORS["time"], ICONS["duration"], "Elapsed", info["duration_str"]),
        _item(COLORS["edit"], ICONS["edit"], "Edits", str(edit_count)),
    ]

    # Line K8s: K8s + Version + Health
    line_k8s: list[str] = []
    if k8s_name:
        line_k8s.append(_item(COLORS["k8s"], ICONS["k8s"], "K8s", k8s_name))
    if version:
        ver_color = COLORS["git_clean"] if is_version_reviewed(version) else COLORS["git_dirty"]
        changelog_url = "https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md"
        ver_link = f"\033]8;;{changelog_url}\033\\v{version}\033]8;;\033\\"
        if version_upgrade:
            # `version_upgrade` is pre-formatted as "v<tag> (Xd)" by get_version_info
            upgrade_link = f"\033]8;;{changelog_url}\033\\{version_upgrade}\033]8;;\033\\"
            line_k8s.append(f"{ver_color}{ICONS['version']} {ver_link}{RESET} {COLORS['upgrade']}\u2192 {upgrade_link}{RESET}")
        else:
            line_k8s.append(f"{ver_color}{ICONS['version']} {ver_link}{RESET}")
    for inc in health:
        if inc.get("service") == "OK":
            line_k8s.append(f"{COLORS['health_ok']}{ICONS['health_ok']} {_health_link('Claude Status: OK')}{RESET}")
        elif inc.get("resolved"):
            label = f"[{inc['service']}] {inc['name']} - {inc['status']}"
            if inc.get("body"):
                label += f" - {inc['body']}"
            if inc.get("time_ago"):
                label += f" ({inc['time_ago']})"
            line_k8s.append(f"{COLORS['health_resolved']}{ICONS['health_ok']} {_health_link(label)}{RESET}")
        else:
            st = inc.get("status", "")
            if st == "Investigating":
                color, icon = COLORS["health_partial"], ICONS["health_partial"]
            elif st == "Monitoring":
                color, icon = COLORS["health_ok"], ICONS["health_degraded"]
            else:
                color, icon = COLORS["health_degraded"], ICONS["health_degraded"]
            label = f"[{inc['service']}] {inc['name']} - {st}"
            if inc.get("body"):
                label += f" - {inc['body']}"
            if inc.get("time_ago"):
                label += f" ({inc['time_ago']})"
            line_k8s.append(f"{color}{icon} {_health_link(label)}{RESET}")

    # Line Usage: Subscription + usage stats
    line_usage: list[str] = []
    if STATUSLINE_CONFIG["subscription_type"]:
        sub_name, sub_icon, sub_color = _detect_subscription(usage)
        line_usage.append(f"{COLORS[sub_color]}{sub_icon} {sub_name}{RESET}")

    if usage and STATUSLINE_CONFIG["subscription_usage"]:
        if usage.get("is_foundry"):
            cost = info["cost_str"]
            if cost:
                line_usage.append(
                    f"{COLORS['session_usage']}{ICONS['duration']} Session: {cost}{RESET}")
            else:
                line_usage.append(
                    f"{COLORS['session_usage']}{ICONS['duration']} Session: {tokens_str} tokens, {info['duration_str']}{RESET}")
        elif usage.get("is_max"):
            line_usage.append(f"{COLORS['session_usage']}{ICONS['duration']} Session: \u221e{RESET}")
            line_usage.append(f"{COLORS['weekly_usage']}{ICONS['week']} Weekly: \u221e{RESET}")
        else:
            sp = usage.get("session_pct")
            if sp and sp != "null":
                sr = usage.get("session_reset", "")
                if sr and sr != "null":
                    line_usage.append(
                        f"{COLORS['session_usage']}{ICONS['duration']} Session: {sp}% "
                        f"{COLORS['reset_time']}{ICONS['reset']} {sr}{RESET}")
                else:
                    line_usage.append(
                        f"{COLORS['session_usage']}{ICONS['duration']} Session: {sp}%{RESET}")
            wp = usage.get("weekly_pct")
            if wp and wp != "null":
                wr = usage.get("weekly_reset", "")
                if wr and wr != "null":
                    line_usage.append(
                        f"{COLORS['weekly_usage']}{ICONS['week']} Weekly: {wp}% "
                        f"{COLORS['reset_time']}{ICONS['reset']} {wr}{RESET}")
                else:
                    line_usage.append(
                        f"{COLORS['weekly_usage']}{ICONS['week']} Weekly: {wp}%{RESET}")
            op = usage.get("opus_pct")
            if op and op != "null" and op != "0":
                line_usage.append(
                    f"{COLORS['opus_usage']}{ICONS['model']} Opus: {op}%{RESET}")
        # Extra usage (independent of rate-limit plan type)
        if not usage.get("is_foundry"):
            es = usage.get("extra_spent")
            if es is not None:
                ep = usage.get("extra_pct", "?")
                elim = usage.get("extra_limit", "?")
                erset = usage.get("extra_reset", "")
                extra_text = f"{es}/{elim} spent ({ep}% used)"
                if erset:
                    extra_text += f" {COLORS['reset_time']}{ICONS['reset']} {erset}"
                line_usage.append(
                    f"{COLORS['extra_usage']}{ICONS['extra']} Extra: {extra_text}{RESET}")

    # Line Codex: Codex usage (only if installed)
    line_codex: list[str] = []
    if codex_usage and codex_usage.get("codex_installed"):
        plan = codex_usage.get("plan_type", "")
        label = f"Codex ({plan})" if plan else "Codex"
        line_codex.append(f"{COLORS['codex_label']}\U0001f9e0 {label}{RESET}")
        sp = codex_usage.get("session_pct")
        if sp and sp != "null":
            sr = codex_usage.get("session_reset", "")
            if sr and sr != "null":
                line_codex.append(
                    f"{COLORS['codex_session']}{ICONS['duration']} Session: {sp}% "
                    f"{COLORS['reset_time']}{ICONS['reset']} {sr}{RESET}")
            else:
                line_codex.append(
                    f"{COLORS['codex_session']}{ICONS['duration']} Session: {sp}%{RESET}")
        wp = codex_usage.get("weekly_pct")
        if wp and wp != "null":
            wr = codex_usage.get("weekly_reset", "")
            if wr and wr != "null":
                line_codex.append(
                    f"{COLORS['codex_weekly']}{ICONS['week']} Weekly: {wp}% "
                    f"{COLORS['reset_time']}{ICONS['reset']} {wr}{RESET}")
            else:
                line_codex.append(
                    f"{COLORS['codex_weekly']}{ICONS['week']} Weekly: {wp}%{RESET}")

    # --- Column alignment ---
    all_lines = [line1, line2, line3, line4, line_k8s, line_usage, line_codex]
    all_widths = [[display_width(item) for item in items] for items in all_lines]

    max_col1 = CELL_WIDTH
    max_col2 = CELL_WIDTH
    for widths in all_widths:
        if len(widths) > 0:
            max_col1 = max(max_col1, widths[0])
        if len(widths) > 1:
            max_col2 = max(max_col2, widths[1])

    joined = [_join_items(items, widths, max_col1, max_col2)
              for items, widths in zip(all_lines, all_widths)]
    line_widths = [display_width(j) for j in joined]
    max_width = max(line_widths) if line_widths else 0

    j_line1, j_line2, j_line3, j_line4, j_line_k8s, j_line_usage, j_line_codex = joined
    w_line1, w_line2, w_line3, w_line4, w_line_k8s, w_line_usage, w_line_codex = line_widths

    # --- Output ---
    # Output a fixed number of lines so Claude Code allocates
    # the full status area height from the very first render.
    # 7 lines if Codex is installed, 6 otherwise.
    has_codex = CODEX_ENABLED and CODEX_AUTH_FILE.exists()
    blank = " " * max_width if max_width > 0 else ""
    out = sys.stdout
    out.write(RESET)
    out.write(((_pad_line(j_line2, w_line2, max_width) if j_line2 else blank) + RESET + "\n"))
    out.write(_pad_line(j_line1, w_line1, max_width) + RESET + "\n")
    out.write(_pad_line(j_line4, w_line4, max_width) + RESET + "\n")
    out.write(_pad_line(j_line3, w_line3, max_width) + RESET + "\n")
    out.write(((_pad_line(j_line_k8s, w_line_k8s, max_width) if j_line_k8s else blank) + RESET + "\n"))
    out.write(((_pad_line(j_line_usage, w_line_usage, max_width) if j_line_usage else blank) + RESET + "\n"))
    if has_codex:
        out.write(((_pad_line(j_line_codex, w_line_codex, max_width) if j_line_codex else blank) + RESET + "\n"))
    out.write(RESET)
    out.flush()

    set_iterm_title(session_id, project_name, repo_name, branch, dir_name)


def _fallback_output() -> None:
    """Print minimal output so the statusline area stays allocated."""
    lines = 7 if CODEX_ENABLED and CODEX_AUTH_FILE.exists() else 6
    for _ in range(lines):
        sys.stdout.write(" \n")
    sys.stdout.flush()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        try:
            log_path = Path.home() / ".claude" / "logs" / "statusline-errors.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a") as f:
                f.write(f"\n--- {datetime.now().isoformat()} ---\n")
                traceback.print_exc(file=f)
        except Exception:
            pass
        _fallback_output()

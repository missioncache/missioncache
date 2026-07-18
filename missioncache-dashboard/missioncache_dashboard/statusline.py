#!/usr/bin/env python3
"""Claude Code Status Line.

Reads JSON from stdin (Claude Code session data) and outputs
a multi-line ANSI-colored status display.

Layout:
  Line 1: Project    - [project name + progress] [fork of?] [saved] [last action] (last action shows even with no active project)
  Line 2: Location   - [dir] [git branch+status]
  Line 3: Session    - [elapsed] [edits]
  Line 4: Metrics    - [model] [effort?]
  Line 5: K8s/Ctx    - [k8s context] [tokens] [ctx%]
  Line 6: Usage      - [mode] [session%] [weekly%] [opus%]
  Line 7: Codex      - [plan] [5h%] [weekly%] (only if codex installed)
  Line 8: Vitals     - [version] [Claude status]

Configuration:
  All visibility toggles (Codex line, Claude subscription usage/type, Claude
  status, status service filter) are managed through the MissionCache dashboard
  Settings screen. The statusline reads them from
  ~/.claude/missioncache-dashboard-config.json on each invocation. Defaults apply
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
from typing import NamedTuple, Optional

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
    # Fork-family accent (matches the dashboard fork tree's cyan) - the
    # shared-layer update indicator, deliberately NOT the ctx_urgent alarm.
    "fork_update": f"{ESC}[38;2;0;212;212m",
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
    "effort_low": f"{ESC}[38;2;255;160;60m",
    "effort_medium": f"{ESC}[38;2;100;200;120m",
    "effort_high": f"{ESC}[38;2;170;180;235m",
    "effort_xhigh": f"{ESC}[38;2;180;140;220m",
    "pr_approved": f"{ESC}[38;2;80;200;120m",
    "pr_pending": f"{ESC}[38;2;220;180;50m",
    "pr_changes": f"{ESC}[38;2;255;109;0m",
    "pr_draft": f"{ESC}[38;2;140;140;150m",
    "pr": f"{ESC}[38;2;130;180;220m",
}

# Rainbow palette for effort=max (cycled per character of the value).
RAINBOW_COLORS = (
    f"{ESC}[38;2;255;140;120m",
    f"{ESC}[38;2;255;110;150m",
    f"{ESC}[38;2;220;100;200m",
)


def _atomic_write_json(path: Path, payload: object) -> None:
    """Write JSON to ``path`` via tmp+rename so concurrent statusline runs cannot
    observe a half-written cache file.

    Multiple Claude Code tabs invoke the statusline on every prompt; a naive
    ``path.write_text(json.dumps(...))`` lets two tabs race on the same cache
    file and produce truncated output that the next reader cannot decode. The
    tmp+``os.replace`` pattern bounds visibility to a complete file or no
    write at all, with no new locking primitives.

    The pid suffix on the tmp path keeps concurrent writers from collusion-
    corrupting each other's tmp file; pid is not stable across reboots or
    PID-reuse, so leftover ``<name>.tmp.*`` files from prior crashes (between
    write_text and os.replace) are best-effort swept on every call. This
    avoids a slow disk-fill in ``~/.claude/scripts/`` without an external
    janitor process.

    Failures are silent (return on OSError) so a full disk or read-only
    mount cannot break the statusline render. TypeError on a non-JSON-safe
    payload is NOT swallowed - the four current call sites pass dicts of
    primitives, and silently stringifying corrupt data is worse than a loud
    crash that the user can see in their terminal.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Best-effort cleanup of stale tmp files from prior crashes.
        cutoff = time.time() - 3600
        for stale in path.parent.glob(f"{path.name}.tmp.*"):
            try:
                if stale.stat().st_mtime < cutoff:
                    stale.unlink()
            except OSError:
                pass
        tmp_path = path.parent / f"{path.name}.tmp.{os.getpid()}"
        tmp_path.write_text(json.dumps(payload))
        os.replace(tmp_path, path)
    except OSError:
        pass


def _rainbow_text(text: str) -> str:
    """Color each character with the next color in RAINBOW_COLORS."""
    return "".join(
        f"{RAINBOW_COLORS[i % len(RAINBOW_COLORS)]}{ch}"
        for i, ch in enumerate(text)
    )

ICONS = {
    "dir": "\U0001f4c1",
    "git": "\U0001f500",
    "project": "\U0001f4cb",
    "k8s": "\u2638\ufe0f",
    "model": "\U0001f916",
    "tokens": "\U0001f522",
    "context": "\U0001f4ca",
    "duration": "\u231a",
    "edit": "\u270f\ufe0f",
    "datetime": "\U0001f550",
    "week": "\U0001f4c5",
    "reset": "\U0001f504",
    "version": "\U0001f4e6",
    "health_ok": "\u2705",
    "health_degraded": "\u26a0\ufe0f",
    "health_partial": "\U0001f7e1",
    "extra": "\U0001f4b3",
    "effort": "\U0001f3af",
    "thinking": "\U0001f9e0",
    "pr_approved": "\U0001f7e2",
    "pr_pending": "\U0001f7e1",
    "pr_changes": "\U0001f534",
    "pr_draft": "⚪",
    "pr": "\U0001f535",
}

PIPE = f"  {COLORS['pipe']}\u2502{RESET}  "


CELL_WIDTH = 24

STATE_DIR = Path.home() / ".claude" / "hooks" / "state"
HOOKS_STATE_DB = Path.home() / ".claude" / "hooks-state.db"
SCRIPTS_DIR = Path.home() / ".claude" / "scripts"
SETTINGS_FILE = Path.home() / ".claude" / "settings.json"
MISSIONCACHE_ACTIVE = Path.home() / ".missioncache" / "active"


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
HEALTH_TTL = 60
HEALTH_URL = "https://status.claude.com/api/v2/incidents.json"

_ALL_HEALTH_COMPONENTS = {
    "yyzkbfz2thpt": "Code",
    "rwppv331jlwc": "claude.ai",
    "k8w3r06qmzrp": "Claude API",
    "0qbwn08sd68x": "platform.claude.com",
    "0scnb50nvy53": "Claude for Government",
    "bpp5gb3hpjcl": "Claude Cowork",
}

_DASHBOARD_CONFIG_FILE = Path.home() / ".claude" / "missioncache-dashboard-config.json"
_DEFAULT_STATUSLINE_CONFIG = {
    "codex": True,
    "subscription_usage": True,
    "subscription_type": True,
    "claude_status": True,
    "claude_status_services": ["Code", "Claude API"],
    # Model-access announcements (e.g. "suspended access to ... Fable 5") are
    # posted as long-lived status.claude.com incidents that pin to the health
    # field for weeks. Hidden by default; opt in via the dashboard Settings.
    "model_suspensions": False,
    # Addon rows normally sit above the health line, which keeps Claude status
    # as the footer. Opt in to push them below it when the addons carry the
    # information you scan for first and status is the afterthought.
    "addons_after_status": False,
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
USAGE_TTL = 60
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

CODEX_USAGE_CACHE = SCRIPTS_DIR / "codex-usage-cache.json"
CODEX_USAGE_TTL = 60
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CODEX_AUTH_FILE = Path.home() / ".codex" / "auth.json"
CODEX_CONFIG_FILE = Path.home() / ".codex" / "config.toml"
CODEX_ENABLED = STATUSLINE_CONFIG["codex"]


def _get_codex_model() -> str | None:
    """Return the model name configured in ~/.codex/config.toml, or None on any failure."""
    try:
        import tomllib
        return tomllib.loads(CODEX_CONFIG_FILE.read_text()).get("model")
    except Exception:
        return None


# ============ DISPLAY WIDTH ============

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mK]|\x1b\][^\x07]*\x07|\x1b\]8;[^\x1b]*\x1b\\")

# SGR color/style sequences only (not OSC 8 hyperlinks, which are not color).
_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")

# NO_COLOR (https://no-color.org): a non-empty value disables ANSI color. We
# deliberately do NOT gate on sys.stdout.isatty() - the statusline's stdout is
# always a pipe to Claude Code, which renders the ANSI itself, so an isatty()
# check would wrongly strip color in normal use. Only the explicit NO_COLOR
# opt-out disables it.
USE_COLOR = not os.environ.get("NO_COLOR")

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


def _codepoint_width(s: str, i: int, n: int) -> int:
    """Display width of the codepoint at ``s[i]``: 0 for a ZWJ or variation
    selector, 2 for emoji or wide/fullwidth CJK, else 1.

    Shared by ``display_width`` and ``_truncate_to_width`` so the
    emoji/ZWJ/VS16/east-asian classification lives in one place. ``n`` is
    ``len(s)``; the VS16 lookahead at ``i + 1`` needs it.
    """
    cp = ord(s[i])
    if cp == 0x200D or cp in (0xFE0E, 0xFE0F):
        return 0
    has_vs16 = i + 1 < n and ord(s[i + 1]) == 0xFE0F
    is_emoji = (
        any(lo <= cp <= hi for lo, hi in _EMOJI_RANGES)
        or has_vs16
        or cp in _EMOJI_SINGLES
    )
    if is_emoji or unicodedata.east_asian_width(s[i]) in ("W", "F"):
        return 2
    return 1


def display_width(s: str) -> int:
    """Calculate display width accounting for ANSI codes, emoji, and CJK."""
    s = _ANSI_RE.sub("", s)
    width = 0
    i = 0
    n = len(s)
    while i < n:
        width += _codepoint_width(s, i, n)
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

    # Debug log - off by default; the statusline runs on every render, so this
    # synchronous serialize+write only happens when explicitly opted in.
    if os.environ.get("MISSIONCACHE_STATUSLINE_DEBUG"):
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
        # Claude Code's used_percentage already accounts for the full context
        # fill, including the system prompt + tool definitions (they are part
        # of current_usage's cached input tokens). A live payload confirms it:
        # used_percentage=18 with current_usage summing to 179757/1000000 =
        # 17.98%, so the reported percentage IS the raw token fraction and the
        # ~170K cached tokens (system + tools + history) are already counted.
        # Adding a flat overhead here would double-count and fire compact
        # warnings early, so the direct path is used verbatim. The estimated
        # fallback below still adds an overhead term because it works from the
        # raw token base when Claude Code omits the percentage.
        ctx_percent = min(int(ctx["used_percentage"]), 100)
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

    effort_level = (data.get("effort") or {}).get("level")
    thinking_enabled = bool((data.get("thinking") or {}).get("enabled"))

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
        "effort_level": effort_level,
        "thinking_enabled": thinking_enabled,
        "pr": data.get("pr"),
    }


# ============ SESSION STATE ============

def update_session_state(
    session_id: str, ctx_percent: int, tokens_str: str
) -> tuple[int, str, str]:
    """Update session state in hooks-state DB.

    Returns (edit_count, last_prompt_at, action) read back from the one
    UPSERT+SELECT on the session_state row, so the render path does not open
    extra connections to the same row for the last-action time and iTerm title.
    """
    if not session_id:
        return 0, "", "Claude Code"
    edit_count = 0
    last_prompt_at = ""
    action = "Claude Code"
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
                "SELECT edit_count, last_prompt_at, action FROM session_state WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row:
                edit_count = row["edit_count"] or 0
                last_prompt_at = row["last_prompt_at"] or ""
                action = row["action"] or "Claude Code"
            db.close()
        except sqlite3.Error:
            pass
    return edit_count, last_prompt_at, action


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
    the reference implementation in mcp-server/src/mcp_missioncache/project_files.py:407.
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


def _read_tasks_content(project_dir: Path, project_name: str) -> str:
    """Return the contents of the project's tasks.md, or "" if unreadable."""
    tasks_file = project_dir / f"{project_name}-tasks.md"
    try:
        return tasks_file.read_text()
    except OSError:
        return ""


class ProjectInfo(NamedTuple):
    name: str = ""
    display: str = ""
    progress: str = ""
    fork_of: str = ""
    # Parent-context mtime when the shared layer changed after this session's
    # last sync; 0.0 = fresh (or not a fork).
    shared_stale_mtime: float = 0.0
    # This project's own context file mtime; 0.0 = no context file (or
    # unstattable). Rendered as the "Saved" cell.
    context_saved_mtime: float = 0.0


# MUST stay byte-identical to context_health._FORK_NAME_RE (the db/MCP copy).
# The statusline is a stdlib-only standalone script and cannot import
# missioncache_db, so the grammar is mirrored here by hand. The no-slash /
# leading-alnum shape is a load-bearing SECURITY control (blocks path
# traversal via a hand-edited header). test_statusline_fork asserts the two
# patterns are equal; keep them in lockstep.
_FORK_HEADER_RE = re.compile(
    r"^\*\*Fork of:\*\*\s*(?:\[\[([A-Za-z0-9][A-Za-z0-9._-]*)\]\]|([A-Za-z0-9][A-Za-z0-9._-]*))\s*$"
)
# Mirror of context_health._FENCE_RE + its CommonMark closer rule (same fence
# char, closer length >= opener). Kept identical so this parser and the db's
# _header_line agree on where the header region ends.
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")


def _parse_fork_of(context_text: str) -> str:
    """Parent name from a ``**Fork of:**`` line in the context header region
    (everything before the first real ``## `` section), or "".

    Fence-aware, mirroring context_health._header_line: a ``## `` INSIDE a
    fenced code block does not end the header region (else the two parsers
    disagree and the db links a fork the statusline renders as plain)."""
    fence_char = None
    fence_len = 0
    for line in context_text.splitlines():
        m = _FENCE_RE.match(line)
        if fence_char is None:
            if m:
                fence_char = m.group(1)[0]
                fence_len = len(m.group(1))
            else:
                stripped = line.strip()
                if stripped.startswith("## "):
                    break
                match = _FORK_HEADER_RE.match(stripped)
                if match:
                    return match.group(1) or match.group(2)
        else:
            # Inside a fence: close only on the same char, length >= opener.
            if m and m.group(1)[0] == fence_char and len(m.group(1)) >= fence_len:
                fence_char = None
                fence_len = 0
    return ""


def _resolve_parent_context(parent: str) -> Optional[Path]:
    """The parent project's context file, searched in active/ then completed/
    (a completed parent's shared layer stays reachable)."""
    for base in (MISSIONCACHE_ACTIVE, MISSIONCACHE_ACTIVE.parent / "completed"):
        for fname in (f"{parent}-context.md", "context.md"):
            candidate = base / parent / fname
            if candidate.is_file():
                return candidate
    return None


def _shared_stale_mtime(
    parent_ctx: Path, session_id: str, parent_name: str
) -> Optional[float]:
    """The parent context's mtime when it changed after this session's last
    sync (per the shared-seen marker), else None. The marker must belong to
    THIS parent - a session that switched between forks of different parents
    has a baseline for the other file, and mtimes across files do not compare.
    No/foreign/corrupt marker reads as fresh - neutral, never a false alarm."""
    # Shape-guard the session id before it becomes a filename: it is a trusted
    # harness UUID today, but a stray '..' or '/' must never let the marker
    # read escape the shared-seen dir.
    if not re.fullmatch(r"[A-Za-z0-9._-]+", session_id or ""):
        return None
    marker = STATE_DIR / "shared-seen" / f"{session_id}.json"
    try:
        data = json.loads(marker.read_text())
        if data.get("parent") != parent_name:
            return None
        seen = data.get("seen_mtime")
        if seen is None:
            return None
        # Exact comparison: the marker stores the same float st_mtime the
        # writer statted, and JSON round-trips float64 exactly.
        mtime = parent_ctx.stat().st_mtime
        return mtime if mtime > float(seen) else None
    except (OSError, ValueError, TypeError):
        return None


def _format_wall_time(mtime: float) -> str:
    """A change timestamp as the user's local wall clock: HH:MM today,
    "Jul 14 14:32" otherwise - the same shape as the Last Action cell, and
    unambiguous internationally (a numeric DD/MM reads as MM/DD to half the
    audience). Absolute on purpose - the statusline only re-renders on
    conversation events, so a relative "25m ago" would sit frozen and
    mislead during idle stretches."""
    dt = datetime.fromtimestamp(mtime)
    if dt.date() == datetime.now().date():
        return dt.strftime("%H:%M")
    return dt.strftime("%b %-d %H:%M")


def _format_saved_time(mtime: float) -> str:
    """The project context's last-save time, ALWAYS with the date ("Jul 14
    23:15", never bare HH:MM): a project resumed days after its last save
    must not read as saved today. Same shape as the Last Action cell."""
    return datetime.fromtimestamp(mtime).strftime("%b %-d %H:%M")


def _project_is_active_in_db(name: str) -> bool:
    """True if a task named ``name`` is active in the tasks DB.

    Non-coding tasks created via ``create_task`` intentionally have no
    directory under ``~/.missioncache/active/``, so a missing directory does
    not always mean the project is gone. Only queried in the dir-missing
    branch of ``get_project_info`` - the hot path never touches
    ``missioncache_db``. Any failure (package missing, migration guard, DB
    error) is treated as 'not active' so the project drops off the statusline.
    """
    try:
        from missioncache_db import TaskDB

        return TaskDB().get_task_by_name(name, "active") is not None
    except Exception:
        return False


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
    project_dir = MISSIONCACHE_ACTIVE / name
    if MISSIONCACHE_ACTIVE.is_dir():
        if not project_dir.is_dir():
            for parent in MISSIONCACHE_ACTIVE.iterdir():
                nested = parent / name
                if parent.is_dir() and nested.is_dir():
                    display = f"{parent.name}/{name}"
                    project_dir = nested
                    break

    # The project_state binding survives long after a project is completed
    # (it is only removed by /missioncache:done, which can miss the stdin
    # session_id). A missing directory usually means the project was
    # archived/completed - but non-coding tasks created via create_task have
    # no directory at all, so before dropping consult the tasks DB by name:
    # keep showing a still-active directoryless project (no progress suffix),
    # and drop everything else (completed/archived/not-found, or DB
    # unavailable) so a finished project stops pinning to the statusline.
    if not project_dir.is_dir():
        if _project_is_active_in_db(name):
            return ProjectInfo(name, display, "")
        return ProjectInfo()

    # Fork awareness: a "**Fork of:**" line in the child's context header
    # marks this project as a fork; the parent's context is its shared layer.
    # Bounded read: the header sits at the very top, so read at most 8 KB
    # instead of slurping the whole context (can be 100 KB+) on every render
    # inside the ~300 ms statusline budget. UnicodeDecodeError is caught too -
    # a non-UTF-8 file must drop only the fork annotation, not (via the
    # main() except) the entire project cell.
    fork_of = ""
    shared_stale_mtime = 0.0
    context_saved_mtime = 0.0
    for ctx_name in (f"{name}-context.md", "context.md"):
        ctx_path = project_dir / ctx_name
        if ctx_path.is_file():
            try:
                # Stat before the read: a non-UTF-8 file drops only the fork
                # annotation, not the Saved stamp.
                context_saved_mtime = ctx_path.stat().st_mtime
                with open(ctx_path, "r", encoding="utf-8", errors="strict") as fh:
                    head = fh.read(8192)
                fork_of = _parse_fork_of(head)
            except (OSError, UnicodeDecodeError):
                pass
            break
    if fork_of:
        parent_ctx = _resolve_parent_context(fork_of)
        if parent_ctx is None:
            fork_of = ""  # unresolvable parent: render as a plain project
        else:
            shared_stale_mtime = (
                _shared_stale_mtime(parent_ctx, session_id, fork_of) or 0.0
            )

    tasks_content = _read_tasks_content(project_dir, name)
    if not tasks_content:
        return ProjectInfo(
            name, display, "", fork_of, shared_stale_mtime, context_saved_mtime
        )
    progress = f" {_parse_task_progress(tasks_content)}"
    return ProjectInfo(
        name, display, progress, fork_of, shared_stale_mtime, context_saved_mtime
    )


# ============ LAST ACTION TIME ============

def _format_last_action(last_prompt_at: str) -> str:
    """Format an ISO last-prompt timestamp as 'Mon D HH:MM', or '' if unusable.

    The timestamp is read back by update_session_state from the same
    session_state row, so this is pure formatting with no DB access.
    """
    if not last_prompt_at:
        return ""
    try:
        return datetime.fromisoformat(last_prompt_at).strftime("%b %-d %H:%M")
    except (ValueError, TypeError):
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


_LATEST_RELEASE_TTL = 21600  # 6 hours - kept long because GitHub's
# unauthenticated releases API is rate-limited at 60/h per IP, and a corporate
# NAT with multiple users running this plugin can exhaust the quota fast at
# tighter TTLs. Releases don't happen multiple times per hour anyway, so a
# stale "latest" indicator is a non-issue.


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
                    _atomic_write_json(cache_file, cache)
        except Exception:
            pass

    # The arrow always points at the newer version. In the standard case
    # (running < latest) that's running -> latest+age. In the canary /
    # cache-lag case (running > latest, e.g. a self-built or pre-release
    # session ahead of the GitHub-tagged release), the display flips so
    # the arrow still points at the newer side: latest -> running. Age
    # only attaches to GitHub's tagged latest (the only side with a
    # known release date) and is dropped in the flipped case.
    if latest_version:
        latest_tup = _parse_semver(latest_version)
        running_tup = _parse_semver(running)
        if latest_tup > running_tup:
            age = ""
            if latest_date:
                age = f" ({(date.today() - latest_date.astimezone().date()).days}d)"
            return running, f"v{latest_version}{age}"
        if running_tup > latest_tup:
            return latest_version, f"v{running}"
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


_MODEL_NOTICE_KEYWORDS = ("suspend", "deprecat", "sunset", "retir", "no longer available")


def _is_model_notice(name: str, body: str = "") -> bool:
    """True when an incident reads as a model access/suspension announcement
    rather than an operational outage.

    Anthropic posts model suspensions as long-lived ``monitoring`` incidents
    (e.g. "We've suspended access to Claude Mythos 5 and Claude Fable 5") that
    never resolve, so they pin to the statusline for weeks. Matched on the
    incident name plus its latest update body against a small keyword set.
    Operational incidents use "elevated errors" / "service disruption"
    phrasing and do not collide with these tokens.
    """
    text = f"{name} {body}".lower()
    return any(k in text for k in _MODEL_NOTICE_KEYWORDS)


def _apply_health_filters(incidents: list[dict]) -> list[dict]:
    """Apply user-configurable filters to a raw incident list and add the
    all-clear fallback.

    Runs on every call (both cache hits and fresh fetches) so toggling
    ``model_suspensions`` in the dashboard takes effect on the next prompt
    render instead of waiting for the 60s health cache to expire. Incidents
    missing the ``is_model_notice`` tag (e.g. a cache written by an older
    version) are treated as real incidents and kept.
    """
    if not STATUSLINE_CONFIG["model_suspensions"]:
        incidents = [i for i in incidents if not i.get("is_model_notice")]
    if not incidents:
        return [{"service": "OK"}]
    return incidents


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
                return _apply_health_filters(cache["incidents"])
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

                if len(affected) == 1:
                    service = affected[0]
                elif len(affected) == 2:
                    service = "Both"
                else:
                    service = ", ".join(affected)
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
                        "is_model_notice": _is_model_notice(
                            inc.get("name", ""), latest.get("body", "") or ""
                        ),
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
                                "is_model_notice": _is_model_notice(inc.get("name", "")),
                            })
                    except Exception:
                        pass
    except Exception:
        pass

    # Cache the raw (unfiltered, OK-fallback-free) incident list so the
    # model_suspensions toggle applies on the next render without waiting for
    # the 60s cache to expire. Filtering + all-clear fallback happen on read.
    _atomic_write_json(HEALTH_CACHE, {"timestamp": time.time(), "incidents": incidents})
    return _apply_health_filters(incidents)


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
        _atomic_write_json(USAGE_CACHE, {
            "cached_at": datetime.now(timezone.utc).isoformat(),
            "data": data,
        })
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
        _atomic_write_json(CODEX_USAGE_CACHE, {
            "cached_at": datetime.now(timezone.utc).isoformat(),
            "parsed": result,
        })
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
    # `usage is None` means no auth data at all (no OAuth token); an empty
    # dict `{}` means we parsed stdin rate_limits but found no window data
    # this render - the user is still authenticated, so keep the personal
    # styling rather than falling through to the free-tier icon.
    if usage is not None:
        if usage.get("is_oauth"):
            # OAuth token exists but usage API failed - show authenticated state
            return "claude.ai", "\u2728", "mode_personal"
        if usage.get("session_pct") or usage.get("weekly_pct"):
            return "claude.ai", "\u2728", "mode_personal"
        return "claude.ai", "\u2728", "mode_personal"
    return "claude.ai", "\U0001f464", "mode_free"


# ============ LINE BUILDING ============

_HEALTH_LINK_URL = "https://status.claude.com"
_DASHBOARD_URL = os.environ.get("MISSIONCACHE_DASHBOARD_URL", "http://localhost:8787")


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


def _render_pr_field(pr: dict | None) -> str | None:
    # Open PR for the current branch (statusline `pr` object, Claude Code 2.1.90+).
    # Icon/color encode review_state; review_state may be absent even when pr is.
    if not pr or not pr.get("number"):
        return None
    state = pr.get("review_state")
    state_map = {
        "approved": "pr_approved",
        "pending": "pr_pending",
        "changes_requested": "pr_changes",
        "draft": "pr_draft",
    }
    key = state_map.get(state, "pr") if isinstance(state, str) else "pr"
    label = f"#{pr['number']}"
    url = pr.get("url")
    value = _osc8_link(url, label) if url else label
    return _item(COLORS[key], ICONS[key], "PR", value)


def _render_effort_field(effort_level: str | None, thinking_enabled: bool) -> str | None:
    # Icon doubles as thinking indicator: brain when thinking is on, dart otherwise.
    # No effort = no field, even if thinking is on (drops signal by design).
    if not effort_level:
        return None
    icon = ICONS["thinking"] if thinking_enabled else ICONS["effort"]
    if effort_level == "max":
        rainbow_value = _rainbow_text("max")
        return f"{COLORS['effort_xhigh']}{icon} Effort: {RESET}{rainbow_value}{RESET}"
    effort_color = COLORS.get(f"effort_{effort_level}", COLORS["effort_medium"])
    return _item(effort_color, icon, "Effort", effort_level)


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


def _truncate_to_width(line: str, max_width: int, line_width: int | None = None) -> str:
    """Truncate a line to ``max_width`` display cells, preserving ANSI codes.

    ANSI/OSC escape sequences pass through without consuming width; visible
    cells are counted with the same emoji/CJK rules as ``display_width``. When
    the line is cut, a single-cell ellipsis is appended and the line is reset
    so nothing wraps past ``max_width`` and no color bleeds after the cut.

    ``line_width`` lets a caller that already knows ``display_width(line)``
    (e.g. ``_pad_line``) pass it in, skipping the redundant full-string pass
    on entry.
    """
    if (line_width if line_width is not None else display_width(line)) <= max_width:
        return line
    if max_width <= 0:
        return ""
    limit = max_width - 1  # leave one cell for the ellipsis
    out: list[str] = []
    width = 0
    i = 0
    n = len(line)
    while i < n:
        m = _ANSI_RE.match(line, i)
        if m:
            out.append(m.group())
            i = m.end()
            continue
        ch = line[i]
        w = _codepoint_width(line, i, n)
        if w == 0:
            # ZWJ / variation selectors carry no width - keep, do not count.
            out.append(ch)
            i += 1
            continue
        if width + w > limit:
            break
        out.append(ch)
        width += w
        i += 1
    return "".join(out) + "…" + RESET


def _pad_line(line: str, line_width: int, max_width: int) -> str:
    if line_width > max_width:
        return _truncate_to_width(line, max_width, line_width)
    pad = max_width - line_width
    return line + (" " * pad) if pad > 0 else line


def _order_status_rows(addon_rows: list[str], health_row: str, addons_after_status: bool) -> list[str]:
    """Order the addon rows and the Claude status/health line.

    Both orderings emit the same number of lines, so the statusline keeps a
    fixed height and Claude Code's status allocation does not jump when the
    flag flips.
    """
    if addons_after_status:
        return [health_row, *addon_rows]
    return [*addon_rows, health_row]


# ============ iTERM TITLE ============

def set_iterm_title(action: str, project_name: str, repo_name: str, branch: str, dir_name: str = "") -> None:
    try:
        tty = open("/dev/tty", "w")
    except OSError:
        return

    action = action or "Claude Code"
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

# ============ USER ADDONS ============
# Users add their own statusline cells without editing this file: each addon is
# an entry in the `statusline_addons` list of the dashboard config
# (~/.claude/missioncache-dashboard-config.json), a distinct top-level key from
# `statusline` so the dashboard's statusline-settings save never touches it. An
# addon's `command` is run - throttled by a per-addon TTL cache, bounded by a
# timeout, fail-closed - and its output becomes a cell. The addon line count is
# derived only from config, never from fetch results, so height stays fixed.

ADDON_COLOR_ALLOW = frozenset(COLORS)
ADDON_DEFAULT_COLOR = "version"
# \Z (not $) so a trailing newline in the id can't slip through - the id is
# used in the cache filename.
_ADDON_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}\Z")
_ADDON_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
_ADDON_OVERRIDE_KEYS = ("value", "label", "icon", "color", "hidden")

# Friendly append-target name -> the internal line list it injects a cell into.
_ADDON_APPEND_TARGETS = {
    "location": "line1",
    "project": "line2",
    "metrics": "line3",
    "session": "line4",
    "context": "line_k8s",
    "usage": "line_usage",
    "codex": "line_codex",
    "vitals": "line_health",
}


def _normalize_addon(raw: object) -> dict | None:
    """Validate + normalize one addon config entry, or None if invalid."""
    if not isinstance(raw, dict):
        return None
    addon_id = raw.get("id")
    if not isinstance(addon_id, str) or not _ADDON_ID_RE.match(addon_id):
        return None
    command = raw.get("command")
    if not isinstance(command, list) or not command:
        return None
    if not all(isinstance(c, str) for c in command):
        return None
    color = raw.get("color")
    if color not in ADDON_COLOR_ALLOW:
        color = ADDON_DEFAULT_COLOR
    try:
        ttl = max(5, int(raw.get("ttl", 60)))
    except (TypeError, ValueError):
        ttl = 60
    try:
        timeout = min(30, max(1, int(raw.get("timeout", 5))))
    except (TypeError, ValueError):
        timeout = 5
    placement = raw.get("placement")
    if not isinstance(placement, dict):
        placement = {}
    mode = placement.get("mode")
    if mode not in ("row", "append"):
        mode = "row"
    group = placement.get("group")
    if not isinstance(group, str) or not group:
        group = addon_id
    try:
        order = int(placement.get("order", 0))
    except (TypeError, ValueError):
        order = 0
    target = placement.get("target")
    if target not in _ADDON_APPEND_TARGETS:
        target = None
    # An append-mode addon with no valid target has nowhere to go and would
    # otherwise render nothing; fall back to giving it its own row.
    if mode == "append" and target is None:
        mode = "row"
    return {
        "id": addon_id,
        "enabled": bool(raw.get("enabled", True)),
        # Strip control chars from static label/icon too (command output is
        # already stripped in _parse_addon_output) so neither path can inject
        # ANSI that breaks the grid.
        "label": _ADDON_CTRL_RE.sub("", str(raw.get("label", ""))),
        "icon": _ADDON_CTRL_RE.sub("", str(raw.get("icon", ""))),
        "color": color,
        "command": list(command),
        "ttl": ttl,
        "timeout": timeout,
        "placement": {"mode": mode, "group": group, "order": order, "target": target},
    }


def _load_addons(cfg_path: Path = _DASHBOARD_CONFIG_FILE) -> list[dict]:
    """Load enabled, validated addons from the config file. Fail-closed to []."""
    try:
        data = json.loads(cfg_path.read_text())
    except Exception:
        return []
    raw_list = data.get("statusline_addons") if isinstance(data, dict) else None
    if not isinstance(raw_list, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for raw in raw_list:
        addon = _normalize_addon(raw)
        if addon is None or not addon["enabled"] or addon["id"] in seen:
            continue
        seen.add(addon["id"])
        out.append(addon)
    return out


def _addon_row_groups(addons: list[dict]) -> list[str]:
    """Ordered distinct row-group names among row-mode addons. Sets the number
    and order of addon lines. Depends only on config, so height is stable."""
    order_by_group: dict[str, int] = {}
    for a in addons:
        p = a["placement"]
        if p["mode"] != "row":
            continue
        g = p["group"]
        if g not in order_by_group or p["order"] < order_by_group[g]:
            order_by_group[g] = p["order"]
    return sorted(order_by_group, key=lambda g: (order_by_group[g], g))


def _parse_addon_output(raw: str | None) -> dict | None:
    """Command stdout -> a normalized data dict, or None when empty.

    Plain text becomes {"value": text}. A JSON object is read as overrides,
    keeping only value/label/icon/color/hidden. A JSON non-object (number,
    array, string) is treated as plain text. String fields are stripped of
    control chars so a command cannot inject ANSI that breaks the grid.
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    data: dict | None = None
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                data = {k: parsed[k] for k in _ADDON_OVERRIDE_KEYS if k in parsed}
        except Exception:
            data = None
    if data is None:
        data = {"value": text}
    for k in ("value", "label", "icon", "color"):
        if isinstance(data.get(k), str):
            data[k] = _ADDON_CTRL_RE.sub("", data[k])
    return data


def _render_addon_cell(addon: dict, data: dict | None) -> str | None:
    """Pure: (addon config, parsed data) -> a cell string, or None to omit."""
    if not data or data.get("hidden"):
        return None
    value = data.get("value")
    if not isinstance(value, str) or not value:
        return None
    label = data["label"] if isinstance(data.get("label"), str) else addon["label"]
    icon = data["icon"] if isinstance(data.get("icon"), str) else addon["icon"]
    color = data.get("color")
    if color not in ADDON_COLOR_ALLOW:
        color = addon["color"]
    return _item(COLORS[color], icon, label, value)


def get_addon_value(addon: dict) -> dict | None:
    """Run an addon's command with a per-id TTL cache + stale fallback."""
    cache = SCRIPTS_DIR / f"addon-{addon['id']}-cache.json"
    try:
        cached = json.loads(cache.read_text())
    except Exception:
        cached = None
    if isinstance(cached, dict):
        try:
            cached_at = datetime.fromisoformat(cached["cached_at"])
            if (datetime.now(timezone.utc) - cached_at).total_seconds() < addon["ttl"]:
                return cached.get("parsed")
        except Exception:
            pass
    stale = cached.get("parsed") if isinstance(cached, dict) else None
    try:
        out = run_cmd(addon["command"], timeout=addon["timeout"])
    except Exception:
        return stale
    parsed = _parse_addon_output(out)
    if parsed is None:
        return stale
    try:
        _atomic_write_json(cache, {
            "cached_at": datetime.now(timezone.utc).isoformat(),
            "parsed": parsed,
        })
    except Exception:
        pass
    return parsed


def _assemble_addon_cells(addons: list[dict], values_by_id: dict) -> tuple[dict, dict]:
    """-> (row_items {group: [cells]}, append_items {internal_row: [cells]})."""
    row_items: dict[str, list[str]] = {}
    append_items: dict[str, list[str]] = {}
    for a in addons:
        cell = _render_addon_cell(a, values_by_id.get(a["id"]))
        if cell is None:
            continue
        p = a["placement"]
        if p["mode"] == "append" and p["target"]:
            append_items.setdefault(_ADDON_APPEND_TARGETS[p["target"]], []).append(cell)
        else:
            row_items.setdefault(p["group"], []).append(cell)
    return row_items, append_items


ADDONS = _load_addons()
ADDON_ROW_GROUPS = _addon_row_groups(ADDONS)
ADDON_LINE_COUNT = len(ADDON_ROW_GROUPS)


def main() -> None:
    raw = sys.stdin.read()
    if not raw.strip():
        return

    info = parse_input(raw)
    session_id = info["session_id"]
    model_name = info["model_name"]
    tokens_str = info["tokens_str"]

    # Claude Code 2.1.153+ sets COLUMNS in the statusline subprocess env.
    # Older versions don't, in which case term_cols stays None and the
    # statusline keeps its prior (no width-aware) behavior.
    term_cols: int | None = None
    try:
        cols_raw = int(os.environ.get("COLUMNS", "0"))
        if cols_raw > 0:
            term_cols = cols_raw
    except ValueError:
        pass

    # Default empty model/tokens (startup or incomplete data)
    model_name = model_name or "Claude"
    tokens_str = tokens_str or "0"

    update_term_session(session_id)
    # One UPSERT+SELECT on the session_state row returns edit_count, the
    # last-prompt timestamp, and the iTerm action, so nothing else re-reads
    # that row this render.
    edit_count, last_prompt_raw, action_title = update_session_state(
        session_id, info["ctx_percent"], tokens_str
    )
    last_action_time = _format_last_action(last_prompt_raw)

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

    # User addons run on their own pool so they never starve the 6 core workers.
    addon_pool = ThreadPoolExecutor(max_workers=min(8, len(ADDONS))) if ADDONS else None
    f_addons = {a["id"]: addon_pool.submit(get_addon_value, a) for a in ADDONS} if addon_pool else {}

    _FUTURE_TIMEOUT = 3
    try:
        project = f_project.result(timeout=_FUTURE_TIMEOUT)
    except Exception:
        project = ProjectInfo()
    project_name, project_display, project_progress = (
        project.name,
        project.display,
        project.progress,
    )
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

    values_by_id: dict = {}
    for _aid, _fut in f_addons.items():
        try:
            values_by_id[_aid] = _fut.result(timeout=_FUTURE_TIMEOUT)
        except Exception:
            values_by_id[_aid] = None

    # Release stragglers without blocking; workers self-terminate
    # via their own internal timeouts (HTTP: 2-3s, subprocess: 2-5s).
    pool.shutdown(wait=False, cancel_futures=True)
    if addon_pool is not None:
        addon_pool.shutdown(wait=False, cancel_futures=True)

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
    pr_field = _render_pr_field(info.get("pr"))
    if pr_field:
        line1.append(pr_field)

    # Line 2 (top row): Project [+ Fork of] [+ Saved] + Last Action. Last
    # Action trails the row; when no MissionCache project is loaded it takes
    # the row's first slot.
    line2: list[str] = []
    if project_name:
        linked_name = _osc8_link(f"{_DASHBOARD_URL}/#projects", project_display)
        if project_progress:
            progress_url = f"{_DASHBOARD_URL}/#projects?task={urllib.parse.quote(project_name, safe='')}&tab=tasks"
            linked_value = f"{linked_name} {_osc8_link(progress_url, project_progress.strip())}"
        else:
            linked_value = linked_name
        line2.append(_item(COLORS["project"], ICONS["project"], "Project", linked_value))
        if project.fork_of:
            # Fork annotation: link to the parent's dashboard modal; a cyan dot
            # (the fork-family accent, matching the dashboard's fork tree) means
            # the shared (parent) context changed since this session's last sync
            # - re-read it before building on stale knowledge. The timestamp is
            # the parent change's local wall-clock time, absolute on purpose
            # (see _format_wall_time).
            parent_url = f"{_DASHBOARD_URL}/#projects?task={urllib.parse.quote(project.fork_of, safe='')}"
            fork_value = _osc8_link(parent_url, project.fork_of)
            if project.shared_stale_mtime:
                stamp = _format_wall_time(project.shared_stale_mtime)
                fork_value += (
                    f" {COLORS['fork_update']}● parent updated {stamp}"
                    f"{RESET}{COLORS['project']}"
                )
            line2.append(_item(COLORS["project"], "⤵", "Fork of", fork_value))
        if project.context_saved_mtime:
            # Links to the project's Context tab in the dashboard modal.
            saved_stamp = _format_saved_time(project.context_saved_mtime)
            ctx_url = (
                f"{_DASHBOARD_URL}/#projects"
                f"?task={urllib.parse.quote(project_name, safe='')}&tab=context"
            )
            line2.append(
                _item(
                    COLORS["datetime"],
                    "\U0001f4be",
                    "Saved",
                    _osc8_link(ctx_url, saved_stamp),
                )
            )
    if last_action_time:
        line2.append(_item(COLORS["datetime"], ICONS["datetime"], "Last Action", last_action_time))

    # Line 3: Metrics
    line3 = [
        _item(COLORS["model"], ICONS["model"], "Model", model_name),
    ]
    effort_field = _render_effort_field(
        info.get("effort_level"), info.get("thinking_enabled", False)
    )
    if effort_field:
        line3.append(effort_field)
    if _is_fast_mode():
        line3.append(f"{COLORS['fast_mode']}\u26a1 Fast mode activated{RESET}")

    # Line 4: Session
    line4 = [
        _item(COLORS["time"], ICONS["duration"], "Elapsed", info["duration_str"]),
        _item(COLORS["edit"], ICONS["edit"], "Edits", str(edit_count)),
    ]

    # Line K8s: K8s + Tokens + Ctx
    line_k8s: list[str] = []
    if k8s_name:
        line_k8s.append(_item(COLORS["k8s"], ICONS["k8s"], "K8s", k8s_name))
    line_k8s.append(_item(COLORS["tokens"], ICONS["tokens"], "Tokens", tokens_str))
    ctx_pct = info["ctx_percent"]
    if ctx_pct >= 80:
        line_k8s.append(_item(COLORS["ctx_urgent"], "\U0001f534", "Ctx", f"{ctx_pct}% (Compact now!)"))
    elif ctx_pct >= 65:
        line_k8s.append(_item(COLORS["ctx_warn"], "\U0001f7e1", "Ctx", f"{ctx_pct}% (Compact recommended)"))
    elif info["ctx_estimated"]:
        line_k8s.append(_item(COLORS["ctx_est"], ICONS["context"], "Ctx", f"{ctx_pct}% (Estimated)"))
    else:
        line_k8s.append(_item(COLORS["ctx"], ICONS["context"], "Ctx", f"{ctx_pct}%"))

    # Line Health: Version + Claude Status (appears after Codex, or in place of
    # it). Last Action moved to the top row alongside Project.
    line_health: list[str] = []
    if version:
        ver_color = COLORS["git_clean"] if is_version_reviewed(version) else COLORS["git_dirty"]
        changelog_url = "https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md"
        ver_link = f"\033]8;;{changelog_url}\033\\v{version}\033]8;;\033\\"
        if version_upgrade:
            # `version_upgrade` is pre-formatted as "v<tag> (Xd)" by get_version_info
            upgrade_link = f"\033]8;;{changelog_url}\033\\{version_upgrade}\033]8;;\033\\"
            line_health.append(f"{ver_color}{ICONS['version']} {ver_link}{RESET} {COLORS['upgrade']}\u2192 {upgrade_link}{RESET}")
        else:
            line_health.append(f"{ver_color}{ICONS['version']} {ver_link}{RESET}")
    for inc in health:
        if inc.get("service") == "OK":
            line_health.append(f"{COLORS['health_ok']}{ICONS['health_ok']} {_health_link('Claude Status: OK')}{RESET}")
        elif inc.get("resolved"):
            label = f"[{inc['service']}] {inc['name']} - {inc['status']}"
            if inc.get("body"):
                label += f" - {inc['body']}"
            if inc.get("time_ago"):
                label += f" ({inc['time_ago']})"
            line_health.append(f"{COLORS['health_resolved']}{ICONS['health_ok']} {_health_link(label)}{RESET}")
        else:
            st = inc.get("status", "")
            if st == "Investigating":
                color, icon = COLORS["health_partial"], ICONS["health_partial"]
            else:
                # Monitoring/Identified are still open incidents - keep them
                # yellow. Green (health_ok) is reserved for the OK all-clear
                # and resolved states so a still-open incident never reads as
                # "all good" at a glance.
                color, icon = COLORS["health_degraded"], ICONS["health_degraded"]
            label = f"[{inc['service']}] {inc['name']} - {st}"
            if inc.get("body"):
                label += f" - {inc['body']}"
            if inc.get("time_ago"):
                label += f" ({inc['time_ago']})"
            line_health.append(f"{color}{icon} {_health_link(label)}{RESET}")

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
                        f"{COLORS['session_usage']}{ICONS['duration']} Session: {sp:>3}% "
                        f"{COLORS['reset_time']}{ICONS['reset']} {sr}{RESET}")
                else:
                    line_usage.append(
                        f"{COLORS['session_usage']}{ICONS['duration']} Session: {sp:>3}%{RESET}")
            wp = usage.get("weekly_pct")
            if wp and wp != "null":
                wr = usage.get("weekly_reset", "")
                if wr and wr != "null":
                    line_usage.append(
                        f"{COLORS['weekly_usage']}{ICONS['week']} Weekly: {wp:>3}% "
                        f"{COLORS['reset_time']}{ICONS['reset']} {wr}{RESET}")
                else:
                    line_usage.append(
                        f"{COLORS['weekly_usage']}{ICONS['week']} Weekly: {wp:>3}%{RESET}")
            op = usage.get("opus_pct")
            if op and op != "null" and op != "0":
                line_usage.append(
                    f"{COLORS['opus_usage']}{ICONS['model']} Opus: {op:>3}%{RESET}")
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
        model = _get_codex_model()
        parts = [p for p in (plan, model) if p]
        label = f"Codex ({', '.join(parts)})" if parts else "Codex"
        line_codex.append(f"{COLORS['codex_label']}\U0001f9e0 {label}{RESET}")
        sp = codex_usage.get("session_pct")
        if sp and sp != "null":
            sr = codex_usage.get("session_reset", "")
            if sr and sr != "null":
                line_codex.append(
                    f"{COLORS['codex_session']}{ICONS['duration']} Session: {sp:>3}% "
                    f"{COLORS['reset_time']}{ICONS['reset']} {sr}{RESET}")
            else:
                line_codex.append(
                    f"{COLORS['codex_session']}{ICONS['duration']} Session: {sp:>3}%{RESET}")
        wp = codex_usage.get("weekly_pct")
        if wp and wp != "null":
            wr = codex_usage.get("weekly_reset", "")
            if wr and wr != "null":
                line_codex.append(
                    f"{COLORS['codex_weekly']}{ICONS['week']} Weekly: {wp:>3}% "
                    f"{COLORS['reset_time']}{ICONS['reset']} {wr}{RESET}")
            else:
                line_codex.append(
                    f"{COLORS['codex_weekly']}{ICONS['week']} Weekly: {wp:>3}%{RESET}")

    # --- User addons: assemble cells, inject append-mode into existing rows ---
    addon_row_items, addon_append_items = _assemble_addon_cells(ADDONS, values_by_id)
    if addon_append_items:
        _rows_by_name = {
            "line1": line1, "line2": line2, "line3": line3, "line4": line4,
            "line_k8s": line_k8s, "line_usage": line_usage,
            "line_codex": line_codex, "line_health": line_health,
        }
        for _row_name, _cells in addon_append_items.items():
            _rows_by_name[_row_name].extend(_cells)
    addon_lines = [addon_row_items.get(g, []) for g in ADDON_ROW_GROUPS]

    # --- Column alignment ---
    all_lines = [line1, line2, line3, line4, line_k8s, line_usage, line_codex, line_health]
    all_widths = [[display_width(item) for item in items] for items in all_lines]

    # line2's second field (Last Action) and line_health's Claude Status are
    # excluded from column-width aggregation so their variable-length text
    # doesn't stretch columns on the rows below or above (Status). line2's
    # Project still contributes to col1 so the project label aligns with
    # Dir/Elapsed/etc.
    max_col1 = CELL_WIDTH
    max_col2 = CELL_WIDTH
    for i, widths in enumerate(all_widths):
        is_line_health = i == len(all_widths) - 1
        is_line2 = i == 1
        if is_line_health:
            continue
        if len(widths) > 0:
            max_col1 = max(max_col1, widths[0])
        if len(widths) > 1 and not is_line2:
            max_col2 = max(max_col2, widths[1])

    # Addon rows contribute their first cell to col1 (like line2) so labels
    # align with the grid, but not to col2, so a wide addon value can't stretch
    # every other row.
    addon_widths = [[display_width(item) for item in items] for items in addon_lines]
    for widths in addon_widths:
        if widths:
            max_col1 = max(max_col1, widths[0])

    joined = [_join_items(items, widths, max_col1, max_col2)
              for items, widths in zip(all_lines, all_widths)]
    addon_joined = [_join_items(items, widths, max_col1, max_col2)
                    for items, widths in zip(addon_lines, addon_widths)]
    line_widths = [display_width(j) for j in joined]
    addon_line_widths = [display_width(j) for j in addon_joined]
    max_width = max(line_widths + addon_line_widths) if (line_widths or addon_line_widths) else 0
    if term_cols is not None:
        max_width = min(max_width, term_cols)

    j_line1, j_line2, j_line3, j_line4, j_line_k8s, j_line_usage, j_line_codex, j_line_health = joined
    w_line1, w_line2, w_line3, w_line4, w_line_k8s, w_line_usage, w_line_codex, w_line_health = line_widths

    # --- Output ---
    # Output a fixed number of lines so Claude Code allocates the full status
    # area height from the very first render: 8 with Codex installed, 7 without
    # (health takes the codex slot, no blank gap). The addon rows and the health
    # line always sum to the same count, so the addons_after_status flag reorders
    # them (see _order_status_rows) without changing the height.
    has_codex = CODEX_ENABLED and CODEX_AUTH_FILE.exists()
    blank = " " * max_width if max_width > 0 else ""
    segments = [RESET]
    segments.append((_pad_line(j_line2, w_line2, max_width) if j_line2 else blank) + RESET + "\n")
    segments.append(_pad_line(j_line1, w_line1, max_width) + RESET + "\n")
    segments.append(_pad_line(j_line4, w_line4, max_width) + RESET + "\n")
    segments.append(_pad_line(j_line3, w_line3, max_width) + RESET + "\n")
    segments.append((_pad_line(j_line_k8s, w_line_k8s, max_width) if j_line_k8s else blank) + RESET + "\n")
    segments.append((_pad_line(j_line_usage, w_line_usage, max_width) if j_line_usage else blank) + RESET + "\n")
    if has_codex:
        segments.append((_pad_line(j_line_codex, w_line_codex, max_width) if j_line_codex else blank) + RESET + "\n")
    addon_rows = [(_pad_line(_j, _w, max_width) if _j else blank) + RESET + "\n"
                  for _j, _w in zip(addon_joined, addon_line_widths)]
    health_row = (_pad_line(j_line_health, w_line_health, max_width) if j_line_health else blank) + RESET + "\n"
    segments.extend(_order_status_rows(addon_rows, health_row, STATUSLINE_CONFIG["addons_after_status"]))
    segments.append(RESET)
    output = "".join(segments)
    if not USE_COLOR:
        output = _SGR_RE.sub("", output)
    out = sys.stdout
    out.write(output)
    out.flush()

    set_iterm_title(action_title, project_name, repo_name, branch, dir_name)


def _fallback_output() -> None:
    """Print minimal output so the statusline area stays allocated."""
    lines = (8 if CODEX_ENABLED and CODEX_AUTH_FILE.exists() else 7) + ADDON_LINE_COUNT
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

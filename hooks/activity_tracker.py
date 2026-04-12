#!/usr/bin/env python3
"""
UserPromptSubmit hook - Records heartbeats for time tracking.

Runs silently on every prompt, recording activity for the current task
via orbit_db's heartbeat-auto command. Skips slash commands, shell
commands, and empty prompts.
"""

import json
import os
import re
import subprocess
import sys

SKIP_PATTERNS = [
    re.compile(r"^/\w+"),        # Slash commands
    re.compile(r"^!\w+"),        # Shell commands
    re.compile(r"^exit$", re.I),
    re.compile(r"^clear$", re.I),
    re.compile(r"^help$", re.I),
    re.compile(r"^y(es)?$", re.I),
    re.compile(r"^n(o)?$", re.I),
    re.compile(r"^\s*$"),        # Empty prompts
]


def should_skip(prompt: str) -> bool:
    trimmed = prompt.strip()
    return any(p.search(trimmed) for p in SKIP_PATTERNS)


def main():
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        return

    # Skip in subagent context
    if data.get("agent_id"):
        return

    raw_prompt = data.get("prompt", "")
    if isinstance(raw_prompt, list):
        raw_prompt = " ".join(
            b.get("text", "") for b in raw_prompt if isinstance(b, dict) and b.get("type") == "text"
        )
    prompt = raw_prompt if isinstance(raw_prompt, str) else ""
    if should_skip(prompt):
        return

    cwd = data.get("cwd", "") or os.getcwd()
    session_id = data.get("session_id", "")

    try:
        subprocess.run(
            ["python3", "-m", "orbit_db", "heartbeat-auto"],
            cwd=cwd,
            timeout=2,
            capture_output=True,
            env={**os.environ, "CLAUDE_SESSION_ID": session_id},
        )
    except (subprocess.TimeoutExpired, OSError):
        pass


if __name__ == "__main__":
    main()

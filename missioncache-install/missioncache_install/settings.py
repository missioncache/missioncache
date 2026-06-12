"""Read/write ~/.claude/settings.json safely.

Always backs up before destructive edits. Every function is idempotent so
`orbit-install` can be re-run without duplicating entries.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any


SETTINGS_FILE = Path.home() / ".claude" / "settings.json"
EDIT_COUNT_URL = "http://localhost:8787/api/hooks/edit-count"
EDIT_COUNT_MATCHER = "Edit|Write|NotebookEdit"


def load() -> dict[str, Any]:
    """Load settings.json. Returns {} if missing."""
    if not SETTINGS_FILE.exists():
        return {}
    return json.loads(SETTINGS_FILE.read_text())


def save(settings: dict[str, Any]) -> None:
    """Write settings.json, creating parent dirs if needed."""
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2))


def backup() -> Path | None:
    """Copy settings.json to settings.json.bak. Returns backup path or None."""
    if not SETTINGS_FILE.exists():
        return None
    bak = SETTINGS_FILE.with_suffix(".json.bak")
    shutil.copy2(SETTINGS_FILE, bak)
    return bak


def enable_plugin(plugin_id: str) -> None:
    """Set enabledPlugins[plugin_id] = True. Idempotent."""
    s = load()
    s.setdefault("enabledPlugins", {})[plugin_id] = True
    save(s)


def disable_plugin(plugin_id: str) -> None:
    """Remove plugin from enabledPlugins. No-op if absent."""
    s = load()
    ep = s.get("enabledPlugins", {})
    if plugin_id in ep:
        ep.pop(plugin_id)
        save(s)


def set_statusline(command: str = "orbit-statusline") -> Path | None:
    """Set statusLine.command. Backs up when overwriting a different existing command.

    Returns the backup path if one was written, otherwise None.
    """
    settings = load()
    existing = settings.get("statusLine")
    needs_backup = bool(
        existing
        and isinstance(existing, dict)
        and existing.get("command") != command
    )
    bak = backup() if needs_backup else None
    settings["statusLine"] = {"type": "command", "command": command}
    save(settings)
    return bak


def unset_statusline() -> None:
    """Remove the statusLine block. No-op if absent."""
    settings = load()
    if "statusLine" not in settings:
        return
    settings.pop("statusLine", None)
    save(settings)


def ensure_edit_count_hook(url: str = EDIT_COUNT_URL) -> bool:
    """Wire the PostToolUse Edit|Write|NotebookEdit -> URL HTTP hook.

    Idempotent: returns True if a new entry was added, False if already present.
    """
    settings = load()
    hooks = settings.setdefault("hooks", {})
    post = hooks.setdefault("PostToolUse", [])
    for entry in post:
        if entry.get("matcher") != EDIT_COUNT_MATCHER:
            continue
        for h in entry.get("hooks", []):
            if h.get("type") == "http" and h.get("url") == url:
                return False  # already wired
        entry.setdefault("hooks", []).append({"type": "http", "url": url})
        save(settings)
        return True
    post.append(
        {"matcher": EDIT_COUNT_MATCHER, "hooks": [{"type": "http", "url": url}]}
    )
    save(settings)
    return True


def remove_edit_count_hook(url: str = EDIT_COUNT_URL) -> bool:
    """Remove the edit-count hook entry. Returns True if something was removed."""
    settings = load()
    post = settings.get("hooks", {}).get("PostToolUse", [])
    changed = False
    for entry in list(post):
        if entry.get("matcher") != EDIT_COUNT_MATCHER:
            continue
        hooks = entry.get("hooks", [])
        remaining = [
            h for h in hooks
            if not (h.get("type") == "http" and h.get("url") == url)
        ]
        if len(remaining) != len(hooks):
            changed = True
            entry["hooks"] = remaining
            if not remaining:
                post.remove(entry)
    if changed:
        save(settings)
    return changed

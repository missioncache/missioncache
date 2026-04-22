"""State tracking via ~/.claude/orbit-install.state.json.

Records what the installer did so --update and --uninstall know what to
operate on. Every write timestamps updated_at; reads tolerate corruption
by moving the bad file aside.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STATE_FILE = Path.home() / ".claude" / "orbit-install.state.json"
STATE_SCHEMA_VERSION = 1


def load() -> dict[str, Any]:
    """Load state from disk. Returns a fresh empty state if missing or corrupt."""
    if not STATE_FILE.exists():
        return _empty_state()
    try:
        return json.loads(STATE_FILE.read_text())
    except json.JSONDecodeError:
        corrupt = STATE_FILE.with_suffix(".json.corrupt")
        STATE_FILE.rename(corrupt)
        return _empty_state()


def save(state: dict[str, Any]) -> None:
    """Persist state to disk. Stamps updated_at."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = _now_iso()
    STATE_FILE.write_text(json.dumps(state, indent=2))


def record_component(component: str, info: dict[str, Any]) -> None:
    """Record that a component was installed with the given metadata."""
    state = load()
    state.setdefault("components", {})[component] = info
    save(state)


def remove_component(component: str) -> dict[str, Any] | None:
    """Remove a component entry. Returns its prior metadata, or None if absent."""
    state = load()
    info = state.get("components", {}).pop(component, None)
    save(state)
    return info


def installed_components() -> list[str]:
    """List components currently tracked in state."""
    return list(load().get("components", {}).keys())


def set_mode(mode: str) -> None:
    """Record install mode (pypi or local)."""
    state = load()
    state["mode"] = mode
    save(state)


def _empty_state() -> dict[str, Any]:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "installed_at": _now_iso(),
        "mode": "pypi",
        "components": {},
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

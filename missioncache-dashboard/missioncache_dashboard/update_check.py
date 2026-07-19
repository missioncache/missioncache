"""MissionCache update discovery.

Compares the installed missioncache packages against PyPI and caches the
answer in ``~/.missioncache/update-check.json``. Consumed by the dashboard
(``/api/update-check`` -> UI banner), the statusline (Vitals indicator),
and the ``/missioncache:load`` resume notice (which only reads the cache,
never fetches).

Stdlib-only on purpose: the statusline imports this module and must stay
importable without third-party dependencies.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from importlib import metadata
from pathlib import Path

# Release sentinels: every release to date bumped at least one of these
# (verified against the CHANGELOG's "Published package versions" lines).
# mcp-missioncache and missioncache-install are not importable from this
# environment (uvx-isolated), so they cannot be compared - a release that
# bumped ONLY those would go unannounced until the next sentinel bump.
PACKAGES = ("missioncache-db", "missioncache-dashboard", "missioncache-auto")

UPDATE_COMMAND = "uvx --refresh missioncache-install@latest --update"
CACHE_PATH = Path.home() / ".missioncache" / "update-check.json"
CACHE_TTL = 6 * 60 * 60  # seconds; PyPI is CDN-backed, be a polite client


def _parse_version(version: str) -> tuple[int, ...]:
    """A semver-ish string as a comparable int tuple; non-numeric segments
    collapse to 0 so malformed input never crashes the comparison."""
    parts = []
    for seg in version.split("."):
        digits = "".join(ch for ch in seg if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _fetch_latest(package: str, timeout: float) -> str | None:
    url = f"https://pypi.org/pypi/{package}/json"
    req = urllib.request.Request(url, headers={"User-Agent": "missioncache-update-check"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)["info"]["version"]


def _read_cache() -> dict | None:
    try:
        data = json.loads(CACHE_PATH.read_text())
        if isinstance(data, dict) and "checked_at" in data:
            return data
    except (OSError, ValueError):
        pass
    return None


def _write_cache(status: dict) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_name(f"{CACHE_PATH.name}.tmp.{os.getpid()}")
        tmp.write_text(json.dumps(status))
        os.replace(tmp, CACHE_PATH)
    except OSError:
        pass  # a failed cache write only costs an extra fetch next time


def get_update_status(ttl: int = CACHE_TTL, timeout: float = 3.0) -> dict:
    """The update status, from cache when fresh, else freshly fetched.

    Never raises: on fetch failure the previous cache is returned even if
    stale (better an old answer than none), and with no cache at all a
    neutral no-update answer is returned.
    """
    cached = _read_cache()
    if cached is not None and time.time() - cached["checked_at"] < ttl:
        return cached

    packages: dict[str, dict] = {}
    update_available = False
    try:
        for pkg in PACKAGES:
            try:
                installed = metadata.version(pkg)
            except metadata.PackageNotFoundError:
                continue
            latest = _fetch_latest(pkg, timeout)
            if latest is None:
                continue
            outdated = _parse_version(latest) > _parse_version(installed)
            packages[pkg] = {
                "installed": installed,
                "latest": latest,
                "outdated": outdated,
            }
            update_available = update_available or outdated
    except Exception:
        # Keep the previous answer (stale beats none) but stamp checked_at
        # so the next TTL window passes before another fetch attempt - an
        # offline machine must not re-fetch on every render.
        status = cached if cached is not None else {
            "update_available": False,
            "packages": {},
            "command": UPDATE_COMMAND,
        }
        status = {**status, "checked_at": time.time(), "error": "fetch failed"}
        _write_cache(status)
        return status

    status = {
        "checked_at": time.time(),
        "update_available": update_available,
        "packages": packages,
        "command": UPDATE_COMMAND,
    }
    _write_cache(status)
    return status

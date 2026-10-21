"""MissionCache update discovery.

Compares the installed missioncache packages, the Claude Code plugin and
the mcp-missioncache on PATH against their latest release, and caches the
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
import shutil
from pathlib import Path

from missioncache_db import MISSIONCACHE_ROOT, replace_with_retry
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from importlib import metadata

# Packages importable from this environment, compared against PyPI.
PACKAGES = ("missioncache-db", "missioncache-dashboard", "missioncache-auto")

# The two installs this environment cannot import: the Claude Code plugin,
# which Claude Code records itself, and the mcp-missioncache on PATH that
# Codex, OpenCode and VSCode run from its own uv or pipx venv.
PLUGIN_KEY = "claude-code-plugin"
PLUGIN_ID = "missioncache@missioncache"
PLUGIN_MANIFEST_URL = (
    "https://raw.githubusercontent.com/missioncache/missioncache/main/.claude-plugin/plugin.json"
)
MCP_DIST = "mcp-missioncache"

UPDATE_COMMAND = "uvx --refresh missioncache-install@latest --update"
# Derives from missioncache_db's root rather than re-deriving it, so the
# MISSIONCACHE_ROOT override reaches this cache too. Stays a module constant
# because the file is this module's own artifact and a test patches the name.
CACHE_PATH = MISSIONCACHE_ROOT / "update-check.json"
CACHE_TTL = 60 * 60  # seconds; a release shows within the hour, five reads an hour at most


def _parse_version(version: str) -> tuple[int, ...]:
    """A semver-ish string as a comparable int tuple; non-numeric segments
    collapse to 0 so malformed input never crashes the comparison."""
    parts = []
    for seg in version.split("."):
        digits = "".join(ch for ch in seg if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _dist_editable(dist: metadata.Distribution) -> bool:
    """True when the package is an editable (pip install -e) install.

    Editable metadata freezes the version at whatever install time recorded,
    so comparing it to PyPI is meaningless - the clone is its own source of
    truth and no update command applies. Detection reads the distribution's
    direct_url.json (PEP 610), the same record pip writes for -e installs.
    """
    try:
        raw = dist.read_text("direct_url.json")
        if not raw:
            return False
        return bool(json.loads(raw).get("dir_info", {}).get("editable"))
    except Exception:
        return False


def _installed_plugin() -> str | None:
    """The Claude Code plugin version from Claude Code's install record.

    Only the GitHub marketplace install counts. ``missioncache@local`` is a
    maintainer checkout, and no update command applies to it.
    """
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    try:
        record = json.loads(
            (config / "plugins" / "installed_plugins.json").read_text(encoding="utf-8")
        )
        entries = record["plugins"][PLUGIN_ID]
        user = [e for e in entries if e.get("scope") == "user"]
        return (user or entries)[0]["version"]
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return None


def _mcp_venvs(exe: str) -> list[Path]:
    """Where the mcp-missioncache on PATH may have its venv.

    On macOS and Linux the PATH entry is a symlink into the venv. On Windows
    uv and pipx copy a launcher instead, so their tool directories are tried.
    """
    tool_dirs = [
        os.environ.get("UV_TOOL_DIR"),
        os.environ.get("APPDATA") and Path(os.environ["APPDATA"], "uv", "data", "tools"),
        os.environ.get("PIPX_HOME") and Path(os.environ["PIPX_HOME"], "venvs"),
    ]
    return [Path(exe).resolve().parent.parent] + [
        Path(d) / MCP_DIST for d in tool_dirs if d
    ]


def _installed_mcp() -> tuple[str, bool] | None:
    """(version, editable) of the mcp-missioncache on PATH, or None without one.

    Claude Code runs its own copy from the plugin, which the plugin check
    covers. This one serves Codex, OpenCode and VSCode.
    """
    exe = shutil.which(MCP_DIST)
    if not exe:
        return None
    for venv in _mcp_venvs(exe):
        sites = [*venv.glob("lib/python*/site-packages"), venv / "Lib" / "site-packages"]
        paths = [str(p) for p in sites if p.is_dir()]
        dist = next(iter(metadata.distributions(name=MCP_DIST, path=paths)), None) if paths else None
        if dist is not None and dist.version:
            return dist.version, _dist_editable(dist)
    return None


def _fetch_json(url: str, timeout: float) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "missioncache-update-check"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _fetch_latest(package: str, timeout: float) -> str | None:
    if package == PLUGIN_KEY:
        return _fetch_json(PLUGIN_MANIFEST_URL, timeout)["version"]
    return _fetch_json(f"https://pypi.org/pypi/{package}/json", timeout)["info"]["version"]


_FAILED = object()


def _try_fetch(package: str, timeout: float):
    """The latest version, or _FAILED, so one unreachable host spares the rest."""
    try:
        return _fetch_latest(package, timeout)
    except Exception:
        return _FAILED


def _installed() -> list[tuple[str, str, bool]]:
    """(name, installed version, editable) for every install found here."""
    found = []
    for pkg in PACKAGES:
        try:
            found.append((pkg, metadata.version(pkg), _dist_editable(metadata.distribution(pkg))))
        except metadata.PackageNotFoundError:
            continue
    plugin = _installed_plugin()
    if plugin:
        found.append((PLUGIN_KEY, plugin, False))
    try:
        mcp = _installed_mcp()
    except Exception:  # a broken venv must not block the other checks
        mcp = None
    if mcp:
        found.append((MCP_DIST, *mcp))
    return found


def _read_cache() -> dict | None:
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "checked_at" in data:
            return data
    except (OSError, ValueError):
        pass
    return None


def _write_cache(status: dict) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_name(f"{CACHE_PATH.name}.tmp.{os.getpid()}")
        tmp.write_text(json.dumps(status), encoding="utf-8")
        replace_with_retry(tmp, CACHE_PATH)
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
    try:
        found = _installed()
        with ThreadPoolExecutor(max_workers=max(len(found), 1)) as pool:
            latests = list(pool.map(lambda entry: _try_fetch(entry[0], timeout), found))
        if found and all(latest is _FAILED for latest in latests):
            raise OSError("every fetch failed")
        previous = (cached or {}).get("packages", {})
        for (pkg, installed, editable), latest in zip(found, latests):
            if latest is _FAILED:
                # This host failed but others answered: keep its last answer.
                if pkg in previous:
                    packages[pkg] = previous[pkg]
                continue
            if latest is None:
                continue
            outdated = (
                not editable
                and _parse_version(latest) > _parse_version(installed)
            )
            packages[pkg] = {
                "installed": installed,
                "latest": latest,
                "outdated": outdated,
            }
            if editable:
                packages[pkg]["editable"] = True
        update_available = any(p.get("outdated") for p in packages.values())
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

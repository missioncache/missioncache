"""Per-machine path map for cross-machine project sharing.

Translates a bundle's portable identifiers (git remotes, vault logical names,
anchor roots) into THIS machine's absolute paths. Lives at
``~/.missioncache/machine.json`` (or under ``$MISSIONCACHE_ROOT``). It is
machine-local and must NEVER be synced between machines.

Mirrors the atomic-write + tolerant-read pattern of
``missioncache_dashboard.lib.config`` but uses a full-document read-modify-write
so a nested section is never clobbered by a shallow ``{**DEFAULTS, **file}``
merge.
"""

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional

# Honors the MISSIONCACHE_ROOT override (same as missioncache_db.DB_PATH) so a
# throwaway root for tests / cross-machine import is respected. Computed
# independently of the heavy package __init__ (mirrors lib/config.CONFIG_FILE).
# Tests monkeypatch this constant directly.
# `or` (not a default arg) so a set-but-empty MISSIONCACHE_ROOT falls back to
# the real dir instead of Path("") == cwd.
MACHINE_FILE = (
    Path(os.environ.get("MISSIONCACHE_ROOT") or str(Path.home() / ".missioncache"))
    / "machine.json"
)

# CLI <kind> -> machine.json section name. Public: the CLI validates against it.
SECTION = {"repo": "repos", "vault": "vaults", "anchor": "anchors"}

_DEFAULTS: dict[str, Any] = {"version": 1, "repos": {}, "vaults": {}, "anchors": {}}


def _fresh_defaults() -> dict[str, Any]:
    return {k: (dict(v) if isinstance(v, dict) else v) for k, v in _DEFAULTS.items()}


def _read() -> dict[str, Any]:
    """Return the machine map; tolerate a missing or corrupt file.

    Missing top-level sections are filled from defaults WITHOUT clobbering any
    section that is present (the bug the shallow ``{**DEFAULTS, **file}`` merge
    in lib/config.py:71 would introduce on nested dicts).
    """
    try:
        data = json.loads(MACHINE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return _fresh_defaults()
    if not isinstance(data, dict):
        return _fresh_defaults()
    for key, default in _DEFAULTS.items():
        if key not in data:
            data[key] = dict(default) if isinstance(default, dict) else default
    return data


def _write(data: dict[str, Any]) -> None:
    """Atomically write the full document via tempfile + os.replace."""
    MACHINE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=MACHINE_FILE.parent,
        prefix=".machine.",
        suffix=".tmp",
        delete=False,
    ) as tf:
        json.dump(data, tf, indent=2, sort_keys=True)
        tf.write("\n")
        tempname = tf.name
    os.replace(tempname, MACHINE_FILE)


def remote_key(url: str) -> str:
    """Canonicalize a git remote URL to a stable cross-machine key.

    Collapses ssh/https/git protocols, scp-form, an ``.git`` suffix, and a
    trailing slash to one key, lowercasing the host only. So
    ``git@github.com:Owner/Repo.git`` and ``https://github.com/Owner/Repo``
    both map to ``github.com/Owner/Repo``.
    """
    s = url.strip()
    had_scheme = False
    for scheme in ("ssh://", "https://", "http://", "git://"):
        if s.startswith(scheme):
            s = s[len(scheme):]
            had_scheme = True
            break
    # Strip any userinfo (git@, user@, user:pass@) before the host, so an
    # embedded credential never lands in the key (or in machine.json).
    at = s.find("@")
    slash = s.find("/")
    if at != -1 and (slash == -1 or at < slash):
        s = s[at + 1:]
    if ":" in s:
        host, rest = s.split(":", 1)
        # Only an explicit scheme makes the colon a port (host:PORT/path). A
        # scp remote (git@host:path) has no port, so a numeric first path
        # segment (e.g. a numeric owner) must be preserved, not stripped.
        if had_scheme:
            m = re.match(r"^\d+/(.*)$", rest)
            rest = m.group(1) if m else rest
        s = f"{host}/{rest}"
    s = s.rstrip("/")
    if s.endswith(".git"):
        s = s[:-4]
    head, sep, tail = s.partition("/")
    return f"{head.lower()}/{tail}" if sep else head.lower()


def resolve(kind: str, name: str) -> Optional[str]:
    """Return the local path mapped for (kind, name), or None.

    ``repo`` lookups normalize ``name`` through ``remote_key`` so a raw URL
    still matches a stored canonical key. ``anchor:HOME`` falls back to the
    live home dir even when unset, so HOME rewrites work on a fresh machine.
    """
    section = SECTION[kind]
    lookup = remote_key(name) if kind == "repo" else name
    val = _read().get(section, {}).get(lookup)
    if val is None and kind == "anchor" and name == "HOME":
        return str(Path.home())
    return val


def record(kind: str, name: str, localpath: str) -> str:
    """Store (kind, name) -> localpath via full-document RMW. Returns the key.

    For ``repo``, ``name`` is normalized through ``remote_key`` first so ssh
    and https variants collapse to one entry.
    """
    section = SECTION[kind]
    key = remote_key(name) if kind == "repo" else name
    data = _read()
    data.setdefault(section, {})[key] = str(localpath)
    _write(data)
    return key


def all_mappings() -> dict[str, Any]:
    """Return the full machine map (the dict the importer consumes)."""
    return _read()


def _git_remote(path: str) -> Optional[str]:
    """Return the origin remote URL for a repo path, or None if not a git repo."""
    try:
        r = subprocess.run(
            ["git", "-C", path, "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = r.stdout.strip()
    return out if r.returncode == 0 and out else None


def _is_linked_worktree(path: str) -> bool:
    """True if ``path`` is a linked git worktree (git-common-dir != git-dir)."""
    try:
        common = subprocess.run(
            ["git", "-C", path, "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        gitdir = subprocess.run(
            ["git", "-C", path, "rev-parse", "--git-dir"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if common.returncode != 0 or gitdir.returncode != 0:
        return False
    return common.stdout.strip() != gitdir.stdout.strip()


def seed(db: Any, dry_run: bool = False) -> dict[str, list]:
    """Best-effort pre-fill of the map from provable local state.

    The HOME anchor is always set. Each tracked repo with a git origin maps to
    ``repos[remote_key]``; linked worktrees are skipped so they never collapse
    the main repo's mapping. Non-git tracked folders are proposed (not stored)
    under vaults (Obsidian-ish path) or anchors. Writes nothing when ``dry_run``.
    Returns ``{"added": [...], "skipped": [...], "proposed": [...]}``.
    """
    data = _read()
    data.setdefault("anchors", {})["HOME"] = str(Path.home())
    added: list = []
    skipped: list = []
    proposed: list = []
    for repo in db.get_repos(active_only=False):
        path = repo.path
        remote = _git_remote(path)
        if remote:
            if _is_linked_worktree(path):
                skipped.append((path, "linked worktree"))
                continue
            key = remote_key(remote)
            existing = data.get("repos", {}).get(key)
            if existing is not None and existing != path:
                # Two clones of one remote: keep the first, flag the rest
                # rather than silently last-wins onto the wrong local path.
                skipped.append((path, f"remote {key} already mapped to {existing}"))
                continue
            data.setdefault("repos", {})[key] = path
            added.append(("repo", key, path))
        elif "Obsidian" in path:
            proposed.append(("vault", repo.short_name, path))
        else:
            proposed.append(("anchor", repo.short_name, path))
    if not dry_run:
        _write(data)
    return {"added": added, "skipped": skipped, "proposed": proposed}

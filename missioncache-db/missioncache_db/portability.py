"""Cross-machine project export (Phase 2 of the cross-machine sharing plan).

Builds a portable bundle for one MissionCache project: a verbatim recursive
copy of its markdown tree plus a ``missioncache.json`` manifest that carries the
logical task row and every machine-specific reference as a *portable
identifier* (git remotes canonicalized, vault wikilinks by logical name,
embedded absolute paths tokenized against ``${HOME}`` / ``${repo:...}`` /
``${vault:...}``).

Export never edits the source project files - it only reads and copies them.
The DB (``tasks.db``) never travels; only the markdown tree + manifest move.
Time history never merges: ``time_total_seconds`` rides along as display-only
origin metadata. Caveat: with the default ``include_time=True`` the export runs
``process_heartbeats()``, which writes aggregated session rows into ``tasks.db``
(normal time accounting). Pass ``include_time=False`` / ``--no-time`` for a run
with no DB side effects.

Spec: ``docs/cross-machine-sharing-plan.md`` sections 3 (manifest), 4.1 (CLI),
6 (reference kinds), 7 (file list), 10 (phasing). Import (``import_bundle``,
``upsert_imported_task``) is Phase 3 and lives separately.

Layering note: this module never imports ``missioncache_dashboard`` - the
dashboard depends on this package, not the other way around.
"""

import fnmatch
import hashlib
import json
import os
import re
import shutil
import socket
import sys
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from missioncache_db import machine_map

MANIFEST_VERSION = 1
BUNDLE_KIND = "missioncache-project-bundle"

# Junk files dropped from the bundle silently (§7). Matched on a lowercased
# basename (see _matches), so patterns are lowercase.
_JUNK_GLOBS = ("*.lock", "*.bak", "*.tmp")
_JUNK_NAMES = (".ds_store",)
# Directories never descended into.
_EXCLUDE_DIRS = (".git",)
# Secret/credential files dropped from the bundle WITH a warning (the bundle is
# meant to be shared, so a leaked secret matters; junk does not). Matched on a
# lowercased basename, so a `.ENV` / `SERVER.PEM` variant cannot slip through.
_SECRET_GLOBS = (
    ".env", ".env.*", "*.env", "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore",
    "*.jks", "*.ppk", "*.kdbx", "*.ovpn", "*-key.json", "secrets.*",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
)
_SECRET_NAMES = (
    "credentials", "credentials.json", ".netrc", ".npmrc", ".pypirc",
    ".git-credentials", ".htpasswd", ".pgpass",
)

# Trailing sentence punctuation that is not part of a captured path.
_PATH_STRIP_CHARS = ".,;:)]}'\""
# Characters that continue a path token (a slash is allowed; whitespace, closing
# brackets, quotes, commas, and backticks terminate it).
_PATH_TAIL_RE = r"[^\s)\]}'\"`,]*"
# A single terminator character (the boundary an anchor must end on so a HOME or
# repo root does not match a prefix of an unrelated sibling directory name).
_PATH_TERMINATOR = r"[\s)\]}'\"`,]"


def _generator() -> str:
    try:
        from importlib.metadata import version

        return f"missioncache-db/{version('missioncache-db')}"
    except Exception:
        return "missioncache-db/unknown"


def _now_iso() -> str:
    """Local time with offset, e.g. ``2026-06-28T14:03:11+03:00``."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _matches(name: str, globs: tuple, names: tuple) -> bool:
    # Case-fold so `.ENV` / `SERVER.PEM` cannot bypass the denylist (fnmatch is
    # case-sensitive on Linux/macOS; the glob/name lists are lowercase).
    low = name.lower()
    return low in names or any(fnmatch.fnmatch(low, g) for g in globs)


def _home_rel(path: str, home: str) -> Optional[str]:
    """POSIX-relative path from ``home`` if ``path`` is under it, else None.

    Returns ``""`` for the home dir itself (so a bare home reference tokenizes
    to ``${HOME}``, not ``${HOME}/.``).
    """
    try:
        rel = Path(path).relative_to(home)
    except ValueError:
        return None
    return "" if str(rel) == "." else rel.as_posix()


def _iter_md_files(scan_dir: Path):
    """Yield ``(rel_path, text)`` for every ``.md`` file under ``scan_dir``.

    Shared by both reference scanners so the walk + decode policy lives in one
    place. ``errors="replace"`` tolerates a non-UTF-8 byte. A read OSError is
    NOT swallowed: ``scan_dir`` is the just-copied bundle tree, so a file that
    cannot be read back is a real anomaly. Letting it propagate fails the export
    loudly (the CLI catches OSError) rather than silently dropping that file's
    references from the manifest while it stays in ``files[]``.
    """
    for rel in sorted(
        (p.relative_to(scan_dir) for p in scan_dir.rglob("*.md")),
        key=lambda p: p.as_posix(),
    ):
        yield rel, (scan_dir / rel).read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Git classification (runtime, per row - never a hardcoded git/non-git list)
# ---------------------------------------------------------------------------


def _is_portable_remote(remote: str) -> bool:
    """True if ``remote`` is a real cross-machine remote, not a local path.

    A local-filesystem origin (``/Users/...``, ``./mirror``, ``~/repo``,
    ``file://...``) canonicalizes to a machine-specific string that resolves to
    nothing on the other machine, so it must NOT masquerade as a portable git
    key. ``file://`` is a URL but addresses the local filesystem, so it is
    excluded before the generic ``://`` check.
    """
    if remote.startswith(("/", ".", "~", "file://")):
        return False
    if "://" in remote:
        return True
    if ":" in remote:  # scp form user@host:path - require a dotted host
        host = remote.split("@")[-1].split(":", 1)[0]
        return "." in host
    return False


def _classify_repo(db: Any, task: Any) -> tuple[Optional[dict], list]:
    """Classify a task's bound repo into a portable discriminated union.

    Returns ``(repo_dict_or_None, warnings)``. ``None`` means the task has no
    repo binding (non-coding task or a dangling repo_id). Classification is by
    the actual ``git remote get-url origin`` exit, never a hardcoded list. A
    non-existent ``repositories.path``, a missing ``git`` binary, a local-path
    origin, and a missing ``origin`` remote are each tolerated and produce a
    distinct, honest warning rather than a crash or a misleading "no remote".
    """
    if task is None or task.repo_id is None:
        return None, []
    repo = db.get_repo(task.repo_id)
    if repo is None:
        return None, [
            f"task repo_id={task.repo_id} has no repositories row; exported with repo=null"
        ]
    path = repo.path
    home = str(Path.home())
    short = repo.short_name
    warnings: list = []

    if not Path(path).exists():
        hr = _home_rel(path, home)
        warnings.append(
            f"repo path {path} is missing on disk; exported as "
            f"{'home-relative' if hr is not None else 'anchor'} (not git-verified)"
        )
        return _anchor_or_home_relative(short, hr), warnings

    if shutil.which("git") is None:
        warnings.append(
            f"git not found on PATH; repo '{short}' ({path}) exported as anchor, "
            f"classification unverified"
        )
        return _anchor_or_home_relative(short, _home_rel(path, home)), warnings

    status, remote = machine_map._git_run(path, "remote", "get-url", "origin")

    if status == "ok" and remote and _is_portable_remote(remote):
        is_wt = machine_map._is_linked_worktree(path)
        if is_wt is None:
            # Probe failed mid-classification - don't silently claim "not a
            # worktree", which would let import collapse it onto the parent.
            warnings.append(
                f"repo '{short}' worktree status could not be determined (git probe "
                f"failed); exported worktree=false, unverified"
            )
            is_wt = False
        tstat, top = machine_map._git_run(path, "rev-parse", "--show-toplevel")
        subpath = ""
        if tstat == "ok" and top:
            rel = os.path.relpath(path, top)
            subpath = "" if rel == "." else rel
        elif tstat == "error":
            warnings.append(
                f"repo '{short}' subpath could not be determined (git probe failed); "
                f"exported as repo root"
            )
        branch = None
        if is_wt:
            bstat, br = machine_map._git_run(path, "rev-parse", "--abbrev-ref", "HEAD")
            # A detached worktree HEAD reports the literal "HEAD" - report None.
            branch = br if (bstat == "ok" and br != "HEAD") else None
        repo_dict = {
            "kind": "git",
            "remote": machine_map.remote_key(remote),
            "subpath": subpath,
            "worktree": is_wt,
            "worktree_branch": branch,
            "short_name": short,
        }
        if is_wt:
            warnings.append(
                f"repo '{short}' is a linked git worktree; import will force "
                f"needs-mapping so it is not collapsed onto the parent checkout "
                f"(worktree binding)"
            )
        return repo_dict, warnings

    # No portable git remote - say WHY (distinct message per outcome), never a
    # flat "no remote" that misattributes a timeout or a local-path origin.
    if status == "ok" and remote:
        warnings.append(
            f"repo '{short}' origin is a local path ({remote}), not a portable "
            f"remote; exported as anchor (import needs a machine.json anchor mapping)"
        )
    elif status == "error":
        warnings.append(
            f"git probe failed (timeout/transient error) for repo '{short}' ({path}); "
            f"exported as anchor, classification unverified"
        )
    else:  # status == "fail" - git ran cleanly, no origin / not a repo
        warnings.append(
            f"repo '{short}' ({path}) has no 'origin' git remote; exported as anchor "
            f"(import needs a machine.json anchor mapping)"
        )
    return _anchor_or_home_relative(short, _home_rel(path, home)), warnings


def _anchor_or_home_relative(short_name: str, home_relative: Optional[str]) -> dict:
    """Build the ``home-relative`` ref when under HOME, else the ``anchor`` ref."""
    if home_relative is not None:
        return {"kind": "home-relative", "home_relative": home_relative,
                "short_name": short_name}
    return {"kind": "anchor", "anchor": short_name,
            "home_relative": None, "short_name": short_name}


# ---------------------------------------------------------------------------
# Reference scanners (code-span aware)
# ---------------------------------------------------------------------------

_HUB_RE = re.compile(r"Hub:\s*\[\[([^\]]+)\]\]")
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_NOTE_NAME_RE = re.compile(r"^[A-Za-z0-9][\w .-]*$")


def _strip_code_spans_lines(text: str) -> list:
    """Return per-line text with code spans removed, preserving line count.

    Fenced (```` ``` ````) blocks become empty lines; inline `` `code` `` spans
    are blanked. Line count is preserved so a ``<file>:<line>`` reference stays
    accurate.
    """
    out: list = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append("")
            continue
        if in_fence:
            out.append("")
            continue
        out.append(re.sub(r"`[^`]*`", "", line))
    return out


def _scan_vaults(scan_dir: Path) -> list:
    """Inventory ``Hub:``/``[[wikilink]]`` references across the markdown tree.

    Report-only: the markdown is never rewritten. Code spans are stripped first
    so bash ``[[ -f ]]``, TOML ``[[plugins."x"]]``, and ``[[a, b, c]]`` code do
    not register as notes. The ``Hub:`` line is high-confidence; other valid
    wikilinks are low-confidence.
    """
    found: list = []
    for rel, text in _iter_md_files(scan_dir):
        for lineno, line in enumerate(_strip_code_spans_lines(text), 1):
            if "[[" not in line:
                continue
            hub = _HUB_RE.search(line)
            if hub and _NOTE_NAME_RE.match(hub.group(1).strip()):
                found.append({
                    "note": hub.group(1).strip(),
                    "raw": hub.group(0),
                    "source": f"{rel.as_posix()}:{lineno}",
                    "confidence": "high",
                })
                continue
            for m in _WIKILINK_RE.finditer(line):
                note = m.group(1).strip()
                if not _NOTE_NAME_RE.match(note):
                    continue
                found.append({
                    "note": note,
                    "raw": m.group(0),
                    "source": f"{rel.as_posix()}:{lineno}",
                    "confidence": "low",
                })
    return found


def _build_path_roots(db: Any) -> list:
    """Roots for embedded-path tokenization, longest-local-path first.

    Each entry is ``(local_root, token_prefix, classification)``. Git repos with
    a PORTABLE remote tokenize to ``${repo:<canonical-remote>}``; configured
    vault roots to ``${vault:<logical>}``. Longest-first so a repo/vault root
    under HOME wins over the bare ``${HOME}`` fallback.
    """
    roots: list = []
    for repo in db.get_repos(active_only=False):
        path = repo.path
        if not Path(path).exists():
            continue
        remote = machine_map._git_remote(path)
        if remote and _is_portable_remote(remote):
            roots.append((path, f"${{repo:{machine_map.remote_key(remote)}}}", "repo"))
    for name, vpath in machine_map.all_mappings().get("vaults", {}).items():
        if vpath:
            roots.append((vpath, f"${{vault:{name}}}", "vault"))
    roots.sort(key=lambda t: len(t[0]), reverse=True)
    return roots


def _tokenize_path(raw: str, home: str, roots: list) -> Optional[tuple]:
    """Tokenize a ``~`` / known-root / HOME-anchored path into a portable form.

    Returns ``(token, classification)`` or ``None``. ``classification`` is
    ``repo`` | ``vault`` | ``home-relative``. ``None`` means the candidate
    resolved to none of those (defensive - anchor-driven detection only surfaces
    home/root-anchored candidates).
    """
    expanded = home + raw[1:] if raw.startswith("~") else raw
    for local_root, token_prefix, classification in roots:
        root = local_root.rstrip("/")
        if expanded == root or expanded.startswith(root + "/"):
            rest = expanded[len(root):].lstrip("/")
            token = f"{token_prefix}/{rest}" if rest else token_prefix
            return token, classification
    hr = _home_rel(expanded, home)
    if hr is not None:
        return (f"${{HOME}}/{hr}" if hr else "${HOME}"), "home-relative"
    return None


def _target_kind(raw: str, classification: str) -> str:
    low = raw.lower()
    if classification == "vault":
        return "vault-note" if low.endswith(".md") else "vault-path"
    if "/.claude/plans/" in low:
        return "plan-file"
    if low.endswith(".md"):
        return "source-file"
    if Path(raw).suffix:
        return "source-file"
    return "dir"


def _embedded_pattern(home: str, roots: list) -> "re.Pattern":
    """A regex that matches only paths anchored at ``~``, HOME, or a known root.

    Detection is anchor-driven on purpose: a naive ``/``-rooted scan captures
    URL paths (``//localhost``, ``/api/sync``) and regex fragments as noise. By
    anchoring on the actual local roots (HOME + git/vault roots), only paths
    that need reconciliation on the target machine are inventoried; everything
    else (URLs, API routes, other people's home dirs in docs) is dropped.

    The trailing ``(?=/|$|<term>)`` boundary is load-bearing: without it, anchor
    ``/h/work/foo`` would match a prefix of the unrelated sibling
    ``/h/work/foobar/x``. The boundary forces a shorter anchor (HOME) to win
    there, so the sibling tokenizes correctly under ``${HOME}`` instead of being
    mislabeled as ``${repo:foo}``.
    """
    anchors = sorted(
        {home.rstrip("/")} | {r[0].rstrip("/") for r in roots if r[0]},
        key=len, reverse=True,  # longest first so a nested root anchors greedily
    )
    alt = "|".join(re.escape(a) for a in anchors)
    body = f"~|{alt}" if alt else "~"
    return re.compile(
        rf"(?<![\w])(?:{body})(?=/|$|{_PATH_TERMINATOR})(?:/{_PATH_TAIL_RE})?"
    )


def _ends_at_whitespace(line: str, end: int) -> bool:
    """True if the match ends at whitespace rather than EOL or a terminator.

    A path token stops at whitespace, so a match ending at a space/tab cannot be
    told apart from a path with an interior space (``/h/Documents/My Notes/x.md``
    captures only ``/h/Documents/My``). Emitting that truncated form would put a
    wrong reference in the manifest (and Phase 3 ``--rewrite-paths`` would
    corrupt the file), and a complete path followed by prose is indistinguishable
    from it, so the caller conservatively DROPS any candidate ending at
    whitespace. This trades inventory completeness (a path mid-line is not
    recorded) for never emitting a truncated, wrong path - the manifest must not
    lie even at the cost of an occasional missed reference.
    """
    return end < len(line) and line[end] in (" ", "\t")


def _scan_embedded_paths(db: Any, scan_dir: Path, home: str) -> list:
    """Inventory embedded HOME / repo / vault paths, tokenized portably.

    Report-only by default (import does not rewrite markdown unless
    ``--rewrite-paths``). Best-effort: a path containing an interior space in
    free prose is ambiguous (where does it end?), so it is dropped rather than
    reported truncated. Scanned over raw lines (not code-span-stripped) so a
    documented ``cd ~/work/...`` inside a fenced block is still captured.
    """
    roots = _build_path_roots(db)
    pattern = _embedded_pattern(home, roots)
    entries: list = []
    seen: set = set()
    for rel, text in _iter_md_files(scan_dir):
        for lineno, line in enumerate(text.splitlines(), 1):
            for m in pattern.finditer(line):
                if _ends_at_whitespace(line, m.end()):
                    continue
                raw = m.group(0).rstrip(_PATH_STRIP_CHARS)
                # Require a real separator past the root (drops a bare "~").
                if "/" not in raw[1:]:
                    continue
                tok = _tokenize_path(raw, home, roots)
                if tok is None:
                    continue
                token, classification = tok
                source = f"{rel.as_posix()}:{lineno}"
                if (raw, source) in seen:
                    continue
                seen.add((raw, source))
                entries.append({
                    "raw": raw,
                    "token": token,
                    "classification": classification,
                    "target_kind": _target_kind(raw, classification),
                    "source": source,
                })
    return entries


# ---------------------------------------------------------------------------
# Bundle assembly
# ---------------------------------------------------------------------------


def _copy_tree(source_dir: Path, dest_dir: Path) -> tuple[list, list]:
    """Copy ``source_dir`` into ``dest_dir``; return ``(files[], warnings)``.

    Symlinks (files and dirs) are skipped so out-of-tree or secret content is
    never dereferenced into a shareable bundle, and a dangling link never aborts
    the export. Secret/credential files are skipped with a warning; junk files
    silently. ``files[]`` is built from the ACTUAL copied tree, each
    ``{path, sha256}`` with ``path`` prefixed by the bundle's ``<name>/`` dir.
    """
    name = dest_dir.name
    files: list = []
    warnings: list = []

    def _on_walk_error(err: OSError) -> None:
        # os.walk silently swallows a scandir failure by default, dropping a
        # whole subtree from BOTH the copy and files[] with no signal. Surface
        # it so the manifest does not silently under-report the project.
        target = getattr(err, "filename", "?")
        warnings.append(f"could not read {target}: {err}; subtree omitted from bundle")

    for dirpath, dirnames, filenames in os.walk(source_dir, onerror=_on_walk_error):
        kept_dirs = []
        for d in dirnames:
            if d in _EXCLUDE_DIRS:
                continue
            if (Path(dirpath) / d).is_symlink():
                rel = (Path(dirpath) / d).relative_to(source_dir).as_posix()
                warnings.append(f"skipped symlinked directory {rel} (not bundled)")
                continue
            kept_dirs.append(d)
        dirnames[:] = kept_dirs

        for fn in filenames:
            abs_path = Path(dirpath) / fn
            rel = abs_path.relative_to(source_dir)
            if _matches(fn, _JUNK_GLOBS, _JUNK_NAMES):
                continue
            if abs_path.is_symlink():
                warnings.append(
                    f"skipped symlink {rel.as_posix()} (points outside the "
                    f"project; not bundled)"
                )
                continue
            if _matches(fn, _SECRET_GLOBS, _SECRET_NAMES):
                warnings.append(
                    f"skipped {rel.as_posix()} (looks like a secret/credential "
                    f"file; not bundled)"
                )
                continue
            dest = dest_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(abs_path, dest)
            files.append({"path": f"{name}/{rel.as_posix()}", "sha256": _sha256(dest)})
    files.sort(key=lambda f: f["path"])
    return files, warnings


def _build_manifest(db: Any, task: Any, name: str, full_path: str,
                    include_time: bool, scan_dir: Path) -> tuple[dict, list]:
    """Assemble the manifest (minus ``files[]``); scan refs from ``scan_dir``.

    ``scan_dir`` is the ALREADY-COPIED bundle tree, so references and ``files[]``
    are read from the same bytes - a file that lands in the bundle is the same
    file scanned for references (no scan-vs-copy drift).
    """
    home = str(Path.home())
    warnings: list = []

    repo_ref, repo_warns = _classify_repo(db, task)
    warnings.extend(repo_warns)
    vaults = _scan_vaults(scan_dir)
    other_paths = _scan_embedded_paths(db, scan_dir, home)

    if task is not None:
        parent_name = None
        if task.parent_id is not None:
            parent = db.get_task(task.parent_id)
            parent_name = parent.name if parent else None
        time_total = 0
        if include_time:
            db.process_heartbeats()
            time_total = db.get_task_time(task.id, "all")
        project = {
            "name": task.name,
            "status": task.status,
            "type": task.task_type,
            "tags": task.tags,
            "priority": task.priority,
            "jira_key": task.jira_key,
            "branch": task.branch,
            "pr_url": task.pr_url,
            "full_path": task.full_path,
            "parent": parent_name,
            "created_at": task.created_at,
            "time_total_seconds": time_total,
        }
    else:
        # On-disk dir with no DB row: minimal, defaults-filled project block.
        warnings.append(
            f"no tasks row for '{name}'; exporting on-disk files with default metadata"
        )
        project = {
            "name": name, "status": "active", "type": "coding",
            "tags": [], "priority": None, "jira_key": None, "branch": None,
            "pr_url": None, "full_path": full_path, "parent": None,
            "created_at": None, "time_total_seconds": 0,
        }

    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "kind": BUNDLE_KIND,
        "generator": _generator(),
        "exported_at": _now_iso(),
        "exported_from": {
            "host": socket.gethostname(),
            "home": home,
            "platform": sys.platform,
        },
        "project": project,
        "references": {
            "repo": repo_ref,
            "vaults": vaults,
            "other_paths": other_paths,
        },
        "files": [],  # filled by the caller from the actual copied tree
    }
    return manifest, warnings


def _atomic_swap_dir(staging: Path, dest: Path) -> str:
    """Replace ``dest`` with ``staging`` atomically, clearing any stale bundle.

    ``staging`` is built as a sibling of ``dest`` (same filesystem), so
    ``os.replace`` is atomic and the old bundle - including files deleted from
    the project since the last export - is removed wholesale rather than left as
    orphans the manifest no longer lists.
    """
    backup = None
    if dest.exists():
        backup = dest.with_name(f"{dest.name}.old-{os.getpid()}")
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        os.replace(dest, backup)
    os.replace(staging, dest)  # same-filesystem sibling -> atomic
    if backup:
        shutil.rmtree(backup, ignore_errors=True)
    return str(dest)


def _atomic_write_tarball(staging: Path, dest: Path, name: str) -> str:
    """Tar ``staging`` to ``dest`` via a sibling temp file + atomic ``os.replace``.

    Writing straight to ``dest`` would leave a truncated archive clobbering a
    prior good bundle if the write fails midway, so build the ``.tgz`` beside
    ``dest`` and swap it in only once it is complete.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.tmp-{os.getpid()}")
    try:
        with tarfile.open(tmp, "w:gz") as tar:
            tar.add(staging, arcname=f"{name}.missioncache-bundle")
        os.replace(tmp, dest)
    finally:
        if tmp.exists():
            tmp.unlink()
    return str(dest)


def export_project(db: Any, name: str, *, out: Optional[str] = None,
                   include_time: bool = True) -> dict:
    """Export project ``name`` to a portable bundle.

    ``out``: ``.tgz``/``.tar.gz`` -> tarball; any other path -> target directory;
    default ``./<name>.missioncache-bundle/``. ``include_time`` runs
    ``process_heartbeats`` and records origin time (skip with ``--no-time`` for a
    pure read with no DB writes). The CLI owns stdout shaping (``--json`` prints
    the returned manifest); this function always writes the bundle to disk via a
    temp dir + atomic swap, so a failed or interrupted export never overwrites a
    previous good bundle.

    Returns ``{"manifest", "bundle_path", "warnings", "file_count", "name"}``.
    Raises ``ValueError`` if ``name`` is not a valid project name, or resolves to
    neither a tasks row nor an on-disk project dir.
    """
    import missioncache_db

    # Validate before any filesystem access - blocks path traversal via name
    # (e.g. "../../etc") and matches the import-side contract.
    missioncache_db.validate_task_name(name)

    task = db.get_task_by_name(name)
    full_path = task.full_path if task is not None else f"active/{name}"
    # Read the live module constant dynamically (env-overridable, monkeypatched
    # in tests) so a patched root wins over an import-time binding.
    root = missioncache_db.MISSIONCACHE_ROOT
    source_dir = root / full_path
    # Refuse a symlinked project dir: os.walk follows the top node regardless of
    # followlinks, so a symlinked active/<name> would drag out-of-tree content
    # into a shareable bundle (the per-file/per-dir symlink skips never see it).
    if source_dir.is_symlink():
        raise ValueError(
            f"project dir {source_dir} is a symlink; refusing to follow it into a bundle"
        )
    if not source_dir.is_dir():
        raise ValueError(
            f"project '{name}' has no files at {source_dir} "
            f"(no tasks row and no on-disk directory)"
        )
    # Defense-in-depth: a DB-supplied full_path is not name-validated, so confirm
    # the resolved dir stays under the data root before reading it.
    if not source_dir.resolve().is_relative_to(root.resolve()):
        raise ValueError(
            f"project dir {source_dir} resolves outside the data root {root}"
        )

    low = out.lower() if out else ""
    is_tarball = bool(out) and (low.endswith(".tgz") or low.endswith(".tar.gz"))

    if is_tarball:
        assert out is not None  # is_tarball is only true when out is set
        dest = Path(out)
        staging = Path(tempfile.mkdtemp(prefix="mc-export-"))
    else:
        dest = Path(out) if out else Path(f"./{name}.missioncache-bundle")
        if dest.exists() and not dest.is_dir():
            raise ValueError(f"--out target {dest} exists and is not a directory")
        dest.parent.mkdir(parents=True, exist_ok=True)
        staging = dest.parent / f".{dest.name}.tmp-{os.getpid()}"
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)

    try:
        files_dir = staging / "files" / name
        files_dir.mkdir(parents=True, exist_ok=True)
        files, copy_warns = _copy_tree(source_dir, files_dir)
        manifest, build_warns = _build_manifest(
            db, task, name, full_path, include_time, files_dir
        )
        manifest["files"] = files
        (staging / "missioncache.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        warnings = build_warns + copy_warns

        if is_tarball:
            bundle_path = _atomic_write_tarball(staging, dest, name)
        else:
            bundle_path = _atomic_swap_dir(staging, dest)
            staging = None  # consumed by the swap; finally must not remove it
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    return {
        "manifest": manifest,
        "bundle_path": bundle_path,
        "warnings": warnings,
        "file_count": len(manifest["files"]),
        "name": name,
    }

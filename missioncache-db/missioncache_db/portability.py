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
import stat
import sys
import tarfile
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from missioncache_db import machine_map

MANIFEST_VERSION = 1
BUNDLE_KIND = "missioncache-project-bundle"

# Decompression-bomb guard for untrusted archives: a project bundle is markdown
# plus small assets, so these caps are far above any real bundle while stopping
# a crafted .tgz/.zip from exhausting disk. Enforced on declared uncompressed
# size + member count before extraction.
_MAX_EXTRACT_BYTES = 512 * 1024 * 1024  # 512 MiB total uncompressed
_MAX_EXTRACT_MEMBERS = 10_000

# Best-effort DuckDB analytics rebuild trigger. The dashboard owns the
# SQLite->DuckDB sync; import never imports missioncache_dashboard (that would
# invert the dashboard->db dependency), it just pokes the HTTP route if the
# dashboard happens to be running. Failure is swallowed - import never blocks on
# the dashboard.
_SYNC_URL = "http://localhost:8787/api/sync"
_SYNC_TIMEOUT = 2.0

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
            # Text members are written LF (§9: export writes LF) so the bundle
            # does not carry CRLF churn into a Phase-4 git-sync folder, and the
            # recorded checksum is of the LF bytes - which import's EOL-normalized
            # verification matches even if the bundle later gains CRLF in transit.
            if fn.lower().endswith((".md", ".json")):
                dest.write_bytes(_normalize_eol(fn, abs_path.read_bytes()))
            else:
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
            "origin_uuid": task.origin_uuid,
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
            "created_at": None, "origin_uuid": None, "time_total_seconds": 0,
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
        # Dot-prefix the backup: scan_repo skips dot-dirs, so a backup left
        # behind by a failed cleanup can never resurface as a phantom task.
        backup = dest.with_name(f".{dest.name}.old-{os.getpid()}")
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        os.replace(dest, backup)
    try:
        os.replace(staging, dest)  # same-filesystem sibling -> atomic
    except OSError:
        # Swap-in failed with dest already moved aside: restore the old tree so
        # the failure leaves dest intact rather than deleted, then re-raise.
        if backup is not None and not dest.exists():
            os.replace(backup, dest)
        raise
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


# ===========================================================================
# Import (Phase 3): place files, reconcile references, name-keyed upsert,
# 3-bucket alignment report. Spec: docs/cross-machine-sharing-plan.md
# sections 5 (resolver + report + conflict policy), 6 (reference kinds),
# 7 (upsert + file list), 8 (tests), 9 (WSL edge cases).
# ===========================================================================


def _normalize_eol(name: str, data: bytes) -> bytes:
    """CRLF -> LF for text bundle members (``*.md`` / ``*.json``).

    Bundles routed through Windows tooling / git autocrlf can gain CRLF; the
    target writes LF so ``Hub:`` / ``[[..]]`` / frontmatter parsers stay stable
    and a git-sync folder does not churn on line-ending diffs. Other files
    (none expected) are copied byte-for-byte.
    """
    if name.lower().endswith((".md", ".json")):
        return data.replace(b"\r\n", b"\n")
    return data


def _entry(bucket: str, kind: str, ident: Any, local: Optional[str],
           hint: str = "") -> dict:
    """One alignment-report line. Every reference produces exactly one."""
    return {"bucket": bucket, "kind": kind, "id": ident, "local": local,
            "hint": hint}


def _bucket(report: dict, entry: dict) -> None:
    key = {"resolved": "resolved", "needs-mapping": "needs_mapping",
           "missing": "missing"}[entry["bucket"]]
    report[key].append(entry)


def _fail(report: dict, message: str) -> dict:
    """Record a hard failure (exit 1, nothing written) and return the report."""
    report["errors"].append(message)
    report["exit_code"] = 1
    return report


def _under_root(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def _under_drvfs(root: Path) -> bool:
    """True if the data root resolves under ``/mnt/`` (WSL DrvFs, §9).

    SQLite WAL can corrupt on DrvFs and it is slow with bad inotify/perms, so
    import warns (but never hard-fails) when ``~/.missioncache`` lives there.
    """
    try:
        return str(root.resolve()).startswith("/mnt/")
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Bundle location + extraction (path-traversal safe)
# ---------------------------------------------------------------------------


def _locate_bundle_root(path: Path) -> Optional[Path]:
    """Return the dir holding ``missioncache.json`` (the bundle, or its child)."""
    if (path / "missioncache.json").is_file():
        return path
    if path.is_dir():
        for child in sorted(path.iterdir()):
            if child.is_dir() and (child / "missioncache.json").is_file():
                return child
    return None


def _check_extract_budget(member_count: int, total_bytes: int) -> None:
    """Refuse an archive whose declared size/count exceeds the bomb caps."""
    if member_count > _MAX_EXTRACT_MEMBERS:
        raise ValueError(
            f"archive has {member_count} members (max {_MAX_EXTRACT_MEMBERS})"
        )
    if total_bytes > _MAX_EXTRACT_BYTES:
        raise ValueError(
            f"archive declares {total_bytes} uncompressed bytes "
            f"(max {_MAX_EXTRACT_BYTES})"
        )


def _safe_extract_tar(tar_path: Path, dest: Path) -> None:
    """Extract a tarball, refusing any member that escapes ``dest``.

    Links are skipped entirely (a symlink/hardlink could redirect a later write
    outside the temp dir). Path traversal (``../``, absolute) raises ValueError.
    """
    dest = dest.resolve()
    with tarfile.open(tar_path, "r:*") as tar:
        members = tar.getmembers()
        _check_extract_budget(len(members), sum(max(0, m.size) for m in members))
        for member in members:
            if member.issym() or member.islnk():
                continue
            target = (dest / member.name).resolve()
            if target != dest and not str(target).startswith(str(dest) + os.sep):
                raise ValueError(f"unsafe path in archive: {member.name}")
            tar.extract(member, dest)


def _safe_extract_zip(zip_path: Path, dest: Path) -> None:
    """Extract a zip, refusing any member that escapes ``dest``."""
    dest = dest.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
        _check_extract_budget(len(infos), sum(max(0, i.file_size) for i in infos))
        for info in infos:
            # Skip symlink entries, mirroring the tar extractor. Python's
            # zipfile.extract writes a symlink member as a plain file (it does
            # not honor S_IFLNK), so this is defense-in-depth rather than a live
            # hole, but it keeps the two extractors symmetric.
            if stat.S_ISLNK(info.external_attr >> 16):
                continue
            target = (dest / info.filename).resolve()
            if target != dest and not str(target).startswith(str(dest) + os.sep):
                raise ValueError(f"unsafe path in archive: {info.filename}")
            zf.extract(info, dest)


def _load_and_validate(bundle_root: Path) -> tuple[Optional[dict], list]:
    """Read + validate the manifest (step 1). Returns ``(manifest, errors)``.

    A non-empty ``errors`` list means hard failure (exit 1, nothing written);
    when fatal-on-first (unreadable, wrong version, bad name) the manifest is
    None. The ``references`` block is defaulted so later steps can index it.
    """
    import missioncache_db

    mpath = bundle_root / "missioncache.json"
    try:
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        # UnicodeDecodeError (a ValueError, not an OSError) is caught explicitly
        # so a bad-encoding manifest produces a clean report error for library
        # callers (the MCP wrapper), not a raw exception the CLI-only catch hides.
        return None, [f"cannot read manifest: {e}"]
    if not isinstance(manifest, dict):
        return None, ["manifest is not a JSON object"]
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        return None, [
            f"unsupported manifest_version {manifest.get('manifest_version')!r} "
            f"(this build imports version {MANIFEST_VERSION})"
        ]
    if manifest.get("kind") != BUNDLE_KIND:
        return None, [f"not a MissionCache bundle (kind={manifest.get('kind')!r})"]
    project = manifest.get("project")
    if not isinstance(project, dict):
        return None, ["manifest has no project block"]
    name = project.get("name")
    try:
        missioncache_db.validate_task_name(name or "")
    except Exception as e:
        return None, [f"invalid project name {name!r}: {e}"]

    errors: list = []
    if project.get("status") not in ("active", "paused", "completed", "archived"):
        errors.append(f"invalid project.status {project.get('status')!r}")
    if project.get("type") not in ("coding", "non-coding"):
        errors.append(f"invalid project.type {project.get('type')!r}")
    # full_path must be exactly <active|global|manual>/<name>. A bare "." or
    # "tasks.db"/"active"/"machine.json" would land the project ON a reserved
    # path inside the data root (the atomic swap then rmtree's it), so anything
    # that is not the canonical two-segment shape is rejected before any write.
    full_path = project.get("full_path")
    if not full_path or not isinstance(full_path, str):
        errors.append("manifest project.full_path is missing")
    else:
        parts = full_path.split("/")
        if len(parts) != 2 or parts[0] not in ("active", "global", "manual") \
                or parts[1] != name:
            errors.append(
                f"manifest project.full_path {full_path!r} is not a valid "
                f"<active|global|manual>/{name} path"
            )
    if not (bundle_root / "files" / name).is_dir():
        errors.append(f"bundle is missing its files/{name}/ tree")

    errors.extend(_verify_checksums(bundle_root, manifest.get("files") or []))

    refs = manifest.setdefault("references", {})
    if not isinstance(refs, dict):
        refs = manifest["references"] = {}
    refs.setdefault("repo", None)
    refs.setdefault("vaults", [])
    refs.setdefault("other_paths", [])
    return manifest, errors


def _verify_checksums(bundle_root: Path, files: list) -> list:
    """Verify each manifest ``files[]`` entry against the on-disk bundle (§3/I8).

    Detects a truncated/corrupted/tampered bundle BEFORE any file is placed -
    "nothing fails silently". Hashing is EOL-normalized (`_normalize_eol`) so a
    bundle whose text files gained CRLF in a Windows/git transit still matches
    the LF checksum the exporter recorded; only real content corruption fails.
    A files[] ``path`` that escapes the bundle tree is rejected, not read.
    """
    problems: list = []
    files_root = (bundle_root / "files").resolve()
    for entry in files:
        rel = entry.get("path") if isinstance(entry, dict) else None
        want = entry.get("sha256") if isinstance(entry, dict) else None
        if not rel or not want:
            problems.append(f"manifest files[] entry missing path/sha256: {entry!r}")
            continue
        disk = (files_root / rel)
        if not disk.resolve().is_relative_to(files_root) or disk.is_symlink():
            problems.append(f"manifest files[] path escapes the bundle: {rel}")
            continue
        if not disk.is_file():
            problems.append(f"bundle file listed in manifest is missing: {rel}")
            continue
        got = hashlib.sha256(
            _normalize_eol(disk.name, disk.read_bytes())
        ).hexdigest()
        if got != want:
            problems.append(f"bundle file checksum mismatch (corrupt bundle): {rel}")
    return problems


# ---------------------------------------------------------------------------
# File placement + content comparison
# ---------------------------------------------------------------------------


def _dir_checksums(root: Path) -> dict:
    """Normalized sha256 per relative path, junk/secret/symlink/.git excluded.

    Normalization (CRLF->LF for text) is applied so a CRLF bundle compares equal
    to its LF on-disk landing - the property idempotent re-import relies on.
    """
    out: dict = {}
    if not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in _EXCLUDE_DIRS and not (Path(dirpath) / d).is_symlink()
        ]
        for fn in filenames:
            ap = Path(dirpath) / fn
            if ap.is_symlink():
                continue
            if _matches(fn, _JUNK_GLOBS, _JUNK_NAMES):
                continue
            if _matches(fn, _SECRET_GLOBS, _SECRET_NAMES):
                continue
            rel = ap.relative_to(root).as_posix()
            out[rel] = hashlib.sha256(_normalize_eol(fn, ap.read_bytes())).hexdigest()
    return out


def _place_files(src_dir: Path, landing_dir: Path) -> tuple:
    """Place the bundle tree at ``landing_dir`` crash-safe + LF-normalized.

    Builds a sibling staging dir, writes every file (text members EOL-normalized),
    then atomically swaps it in (mirroring ``_atomic_swap_dir`` + lib/config.py's
    tmp+os.replace). A failed/interrupted import never leaves a half-written
    project dir clobbering the previous one.

    Applies the SAME exclusions as the export-side ``_copy_tree`` (symlinks,
    secret/credential files, junk) so a hand-crafted or third-party bundle can
    never plant a ``.env``/``id_rsa`` on disk or dereference a symlink (e.g.
    ``notes.md -> ~/.ssh/id_rsa``) into the placed tree - the symlink path
    matters because a directory bundle skips the archive extractor's link guard.
    Returns ``(placed_rel_paths, warnings)``.
    """
    landing_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = landing_dir.parent / f".{landing_dir.name}.import-tmp-{os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    placed: list = []
    warnings: list = []
    try:
        for dirpath, dirnames, filenames in os.walk(src_dir):
            kept = []
            for d in dirnames:
                if d in _EXCLUDE_DIRS:
                    continue
                if (Path(dirpath) / d).is_symlink():
                    rel = (Path(dirpath) / d).relative_to(src_dir).as_posix()
                    warnings.append(f"skipped symlinked directory {rel} (not placed)")
                    continue
                kept.append(d)
            dirnames[:] = kept
            for fn in filenames:
                ap = Path(dirpath) / fn
                rel = ap.relative_to(src_dir)
                if _matches(fn, _JUNK_GLOBS, _JUNK_NAMES):
                    continue
                if ap.is_symlink():
                    warnings.append(f"skipped symlink {rel.as_posix()} (not placed)")
                    continue
                if _matches(fn, _SECRET_GLOBS, _SECRET_NAMES):
                    warnings.append(
                        f"skipped {rel.as_posix()} (looks like a secret/credential "
                        f"file; not placed)"
                    )
                    continue
                dest = staging / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(_normalize_eol(fn, ap.read_bytes()))
                placed.append(rel.as_posix())
        _atomic_swap_dir(staging, landing_dir)
        staging = None  # consumed by the swap
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    placed.sort()
    return placed, warnings


# ---------------------------------------------------------------------------
# Collision classification (§5 conflict policy)
# ---------------------------------------------------------------------------


def _incoming_repo_key(repo_ref: Optional[dict]) -> tuple:
    """Comparable identity for the bundle's repo ref (canonical, not local id)."""
    if not repo_ref:
        return ("null", None)
    kind = repo_ref.get("kind")
    if kind == "git":
        return ("git", repo_ref.get("remote"))
    if kind == "anchor":
        return ("anchor", repo_ref.get("anchor") or repo_ref.get("short_name"))
    if kind == "home-relative":
        return ("home-relative", repo_ref.get("home_relative"))
    return ("unknown", None)


def _existing_repo_key(db: Any, task: Any) -> tuple:
    """Comparable identity for an existing local row's repo.

    Only a portable git remote yields a CONCRETE identity (``("git", remote)``)
    that can be compared across machines. A non-git binding (anchor /
    home-relative / dangling) has no machine-portable identity - its local
    basename differs per machine - so it is reported as ``("null", None)`` and
    treated as a same-project (re)bind by ``_same_project``. Minting an
    ``("anchor", short_name)`` key here was the bug that broke re-import of any
    non-git project: the basename never matched the bundle's logical name across
    machines, so a byte-identical re-import aborted as "different project".
    """
    if task.repo_id is None:
        return ("null", None)
    repo = db.get_repo(task.repo_id)
    if repo is None:
        return ("null", None)
    remote = machine_map._git_remote(repo.path) if Path(repo.path).exists() else None
    if remote and _is_portable_remote(remote):
        return ("git", machine_map.remote_key(remote))
    return ("null", None)


def _same_project(incoming: tuple, existing: tuple,
                  incoming_uuid: Optional[str] = None,
                  existing_uuid: Optional[str] = None) -> bool:
    """Whether an incoming bundle and an existing row are the same project (§5).

    The stable ``origin_uuid`` is authoritative WHEN BOTH SIDES CARRY ONE: equal
    uuids are the same project, different uuids are different projects (this
    closes the force-clobber hole where a null-repo row was treated as always
    the same project, so an unrelated bundle could overwrite it with --force).

    When either side lacks a uuid - an old bundle, or a pre-migration row that
    was never re-created (rows are deliberately not backfilled, see initialize())
    - fall back to the repo-identity heuristic: "different" fires only when the
    existing row binds a CONCRETE repo (git) that differs from the incoming one;
    a null-repo existing row is treated as the same project getting its binding.
    """
    if incoming_uuid and existing_uuid:
        return incoming_uuid == existing_uuid
    if incoming == existing:
        return True
    return existing[0] == "null"


def _classify_collision(db: Any, name: str, full_path: str, landing_dir: Path,
                        bundle_files_dir: Path, repo_ref: Optional[dict],
                        force: bool, incoming_uuid: Optional[str] = None) -> str:
    """Decide CREATE / UPDATE / ABORT per §5. Returns a decision token.

    Identity is the stable ``origin_uuid`` when both sides carry one, else the
    ``active/<name>`` dir slot + the repo the row binds. A different-project
    name collision never gets clobbered, even with ``--force`` (the
    force-must-not-destroy-an-unrelated-project property).
    """
    existing = db.find_import_target(name, full_path)
    if existing is None:
        return "create"
    if not _same_project(
        _incoming_repo_key(repo_ref), _existing_repo_key(db, existing),
        incoming_uuid, existing.origin_uuid,
    ):
        return "abort_different"
    # Row exists but its files are gone/empty: restoring them is non-destructive,
    # so place without demanding --force (a missing landing is not "local edits").
    if not landing_dir.is_dir() or not any(landing_dir.iterdir()):
        return "update_restore"
    # Normalized comparison: a CRLF bundle compares equal to its LF landing
    # (see _dir_checksums) - the property idempotent re-import relies on.
    if _dir_checksums(bundle_files_dir) == _dir_checksums(landing_dir):
        return "update_noop"
    if force:
        return "update_force"
    return "abort_same_differs"


# ---------------------------------------------------------------------------
# Reference resolution (§6)
# ---------------------------------------------------------------------------


def _bind_repo(db: Any, path: Path) -> Optional[int]:
    """Resolve/insert a repositories row for ``path``, gated on existence.

    ``add_repo`` has no disk check (§9), so never call it for a path that does
    not exist - that would pollute ``repositories`` with a bogus machine-specific
    row. Reuses an existing row when present.
    """
    existing = db.get_repo_by_path(path)
    if existing:
        return existing.id
    if not path.exists():
        return None
    return db.add_repo(str(path))


def _resolve_repo(db: Any, repo_ref: Optional[dict], repo_override: Optional[str],
                  dry_run: bool) -> tuple:
    """Resolve the primary repo binding (§6.1). Returns ``(repo_id, entry, warns)``.

    ``--repo`` wins over the map. A git worktree ref is forced to needs-mapping
    so it never silently collapses onto the parent checkout. A mapped-but-absent
    path is ``missing``; an unmapped portable ref is ``needs-mapping``.
    """
    warnings: list = []

    if repo_override:
        p = Path(repo_override).expanduser()
        if p.is_dir():
            repo_id = None if dry_run else _bind_repo(db, p)
            return repo_id, _entry("resolved", "repo", str(p), str(p)), warnings
        return None, _entry(
            "missing", "repo", repo_override, str(p),
            f"--repo path {p} does not exist; create/clone it then re-import",
        ), warnings

    if repo_ref is None:
        return None, _entry("resolved", "repo", "(none)", None), warnings

    kind = repo_ref.get("kind")

    if kind == "git":
        remote = repo_ref.get("remote")
        if repo_ref.get("worktree"):
            return None, _entry(
                "needs-mapping", "repo(worktree)", remote, None,
                f"bundle was bound to a linked git worktree; map the real checkout: "
                f"missioncache-db config set-path repo:{remote} <local-path> && re-import",
            ), warnings
        local = machine_map.resolve("repo", remote)
        if not local:
            return None, _entry(
                "needs-mapping", "repo", remote, None,
                f"missioncache-db config set-path repo:{remote} <local-path> && re-import",
            ), warnings
        if not Path(local).is_dir():
            return None, _entry(
                "missing", "repo", remote, local,
                f"mapped path {local} does not exist; clone {remote} there then re-import",
            ), warnings
        actual = machine_map._git_remote(local)
        if actual and machine_map.remote_key(actual) == remote:
            repo_id = None if dry_run else _bind_repo(db, Path(local))
            return repo_id, _entry("resolved", "repo", remote, local), warnings
        warnings.append(
            f"mapped path {local} origin does not match {remote}; not binding repo"
        )
        return None, _entry(
            "needs-mapping", "repo", remote, local,
            f"mapped path {local} points at a different repo; fix it: "
            f"missioncache-db config set-path repo:{remote} <correct-path> && re-import",
        ), warnings

    if kind == "anchor":
        anchor = repo_ref.get("anchor") or repo_ref.get("short_name")
        local = machine_map.resolve("anchor", anchor)
        if not local:
            return None, _entry(
                "needs-mapping", "repo", anchor, None,
                f"missioncache-db config set-path anchor:{anchor} <local-path> && re-import",
            ), warnings
        if not Path(local).is_dir():
            return None, _entry(
                "missing", "repo", anchor, local,
                f"mapped path {local} does not exist; create it then re-import",
            ), warnings
        repo_id = None if dry_run else _bind_repo(db, Path(local))
        return repo_id, _entry("resolved", "repo", anchor, local), warnings

    if kind == "home-relative":
        hr = repo_ref.get("home_relative") or ""
        home = machine_map.resolve("anchor", "HOME") or str(Path.home())
        local = str(Path(home) / hr) if hr else home
        if Path(local).is_dir():
            repo_id = None if dry_run else _bind_repo(db, Path(local))
            return repo_id, _entry("resolved", "repo", hr or "~", local), warnings
        return None, _entry(
            "missing", "repo", hr or "~", local,
            f"path {local} does not exist; create it then re-import",
        ), warnings

    return None, _entry(
        "needs-mapping", "repo", str(kind), None,
        f"unrecognized repo ref kind {kind!r}; cannot resolve",
    ), warnings


def _find_note(roots: list, note: str) -> Optional[Path]:
    """Find ``<note>.md`` under any vault root (top-level first, then recursive)."""
    for root in roots:
        cand = root / f"{note}.md"
        if cand.is_file():
            return cand
    for root in roots:
        if root.is_dir():
            hit = next(root.rglob(f"{note}.md"), None)
            if hit is not None:
                return hit
    return None


def _resolve_vaults(vault_refs: list) -> list:
    """Resolve ``Hub:``/``[[wikilink]]`` notes against the vault map (§6.2)."""
    entries: list = []
    vault_map = machine_map.all_mappings().get("vaults", {})
    roots = [Path(v) for v in vault_map.values() if v]
    for ref in vault_refs:
        note = ref.get("note")
        if not roots:
            entries.append(_entry(
                "needs-mapping", "vault", note, None,
                f"no vault roots mapped; missioncache-db config set-path "
                f"vault:<name> <vault-root> && re-import",
            ))
            continue
        found = _find_note(roots, note)
        if found is not None:
            entries.append(_entry("resolved", "vault", note, str(found)))
        else:
            entries.append(_entry(
                "missing", "vault", note, None,
                f"note {note}.md not found under any mapped vault root; "
                f"create it then re-import",
            ))
    return entries


_TOKEN_RE = re.compile(r"^\$\{(repo|vault):(.+?)\}(?:/(.*))?$")


def _expand_token(token: str, mapping: dict) -> Optional[str]:
    """Re-expand a portable path token against THIS machine's map (§6.3).

    Returns the local absolute path, or None when no mapping can produce one
    (an unmapped ``${repo:...}`` / ``${vault:...}``). ``${HOME}`` always
    resolves (live home dir as the fallback).
    """
    if token == "${HOME}" or token.startswith("${HOME}/"):
        home = mapping.get("anchors", {}).get("HOME") or str(Path.home())
        rest = token[len("${HOME}"):].lstrip("/")
        return str(Path(home) / rest) if rest else home
    m = _TOKEN_RE.match(token)
    if m:
        kind, key, rest = m.group(1), m.group(2), m.group(3) or ""
        if kind == "repo":
            base = mapping.get("repos", {}).get(machine_map.remote_key(key))
        else:
            base = mapping.get("vaults", {}).get(key)
        if not base:
            return None
        return str(Path(base) / rest) if rest else base
    return None


def _embedded_hint(token: str) -> tuple:
    """``(id, hint)`` for an unmapped embedded path token."""
    m = _TOKEN_RE.match(token)
    if m:
        kind, key = m.group(1), m.group(2)
        return key, (f"missioncache-db config set-path {kind}:{key} "
                     f"<local-path> && re-import")
    return token, f"no machine.json mapping produces a local path for {token}"


def _rewrite_embedded(landing_dir: Path, source: str, raw: str, local: str) -> str:
    """Replace the recorded ``raw`` with ``local`` on its exact line. Returns a
    status: ``"rewritten"`` | ``"noop"`` (raw not present as a token) | ``"failed"``.

    Surgical on two axes: only the single ``<file>:<line>`` the manifest recorded
    is touched, AND ``raw`` is matched only as a COMPLETE path token (followed by
    a path terminator or end-of-line). The token boundary stops a shorter path
    from corrupting a longer prefix-colliding path on the same line
    (``/h/work`` must not rewrite the ``/h/work`` inside ``/h/work-backup/x``).
    The three distinct return values let the caller tell a genuine write failure
    (IO error) apart from "nothing to do", instead of mislabeling both.
    """
    relfile, _, lineno_s = source.rpartition(":")
    if not relfile:
        return "failed"
    path = landing_dir / relfile
    # ``source`` comes from the untrusted manifest: refuse any target that
    # escapes the landing dir (via ../ or absolute) or goes through a symlink,
    # else a crafted bundle gets an arbitrary-file write on --rewrite-paths.
    try:
        if not path.resolve().is_relative_to(landing_dir.resolve()):
            return "failed"
    except OSError:
        return "failed"
    if path.is_symlink() or not path.is_file():
        return "failed"
    try:
        idx = int(lineno_s) - 1
        lines = path.read_text(encoding="utf-8", errors="replace").split("\n")
    except (ValueError, OSError):
        return "failed"
    if not (0 <= idx < len(lines)):
        return "failed"
    pattern = re.escape(raw) + r"(?=" + _PATH_TERMINATOR + r"|$)"
    new_line, n = re.subn(pattern, lambda _m: local, lines[idx])
    if n == 0:
        return "noop"
    lines[idx] = new_line
    try:
        path.write_text("\n".join(lines), encoding="utf-8")
    except OSError:
        return "failed"
    return "rewritten"


def _resolve_embedded(refs: list) -> tuple:
    """Classify embedded absolute paths into report entries (§6.3, read-only).

    Returns ``(entries, plan)``. ``plan`` lists the rewrite targets (each tied to
    its report entry) for the optional post-placement ``_apply_embedded_rewrites``
    pass - rewriting is deferred until after the files are placed, and is opt-in
    via ``--rewrite-paths``.
    """
    entries: list = []
    plan: list = []
    mapping = machine_map.all_mappings()
    for ref in refs:
        token = ref.get("token") or ""
        raw = ref.get("raw")
        source = ref.get("source") or ""
        local = _expand_token(token, mapping)
        if local is None:
            ident, hint = _embedded_hint(token)
            entries.append(_entry("needs-mapping", "embedded-path", ident, None, hint))
            continue
        if Path(local).exists():
            entry = _entry("resolved", "embedded-path", token, local)
        else:
            entry = _entry(
                "missing", "embedded-path", token, local,
                f"path {local} does not exist; create it then re-import",
            )
        entries.append(entry)
        if raw and source:
            plan.append({"entry": entry, "raw": raw, "source": source, "local": local})
    return entries, plan


def _apply_embedded_rewrites(plan: list, landing_dir: Path) -> list:
    """Apply opt-in ``--rewrite-paths`` edits after files are placed (I10).

    A requested rewrite that fails with an IO error DEMOTES its entry out of
    ``resolved`` (the entry dict is shared with the report), so the user is never
    told a path was rewritten when it was not. A ``noop`` (text already absent on
    the line) only warns. Returns warning strings.
    """
    warnings: list = []
    for item in plan:
        status = _rewrite_embedded(
            landing_dir, item["source"], item["raw"], item["local"]
        )
        if status == "failed":
            entry = item["entry"]
            entry["bucket"] = "missing"
            entry["hint"] = (
                f"embedded path could not be rewritten at {item['source']}; "
                f"edit it by hand"
            )
            warnings.append(
                f"could not rewrite embedded path at {item['source']} (read/write failed)"
            )
        elif status == "noop":
            warnings.append(
                f"embedded path at {item['source']} not rewritten "
                f"(text not found on that line; may already be rewritten)"
            )
    return warnings


def _resolve_parent(db: Any, parent_name: Optional[str]) -> tuple:
    """Reconcile the parent task by NAME -> local parent_id (§5 step 8)."""
    if not parent_name:
        return None, None
    parent = db.get_task_by_name(parent_name)
    if parent is not None:
        return parent.id, _entry("resolved", "parent", parent_name, f"task#{parent.id}")
    return None, _entry(
        "needs-mapping", "parent", parent_name, None,
        f"import the parent project '{parent_name}' first, then re-import",
    )


def _trigger_duckdb_sync() -> Optional[str]:
    """Best-effort poke at the dashboard's SQLite->DuckDB sync route (§5 step 10).

    NEVER imports missioncache_dashboard (that inverts the dashboard->db
    dependency). Pure stdlib HTTP, short timeout, failure swallowed. Returns a
    note string when the dashboard was unreachable, else None.
    """
    import urllib.request

    try:
        req = urllib.request.Request(_SYNC_URL, method="POST")
        with urllib.request.urlopen(req, timeout=_SYNC_TIMEOUT):
            return None
    except Exception:
        # Covers both "dashboard not running" and "running but errored" - the
        # advice is the same and we never block import on it.
        return ("dashboard sync not confirmed; DuckDB analytics will refresh on "
                "next dashboard start")


def import_bundle(db: Any, bundle: str, *, repo_override: Optional[str] = None,
                  force: bool = False, rewrite: bool = False,
                  dry_run: bool = False) -> dict:
    """Import a portable bundle and emit a 3-bucket alignment report (Phase 3).

    Pipeline (§5): validate -> classify collision -> place files (atomic, LF) ->
    resolve repo / vaults / embedded paths -> name-keyed upsert -> reconcile
    parent -> record origin time (display-only) -> best-effort DuckDB rebuild.
    ``--dry-run`` runs the full classification with every write suppressed.

    DB authority: import never re-parses the placed markdown for DB fields - the
    manifest is authoritative. The ``jira_key``/``branch``/``pr_url`` copies that
    also live in the markdown are treated as inert (the "no double source of
    truth" property holds because they are never re-read, not because they are
    absent).

    Returns the report dict; the CLI owns all I/O and exits on ``report["exit_code"]``
    (0 = all resolved, 2 = imported with needs-mapping/missing entries, 1 = hard
    failure with nothing committed).
    """
    import missioncache_db

    report: dict = {
        "name": None, "action": None, "task_id": None,
        "resolved": [], "needs_mapping": [], "missing": [],
        "warnings": [], "notes": [], "errors": [],
        "exit_code": 0, "dry_run": dry_run, "time_origin_seconds": 0,
        "bundle_dir": None,
    }
    tmpdir: Optional[Path] = None
    try:
        bundle_path = Path(bundle).expanduser()
        if not bundle_path.exists():
            return _fail(report, f"bundle not found: {bundle}")

        # --- locate / extract bundle root ---
        try:
            if bundle_path.is_dir():
                bundle_root = _locate_bundle_root(bundle_path)
            else:
                tmpdir = Path(tempfile.mkdtemp(prefix="mc-import-"))
                low = bundle_path.name.lower()
                if low.endswith((".tgz", ".tar.gz")) or tarfile.is_tarfile(bundle_path):
                    _safe_extract_tar(bundle_path, tmpdir)
                elif low.endswith(".zip") or zipfile.is_zipfile(bundle_path):
                    _safe_extract_zip(bundle_path, tmpdir)
                else:
                    return _fail(report, f"unrecognized bundle format: {bundle}")
                bundle_root = _locate_bundle_root(tmpdir)
        except (ValueError, OSError, tarfile.TarError, zipfile.BadZipFile) as e:
            return _fail(report, f"could not open bundle: {e}")

        if bundle_root is None:
            return _fail(report, "missioncache.json not found in bundle")
        report["bundle_dir"] = str(bundle_root)

        # --- step 1: validate ---
        manifest, errors = _load_and_validate(bundle_root)
        if errors:
            report["errors"].extend(errors)
            report["exit_code"] = 1
            return report
        assert manifest is not None  # errors empty => manifest present

        project = manifest["project"]
        refs = manifest["references"]
        name = project["name"]
        full_path = project["full_path"]
        report["name"] = name
        bundle_files_dir = bundle_root / "files" / name
        # A directory bundle skips the archive extractor's symlink guard, so a
        # symlinked files/<name> top would let _place_files (followlinks=False
        # walks INTO a symlinked root) copy an out-of-tree tree. The checksum
        # check only catches this when files[] is populated; guard it directly.
        if bundle_files_dir.is_symlink():
            return _fail(report, f"bundle files/{name} is a symlink; refusing to import")

        root = missioncache_db.MISSIONCACHE_ROOT
        landing_dir = root / full_path
        # Must be a STRICT descendant: landing == root would make the atomic swap
        # rmtree the whole data dir. (full_path is already shape-validated, this
        # is belt-and-suspenders.)
        if not _under_root(landing_dir, root) \
                or landing_dir.resolve() == root.resolve():
            return _fail(
                report,
                f"project full_path {full_path!r} does not resolve to a project "
                f"directory under the data root {root}",
            )

        # WSL DrvFs warning (§9): SQLite WAL corrupts on /mnt/.
        if _under_drvfs(root):
            report["warnings"].append(
                f"{root} is under /mnt/ (DrvFs); move ~/.missioncache to the WSL "
                f"native filesystem - SQLite WAL can corrupt on DrvFs"
            )

        # --- step 2: classify collision ---
        incoming_uuid = project.get("origin_uuid")
        decision = _classify_collision(
            db, name, full_path, landing_dir, bundle_files_dir, refs["repo"],
            force, incoming_uuid,
        )
        if decision == "abort_different":
            return _fail(
                report,
                f"name '{name}' is already used by a DIFFERENT project (identity "
                f"mismatch); rename one side - --force will not clobber an "
                f"unrelated project",
            )
        if decision == "abort_same_differs":
            return _fail(
                report,
                f"project '{name}' already exists with different local content. "
                f"If it is the same project, pass --force to overwrite its files; "
                f"if it is a different project that happens to share the name, "
                f"rename one side first.",
            )
        action = "created" if decision == "create" else "updated"
        place = decision in ("create", "update_force", "update_restore")

        # --- resolve references (reads; _resolve_repo may add_repo) ---
        repo_id, repo_entry, repo_warns = _resolve_repo(
            db, refs["repo"], repo_override, dry_run
        )
        report["warnings"].extend(repo_warns)
        vault_entries = _resolve_vaults(refs["vaults"])
        emb_entries, emb_plan = _resolve_embedded(refs["other_paths"])
        parent_id, parent_entry = _resolve_parent(db, project.get("parent"))

        # --- the DB write FIRST: a UNIQUE conflict aborts here, before any file
        # is touched, so exit 1 truly means nothing was committed. The mirror
        # direction holds too: if placement fails AFTER this commit, the
        # rollback below deletes/restores the row (pre_image is the restore
        # source for the update case). ---
        task = None
        pre_image = None
        if not dry_run:
            pre_image = db.find_import_target(name, full_path)
            task, action = db.upsert_imported_task(
                name, full_path,
                repo_id=repo_id,
                status=project["status"],
                task_type=project["type"],
                tags=project.get("tags") or [],
                priority=project.get("priority"),
                jira_key=project.get("jira_key"),
                branch=project.get("branch"),
                pr_url=project.get("pr_url"),
                parent_id=parent_id,
                created_at=project.get("created_at"),
                origin_uuid=incoming_uuid,
            )
            report["task_id"] = task.id

        # --- place files AFTER the row is safely written (atomic + LF) ---
        if place and not dry_run:
            try:
                placed, place_warns = _place_files(bundle_files_dir, landing_dir)
            except OSError as e:
                try:
                    db.rollback_imported_task(action, task.id, pre_image)
                    if action == "created":
                        report["task_id"] = None
                    report["notes"].append(
                        f"rolled back the '{action}' DB row after the placement failure"
                    )
                except Exception as rb:
                    report["warnings"].append(
                        f"could NOT roll back task row {task.id} after the "
                        f"placement failure ({rb}); a re-import will heal it "
                        f"(update_restore)"
                    )
                return _fail(report, f"file placement failed: {e}")
            report["warnings"].extend(place_warns)
        else:
            placed = sorted(
                p.relative_to(bundle_files_dir).as_posix()
                for p in bundle_files_dir.rglob("*") if p.is_file()
            )

        # --- apply opt-in embedded rewrites AFTER placement ---
        if rewrite and not dry_run:
            report["warnings"].extend(
                _apply_embedded_rewrites(emb_plan, landing_dir)
            )

        # --- assemble the report (embedded buckets reflect any rewrite demotion) ---
        report["resolved"].append(
            _entry("resolved", "files", len(placed), str(landing_dir))
        )
        _bucket(report, repo_entry)
        for entry in vault_entries:
            _bucket(report, entry)
        for entry in emb_entries:
            _bucket(report, entry)
        if parent_entry is not None:
            _bucket(report, parent_entry)
        if task is not None:
            report["resolved"].append(
                _entry("resolved", "db-row", task.id, str(landing_dir))
            )
        else:
            report["resolved"].append(
                _entry("resolved", "db-row", f"(dry-run: would be {action})",
                       str(landing_dir))
            )
        report["action"] = action

        # --- pass-through portables (inert in markdown, carried by manifest) ---
        for field in ("jira_key", "branch", "pr_url"):
            val = project.get(field)
            if val:
                report["resolved"].append(
                    _entry("resolved", "portable", field, None, str(val))
                )

        # --- time (display-only origin metadata) ---
        origin = int(project.get("time_total_seconds") or 0)
        report["time_origin_seconds"] = origin
        report["resolved"].append(_entry(
            "resolved", "time", "origin", None,
            f"{origin}s origin time is display-only; not imported "
            f"(this machine starts at 0)",
        ))

        # --- best-effort DuckDB rebuild ---
        if dry_run:
            report["notes"].append("dry-run: no files placed, no DB row written")
        else:
            note = _trigger_duckdb_sync()
            if note:
                report["notes"].append(note)

        # --- exit code ---
        report["exit_code"] = 2 if (report["needs_mapping"] or report["missing"]) else 0
        return report

    except missioncache_db.ImportConflictError as e:
        report["errors"].append(str(e))
        report["exit_code"] = 1
        return report
    finally:
        if tmpdir is not None and tmpdir.exists():
            shutil.rmtree(tmpdir, ignore_errors=True)


def format_report_lines(report: dict) -> list:
    """Human-readable stdout lines for an import report.

    Kept here (not in the CLI branch) so the dispatch stays thin and the deep
    bucket/entry/hint nesting lives in one testable place. Warnings are printed
    to stderr by the caller; this returns the stdout summary only.
    """
    verb = "would import" if report["dry_run"] else "imported"
    outcome = "fully resolved" if report["exit_code"] == 0 else "see report below"
    lines = [f"{verb} '{report['name']}' ({report['action']}) - {outcome}"]
    for bucket, label in (("resolved", "resolved"),
                          ("needs_mapping", "needs mapping"),
                          ("missing", "missing")):
        rows = report[bucket]
        if not rows:
            continue
        lines.append(f"  [{label}] {len(rows)}")
        for entry in rows:
            local = f" -> {entry['local']}" if entry["local"] else ""
            lines.append(f"    {entry['kind']}: {entry['id']}{local}")
            if entry["hint"] and bucket != "resolved":
                lines.append(f"      fix: {entry['hint']}")
    for note in report["notes"]:
        lines.append(f"  note: {note}")
    return lines

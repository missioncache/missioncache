"""One-time migration: context-file conventions for existing active projects.

For every project under ``MISSIONCACHE_ROOT/active/`` (except the hardcoded
skip list) this script, additively and idempotently:

1. Inserts the ``## Waiting on`` section (usage note + empty table)
   immediately before ``## Next Steps`` - skipped when the section exists.
2. Repairs entries misplaced by the pre-2026-07-11 unanchored heading
   regex: dated ``###`` blocks that were inserted after a PROSE line merely
   mentioning ``## Recent Changes`` are extracted and merged back into the
   real Recent Changes flow. Only fires when such a run exists.
3. Consolidates legacy ``## Recent Changes (timestamp)`` h2 sections into
   one canonical ``## Recent Changes`` section (a legacy section's own
   prose becomes a dated ``###`` entry).
4. Enforces the Recent Changes cap: the newest ``RECENT_CHANGES_CAP``
   entries stay (merged entries are date-sorted first, since document
   order is only trustworthy in sections maintained by the live writers),
   the overflow rolls into ``<name>-journal.md`` (oldest first) with the
   pointer line at the section bottom.

Everything else in the file is byte-identical. ``--dry-run`` prints the
per-project change summary without writing.

Idempotent on clean reruns (a second run reports "already migrated").
One caveat: a rerun after the journal-first crash window (journal written,
context replace never happened) re-rolls the same entries and duplicates
them in the journal - the documented duplication-over-loss tradeoff, same
as the live writer's.

NOTE: ``_file_lock`` and the journal-before-context write order are
duplicated from ``mcp_missioncache.project_files`` on purpose (same reason
as ``hooks/pre_compact.py``): the script must not depend on the mcp-server
package. If you change locking semantics there, mirror the change here.
"""

import contextlib
import fcntl
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from missioncache_db import context_health

# Projects to leave fully untouched (e.g. one with a live session mid-work).
# centra-aip-e2e-integration sat here during the 2026-07-11 migration until
# its session closed; empty now, kept as the escape hatch for reruns.
SKIP_PROJECTS: set[str] = set()

MISSIONCACHE_ROOT = Path(
    os.environ.get("MISSIONCACHE_ROOT") or str(Path.home() / ".missioncache")
)

_H2_LINE_RE = re.compile(r"^## ", re.MULTILINE)
_RC_H2_RE = re.compile(r"^## Recent Changes([^\n]*)$", re.MULTILINE)
_H3_DATED_RE = re.compile(r"^### ", re.MULTILINE)


@contextlib.contextmanager
def _file_lock(path):
    """Sidecar-lockfile flock, mirroring mcp_missioncache.project_files."""
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lockfd:
        fcntl.flock(lockfd.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lockfd.fileno(), fcntl.LOCK_UN)


def _find_misplaced_run(content):
    """(run_start, run_end) of ``###`` blocks stranded after a prose line
    that mentions ``## Recent Changes`` mid-line, or None.

    The pre-fix writer matched the literal anywhere, so entries were
    inserted right after the first line CONTAINING the string - even a
    bullet. The run is every consecutive ``###`` block from the first one
    after that line up to the next real ``## `` heading. The anchor line
    must not itself be ANY heading (``^(?!#)``), and all scanning runs on
    the fence-masked text so code samples can't fake the shape.
    """
    masked = context_health.mask_fences(content)
    for match in re.finditer(r"^(?!#).*## Recent Changes", masked, re.MULTILINE):
        line_end = masked.find("\n", match.start())
        if line_end == -1:
            return None
        region_start = line_end + 1
        next_h2 = _H2_LINE_RE.search(masked, region_start)
        region_end = next_h2.start() if next_h2 else len(masked)
        h3 = _H3_DATED_RE.search(masked, region_start)
        if h3 is None or h3.start() >= region_end:
            continue
        # Only whitespace may sit between the anchor line and the first ###
        # block - otherwise this is not the injection shape.
        if masked[region_start : h3.start()].strip():
            continue
        return h3.start(), region_end
    return None


def _split_h3_entries(text):
    """Split text into ``###``-headed entries; returns (preamble, entries).

    Fence-aware: a ``### `` line inside a code fence never starts an entry.
    """
    masked = context_health.mask_fences(text)
    starts = [m.start() for m in re.finditer(r"^### ", masked, re.MULTILINE)]
    if not starts:
        return text, []
    entries = [
        text[starts[i] : starts[i + 1] if i + 1 < len(starts) else len(text)]
        for i in range(len(starts))
    ]
    return text[: starts[0]], entries


def _entry_sort_key(entry):
    """Datetime parsed from an entry's ``### <ts>`` heading, for date-sorting.

    Merged entries (misplaced run + legacy sections) are only APPROXIMATELY
    newest-first in document order, and the cap trusts order - so merged
    lists get an explicit sort. Unparsable headings sort oldest (datetime
    min) and keep their relative document order (stable sort).
    """
    heading = entry.split("\n", 1)[0]
    raw = heading[4:].strip() if heading.startswith("### ") else ""
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw[: len("2026-01-01 00:00")].strip(), fmt)
        except ValueError:
            continue
    return datetime.min


def _collect_rc_sections(content):
    """All anchored ``## Recent Changes...`` sections as (start, end, suffix, body).

    Scans the fence-masked text (a fenced fake heading is not a section);
    slices come from the original.
    """
    masked = context_health.mask_fences(content)
    sections = []
    for match in _RC_H2_RE.finditer(masked):
        body_start = match.end() + 1
        next_h2 = _H2_LINE_RE.search(masked, body_start)
        body_end = next_h2.start() if next_h2 else len(content)
        sections.append(
            (match.start(), body_end, match.group(1).strip(), content[body_start:body_end])
        )
    return sections


def migrate_content(content, journal_name):
    """Pure transform: returns (new_content, journal_append_or_None, summary)."""
    summary = {
        "waiting_on_inserted": False,
        "misplaced_moved": 0,
        "legacy_sections_merged": 0,
        "rolled_to_journal": 0,
    }

    # ── Step 1: repair misplaced entries (unanchored-regex victims) ──
    misplaced_entries = []
    run = _find_misplaced_run(content)
    if run is not None:
        run_start, run_end = run
        _, misplaced_entries = _split_h3_entries(content[run_start:run_end])
        summary["misplaced_moved"] = len(misplaced_entries)
        content = content[:run_start].rstrip("\n") + "\n\n" + content[run_end:]

    # ── Step 2: consolidate legacy / multiple RC h2 sections ──
    sections = _collect_rc_sections(content)
    needs_consolidation = len(sections) > 1 or (sections and sections[0][2]) or misplaced_entries
    if sections and needs_consolidation:
        all_entries = list(misplaced_entries)  # newest first (doc order)
        for _, _, suffix, body in sections:
            preamble, entries = _split_h3_entries(body)
            all_entries.extend(entries)
            if preamble.strip():
                # The section's own prose is that timestamp's entry.
                ts = suffix.strip("() ") if suffix else "undated"
                all_entries.append(f"### {ts}\n\n{preamble.strip()}\n")
            if suffix or len(sections) > 1:
                summary["legacy_sections_merged"] += 1
        # Merged entries come from multiple origins, so document order is
        # only approximately newest-first - date-sort (newest first, stable
        # for unparsable headings) BEFORE the cap decides what stays.
        all_entries.sort(key=_entry_sort_key, reverse=True)
        # Remove all RC sections (reverse order keeps offsets valid);
        # reinsert one canonical section at the first section's position.
        insert_at = sections[0][0]
        for start, end, _, _ in reversed(sections):
            content = content[:start] + content[end:]
        rebuilt = "## Recent Changes\n\n" + "\n".join(
            e.rstrip() + "\n" for e in all_entries
        ) + "\n"
        content = content[:insert_at] + rebuilt + content[insert_at:]
    elif misplaced_entries and not sections:
        # No RC section at all: create one at EOF from the misplaced run.
        content = (
            content.rstrip("\n")
            + "\n\n## Recent Changes\n\n"
            + "\n".join(e.rstrip() + "\n" for e in misplaced_entries)
            + "\n"
        )

    # ── Step 3: Waiting on section (before Next Steps) ──
    if context_health.extract_section(content, "Waiting on") is None:
        content = context_health.insert_waiting_on_before_next_steps(
            content, context_health.build_waiting_on_section([])
        )
        summary["waiting_on_inserted"] = True

    # ── Step 4: cap + journal rollover + pointer line ──
    content, journal_append, moved = context_health.split_recent_changes_for_cap(
        content, journal_name
    )
    summary["rolled_to_journal"] = moved

    return content, journal_append, summary


def migrate_one(context_path, dry_run):
    """Migrate a single project's context file (locked, atomic, journal-first)."""
    journal_path = context_health.derive_journal_path(context_path)
    with _file_lock(context_path):
        original = context_path.read_text()
        new_content, journal_append, summary = migrate_content(
            original, journal_path.name
        )
        summary["changed"] = new_content != original
        summary["size_before_kb"] = len(original.encode()) // 1024
        summary["size_after_kb"] = len(new_content.encode()) // 1024
        if dry_run or not summary["changed"]:
            return summary
        if journal_append:
            if journal_path.exists():
                journal_content = journal_path.read_text().rstrip("\n") + "\n\n"
            else:
                journal_content = (
                    context_health.journal_header(context_path.parent.name) + "\n"
                )
            journal_content += journal_append
            journal_tmp = journal_path.with_name(journal_path.name + ".tmp")
            journal_tmp.write_text(journal_content)
            os.replace(journal_tmp, journal_path)
        tmp_path = context_path.with_name(context_path.name + ".tmp")
        tmp_path.write_text(new_content)
        os.replace(tmp_path, context_path)
    return summary


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    dry_run = "--dry-run" in argv
    prefix = "DRY RUN: " if dry_run else ""
    active_dir = MISSIONCACHE_ROOT / "active"
    if not active_dir.exists():
        print(f"No active dir at {active_dir}")
        return 0

    for project_dir in sorted(p for p in active_dir.iterdir() if p.is_dir()):
        name = project_dir.name
        if name in SKIP_PROJECTS:
            print(f"{name}: SKIPPED (hardcoded skip list)")
            continue
        context_path = project_dir / f"{name}-context.md"
        if not context_path.exists():
            context_path = project_dir / "context.md"
        if not context_path.exists():
            print(f"{name}: no context file, nothing to do")
            continue
        s = migrate_one(context_path, dry_run)
        if not s["changed"]:
            print(f"{name}: already migrated, no changes")
            continue
        bits = []
        if s["waiting_on_inserted"]:
            bits.append("Waiting on inserted")
        if s["misplaced_moved"]:
            bits.append(f"{s['misplaced_moved']} misplaced entries repaired")
        if s["legacy_sections_merged"]:
            bits.append(f"{s['legacy_sections_merged']} legacy RC sections merged")
        if s["rolled_to_journal"]:
            bits.append(f"{s['rolled_to_journal']} entries -> journal")
        print(
            f"{name}: {prefix}{'; '.join(bits)} "
            f"({s['size_before_kb']}KB -> {s['size_after_kb']}KB)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

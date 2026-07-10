"""Structure helpers for MissionCache context files - parse AND shape.

Owns the shared knowledge of a context file's structure: section index,
"Waiting on" table rows, Recent Changes dated subsections (including the
prepend shape used by every writer), the cap/rollover split into a
per-project journal file, the load-time digest, and per-project health
checks. Consumed by the missioncache-db CLI (``health``), the MCP server
(``get_context_digest``, ``update_context_file``), the pre-compact hook,
and the one-time migration script. The dashboard keeps its own independent
parser copy by design (missioncache-dashboard/.../server.py) - when section
semantics change here, check whether that mirror needs to track.

Stdlib-only on purpose (mirrors ``machine_map.py``): the MCP server imports
from missioncache_db, never the reverse, and nothing here may drag heavy
imports into hook-adjacent paths.

All structure scanning is FENCE-AWARE: lines inside fenced code blocks
(``` or ~~~) are invisible to heading/subsection/table detection, so a
code sample containing a column-0 ``## Recent Changes`` can never shadow
the real section or be torn apart by the cap (the bug class that produced
the 2026-07-11 anchored-regex fix).

All functions are tolerant readers: a missing section returns ``None``/``[]``,
a malformed date returns ``None``. Parsers never raise on bad content.
"""

import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

# Thresholds are plain module constants by design (no config-table knobs).
# Tests monkeypatch these constants directly.
RECENT_CHANGES_CAP = 12
STALE_CONTEXT_DAYS = 14
STALE_WAITING_DAYS = 7
CONTEXT_SIZE_BUDGET_KB = 100

# Sections every context file is expected to carry. "Key Architectural
# Decisions" and "Key Files" are canonical too but older files predate the
# convention; the health check only flags the resume-critical core.
CORE_SECTIONS = ["Description", "Gotchas", "Waiting on", "Next Steps", "Recent Changes"]

# Plain text (not italics) to match the hand-written shape the convention
# was lifted from.
WAITING_ON_NOTE = (
    "External replies/events that gate work. Check on every resume; "
    "when one resolves, act on what it gates and move the row into "
    "Recent Changes."
)

WAITING_ON_TABLE_HEADER = "| What | Who | Since | Gates |\n|------|-----|-------|-------|"

# The pointer line lives at the BOTTOM of the Recent Changes section, not
# under the heading: both live writers (update_context_file and the
# pre-compact hook) prepend new ### subsections immediately after the
# heading line, so a pointer placed there would drift into the middle.
# The bottom is stable - prepends happen at the top, the cap trims at the
# bottom. ``_POINTER_PREFIX`` is the detection anchor; keep them in sync.
_POINTER_PREFIX = "Older entries live in `"
RECENT_CHANGES_POINTER = "Older entries live in `{journal_name}` (oldest first)."

_H2_RE = re.compile(r"^## (.+?)\s*$", re.MULTILINE)
_H2_LINE_RE = re.compile(r"^## ", re.MULTILINE)
_H3_LINE_RE = re.compile(r"^### ", re.MULTILINE)
_LAST_UPDATED_RE = re.compile(r"\*\*Last Updated:\*\*\s*(.+)")
_ISO_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
# Split on pipes that are not escaped as \| (cell-content pipes).
_UNESCAPED_PIPE_RE = re.compile(r"(?<!\\)\|")


def mask_fences(content: str) -> str:
    """Same-length copy of ``content`` with fenced-code lines blanked.

    Fence delimiter lines and everything between them become runs of
    spaces (newlines preserved), so regex offsets computed on the masked
    text are valid indices into the original. Every structure scan in this
    module goes through this, keeping code samples invisible to heading /
    subsection / table detection. Tolerant closer: any fence of the same
    character type closes the block.
    """
    if "```" not in content and "~~~" not in content:
        return content
    lines = content.split("\n")
    fence_char: Optional[str] = None
    for i, line in enumerate(lines):
        match = _FENCE_RE.match(line)
        if fence_char is None:
            if match:
                fence_char = match.group(1)[0]
                lines[i] = " " * len(line)
        else:
            closes = match is not None and match.group(1)[0] == fence_char
            lines[i] = " " * len(line)
            if closes:
                fence_char = None
    return "\n".join(lines)


def derive_journal_path(context_path: Path) -> Path:
    """Journal filename for a context file, in the same directory.

    ``X-context.md`` -> ``X-journal.md``; legacy bare ``context.md`` ->
    ``journal.md``. Derived from the filename (not the task name) because
    the live writers only receive the context path.
    """
    name = context_path.name
    if name == "context.md":
        return context_path.with_name("journal.md")
    if name.endswith("-context.md"):
        return context_path.with_name(name[: -len("-context.md")] + "-journal.md")
    # Defensive: unknown naming keeps the stem and appends -journal.
    return context_path.with_name(context_path.stem + "-journal.md")


def journal_header(project_name: str) -> str:
    """Header written when the journal file is first created."""
    return (
        f"# {project_name} - Journal\n\n"
        "Overflow of Recent Changes rolled out of the context file, oldest "
        "first. Auto-managed by MissionCache - greppable history, never read "
        "on resume.\n"
    )


def section_index(content: str) -> list[dict[str, Any]]:
    """All ``## `` headings as ``{"name", "line"}`` (1-based), in order."""
    masked = mask_fences(content)
    index = []
    for match in _H2_RE.finditer(masked):
        line = masked.count("\n", 0, match.start()) + 1
        index.append({"name": match.group(1), "line": line})
    return index


def _section_span(content: str, name: str) -> Optional[tuple[int, int, int]]:
    """(heading_start, body_start, body_end) for ``## <name>``, or None.

    The heading tolerates trailing text (``## Next Steps (post-pivot)``,
    legacy ``## Recent Changes (2026-04-30 10:20)``). The body runs to the
    next ``## `` heading or EOF. Headings inside fenced code blocks are
    never matched (search runs on the fence-masked text; offsets are valid
    for the original).
    """
    masked = mask_fences(content)
    heading_re = re.compile(rf"^## {re.escape(name)}[^\n]*$", re.MULTILINE)
    match = heading_re.search(masked)
    if not match:
        return None
    body_start = match.end() + 1 if match.end() < len(content) else match.end()
    next_h2 = _H2_LINE_RE.search(masked, body_start)
    body_end = next_h2.start() if next_h2 else len(content)
    return match.start(), body_start, body_end


def extract_section(content: str, name: str) -> Optional[str]:
    """Verbatim body of ``## <name>`` (heading excluded), or None if absent."""
    span = _section_span(content, name)
    if span is None:
        return None
    return content[span[1] : span[2]]


def _subsection_starts(masked_body: str) -> list[int]:
    """Offsets of ``### `` subsection starts within a fence-masked body."""
    return [m.start() for m in _H3_LINE_RE.finditer(masked_body)]


def parse_recent_changes_subsections(content: str) -> list[tuple[str, str]]:
    """``(heading_line, body)`` per ``### `` subsection, document order.

    Scoped to the FIRST ``## Recent Changes`` heading only. Legacy sibling
    ``## Recent Changes (timestamp)`` h2 blocks are deliberately ignored
    (the migration script moves those); scoping prevents an un-migrated
    file from being mis-capped across fragments. Dated ``###`` blocks that
    live under OTHER sections, or inside fenced code blocks, are likewise
    out of scope.
    """
    span = _section_span(content, "Recent Changes")
    if span is None:
        return []
    _, body_start, body_end = span
    body = content[body_start:body_end]
    masked_body = mask_fences(content)[body_start:body_end]
    starts = _subsection_starts(masked_body)
    subsections = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(body)
        heading_line, _, rest = body[start:end].partition("\n")
        subsections.append((heading_line.rstrip(), rest))
    return subsections


def prepend_recent_changes(content: str, timestamp: str, changes_md: str) -> str:
    """Insert a dated ``### <timestamp>`` subsection under Recent Changes.

    The ONE owner of the prepend shape, used by both live writers
    (``update_context_file`` and the pre-compact hook) - each previously
    carried its own copy, which is why the 2026-07-11 unanchored-regex bug
    had to be fixed twice. Fence-aware and ^-anchored: neither a prose
    mention of the literal heading nor a heading-looking line inside a
    code fence can become the insertion anchor. Tolerates the legacy
    ``## Recent Changes (timestamp)`` heading form; creates the section at
    EOF when missing.
    """
    new_subsection = f"### {timestamp}\n\n{changes_md}\n"
    masked = mask_fences(content)
    match = re.search(r"^## Recent Changes[^\n]*\n", masked, re.MULTILINE)
    if match:
        heading_end = match.end()
        return content[:heading_end] + f"\n{new_subsection}\n" + content[heading_end:]
    return content + f"\n## Recent Changes\n\n{new_subsection}"


def split_recent_changes_for_cap(
    content: str, journal_name: str, cap: Optional[int] = None
) -> tuple[str, Optional[str], int]:
    """Enforce the Recent Changes cap; return overflow as journal text.

    Returns ``(new_content, journal_append_or_None, moved_count)``. Keeps
    the newest ``cap`` ``###`` subsections in place; the overflow - the
    oldest, at the bottom of the section - is removed and returned
    REVERSED to oldest-first so the journal file always reads oldest ->
    newest top to bottom.

    "Newest" is positional: the live writers maintain the newest-first
    invariant by always prepending under the heading, so document order IS
    date order. Bulk producers of merged sections (the migration script)
    must date-sort before handing a section to this function.

    Also owns the pointer line: on any rollover it is (re)placed at the
    bottom of the section. Existing pointer lines anywhere in the section
    are removed first so the line never duplicates or drifts.

    No-op (content returned unchanged) when the count is within the cap.
    """
    # Resolved at call time (not a def-time default) so monkeypatching
    # RECENT_CHANGES_CAP works, per the module's stated contract.
    if cap is None:
        cap = RECENT_CHANGES_CAP
    span = _section_span(content, "Recent Changes")
    if span is None:
        return content, None, 0
    _, body_start, body_end = span
    body = content[body_start:body_end]
    masked_body = mask_fences(content)[body_start:body_end]
    starts = _subsection_starts(masked_body)
    if len(starts) <= cap:
        return content, None, 0

    def segment(i: int) -> str:
        end = starts[i + 1] if i + 1 < len(starts) else len(body)
        return body[starts[i] : end]

    preamble = body[: starts[0]]
    preamble = "\n".join(
        line for line in preamble.splitlines()
        if not line.strip().startswith(_POINTER_PREFIX)
    )
    preamble = preamble.rstrip() + "\n\n" if preamble.strip() else ""

    def render(seg: str) -> str:
        seg = "\n".join(
            line for line in seg.splitlines()
            if not line.strip().startswith(_POINTER_PREFIX)
        )
        return seg.rstrip() + "\n"

    kept = [render(segment(i)) for i in range(cap)]
    overflow = [render(segment(i)) for i in range(cap, len(starts))]

    pointer = RECENT_CHANGES_POINTER.format(journal_name=journal_name)
    new_body = preamble + "\n".join(kept) + f"\n{pointer}\n\n"
    journal_append = "\n".join(reversed(overflow)) + "\n"
    new_content = content[:body_start] + new_body + content[body_end:]
    return new_content, journal_append, len(overflow)


def _escape_cell(value: str) -> str:
    """Make a value safe as a markdown table cell: no newlines, pipes escaped."""
    return value.replace("\n", " ").replace("|", "\\|").strip()


def _unescape_cell(value: str) -> str:
    return value.strip().replace("\\|", "|")


def _split_row(line: str) -> list[str]:
    """Split a table row on unescaped pipes, dropping the outer delimiters."""
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|") and not stripped.endswith("\\|"):
        stripped = stripped[:-1]
    return _UNESCAPED_PIPE_RE.split(stripped)


def parse_waiting_on(content: str) -> list[dict[str, str]]:
    """Waiting-on table rows as ``{"what", "who", "since", "gates"}``.

    Skips the header and separator rows. An empty table (header only) or a
    missing section returns ``[]``. Cells are whitespace-trimmed and
    ``\\|`` unescapes back to a literal pipe (the render side escapes);
    short rows are padded with empty strings rather than dropped. Lines
    inside fenced code blocks are never treated as table rows.
    """
    span = _section_span(content, "Waiting on")
    if span is None:
        return []
    body = content[span[1] : span[2]]
    masked_body = mask_fences(content)[span[1] : span[2]]
    rows = []
    for line, masked_line in zip(body.splitlines(), masked_body.splitlines()):
        if not masked_line.strip().startswith("|"):
            continue
        cells = [_unescape_cell(c) for c in _split_row(line)]
        if cells and all(re.fullmatch(r"[-: ]*", c) for c in cells):
            continue  # separator row
        if cells and cells[0].lower() == "what":
            continue  # header row
        cells += [""] * (4 - len(cells))
        rows.append(
            {"what": cells[0], "who": cells[1], "since": cells[2], "gates": cells[3]}
        )
    return rows


def render_waiting_on_row(row: dict[str, str]) -> str:
    """One markdown table row from a waiting-on dict (cells pipe-escaped)."""
    return (
        f"| {_escape_cell(row.get('what', ''))} | {_escape_cell(row.get('who', ''))} "
        f"| {_escape_cell(row.get('since', ''))} | {_escape_cell(row.get('gates', ''))} |"
    )


def build_waiting_on_section(rows: list[dict[str, str]]) -> str:
    """Full ``## Waiting on`` section text (heading + note + table)."""
    lines = [render_waiting_on_row(r) for r in rows]
    table = WAITING_ON_TABLE_HEADER + ("\n" + "\n".join(lines) if lines else "")
    return f"## Waiting on\n\n{WAITING_ON_NOTE}\n\n{table}\n"


def replace_waiting_on_table(content: str, rows: list[dict[str, str]]) -> str:
    """Rebuild the table inside an existing ``## Waiting on`` section.

    Preserves the section's non-table prose (the usage note, hand-written
    variants included) - only the contiguous table block is replaced. When
    the section carries no table yet, the table is appended at the end of
    the section. Returns content unchanged when the section is absent
    (callers self-heal via ``insert_waiting_on_before_next_steps`` first).
    Fenced lines are never mistaken for table rows.
    """
    span = _section_span(content, "Waiting on")
    if span is None:
        return content
    _, body_start, body_end = span
    body = content[body_start:body_end]
    masked_lines = mask_fences(content)[body_start:body_end].splitlines()
    lines = body.splitlines()
    first_table = last_table = None
    for i, masked_line in enumerate(masked_lines):
        if masked_line.strip().startswith("|"):
            if first_table is None:
                first_table = i
            last_table = i
    table = WAITING_ON_TABLE_HEADER + (
        "\n" + "\n".join(render_waiting_on_row(r) for r in rows) if rows else ""
    )
    if first_table is None or last_table is None:
        new_body = body.rstrip() + "\n\n" + table + "\n\n"
    else:
        new_lines = lines[:first_table] + table.splitlines() + lines[last_table + 1 :]
        new_body = "\n".join(new_lines).rstrip() + "\n\n"
    return content[:body_start] + new_body + content[body_end:]


def insert_waiting_on_before_next_steps(content: str, section_text: str) -> str:
    """Insert the section immediately before ``## Next Steps``.

    Fallbacks (defensive - every current project has Next Steps): before
    ``## Recent Changes``, else append at EOF. The caller is responsible
    for checking ``## Waiting on`` is absent first.
    """
    section_block = section_text.rstrip() + "\n\n"
    for anchor in ("Next Steps", "Recent Changes"):
        span = _section_span(content, anchor)
        if span is not None:
            pos = span[0]
            prefix = content[:pos]
            if prefix and not prefix.endswith("\n\n"):
                prefix = prefix.rstrip("\n") + "\n\n"
            return prefix + section_block + content[pos:]
    return content.rstrip("\n") + "\n\n" + section_block


def parse_last_updated(content: str) -> Optional[datetime]:
    """Parse the ``**Last Updated:**`` header; None when absent/malformed."""
    match = _LAST_UPDATED_RE.search(mask_fences(content))
    if not match:
        return None
    raw = match.group(1).strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def parse_since_date(cell: str) -> Optional[date]:
    """Parse a Since cell into a date; None on malformed.

    Tolerates the hand-written ``~2026-07-09`` approximation prefix by
    extracting the first ISO-looking date anywhere in the cell. Impossible
    dates (``2026-13-40``) and free text (``yesterday``) return None.
    """
    match = _ISO_DATE_RE.search(cell)
    if not match:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


def _header_line(content: str, prefix: str) -> Optional[str]:
    """A header-region line starting with ``prefix`` (before the first ##)."""
    first_h2 = _H2_LINE_RE.search(mask_fences(content))
    header = content[: first_h2.start()] if first_h2 else content
    for line in header.splitlines():
        if line.strip().startswith(prefix):
            return line.strip()
    return None


def build_digest(content: str, path: Path) -> dict[str, Any]:
    """The /missioncache:load digest: resume-critical slices, not the file.

    Reads nothing from disk except the size (falls back to the string's
    byte length when the path does not exist, which keeps pure-string
    tests possible).
    """
    last_updated_match = _LAST_UPDATED_RE.search(mask_fences(content))
    subsections = parse_recent_changes_subsections(content)
    try:
        file_size = path.stat().st_size
    except OSError:
        file_size = len(content.encode())
    return {
        "last_updated": last_updated_match.group(1).strip() if last_updated_match else None,
        "hub": _header_line(content, "Hub:"),
        "related_projects": _header_line(content, "**Related projects:**"),
        "waiting_on": extract_section(content, "Waiting on"),
        "next_steps": extract_section(content, "Next Steps"),
        "recent_changes_last3": [
            f"{heading}\n{body.rstrip()}" for heading, body in subsections[:3]
        ],
        "section_index": section_index(content),
        "file_size_bytes": file_size,
        "health_warnings": check_context_health(content, path),
    }


def check_context_health(
    content: str, path: Path, now: Optional[datetime] = None
) -> list[str]:
    """Health warnings for one context file; empty list means healthy.

    Checks: stale ``Last Updated`` (> STALE_CONTEXT_DAYS), each stale
    Waiting-on row (Since > STALE_WAITING_DAYS; malformed Since skipped
    silently), file size over budget, each missing core section, Recent
    Changes over cap. Report-only strings, no exceptions.
    """
    now = now or datetime.now()
    warnings = []

    last_updated = parse_last_updated(content)
    if last_updated is not None:
        age = (now - last_updated).days
        if age > STALE_CONTEXT_DAYS:
            warnings.append(f"Last Updated is {age} days old (> {STALE_CONTEXT_DAYS}d)")

    for row in parse_waiting_on(content):
        since = parse_since_date(row["since"])
        if since is None:
            continue
        age = (now.date() - since).days
        if age > STALE_WAITING_DAYS:
            what = row["what"][:60]
            warnings.append(
                f"Waiting on '{what}' ({row['who']}) is {age} days old "
                f"(> {STALE_WAITING_DAYS}d)"
            )

    try:
        file_size = path.stat().st_size
    except OSError:
        file_size = len(content.encode())
    if file_size > CONTEXT_SIZE_BUDGET_KB * 1024:
        warnings.append(
            f"context file is {file_size // 1024}KB (> {CONTEXT_SIZE_BUDGET_KB}KB budget)"
        )

    for name in CORE_SECTIONS:
        if extract_section(content, name) is None:
            warnings.append(f"missing core section: ## {name}")

    over = len(parse_recent_changes_subsections(content)) - RECENT_CHANGES_CAP
    if over > 0:
        warnings.append(
            f"Recent Changes is {over} entries over the {RECENT_CHANGES_CAP}-entry cap "
            "(journal rollover pending)"
        )

    return warnings

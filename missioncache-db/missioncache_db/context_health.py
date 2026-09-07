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
# A Recent Changes subsection boundary is a DATED ``### <timestamp>`` heading
# (the shape both live writers emit). A bare ``### `` sub-heading inside an
# entry body is NOT a boundary - it stays with its entry.
_DATED_H3_RE = re.compile(r"^### \d{4}-\d{2}-\d{2}", re.MULTILINE)
# The full shape a live writer emits: `### YYYY-MM-DD HH:MM` and nothing
# else on the line. Stricter than _DATED_H3_RE on purpose - `repair` moves
# blocks BACK into Recent Changes, and a hand-written `### 2026-08-14 sync`
# inside someone's prose is not one of ours, so dragging it in would rewrite
# their section.
_WRITER_H3_RE = re.compile(r"^### (\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s*$", re.MULTILINE)
_LAST_UPDATED_RE = re.compile(r"\*\*Last Updated:\*\*\s*(.+)")
_ISO_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
# A heading at column 0, checked by sanitize_bullet on the FIRST line only
# (rule 3): every later line is indented by rule 4 instead.
_LEADING_HEADING_RE = re.compile(r"^#{1,6}\s")
# Split on pipes that are not escaped as \| (cell-content pipes).
_UNESCAPED_PIPE_RE = re.compile(r"(?<!\\)\|")


def mask_fences(content: str) -> str:
    """Same-length copy of ``content`` with fenced-code lines blanked.

    Fence delimiter lines and everything between them become runs of
    spaces (newlines preserved), so regex offsets computed on the masked
    text are valid indices into the original. Every structure scan in this
    module goes through this, keeping code samples invisible to heading /
    subsection / table detection. Closer rule follows CommonMark: a fence
    closes only on a fence of the SAME character whose length is >= the
    opener's, so a shorter inner fence (e.g. ``` inside a ```` block) is
    content, not a closer.

    An opener with NO closer is deliberately NOT treated as a fence, which
    is where this diverges from CommonMark (there an unclosed fence runs to
    end of document). Measured reason: a PreCompact snapshot truncated
    mid-code-block leaves one dangling opener, and under the CommonMark
    reading every heading below it went invisible - which made
    ``_section_span`` return None for sections that plainly exist and made
    ``append_to_section_body`` create a duplicate section at EOF on every
    write (five ``## Key Files`` headings in one real file). A dangling
    opener read as ordinary text costs at most one stray heading being
    visible; read as a fence it silently swallows the rest of the file.
    """
    if "```" not in content and "~~~" not in content:
        return content
    lines = content.split("\n")
    closed, _ = _fence_scan(lines)
    for start, end in closed:
        for i in range(start, end + 1):
            lines[i] = " " * len(lines[i])
    return "\n".join(lines)


def _fence_scan(lines: list[str]) -> tuple[list[tuple[int, int]], list[int]]:
    """``(closed_ranges, dangling_opener_indices)`` over ``lines``.

    A dangling opener is skipped and scanning RESUMES on the line after it,
    so a properly closed block further down is still recognised. Dropping the
    rest of the scan instead would let a ``## `` inside a real fenced block
    become a live section anchor - the same damage class as the bug this
    tolerance was added for, just from the other direction.
    """
    closed: list[tuple[int, int]] = []
    dangling: list[int] = []
    start = 0
    while start < len(lines):
        opener = -1
        fence = ""
        for i in range(start, len(lines)):
            match = _FENCE_RE.match(lines[i])
            if match is None:
                continue
            if opener == -1:
                opener, fence = i, match.group(1)
            elif (
                match.group(1)[0] == fence[0]
                and len(match.group(1)) >= len(fence)
                # CommonMark: a CLOSING fence carries nothing but whitespace
                # after the delimiter - an info string is opener-only. Without
                # this, a truncated ```` ```markdown ```` block pairs with the
                # NEXT block's ```` ```python ```` opener and everything
                # between them is masked, which silently hid a whole Recent
                # Changes entry from the cap, the digest and the journal while
                # the fence warning pointed at the wrong line entirely.
                and not lines[i][match.end() :].strip()
            ):
                closed.append((opener, i))
                break
        else:
            # Ran off the end: either no opener at all, or one never closed.
            if opener != -1:
                dangling.append(opener)
                start = opener + 1
                continue
            break
        start = closed[-1][1] + 1
    return closed, dangling


def dangling_fence(content: str) -> Optional[tuple[int, str]]:
    """``(1-based line, the opener's delimiter)`` for an unclosed fence, else None.

    The companion to ``mask_fences``' dangling-opener rule: the parsers no
    longer break on one, but it is still malformed content worth reporting -
    and ``sanitize_bullet`` needs the delimiter to close it correctly. The
    closer must be the SAME character and at least as long as the opener, so
    a ``~~~`` or a ```` ```` ```` block cannot be closed with three backticks.
    """
    if "```" not in content and "~~~" not in content:
        return None
    lines = content.split("\n")
    _, dangling = _fence_scan(lines)
    if not dangling:
        return None
    # The LAST one: closing appends at the end of the text, which pairs with
    # the innermost still-open fence. sanitize_bullet loops, so several
    # dangling openers all get closed, innermost first.
    index = dangling[-1]
    match = _FENCE_RE.match(lines[index])
    return index + 1, match.group(1) if match else "```"


def unbalanced_fence_line(content: str) -> Optional[int]:
    """1-based line of a fence opener that is never closed, or None."""
    found = dangling_fence(content)
    return found[0] if found else None


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
    # An EXACT heading wins over a prefix-tolerant one. The tolerance exists
    # to read legacy `## Recent Changes (2026-04-19 07:53)` blocks from the
    # name "Recent Changes", but taking the first match meant that in a file
    # carrying BOTH a legacy sibling and the real bare heading, every read
    # and every write resolved to the legacy block - silently, since
    # duplicate_sections sees no duplicate there.
    exact_re = re.compile(rf"^## {re.escape(name)}[ \t]*$", re.MULTILINE)
    heading_re = re.compile(rf"^## {re.escape(name)}[^\n]*$", re.MULTILINE)
    match = exact_re.search(masked) or heading_re.search(masked)
    if not match:
        return None
    body_start = match.end() + 1 if match.end() < len(content) else match.end()
    next_h2 = _H2_LINE_RE.search(masked, body_start)
    body_end = next_h2.start() if next_h2 else len(content)
    return match.start(), body_start, body_end


def _section_heading_matches(content: str, name: str) -> list[str]:
    """Every visible ``## <name>`` heading line whose text matches EXACTLY.

    Exact, not the prefix-tolerant shape ``_section_span`` reads with, and
    the difference is load-bearing. Context files predating the 2026-07-11
    conventions migration carry a run of legacy
    ``## Recent Changes (2026-04-19 07:53)`` sibling headings - dozens in
    the worst file - with no bare ``## Recent Changes`` among them.
    Counting those as duplicates of "Recent Changes" would make
    ``_require_unambiguous`` refuse every write to a file that is merely
    old, and ``repair`` could not clear it because ``duplicate_sections``
    (which keys on the full heading text) correctly sees no duplicates
    there. Both functions agree: a duplicate is the same heading text twice.
    """
    heading_re = re.compile(rf"^## {re.escape(name)}[ \t]*$", re.MULTILINE)
    return heading_re.findall(mask_fences(content))


class AmbiguousSectionError(ValueError):
    """More than one ``## <name>`` heading, so a write target is ambiguous."""


def _require_unambiguous(content: str, name: str) -> None:
    """Refuse to write into a section name that appears more than once.

    Duplicate sections used to be produced silently: an unclosed fence hid
    the real heading, ``_section_span`` returned None, and the writer created
    a fresh section at EOF - repeatedly. ``mask_fences`` no longer creates
    that blindness, but files carrying the old damage still exist, and
    appending to the FIRST of five ``## Key Files`` sections is a guess, not
    a write. Fail loudly and let ``missioncache-db repair`` merge them.
    """
    matches = _section_heading_matches(content, name)
    if len(matches) > 1:
        raise AmbiguousSectionError(
            f"{len(matches)} '## {name}' sections in this file; "
            f"run `missioncache-db repair` to merge them before writing"
        )


def extract_section(content: str, name: str) -> Optional[str]:
    """Verbatim body of ``## <name>`` (heading excluded), or None if absent."""
    span = _section_span(content, name)
    if span is None:
        return None
    return content[span[1] : span[2]]


def replace_section_body(
    content: str, name: str, new_body: str, strict: bool = True
) -> str:
    """Replace the whole body of ``## <name>``; create at EOF if absent.

    Fence-aware (locates via ``_section_span``): a column-0 ``## <name>``
    inside a fenced code block can never be mistaken for the section, the
    same protection Recent Changes and Waiting on already have. Used for
    whole-section replacements like Next Steps. The heading line is kept;
    the body becomes a blank line, ``new_body``, then a trailing blank line
    before the next section.

    Raises ``AmbiguousSectionError`` when the name appears more than once,
    unless ``strict=False``. The PM mirror passes False: it renders its
    sections FROM the database after the row is already committed and must
    never propagate, so a refusal there is swallowed by its caller and the
    markdown silently stops matching the DB. Writing into the first of two
    is wrong, but it is what happened before the guard existed and it stays
    visible; `missioncache-db repair` merges the duplicates.
    """
    if strict:
        _require_unambiguous(content, name)
    span = _section_span(content, name)
    if span is None:
        return content.rstrip("\n") + f"\n\n## {name}\n\n{new_body}\n"
    _, body_start, body_end = span
    return content[:body_start] + f"\n{new_body}\n\n" + content[body_end:]


def append_to_section_body(
    content: str, name: str, added: str, drop_lines: tuple[str, ...] = ()
) -> str:
    """Append ``added`` to the body of ``## <name>``; create at EOF if absent.

    Fence-aware, same as ``replace_section_body``. Existing body lines are
    preserved except any whose stripped form is in ``drop_lines`` (used to
    strip ``- TBD`` / ``1. TBD`` template placeholders on the first real
    write).

    Raises ``AmbiguousSectionError`` when the name appears more than once.
    """
    _require_unambiguous(content, name)
    span = _section_span(content, name)
    if span is None:
        return content.rstrip("\n") + f"\n\n## {name}\n\n{added}\n"
    _, body_start, body_end = span
    body = content[body_start:body_end]
    existing_lines = [
        line for line in body.strip().splitlines() if line.strip() not in drop_lines
    ]
    existing = "\n".join(existing_lines)
    combined = f"{existing}\n{added}" if existing else added
    return content[:body_start] + f"\n{combined}\n\n" + content[body_end:]


def remove_section(content: str, heading: str) -> tuple[str, Optional[str]]:
    """Delete ``## <heading>`` and its body. Returns ``(content, body_or_None)``.

    Returns the body it cut, so a caller moving a section to another project
    gets the text of the section it actually removed. Reading the body with
    ``extract_section`` and removing with this used to be two different
    lookups: ``extract_section`` is prefix-tolerant and would return the body
    of ``## Notes (old)`` while this deleted ``## Notes``.

    Matches the heading text EXACTLY, unlike ``_section_span``'s deliberately
    prefix-tolerant ``^## {name}[^\\n]*$``. That tolerance is right for
    reading (it finds ``## Recent Changes (2026-04-30 10:20)`` from the name
    "Recent Changes") and wrong for a destructive op, where it would let
    "Key" delete ``## Key Files``. Callers have the verbatim heading from
    ``section_index``, date suffix included, so exact is no burden.
    """
    masked = mask_fences(content)
    heading_re = re.compile(rf"^## {re.escape(heading)}[ \t]*$", re.MULTILINE)
    match = heading_re.search(masked)
    if match is None:
        return content, None
    body_start = match.end() + 1 if match.end() < len(content) else match.end()
    next_h2 = _H2_LINE_RE.search(masked, body_start)
    end = next_h2.start() if next_h2 else len(content)
    return content[: match.start()] + content[end:], content[body_start:end]


# A markdown list item or table row, i.e. a line that starts a removable
# unit inside a section body. Continuation lines are indented or blank.
_LIST_ITEM_RE = re.compile(r"^(?:[-*]\s|\d+\.\s|\|)")
# Table header/separator rows are structure, never a removal target.
_TABLE_SEPARATOR_RE = re.compile(r"^\|[\s:|-]+\|?\s*$")


def remove_list_item(
    content: str, section: str, match_text: str
) -> tuple[str, Optional[str]]:
    """Remove the first list item in ``## <section>`` containing ``match_text``.

    Returns ``(content, removed_item_text_or_None)``. Substring matching,
    the same shape ``waiting_on_resolve`` uses.

    Removes the WHOLE item - the ``- `` (or ``| ``) line plus every indented
    or blank continuation line under it - because ``sanitize_bullet`` makes
    multi-line items normal: a gotcha carrying a demoted heading is one item
    spanning several lines, and dropping only the matching line would strand
    the rest as loose prose. Table header and separator rows are skipped, so
    matching "File" cannot delete a ``| File | Purpose |`` header.
    """
    # Resolve the heading EXACTLY. _section_span is prefix-tolerant, so a
    # truncated name walks straight past the caller's guard list: asking for
    # "Recent Change" resolved to "## Recent Changes" and removed an entry
    # from prepend-only history that BULLETS_REMOVE_FORBIDDEN exists to
    # protect. Destructive ops resolve exactly, same rule as remove_section.
    if not _section_heading_matches(content, section):
        return content, None
    span = _section_span(content, section)
    if span is None:
        return content, None
    _, body_start, body_end = span
    body = content[body_start:body_end]
    lines = body.split("\n")
    # Fence-masked view for structure decisions, per the module contract: a
    # `- rm -rf /x` inside a code sample is not a list item, and removing it
    # took the closing fence with it and left the file with a dangling one.
    masked_lines = mask_fences(body).split("\n")

    def _is_removable_start(i: int) -> bool:
        line = masked_lines[i]
        if not _LIST_ITEM_RE.match(line) or _TABLE_SEPARATOR_RE.match(line):
            return False
        # A table HEADER row is the one directly above the separator row.
        # Without this, matching "File" would delete `| File | Purpose |`.
        nxt = next((l for l in masked_lines[i + 1 :] if l.strip()), "")
        return not _TABLE_SEPARATOR_RE.match(nxt)

    starts = [i for i in range(len(lines)) if _is_removable_start(i)]
    for pos, start in enumerate(starts):
        end = starts[pos + 1] if pos + 1 < len(starts) else len(lines)
        # An item ends where its own continuation ends: at a blank line
        # followed by unindented prose. Running to the next item start
        # swallowed a closing paragraph into the LAST bullet of a section.
        for j in range(start + 1, end):
            if masked_lines[j].strip():
                continue
            following = next(
                (k for k in range(j + 1, end) if masked_lines[k].strip()), None
            )
            if following is not None and not masked_lines[following].startswith(
                (" ", "\t")
            ):
                end = j
                break
        item_lines = lines[start:end]
        # Trailing blank lines belong to the gap, not the item.
        while item_lines and not item_lines[-1].strip():
            item_lines.pop()
            end -= 1
        item = "\n".join(item_lines)
        if match_text not in item:
            continue
        # A table header row directly above a removed row stays: it is the
        # section's structure, not part of the item.
        remaining = lines[:start] + lines[end:]
        new_body = "\n".join(remaining)
        return content[:body_start] + new_body + content[body_end:], item
    return content, None


def _subsection_starts(masked_body: str) -> list[int]:
    """Offsets of dated ``### `` subsection starts within a fence-masked body."""
    return [m.start() for m in _DATED_H3_RE.finditer(masked_body)]


def orphaned_recent_changes(
    content: str, writer_shaped_only: bool = False
) -> list[int]:
    """1-based lines of dated ``### `` entries OUTSIDE the Recent Changes span.

    A dated subsection is only reachable to the cap, the digest and the
    dashboard while it sits inside ``## Recent Changes``. A stray ``## ``
    heading written into the section ends it early and strands every entry
    below - the failure this whole module was hardened against, measured on
    seven live projects.

    ``writer_shaped_only`` narrows the match to the full ``### YYYY-MM-DD
    HH:MM`` shape the live writers emit, which is what ``repair`` will move.
    The health check needs BOTH counts: a hand-written ``### 2026-08-14 sync``
    is genuinely stranded and worth reporting, but repair deliberately leaves
    it alone rather than dragging someone's prose heading into the section,
    so telling the user to run repair would be an instruction that changes
    nothing.
    """
    masked = mask_fences(content)
    span = _section_span(content, "Recent Changes")
    lo, hi = (span[1], span[2]) if span else (0, 0)
    pattern = _WRITER_H3_RE if writer_shaped_only else _DATED_H3_RE
    return [
        masked.count("\n", 0, m.start()) + 1
        for m in pattern.finditer(masked)
        if not (lo <= m.start() < hi)
    ]


def entries_hidden_by_fences(content: str) -> list[int]:
    """1-based lines of writer-shaped ``### `` entries swallowed by a fence.

    The residue of the truncated-snapshot damage, and the case no other
    check catches. A snapshot cut mid-code-block leaves ``` ```markdown ```
    open; the next complete block's closing fence then closes it, so the
    fences BALANCE and everything between is legitimately code as far as
    CommonMark is concerned. Every Recent Changes entry in that span is
    invisible to the cap, the digest, the dashboard and the journal, and
    nothing about the file looks wrong.

    Detected by counting what the raw text has against what the masked text
    has. No repair can fix it - which lines were meant to be code is not
    recoverable - so this is a report, and the fix is to close or delete the
    truncated block by hand.
    """
    masked = mask_fences(content)
    visible = {m.start() for m in _WRITER_H3_RE.finditer(masked)}
    return [
        content.count("\n", 0, m.start()) + 1
        for m in _WRITER_H3_RE.finditer(content)
        if m.start() not in visible
    ]


def duplicate_sections(content: str) -> dict[str, int]:
    """``{section name: count}`` for every ``## `` name appearing more than once."""
    names = [entry["name"] for entry in section_index(content)]
    return {name: names.count(name) for name in dict.fromkeys(names) if names.count(name) > 1}


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


def escape_cell(value: str) -> str:
    """Make a value safe as a markdown table cell: no newlines, pipes escaped.

    Public because Key Files rows are built by the MCP writer from
    caller-supplied path and description strings, and a newline in either
    breaks the row apart into forged markdown that the section parsers then
    read as structure.
    """
    return " ".join(value.replace("|", "\\|").split())


# Back-compat alias for the in-module callers.
_escape_cell = escape_cell


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


def insert_section_before(
    content: str, section_text: str, anchors: tuple[str, ...]
) -> str:
    """Insert ``section_text`` immediately before the first anchor that exists.

    ``anchors`` is tried in order and the section is appended at EOF when none
    match. Fence-aware via ``_section_span``, and the preceding text is
    normalized to end in a blank line so the inserted heading always starts its
    own block.
    """
    section_block = section_text.rstrip() + "\n\n"
    for anchor in anchors:
        span = _section_span(content, anchor)
        if span is not None:
            pos = span[0]
            prefix = content[:pos]
            if prefix and not prefix.endswith("\n\n"):
                prefix = prefix.rstrip("\n") + "\n\n"
            return prefix + section_block + content[pos:]
    return content.rstrip("\n") + "\n\n" + section_block


def insert_waiting_on_before_next_steps(content: str, section_text: str) -> str:
    """Insert the section immediately before ``## Next Steps``.

    Fallbacks (defensive - every current project has Next Steps): before
    ``## Recent Changes``, else append at EOF. The caller is responsible
    for checking ``## Waiting on`` is absent first.
    """
    return insert_section_before(content, section_text, ("Next Steps", "Recent Changes"))


_HEADING_ANY_RE = re.compile(r"^(#{1,5})(\s)", re.MULTILINE)


def demote_headings(text: str) -> str:
    """Push every real markdown heading in ``text`` one level deeper.

    Used on caller-supplied body text before it is nested under a section
    heading. Without it, a body carrying its own ``## Next Steps`` (exactly what
    a pasted meeting summary looks like) creates a SECOND top-level section, and
    because ``_section_span`` takes the first match, the project's real Next
    Steps becomes unreachable to the digest and to every later write.

    Fence-aware, so a ``## foo`` inside a code block is left alone. ``######`` is
    already the deepest level markdown has and is left as-is rather than
    corrupted into seven hashes.
    """
    masked = mask_fences(text)
    out: list[str] = []
    last = 0
    for match in _HEADING_ANY_RE.finditer(masked):
        out.append(text[last : match.start()])
        out.append("#" + match.group(1) + match.group(2))
        last = match.end()
    out.append(text[last:])
    return "".join(out)


def sanitize_bullet(text: str) -> str:
    """Make caller-supplied text safe to nest under a ``- `` list item.

    Every structure scan in this module anchors at column 0 (``_H2_LINE_RE``
    is ``^## ``, ``_DATED_H3_RE`` is ``^### \\d{4}``), so any line of a bullet
    body that starts a heading there can end a section or forge a Recent
    Changes boundary. That is not hypothetical: the PreCompact snapshot
    writes assistant replies verbatim, and eight snapshots across the live
    projects carried a column-0 ``## `` heading that truncated Recent Changes
    and orphaned every entry below it.

    The result is safe to interpolate as the body of a list item
    (``f"- {sanitize_bullet(x)}"``), which is what every caller does, and
    safe as a section body. Four transforms:

    1. Close a dangling code fence, with the SAME delimiter the opener used
       (``~~~`` and a four-backtick fence are both legal and neither closes
       on ```` ``` ````). Closing it takes the headings below it out of
       rules 2 and 3 entirely - they are inside a fence now, which is where
       they render as code anyway.
    2. Demote headings one level, so the nesting reads correctly.
    3. Push a leading fence or heading onto its own line. THIS IS NOT
       COSMETIC. Rule 4 cannot indent line 1 - in a bullet it sits after
       ``- `` and is not at column 0 anyway - so a first line that IS
       structure has to move. Two measured failures, one per shape. A text
       starting with ```` ``` ````: ``_FENCE_RE`` allows up to three
       leading SPACES, not a list marker, so the prefix hides the opener
       while the closer appended by rule 1 lands at column 0 and becomes
       one - that produced a duplicate ``## Recent Changes`` and made
       ``check_context_health`` report four core sections "missing" that
       were plainly present. A text starting with ``## 2026-08-14 sync``
       (an ``imported_event`` body, which has no ``- `` prefix at all):
       demoted to ``### 2026-08-14 sync`` at column 0, a fake dated
       boundary inside the event's own section.
    4. Indent every continuation line by two spaces - correct markdown list
       continuation, and the load-bearing half for headings. Demotion alone
       is NOT enough: ``## 2026-08-14 x`` demotes to ``### 2026-08-14 x``,
       a perfectly good fake dated boundary. Two spaces keeps a heading
       rendering as a heading and a fence rendering as a fence (both allow
       up to three), while neither can match a column-0 anchor.
    """
    if not text:
        return text
    # Loop: closing the innermost dangling fence can expose an outer one.
    # Bounded so malformed input cannot spin.
    for _ in range(10):
        dangling = dangling_fence(text)
        if dangling is None:
            break
        text = text.rstrip("\n") + f"\n{dangling[1]}"
    text = demote_headings(text)
    lines = text.split("\n")
    if lines and (_FENCE_RE.match(lines[0]) or _LEADING_HEADING_RE.match(lines[0])):
        lines.insert(0, "")
    return "\n".join(
        line if i == 0 or not line.strip() else "  " + line
        for i, line in enumerate(lines)
    )


def upsert_related_projects(content: str, project_name: str, note: str = "") -> str:
    """Record ``project_name`` on the ``**Related projects:**`` header line.

    Extends the line when it already exists, creates it at the end of the header
    region (before the first ``##``) otherwise, and is a no-op when the project
    is already listed. The digest reads this line via ``_header_line`` and
    /missioncache:load renders it, so it is how both sides of a cross-project
    link learn the link exists.

    ``project_name`` must already be validated to the ``_FORK_NAME_RE`` shape by
    the caller; ``note`` is flattened here because a newline in it would end the
    header line and let the remainder pose as a header of its own (a planted
    ``Hub:`` line is read by the resume flow, and a planted ``**Fork of:**`` line
    redirects which project's context is loaded as the shared layer).
    """
    note = " ".join(note.split())
    entry = f"[[{project_name}]]" + (f" ({note})" if note else "")
    existing = _header_line(content, "**Related projects:**")

    if existing is not None:
        if f"[[{project_name}]]" in existing:
            return content
        return content.replace(existing, f"{existing}, {entry}", 1)

    header_line = f"**Related projects:** {entry}"
    first_h2 = _H2_LINE_RE.search(mask_fences(content))
    if first_h2 is None:
        return content.rstrip("\n") + f"\n{header_line}\n"
    pos = first_h2.start()
    return content[:pos].rstrip("\n") + f"\n{header_line}\n\n" + content[pos:]


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


# MUST stay byte-identical to statusline._FORK_HEADER_RE (the dashboard copy),
# which mirrors this by hand because the statusline is stdlib-only and cannot
# import this module. The no-slash / leading-alnum shape is a load-bearing
# security control (blocks path traversal via a hand-edited header).
# test_statusline_fork asserts the two patterns are equal.
_FORK_NAME_RE = re.compile(
    r"^\*\*Fork of:\*\*\s*(?:\[\[([A-Za-z0-9][A-Za-z0-9._-]*)\]\]|([A-Za-z0-9][A-Za-z0-9._-]*))\s*$"
)


def parse_fork_parent(content: str) -> Optional[str]:
    """The parent project name from a ``**Fork of:**`` header line, or None.

    Reads ONLY the header region (before the first ``##``), so a "Fork of"
    mention in the body or a fenced example never counts. Accepts the plain
    name or a balanced ``[[wikilink]]``; malformed half-links are rejected.
    """
    line = _header_line(content, "**Fork of:**")
    if not line:
        return None
    match = _FORK_NAME_RE.match(line)
    if not match:
        return None
    return match.group(1) or match.group(2)


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
        "fork_of": _header_line(content, "**Fork of:**"),
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

    # Structural damage. All three come from the same failure - free-form
    # text carrying column-0 headings or a truncated code fence written into
    # the file. `missioncache-db repair` fixes the duplicate sections and
    # (most of) the stranded entries; the fence it only reports, so each
    # warning names its own remedy rather than pointing at one command.
    fence_line = unbalanced_fence_line(content)
    if fence_line is not None:
        warnings.append(
            f"unbalanced code fence opened at line {fence_line} "
            "(run `missioncache-db repair`)"
        )

    hidden = entries_hidden_by_fences(content)
    if hidden:
        warnings.append(
            f"{len(hidden)} Recent Changes entries (first at line {hidden[0]}) sit "
            "inside a code fence and are invisible to every reader - a snapshot "
            "was truncated mid-block and a later block's closing fence swallowed "
            "them. Close or delete the truncated block by hand; `repair` cannot "
            "guess which lines were meant to be code"
        )

    for name, count in duplicate_sections(content).items():
        warnings.append(
            f"{count} '## {name}' sections (writes to it will be refused; "
            "run `missioncache-db repair`)"
        )

    orphans = orphaned_recent_changes(content)
    if orphans:
        repairable = len(orphaned_recent_changes(content, writer_shaped_only=True))
        # Only promise repair for what repair will actually move. The rest
        # are hand-written dated headings that it deliberately leaves alone,
        # and pointing the user at a command that changes nothing is worse
        # than saying so.
        if repairable == len(orphans):
            remedy = "run `missioncache-db repair`"
        elif repairable:
            remedy = (
                f"`missioncache-db repair` moves {repairable} of them; the rest "
                "are hand-written headings to move yourself"
            )
        else:
            remedy = (
                "these are hand-written headings, not writer entries - move "
                "them yourself, `repair` leaves them alone"
            )
        warnings.append(
            f"{len(orphans)} dated Recent Changes entries sit outside the section "
            f"(first at line {orphans[0]}) and never roll to the journal ({remedy})"
        )

    return warnings


# ── repair ───────────────────────────────────────────────────────────────

def _merge_duplicate_sections(content: str) -> tuple[str, list[str]]:
    """Fold repeat ``## <name>`` sections into the first one of that name.

    Splices the text directly instead of going through
    ``append_to_section_body``: that writer refuses an ambiguous target, and
    ambiguity is precisely what this is here to remove.
    """
    merged: list[str] = []
    for name, count in duplicate_sections(content).items():
        masked = mask_fences(content)
        # EXACT, matching duplicate_sections and _section_heading_matches.
        # Prefix-tolerant here would be destructive: a file with two bare
        # `## Recent Changes` plus three legacy `## Recent Changes (2026-04-19
        # 07:53)` siblings would collapse all five into one - deleting three
        # headings the detector never counted - while reporting "2 -> 1".
        heading_re = re.compile(rf"^## {re.escape(name)}[ \t]*$", re.MULTILINE)
        spans = []
        for match in heading_re.finditer(masked):
            body_start = match.end() + 1
            next_h2 = _H2_LINE_RE.search(masked, body_start)
            spans.append(
                (match.start(), body_start, next_h2.start() if next_h2 else len(content))
            )
        if len(spans) < 2:
            continue
        _, first_body_start, first_end = spans[0]
        bodies = [content[first_body_start:first_end].strip()]
        bodies += [content[b:e].strip() for _, b, e in spans[1:]]
        # Cut the later copies bottom-up so earlier offsets stay valid, then
        # write the joined body back into the first one.
        for start, _, end in reversed(spans[1:]):
            content = content[:start] + content[end:]
        joined = "\n\n".join(b for b in bodies if b)
        content = (
            content[:first_body_start] + f"\n{joined}\n\n" + content[first_end:]
        )
        merged.append(f"{name} ({count} -> 1)")
    return content, merged


def _reabsorb_orphans(content: str) -> tuple[str, int]:
    """Move stranded writer-shaped ``### `` blocks back under Recent Changes."""
    span = _section_span(content, "Recent Changes")
    if span is None:
        return content, 0
    masked = mask_fences(content)
    _, rc_start, rc_end = span

    blocks: list[tuple[int, int, str]] = []
    starts = [m.start() for m in _WRITER_H3_RE.finditer(masked)]
    for i, start in enumerate(starts):
        if rc_start <= start < rc_end:
            continue
        # A stranded block ends at the next writer-shaped heading or the
        # next `## ` section, whichever comes first.
        next_block = starts[i + 1] if i + 1 < len(starts) else len(content)
        next_h2 = _H2_LINE_RE.search(masked, start)
        end = min(next_block, next_h2.start() if next_h2 else len(content))
        blocks.append((start, end, content[start:end]))
    if not blocks:
        return content, 0

    # Cut from the bottom up so earlier offsets stay valid.
    for start, end, _ in reversed(blocks):
        content = content[:start] + content[end:]

    texts = [_strip_pointer(text).rstrip() for _, _, text in blocks]
    existing = extract_section(content, "Recent Changes") or ""
    combined = _strip_pointer(existing).strip()
    reabsorbed = "\n\n".join(t for t in texts if t)
    new_body = f"{combined}\n\n{reabsorbed}" if combined else reabsorbed
    # Newest first, the invariant every live writer maintains by prepending.
    new_body = _sort_subsections_newest_first(new_body)
    return replace_section_body(content, "Recent Changes", new_body), len(blocks)


def _strip_pointer(text: str) -> str:
    """Drop journal pointer lines; the cap re-places exactly one at the bottom."""
    return "\n".join(
        line
        for line in text.splitlines()
        if not line.strip().startswith(_POINTER_PREFIX)
    )


def _sort_subsections_newest_first(body: str) -> str:
    """Order a Recent Changes body's dated subsections newest first."""
    starts = [m.start() for m in _WRITER_H3_RE.finditer(mask_fences(body))]
    if not starts:
        return body
    preamble = body[: starts[0]].strip()
    chunks = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(body)
        stamp = _WRITER_H3_RE.match(body, start)
        chunks.append((stamp.group(1) if stamp else "", body[start:end].rstrip()))
    chunks.sort(key=lambda c: c[0], reverse=True)
    ordered = "\n\n".join(chunk for _, chunk in chunks)
    return f"{preamble}\n\n{ordered}" if preamble else ordered


def repair_content(content: str, journal_name: str) -> tuple[str, str | None, dict]:
    """Repair one context file's structure. Pure.

    Returns ``(new_content, journal_append_or_None, report)``. Fixes what is
    mechanical and safe: duplicate sections are merged into the first of
    their name, stranded Recent Changes blocks are moved back into the
    section and re-sorted newest-first, and the cap then rolls the overflow
    into the journal the way it should have all along. The cap runs
    unconditionally, so a file whose only problem is an overdue rollover is
    fixed too.

    Deliberately does NOT rewrite prose. An unclosed fence is REPORTED and
    left alone: closing it guesses where the code ended. A stray
    pasted-output section is neither reported nor removed - deciding a
    section is junk is the user's call, and
    ``update_context_file(sections_remove=[...])`` is the sanctioned way to
    drop one.
    """
    report = {
        "merged_sections": [],
        "reabsorbed_entries": 0,
        "rolled_to_journal": 0,
        "unbalanced_fence_line": unbalanced_fence_line(content),
        "orphans_left": 0,
        "skipped_for_fence": False,
    }
    if report["unbalanced_fence_line"] is not None:
        # Refuse to restructure a file whose fence state is unknown. Every
        # decision below rests on which lines are code, and an unclosed fence
        # means that answer is a guess. Measured before this guard: a Key
        # Files section ending in a truncated ```markdown sample containing
        # `## Gotchas` was read as a real duplicate section, and the merge
        # hoisted a line OUT of the code sample into the live Gotchas
        # section - in the same run that printed "fence NOT repaired, fix by
        # hand", with no backup. Reporting and then restructuring anyway is
        # the worst of both.
        report["skipped_for_fence"] = True
        return content, None, report
    report["skipped_for_fence"] = False
    content, merged = _merge_duplicate_sections(content)
    report["merged_sections"] = merged
    content, reabsorbed = _reabsorb_orphans(content)
    report["reabsorbed_entries"] = reabsorbed
    # What repair could NOT move, so "reabsorbed 0" is never mistaken for
    # "the file was clean". Two real causes: the file has no ``## Recent
    # Changes`` heading to move anything into, and hand-written dated
    # headings that _WRITER_H3_RE deliberately does not claim.
    report["orphans_left"] = len(orphaned_recent_changes(content))
    content, journal_append, rolled = split_recent_changes_for_cap(
        content, journal_name
    )
    report["rolled_to_journal"] = rolled
    return content, journal_append, report

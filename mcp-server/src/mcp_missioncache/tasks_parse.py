"""Pure-function parser for tasks.md checklist items.

The orbit DB tracks projects, not checklist items. Items are markdown
lines like ``- [ ] 54a. M11.2 - VSCode statusline extension``. Parsing
is consumed by the MCP tool layer (validating task numbers passed to
``set_active_orbit_tasks``) and the ``update_tasks_file`` completion
diff. The statusline has its own duplicate of the regex to keep
orbit-dashboard free of any mcp_orbit dependency.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Match a checklist line:
#   "- [ ] 54a. text"
#   "  - [x] 0.1. text"
#   "- [ ] 8. text"
# Captures (1) checked? (x or space), (2) number string (54a, 0.1, 8),
# (3) trailing description text.
_CHECKLIST_RE = re.compile(
    r"^\s*[-*]\s*\[\s*([xX ])\s*\]\s*([0-9]+(?:[.][0-9]+)*[a-z]?)\s*\.\s*(.*?)\s*$"
)


@dataclass(frozen=True)
class ChecklistItem:
    number: str
    text: str
    checked: bool


def parse_tasks_md(content: str) -> list[ChecklistItem]:
    """Parse all checklist items from a tasks.md file body.

    Returns items in source order. Both ``[x]`` (checked) and ``[ ]``
    (pending) are included; callers filter as needed.
    """
    items: list[ChecklistItem] = []
    for line in content.splitlines():
        m = _CHECKLIST_RE.match(line)
        if not m:
            continue
        items.append(
            ChecklistItem(
                number=m.group(2),
                text=m.group(3),
                checked=m.group(1).lower() == "x",
            )
        )
    return items


def find_item(items: list[ChecklistItem], number: str) -> ChecklistItem | None:
    """Return the checklist item matching ``number`` exactly, or None."""
    for item in items:
        if item.number == number:
            return item
    return None

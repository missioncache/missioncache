"""MCPServer application instance, shared across tool modules.

mcp SDK 2.0 renamed FastMCP to MCPServer and moved it from
mcp.server.fastmcp to mcp.server.mcpserver (the 2.0.0 migration guide);
the decorator API and stdio run are unchanged.
"""

from mcp.server.mcpserver import MCPServer

INSTRUCTIONS = (
    "MissionCache project/task tracking.\n\n"
    "Write a project's plan, context and tasks files through the MCP tools "
    "(update_context_file, update_tasks_file), never by editing them "
    "directly. Only the tool path takes the per-file lock, and more than one "
    "session can be live on a project at once, so a direct write can drop "
    "another session's edit.\n\n"
    "Save at milestones rather than at the end: a finished task, a decision, "
    "a constraint you discovered, before a long operation. The save command "
    "is /missioncache:save in Claude Code and /missioncache-save elsewhere.\n\n"
    "In the context file, section names are exact - code finds sections by "
    "name. Recent Changes is prepend-only and capped, so add at the top and "
    "never rewrite older entries. A section carrying a 'Managed by "
    "MissionCache' comment is rendered from the database: change it through "
    "the PM tools, not in the file.\n\n"
    "Removing is a tool call too. Drop a dead section or item with "
    "update_context_file's sections_remove / bullets_remove, and a superseded "
    "task with update_tasks_file's tasks_remove - leaving one ticked or "
    "sitting there skews the progress counter. When content belongs in "
    "another project, use move_to_project rather than a remove plus an add: "
    "it holds both projects' locks, so a split cannot half-apply.\n\n"
    "Keep a Recent Changes entry to a short summary line. Never paste session "
    "output into one: the tools sanitize what they are given so a stray '## ' "
    "heading cannot break the file, but an entry that long is unreadable on "
    "resume and defeats the point of the section.\n\n"
    "Some operations are deliberately CLI-only and have no MCP tool: "
    "cross-machine export/import of projects (missioncache-db export/import) "
    "and the per-machine path map (missioncache-db config), tag keyword "
    "management, and DB maintenance (prune, cleanup, health). For those, run "
    "the missioncache-db CLI via the shell - `missioncache-db` with no "
    "arguments prints the full command reference, and docs/cli.md in the "
    "MissionCache repo documents each command."
)

mcp = MCPServer("missioncache", instructions=INSTRUCTIONS)

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
    "Some operations are deliberately CLI-only and have no MCP tool: "
    "cross-machine export/import of projects (missioncache-db export/import) "
    "and the per-machine path map (missioncache-db config), tag keyword "
    "management, and DB maintenance (prune, cleanup, health). For those, run "
    "the missioncache-db CLI via the shell - `missioncache-db` with no "
    "arguments prints the full command reference, and docs/cli.md in the "
    "MissionCache repo documents each command."
)

mcp = MCPServer("missioncache", instructions=INSTRUCTIONS)

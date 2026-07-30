"""MCPServer application instance, shared across tool modules.

mcp SDK 2.0 renamed FastMCP to MCPServer and moved it from
mcp.server.fastmcp to mcp.server.mcpserver (the 2.0.0 migration guide);
the decorator API and stdio run are unchanged.
"""

from mcp.server.mcpserver import MCPServer

INSTRUCTIONS = (
    "MissionCache project/task tracking. Some operations are deliberately "
    "CLI-only and have no MCP tool: cross-machine export/import of projects "
    "(missioncache-db export/import) and the per-machine path map "
    "(missioncache-db config), tag keyword management, and DB maintenance "
    "(prune, cleanup, health). For those, run the missioncache-db CLI via the shell - "
    "`missioncache-db` with no arguments prints the full command reference, "
    "and docs/cli.md in the MissionCache repo documents each command."
)

mcp = MCPServer("missioncache", instructions=INSTRUCTIONS)

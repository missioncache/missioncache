"""FastMCP application instance, shared across tool modules."""

from mcp.server.fastmcp import FastMCP

INSTRUCTIONS = (
    "MissionCache project/task tracking. Some operations are deliberately "
    "CLI-only and have no MCP tool: cross-machine export/import of projects "
    "(missioncache-db export/import) and the per-machine path map "
    "(missioncache-db config), tag keyword management, and DB maintenance "
    "(prune, cleanup). For those, run the missioncache-db CLI via the shell - "
    "`missioncache-db` with no arguments prints the full command reference, "
    "and docs/cli.md in the MissionCache repo documents each command."
)

mcp = FastMCP("missioncache", instructions=INSTRUCTIONS)

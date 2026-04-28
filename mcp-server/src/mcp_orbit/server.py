"""
Orbit MCP Server - Fast task management for Claude Code.

Slim entry point. Tool implementations live in split modules
(tools_tasks, tools_docs, tools_tracking, tools_iteration, tools_planning).
Importing them registers their tools with the shared mcp instance from app.py.
"""

import logging

# Import shared mcp instance
from .app import mcp  # noqa: F401

# Import tool modules to trigger @mcp.tool() registration
from . import tools_tasks  # noqa: F401
from . import tools_docs  # noqa: F401
from . import tools_tracking  # noqa: F401
from . import tools_iteration  # noqa: F401
from . import tools_planning  # noqa: F401
from . import tools_active  # noqa: F401

# Configure logging
logging.basicConfig(level=logging.INFO)


def main():
    """Run the MCP server."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

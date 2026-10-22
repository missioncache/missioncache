"""
MissionCache MCP Server - Fast task management for Claude Code.

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
from . import tools_pm  # noqa: F401


def _forbid_unknown_arguments():
    """Make every tool reject an argument it does not declare.

    The SDK's argument models ignore extra keys, so a misspelled name
    (``recent_change`` for ``recent_changes``) was dropped and the tool
    still reported success. Rejecting it hands the caller an error that
    names the stray key, and the declared names are in the schema.
    """
    for tool in mcp._tool_manager.list_tools():
        model = tool.fn_metadata.arg_model
        model.model_config["extra"] = "forbid"
        model.model_rebuild(force=True)
        tool.parameters = model.model_json_schema(by_alias=True)


_forbid_unknown_arguments()

# Configure logging
logging.basicConfig(level=logging.INFO)


def main():
    """Run the MCP server."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

"""Tests for the FastMCP application instance (app.py)."""

from mcp_missioncache.app import INSTRUCTIONS, mcp


def test_server_instructions_carry_cli_signpost():
    """The server instructions are the only place all MCP clients
    (Claude Code, Codex, OpenCode, VSCode) learn the deliberately-CLI-only
    surface exists - guard both the wiring and the signpost content."""
    assert mcp.instructions == INSTRUCTIONS
    for marker in ("CLI-only", "export/import", "missioncache-db", "docs/cli.md"):
        assert marker in INSTRUCTIONS

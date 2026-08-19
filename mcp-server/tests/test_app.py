"""Tests for the FastMCP application instance (app.py)."""

from mcp_missioncache.app import INSTRUCTIONS, mcp


def test_server_instructions_are_wired_to_the_server():
    assert mcp.instructions == INSTRUCTIONS


def test_server_instructions_carry_cli_signpost():
    """The server instructions are the only place all MCP clients
    (Claude Code, Codex, OpenCode, VSCode) learn the deliberately-CLI-only
    surface exists."""
    for marker in ("CLI-only", "export/import", "missioncache-db", "docs/cli.md"):
        assert marker in INSTRUCTIONS


def test_server_instructions_carry_the_write_through_the_tools_rule():
    """The one correctness invariant in here, and the reason this string grew.

    Only the MCP write path takes the per-file lock. A tool with shell access
    can edit a context file directly and drop a parallel session's write, and
    outside Claude Code nothing else tells it not to: the behavioural guidance
    in rules/missioncache.md is installed to ~/.claude/rules/ and reaches no
    other client. Phrase-level rather than an equality check, because an
    equality check passes on any content and would let a future trim drop this
    silently.
    """
    lowered = INSTRUCTIONS.lower()
    assert "update_context_file" in INSTRUCTIONS
    assert "update_tasks_file" in INSTRUCTIONS
    assert "lock" in lowered, "the reason direct edits are unsafe must be stated"
    assert "never by editing them" in lowered or "never edit" in lowered


def test_server_instructions_name_the_save_command_for_both_namespaces():
    """Claude Code uses /missioncache:save, the other tools use the flat form.

    The string is read by all four, so naming only one leaves three clients
    pointed at a command that does not exist there.
    """
    assert "/missioncache:save" in INSTRUCTIONS
    assert "/missioncache-save" in INSTRUCTIONS


def test_server_instructions_carry_the_context_file_contract():
    """Section names are load-bearing and Recent Changes is prepend-only."""
    lowered = INSTRUCTIONS.lower()
    assert "recent changes" in lowered
    assert "prepend-only" in lowered
    assert "managed by missioncache" in lowered

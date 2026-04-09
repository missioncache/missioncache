# Orbit Plugin - Maintainer Guide

## Architecture

- **MCP Server**: Primary interface (`mcp-server/src/mcp_orbit/`)
- **Database**: `orbit-db/` package (SQLite at `~/.claude/tasks.db`)
- **Hooks**: Auto-save on compaction, detect active project on start
- **Commands**: Slash commands (`/orbit:new`, `/orbit:go`, `/orbit:save`, `/orbit:done`, `/orbit:prompts`, `/orbit:mode`)
- **Orbit Auto**: Autonomous execution CLI (`orbit-auto/`)
- **Orbit Dashboard**: Web UI at localhost:8787 (`orbit-dashboard/`)
- **Statusline**: Optional terminal status display (`statusline/`)
- **Rules** (`rules/`): Claude behavioral guidance symlinked into `~/.claude/rules/` by the installer

## Key Files

| File | Purpose |
|------|---------|
| `mcp-server/src/mcp_orbit/server.py` | MCP entry point, registers all tools |
| `mcp-server/src/mcp_orbit/db.py` | orbit_db wrapper |
| `mcp-server/src/mcp_orbit/orbit.py` | File operations (create, update, parse) |
| `mcp-server/src/mcp_orbit/iteration_log.py` | Autonomous loop logging |
| `mcp-server/src/mcp_orbit/models.py` | Pydantic response models |
| `mcp-server/src/mcp_orbit/errors.py` | OrbitError, OrbitFileNotFoundError |
| `mcp-server/src/mcp_orbit/config.py` | Configuration via ORBIT_ env vars |
| `mcp-server/src/mcp_orbit/tools_tasks.py` | Task lifecycle tools |
| `mcp-server/src/mcp_orbit/tools_docs.py` | Documentation tools |
| `mcp-server/src/mcp_orbit/tools_tracking.py` | Time tracking tools |
| `mcp-server/src/mcp_orbit/tools_iteration.py` | Iteration logging tools |
| `mcp-server/src/mcp_orbit/tools_planning.py` | Planning tools |
| `orbit-db/orbit_db/__init__.py` | Core database layer (~3400 lines) |
| `orbit-auto/orbit_auto/cli.py` | Orbit Auto CLI entry point |
| `orbit-dashboard/server.py` | FastAPI dashboard backend |
| `hooks/hooks.json` | Hook definitions |
| `hooks/session_start.py` | SessionStart hook |
| `hooks/pre_compact.py` | PreCompact hook |
| `hooks/stop.py` | Stop hook |
| `commands/*.md` | Slash command definitions |
| `templates/` | File templates for orbit project files |
| `rules/*.md` | Claude rule files installed to `~/.claude/rules/` (via symlink) |

## MCP Server Configuration

MCP server config is inlined in `.claude-plugin/plugin.json` under the `mcpServers` key. Tools appear as `mcp__plugin_orbit_pm__*` in Claude Code.

## Adding a New MCP Tool

1. Add tool in the appropriate `tools_*.py` module:
   ```python
   @mcp.tool()
   async def my_tool(
       param: Annotated[str, Field(description="Parameter description")],
   ) -> dict:
       """Tool description shown in help."""
       db = get_db()
       try:
           return {"success": True, ...}
       except OrbitError as e:
           return e.to_dict()
       except Exception as e:
           logger.exception("Error in my_tool")
           return {"error": True, "message": str(e)}
   ```

2. Import and register in `server.py`
3. Add response model in `models.py` if needed

## Adding a New Command

1. Create `commands/<name>.md` with frontmatter:
   ```yaml
   ---
   description: "Short description for /help"
   argument-hint: "[optional-args]"
   ---
   ```

2. Add instructions for Claude to follow when command is invoked
3. Reinstall plugin: `claude plugins install orbit@local`

## Database

orbit-db provides `OrbitDB` class with these key tables:
- `repositories` - Tracked git repos
- `tasks` - Projects (name, status, jira_key, tags)
- `heartbeats` - WakaTime-style activity records
- `sessions` - Aggregated work sessions
- `auto_executions` - Orbit Auto run records
- `auto_execution_logs` - Execution streaming logs

## Dashboard Dual-DB Pattern

- **SQLite** (`~/.claude/tasks.db`): Source of truth for writes
- **DuckDB** (`~/.claude/tasks.duckdb`): Analytics database for fast reads
- `orbit-dashboard/lib/analytics_db.py` handles DuckDB operations

## Testing

```bash
# Run MCP server manually
cd mcp-server && uvx --from . mcp-orbit

# Test imports
uvx --from . python -c "from mcp_orbit.server import mcp; print('OK')"

# Run dashboard locally
cd orbit-dashboard && python3 server.py

# Test orbit-auto
orbit-auto --dry-run my-project
```

## Installation

```bash
./setup.sh
```

Or manually:
```bash
pip install -e ./orbit-db
pip install -e ./orbit-auto
claude plugins install orbit@local
```

## Dependencies

- Python 3.11+
- mcp>=1.0.0
- pydantic>=2.0.0
- pydantic-settings>=2.0.0
- fastapi, uvicorn, duckdb (dashboard)

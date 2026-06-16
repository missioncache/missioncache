# MissionCache Plugin - Maintainer Guide

## Architecture

- **MCP Server**: Primary interface (`mcp-server/src/mcp_missioncache/`)
- **Database**: `missioncache-db/` package (SQLite at `~/.missioncache/tasks.db`)
- **Hooks**: Auto-save on compaction, detect active project on start
- **Commands**: Slash commands (`/missioncache:new`, `/missioncache:load`, `/missioncache:save`, `/missioncache:done`, `/missioncache:prompts`, `/missioncache:mode`)
- **MissionCache Auto**: Autonomous execution CLI (`missioncache-auto/`)
- **MissionCache Dashboard**: Web UI at localhost:8787 (`missioncache-dashboard/`)
- **Statusline**: Optional terminal status display (bundled in `missioncache-dashboard/missioncache_dashboard/statusline.py`, installed via the `missioncache-statusline` pip entry point)
- **Rules** (`rules/`): Claude behavioral guidance symlinked into `~/.claude/rules/` by the installer

## Key Files

| File | Purpose |
|------|---------|
| `mcp-server/src/mcp_missioncache/server.py` | MCP entry point, registers all tools |
| `mcp-server/src/mcp_missioncache/db.py` | missioncache_db wrapper |
| `mcp-server/src/mcp_missioncache/project_files.py` | File operations (create, update, parse) |
| `mcp-server/src/mcp_missioncache/iteration_log.py` | Autonomous loop logging |
| `mcp-server/src/mcp_missioncache/models.py` | Pydantic response models |
| `mcp-server/src/mcp_missioncache/errors.py` | MissionCacheError, MissionCacheFileNotFoundError |
| `mcp-server/src/mcp_missioncache/config.py` | Configuration via MISSIONCACHE_ env vars |
| `mcp-server/src/mcp_missioncache/tools_tasks.py` | Task lifecycle tools |
| `mcp-server/src/mcp_missioncache/tools_docs.py` | Documentation tools |
| `mcp-server/src/mcp_missioncache/tools_tracking.py` | Time tracking tools |
| `mcp-server/src/mcp_missioncache/tools_iteration.py` | Iteration logging tools |
| `mcp-server/src/mcp_missioncache/tools_planning.py` | Planning tools |
| `missioncache-db/missioncache_db/__init__.py` | Core database layer (~3400 lines) |
| `missioncache-auto/missioncache_auto/cli.py` | MissionCache Auto CLI entry point |
| `missioncache-dashboard/missioncache_dashboard/server.py` | FastAPI dashboard backend |
| `hooks/hooks.json` | Hook definitions |
| `hooks/session_start.py` | SessionStart hook |
| `hooks/pre_compact.py` | PreCompact hook |
| `hooks/stop.py` | Stop hook |
| `hooks/activity_tracker.py` | UserPromptSubmit hook (heartbeat recording) |
| `hooks/task_tracker.py` | UserPromptSubmit hook (missioncache task-tracking divergence reminder) |
| `commands/*.md` | Slash command definitions |
| `templates/` | File templates for missioncache project files |
| `rules/*.md` | Claude rule files installed to `~/.claude/rules/` (via symlink) |

## MCP Server Configuration

MCP server config is inlined in `.claude-plugin/plugin.json` under the `mcpServers` key. Tools appear as `mcp__plugin_missioncache_pm__*` in Claude Code.

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
       except MissionCacheError as e:
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
3. Reinstall plugin (maintainer dev loop uses the local marketplace): `claude plugins install missioncache@local`

## Database

missioncache-db provides the `TaskDB` class with these key tables:
- `repositories` - Tracked git repos
- `tasks` - Projects (name, status, jira_key, tags)
- `heartbeats` - WakaTime-style activity records
- `sessions` - Aggregated work sessions
- `auto_executions` - MissionCache Auto run records
- `auto_execution_logs` - Execution streaming logs

## Dashboard Dual-DB Pattern

- **SQLite** (`~/.missioncache/tasks.db`): Source of truth for writes
- **DuckDB** (`~/.missioncache/tasks.duckdb`): Analytics database for fast reads
- `missioncache-dashboard/missioncache_dashboard/lib/analytics_db.py` handles DuckDB operations

## Testing

```bash
# Run MCP server manually
cd mcp-server && uvx --from . mcp-missioncache

# Test imports
uvx --from . python -c "from mcp_missioncache.server import mcp; print('OK')"

# Run dashboard locally (via the pip-installed entry point)
missioncache-dashboard serve

# Test missioncache-auto
missioncache-auto --dry-run my-project
```

## Installation

Two paths depending on context:

**Public user install** (plugin core + dashboard + missioncache-auto + statusline, via PyPI; live once the missioncache-* packages publish at Task 74):
```bash
uvx missioncache-install
# or
pipx run missioncache-install
```

**Maintainer install** (clone + editable pip installs + local marketplace for fast iteration):
```bash
git clone https://github.com/missioncache/missioncache.git
cd missioncache
uvx missioncache-install --local
```

Or manually, without `missioncache-install`:
```bash
pip install -e ./missioncache-db
pip install -e ./missioncache-auto
pip install -e ./missioncache-dashboard
claude plugins install missioncache@local
```

The `@local` suffix refers to the local marketplace that `missioncache-install --local` creates under `~/.claude/plugins/local-marketplace/`. The `@missioncache` suffix refers to the GitHub-hosted marketplace defined in this repo's `.claude-plugin/marketplace.json` (the marketplace `name`). They are independent and can coexist. In `--local` mode the installer always sets up the local marketplace; in default PyPI mode it never touches it. If you have both installed, use `claude plugins list` to see which is active.

## Dependencies

- Python 3.11+
- mcp>=1.0.0
- pydantic>=2.0.0
- pydantic-settings>=2.0.0
- fastapi, uvicorn, duckdb (dashboard)

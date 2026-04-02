# Development Workflow

## After Editing Plugin Files

Changes to commands, hooks, templates, MCP server, `.mcp.json`, or `plugin.json` are **cached** by Claude Code. After editing, refresh the cache:

```bash
claude plugins install orbit@local
```

Then restart your Claude Code session to pick up the changes.

## After Editing the Dashboard

```bash
launchctl unload ~/Library/LaunchAgents/com.orbit.dashboard.plist
launchctl load ~/Library/LaunchAgents/com.orbit.dashboard.plist
```

Or just restart the service. Frontend-only changes (index.html) take effect on browser refresh if the server reads the file dynamically.

## After Editing orbit-db or orbit-auto

No action needed. Both are pip-installed in editable mode (`pip install -e`), so Python reads directly from the repo.

## After Editing the Statusline

No action needed. `~/.claude/scripts/statusline.py` is a symlink to the repo copy.

## Quick Reference

| Component | Needs reinstall? | Needs restart? |
|-----------|-----------------|----------------|
| commands/*.md | `claude plugins install orbit@local` | New Claude session |
| hooks/*.py | `claude plugins install orbit@local` | New Claude session |
| mcp-server/ | `claude plugins install orbit@local` | New Claude session |
| templates/ | `claude plugins install orbit@local` | New Claude session |
| .mcp.json | `claude plugins install orbit@local` | New Claude session |
| orbit-db/ | No | No |
| orbit-auto/ | No | No |
| statusline/ | No | No |
| orbit-dashboard/ | No | Restart launchd service |

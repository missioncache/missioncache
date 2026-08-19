# MissionCache for VS Code

Status-bar companion for [MissionCache](https://github.com/missioncache/missioncache) - the project manager for AI coding agents.

Shows your active MissionCache project and task progress in the status bar. Click it for quick access to the project's tasks and context files, the dashboard, and your other active projects.

**This extension requires the MissionCache CLI.** Install it first:

```
uvx missioncache-install
```

The extension reads project state through the `missioncache-db` CLI on your PATH. Without it, the status bar shows "MissionCache: not installed" and nothing else works - the extension is a companion to the CLI install, not a standalone tool.

Works in VS Code, Cursor, and Windsurf (published to both the VS Marketplace and Open VSX).

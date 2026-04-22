# Security Policy

## Reporting a Vulnerability

Please do not report security issues via public GitHub issues. Use GitHub's private vulnerability reporting channel instead:

1. Go to https://github.com/tomerbr1/claude-orbit/security
2. Click **Report a vulnerability**
3. Describe the issue, affected component, and steps to reproduce

This opens a private channel between you and the maintainer. You will get an acknowledgment as soon as I can read it. Orbit is a single-maintainer project, so response times are best-effort.

## Scope

Orbit is a Claude Code plugin built on top of the MCP protocol. In-scope reports include:

- Issues in orbit's own code: plugin core, `orbit-db`, `orbit-auto`, the dashboard backend, hooks, statusline.
- Issues in orbit's install or setup flow: the `orbit-install` package, rule-file installation, database initialization.
- Issues in how orbit handles user data: tasks, heartbeats, orbit files, the SQLite and DuckDB stores under `~/.claude/`.

Out of scope:

- Issues in Claude Code itself - please report those to Anthropic.
- Issues in the MCP protocol specification - please report those upstream to the MCP project.
- Issues in third-party dependencies (FastAPI, DuckDB, etc.) - please report those to the respective projects. If the issue is in how orbit *uses* a dependency, that is in scope here.

## What to include

- A clear description of the issue.
- Steps to reproduce on a clean orbit install (use `uvx orbit-install` as the baseline).
- The affected component and version (commit hash from `git log`).
- Any logs, stack traces, or proof-of-concept output that help confirm the issue.

## Disclosure

Once a fix is available, I will publish a patched release and credit the reporter in the release notes unless you ask to remain anonymous. Coordinated disclosure timelines are negotiable.

Thank you for helping keep orbit safe for everyone who uses it.

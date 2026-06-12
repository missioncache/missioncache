# orbit-dashboard

Task analytics and autonomous execution monitoring for the
[orbit](https://github.com/tomerbr1/orbit-pm) Claude Code plugin.

A local FastAPI web dashboard at `http://localhost:8787` that surfaces:

- Per-project, per-repo, per-day time breakdowns
- Orbit Auto execution monitoring with live SSE streaming
- Claude Code usage stats (session/weekly limits, token costs)
- Activity timeline with tracked and untracked session reconciliation

Built on a dual-DB pattern: SQLite (writes, via `orbit-db`) + DuckDB
(analytics reads).

## Install

```bash
pip install orbit-dashboard
```

Optional feature extras:

```bash
pip install "orbit-dashboard[rss]"    # RSS feeds feature
pip install "orbit-dashboard[learn]"  # AI-generated learning docs
```

## Run

```bash
# Default: serve on port 8787
orbit-dashboard

# Override port via env var
ORBIT_DASHBOARD_PORT=9000 orbit-dashboard
```

Open `http://localhost:8787` in your browser.

## Install as a service

`orbit-dashboard install-service` registers the dashboard as a launchd
(macOS) or systemd user unit (Linux) so it starts on login. See the
[orbit project](https://github.com/tomerbr1/orbit-pm) for the full
install guide.

## License

MIT

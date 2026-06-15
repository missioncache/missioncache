# missioncache-dashboard

Task analytics and autonomous execution monitoring for the
[MissionCache](https://github.com/missioncache/missioncache) Claude Code plugin.

A local FastAPI web dashboard at `http://localhost:8787` that surfaces:

- Per-project, per-repo, per-day time breakdowns
- MissionCache Auto execution monitoring with live SSE streaming
- Claude Code usage stats (session/weekly limits, token costs)
- Activity timeline with tracked and untracked session reconciliation

Built on a dual-DB pattern: SQLite (writes, via `missioncache-db`) + DuckDB
(analytics reads).

## Install

```bash
pip install missioncache-dashboard
```

Optional feature extras:

```bash
pip install "missioncache-dashboard[rss]"    # RSS feeds feature
pip install "missioncache-dashboard[learn]"  # AI-generated learning docs
```

## Run

```bash
# Default: serve on port 8787
missioncache-dashboard

# Override port via env var
MISSIONCACHE_DASHBOARD_PORT=9000 missioncache-dashboard
```

Open `http://localhost:8787` in your browser.

## Install as a service

`missioncache-dashboard install-service` registers the dashboard as a launchd
(macOS) or systemd user unit (Linux) so it starts on login. See the
[MissionCache project](https://github.com/missioncache/missioncache) for the full
install guide.

## License

MIT

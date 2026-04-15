#!/usr/bin/env python3
"""Seed a sandboxed orbit installation with realistic fake data for screenshots.

Usage (always via HOME override - this is the safety mechanism):

    HOME=/tmp/orbit-demo python3.11 orbit-dashboard/scripts/seed_demo_data.py

Then run the dashboard against the same HOME on an alternate port:

    HOME=/tmp/orbit-demo ORBIT_DASHBOARD_PORT=8789 \\
        python3.11 orbit-dashboard/server.py

    open http://localhost:8789

The seeder refuses to run if HOME is your real user home (safety check against
pwd.getpwuid). It creates:

    $HOME/.claude/tasks.db                  - SQLite with fixture data
    $HOME/.claude/tasks.duckdb              - DuckDB (migrated from SQLite)
    $HOME/.claude/orbit/active/<name>/      - plan/context/tasks files
    $HOME/.claude/orbit/completed/<name>/   - completed project files
    $HOME/projects/{api,docs,pipelines,data}/ - empty repo dirs referenced by tasks
"""
from __future__ import annotations

import json
import os
import pwd
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path


# =============================================================================
# Safety check - refuse to run against the user's real home directory
# =============================================================================

def safety_check() -> Path:
    """Verify HOME is sandboxed and return the resolved demo home path."""
    demo_home = Path(os.environ["HOME"]).expanduser().resolve()
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir).expanduser().resolve()

    if demo_home == real_home:
        sys.exit(
            "REFUSING to seed: $HOME resolves to your real user home "
            f"({real_home}).\nRun with an isolated HOME instead:\n"
            "    HOME=/tmp/orbit-demo python3.11 "
            "orbit-dashboard/scripts/seed_demo_data.py"
        )

    existing_db = demo_home / ".claude" / "tasks.db"
    if existing_db.exists() and existing_db.stat().st_size > 1024:
        sys.exit(
            f"REFUSING to seed: {existing_db} already exists and is >1KB.\n"
            "Delete it first if you want a fresh seed:\n"
            f"    rm -rf {demo_home}/.claude {demo_home}/projects"
        )

    return demo_home


# =============================================================================
# Fixture data - projects, repos, activity patterns
# =============================================================================

REPO_SHORTNAMES = ["api", "docs", "pipelines", "data"]

# Anchor everything to "now" so each run produces fresh relative timestamps
NOW = datetime.now().replace(microsecond=0)


def iso(dt: datetime) -> str:
    """Format a datetime as the orbit_db canonical local-time ISO string."""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# Project descriptors: each dict drives a row in tasks + heartbeats + sessions
# + orbit files. Time patterns are rendered at seed time relative to NOW.
PROJECTS = [
    {
        "name": "api-gateway-rewrite",
        "repo": "api",
        "status": "active",
        "jira_key": "ENG-4821",
        "priority": 2,
        "branch": "feat/api-gateway-rewrite",
        "description": "Replace Express-based API gateway with Fastify to cut p99 latency by 30-40%",
        "remaining": "Write path migration, staging rollout, then HTTP/2 follow-up",
        "started_days_ago": 8,
        "last_active_hours_ago": 2,
        "pattern": "heavy",
        "tasks_total": 12,
        "tasks_done": 7,
        # Dependency graph as {task_num: [deps]}. Tasks not listed have no deps.
        "prompt_deps": {
            2: [1], 3: [2], 4: [2], 5: [3, 4], 6: [5],
            7: [6], 8: [7], 9: [8], 10: [9], 11: [10], 12: [11],
        },
    },
    {
        "name": "auth-refactor",
        "repo": "api",
        "status": "active",
        "jira_key": "ENG-4903",
        "priority": 1,
        "branch": "feat/auth-refactor",
        "description": "Split monolithic auth module into JWT issuance and session services",
        "remaining": "Extract session module, update 3 legacy routes, drop shim",
        "started_days_ago": 1,
        "last_active_hours_ago": 1,
        "pattern": "fresh",
        "tasks_total": 8,
        "tasks_done": 3,
        "prompt_deps": {
            2: [1], 3: [1], 4: [2, 3], 5: [4],
            6: [4], 7: [5, 6], 8: [7],
        },
    },
    {
        "name": "docs-site-migration",
        "repo": "docs",
        "status": "active",
        "jira_key": None,
        "priority": None,
        "branch": "migration/to-vitepress",
        "description": "Migrate public docs site from Docusaurus to VitePress",
        "remaining": "Port remaining 11 MDX pages, wire Algolia, test URL redirects",
        "started_days_ago": 5,
        "last_active_hours_ago": 8,
        "pattern": "moderate",
        "tasks_total": 6,
        "tasks_done": 4,
        "prompt_deps": {
            2: [1], 3: [1], 4: [2, 3], 5: [4], 6: [5],
        },
    },
    {
        "name": "kafka-consumer-fix",
        "repo": "pipelines",
        "status": "active",
        "jira_key": "INFRA-118",
        "priority": 3,
        "branch": "fix/consumer-rebalance",
        "description": "Fix consumer rebalance storm during broker restart",
        "remaining": "Finish static-membership rollout + 3-broker-restart validation",
        "started_days_ago": 2,
        "last_active_hours_ago": 30,
        "pattern": "scattered",
        "tasks_total": 4,
        "tasks_done": 1,
        "has_auto_run": True,
        "prompt_deps": {
            2: [1], 3: [1], 4: [2, 3],
        },
    },
    {
        "name": "circuit-breaker-tuning",
        "repo": "api",
        "status": "completed",
        "jira_key": None,
        "priority": None,
        "branch": "fix/circuit-breaker",
        "description": "Tune circuit breaker thresholds for upstream timeouts",
        "summary": "Shipped. p99 downstream errors dropped from 2.1% to 0.4%; per-upstream breaker adopted as default",
        "started_days_ago": 28,
        "completed_days_ago": 25,
        "pattern": "completed-short",
        "tasks_total": 6,
        "tasks_done": 6,
    },
    {
        "name": "ml-feature-store-poc",
        "repo": "data",
        "status": "completed",
        "jira_key": "DATA-77",
        "priority": None,
        "branch": "poc/feature-store",
        "description": "Evaluate Feast vs Tecton for offline/online feature parity",
        "summary": "Recommended Feast. Lower integration cost, serving latency within budget (15ms p99)",
        "started_days_ago": 14,
        "completed_days_ago": 7,
        "pattern": "completed-long",
        "tasks_total": 8,
        "tasks_done": 8,
    },
]


# Activity patterns → list of (day_offset, start_hour, duration_minutes)
# Rendered into heartbeats + sessions relative to NOW - day_offset days.
PATTERNS = {
    "heavy": [
        # Day -8: started the project, 2 sessions
        (-8, 14, 85),
        (-8, 16, 45),
        # Day -7: deep work
        (-7, 9, 95),
        (-7, 11, 60),
        (-7, 14, 110),
        # Day -6: less active
        (-6, 15, 40),
        # Day -3: review + refactor
        (-3, 10, 75),
        (-3, 13, 50),
        # Day -1: yesterday big push
        (-1, 9, 90),
        (-1, 13, 120),
        (-1, 16, 55),
        # Today: 2h finishing up
        (0, 10, 70),
        (0, 13, 45),
    ],
    "fresh": [
        # Day -1: started late yesterday
        (-1, 17, 35),
        # Today: kicked into gear
        (0, 9, 40),
        (0, 11, 50),
        (0, 14, 28),
    ],
    "moderate": [
        (-5, 10, 65),
        (-5, 14, 45),
        (-4, 11, 55),
        (-2, 10, 50),
        (-2, 15, 32),
    ],
    "scattered": [
        (-2, 15, 18),
        (-2, 16, 12),
        (-1, 10, 22),
        (-1, 14, 15),
    ],
    "completed-short": [
        (-28, 10, 60),
        (-27, 11, 55),
        (-26, 10, 50),
        (-25, 14, 40),
    ],
    "completed-long": [
        (-14, 10, 75),
        (-13, 11, 90),
        (-12, 14, 80),
        (-11, 10, 65),
        (-10, 11, 85),
        (-9, 14, 95),
        (-8, 10, 70),
        (-7, 11, 40),
    ],
}


# Realistic orbit file content keyed by project name
# Plan files - short, concrete, engineering-voiced
PLANS: dict[str, str] = {
    "api-gateway-rewrite": """# API Gateway Rewrite - Plan

## Goal
Replace the Express-based API gateway with a Fastify-based implementation
to cut p99 latency by 30-40% and unlock HTTP/2 support for the mobile clients.

## Approach
1. Stand up Fastify alongside Express on an internal staging subdomain
2. Migrate the read path (GET /api/*) first, measure p99 before cutting over
3. Migrate the write path (POST/PUT/DELETE) once reads are stable
4. Tear down Express once all traffic is on Fastify
5. Add HTTP/2 support as a follow-up pass

## Success Criteria
- p99 latency on read path drops below 60ms (currently ~95ms)
- Zero regressions on the existing integration test suite
- Health check endpoint response time within 5ms
""",
    "auth-refactor": """# Auth Refactor - Plan

## Goal
Split the monolithic `auth.py` module into two services: JWT issuance
and session management. The current module conflates stateless token logic
with stateful session tracking, making both harder to reason about.

## Approach
1. Extract JWT signing/verification into `auth/jwt.py` (stateless, pure functions)
2. Extract session lifecycle into `auth/sessions.py` (DB-backed, stateful)
3. Leave `auth/__init__.py` as a thin compatibility shim during migration
4. Migrate call sites one route group at a time
5. Drop the shim once all routes point at the new modules

## Success Criteria
- No behavior change visible to clients
- Unit test coverage for JWT logic above 95%
- Session module has independent integration tests
""",
    "docs-site-migration": """# Docs Site Migration - Plan

## Goal
Migrate the public docs site from Docusaurus v2 to VitePress. The current
Docusaurus build takes 4+ minutes and the React-heavy runtime is overkill for
what is essentially static markdown with some code samples.

## Approach
1. Audit existing Docusaurus content - pages, components, custom plugins
2. Set up VitePress scaffold with theme matching the current look
3. Port pages one section at a time, verify each batch in local preview
4. Rewrite the 3 custom React components as VitePress Vue components
5. Wire up the existing search (Algolia) to VitePress
6. Switch DNS once feature parity is confirmed

## Success Criteria
- Build time under 30 seconds (vs 4+ minutes)
- All existing URLs preserved (301 redirects for any that have to change)
- Search works identically
""",
    "kafka-consumer-fix": """# Kafka Consumer Fix - Plan

## Goal
Fix the consumer rebalance storm that happens when a broker restarts.
Currently during broker restart, consumers enter an endless rebalance loop
and stop making progress for 5-10 minutes.

## Approach
1. Reproduce in staging - restart a broker, observe the rebalance loop
2. Identify the root cause - heartbeat misconfig, static membership, or
   consumer group state
3. Apply the targeted fix (likely: static group membership + longer
   session timeout)
4. Verify in staging with 3 consecutive broker restarts

## Success Criteria
- Broker restart causes at most one rebalance event per consumer group
- Consumer lag stays within 2x normal during and after restart
- Zero message loss across the restart window
""",
    "circuit-breaker-tuning": """# Circuit Breaker Tuning - Plan

## Goal
Tune the circuit breaker thresholds for the upstream-timeout case.
Current thresholds are too aggressive - a single slow upstream trips
the breaker for the entire pool, which causes cascading fallbacks.

## Approach
1. Measure baseline timeout rate per upstream
2. Move from pool-wide breaker to per-upstream breaker
3. Raise the failure threshold from 3 to 8
4. Add half-open probe logic

## Outcome
Shipped. p99 downstream errors dropped from 2.1% to 0.4%.
Half-open probe logic is now the standard for new upstream integrations.
""",
    "ml-feature-store-poc": """# ML Feature Store POC - Plan

## Goal
Evaluate Feast and Tecton as candidate feature stores. The ML team needs
offline/online feature parity for the recommendation models, and the current
ad-hoc Parquet + Redis setup has drifted twice this quarter.

## Approach
1. Define 5 representative features (mix of real-time and batch)
2. Build the POC in both Feast and Tecton, against the same source data
3. Measure: offline training parity, online serving latency, integration
   cost, ops burden
4. Write up the comparison and recommend

## Outcome
Recommended Feast for phase 1. Tecton's serving SLOs were better but the
integration cost was too high for a team of our size. Feast ships with
enough serving latency headroom to hit our targets.
""",
}


# Context files - architectural decisions, key files, gotchas, next steps
CONTEXTS: dict[str, str] = {
    "api-gateway-rewrite": """# API Gateway Rewrite - Context

## Key Architectural Decisions
- Fastify chosen over Hapi because the plugin ecosystem is deeper and the
  schema-first request validation matches our existing JSON schema workflow
- HTTP/2 deferred to a follow-up pass - the rewrite scope is big enough
  without adding protocol changes
- Read path migrated first because the traffic is 10x the write path and
  gives us a real signal on p99 improvement before we commit fully

## Key Files
| File | Purpose |
|------|---------|
| `src/gateway/index.ts` | Main entry point |
| `src/gateway/routes/read.ts` | Fastify read-path routes |
| `src/gateway/routes/write.ts` | Still on Express (pending migration) |
| `tests/integration/p99.test.ts` | p99 regression gate |

## Gotchas
- Express middleware order matters - the auth middleware runs before rate
  limiting, but the naive Fastify port had it reversed
- The health check was accidentally returning 200 during cold start because
  the Fastify `ready` hook fires before the DB pool is warm. Fixed with
  an explicit DB ping in the handler.

## Next Steps
- Wire up the write path (POST /api/orders first)
- Remove the legacy Express route handlers
- Schedule the p99 measurement window
""",
    "auth-refactor": """# Auth Refactor - Context

## Key Architectural Decisions
- JWT module is pure functions only (no class, no state) so it can be
  imported anywhere including hot paths without DB connections
- Session module owns the `sessions` table exclusively - no other module
  writes to it directly
- Compatibility shim stays for one full release cycle minimum to avoid
  breaking any forgotten callers

## Key Files
| File | Purpose |
|------|---------|
| `src/auth/__init__.py` | Compatibility shim |
| `src/auth/jwt.py` | Stateless JWT signing/verification |
| `src/auth/sessions.py` | Session lifecycle (pending) |
| `tests/auth/` | Unit tests for each module |

## Gotchas
- The old `auth.verify_token()` had a hidden fallback to session lookup
  that three routes relied on. The refactor needs to preserve that OR
  update those call sites explicitly.

## Next Steps
- Finish extracting the session code into `sessions.py`
- Update the 3 routes that relied on the hidden fallback
- Run the full integration suite before dropping the shim
""",
    "docs-site-migration": """# Docs Site Migration - Context

## Key Architectural Decisions
- VitePress over Astro - VitePress is tightly focused on docs and has
  first-class markdown extensions for code groups and callouts. Astro
  is more general but we do not need the flexibility.
- Keep the existing Algolia index - rebuilding it would delay launch

## Key Files
| File | Purpose |
|------|---------|
| `docs/.vitepress/config.ts` | Site config |
| `docs/.vitepress/theme/` | Custom theme overrides |
| `docs/public/_redirects` | URL migration map |

## Gotchas
- Docusaurus used `.mdx` for all pages but only ~15% actually used MDX
  features. VitePress uses `.md` with limited Vue support - mostly
  a drop-in port but the MDX pages need rewriting.

## Next Steps
- Port the remaining 11 MDX pages
- Wire up search
- Test the redirects
""",
    "kafka-consumer-fix": """# Kafka Consumer Fix - Context

## Key Architectural Decisions
- Switching to static group membership instead of dynamic - the rebalance
  storms are caused by the broker restart triggering a reassignment that
  cascades through the whole group
- Session timeout raised from 10s to 45s - gives the broker time to come
  back before the group decides a member is gone

## Key Files
| File | Purpose |
|------|---------|
| `pipelines/consumer/config.py` | Consumer group config |
| `pipelines/consumer/main.py` | Main consumer loop |

## Gotchas
- Static group membership requires unique `group.instance.id` per consumer.
  Our k8s StatefulSet already provides pod names that work, but the
  Deployment-based consumers need a new approach.

## Next Steps
- Finish the static-membership rollout for StatefulSet consumers
- Design the Deployment-based consumer path
- Run the 3-broker-restart validation
""",
    "circuit-breaker-tuning": """# Circuit Breaker Tuning - Context

## Outcome
Shipped and stable for 3 weeks. p99 downstream errors dropped from 2.1%
to 0.4%. Per-upstream breaker is now documented as the default pattern
for new integrations.

## Key Files
| File | Purpose |
|------|---------|
| `src/http/breaker.ts` | Per-upstream circuit breaker |
| `src/http/probe.ts` | Half-open probe logic |

## Gotchas (for the next person to touch this)
- The probe interval is hardcoded at 5 seconds. Too fast and you thrash
  the upstream; too slow and recovery takes forever. 5s is empirically
  correct for our upstream set but may not generalize.
""",
    "ml-feature-store-poc": """# ML Feature Store POC - Context

## Outcome
Recommended Feast. Detailed writeup in the linked Confluence doc.

## Decision Matrix
| Criterion | Feast | Tecton |
|-----------|-------|--------|
| Offline/online parity | Good | Excellent |
| Serving latency p99 | 15ms | 8ms |
| Integration cost | Low | High |
| Ops burden | Low | Medium |
| Community | Open source | Commercial |

## Recommendation
Feast for phase 1. Revisit Tecton if serving latency becomes a bottleneck.
""",
}


# Tasks files - hierarchical checklist
def make_tasks_file(project: dict) -> str:
    """Generate a tasks.md from a PROJECTS entry.

    Emits a `**Remaining:**` metadata line when the project has a `remaining`
    field; the dashboard parses it into the Active Projects progress column.
    """
    name = project["name"]
    total = project["tasks_total"]
    done = project["tasks_done"]
    tasks_by_project = {
        "api-gateway-rewrite": [
            "Audit Express gateway - routes, middleware, custom handlers",
            "Set up Fastify scaffold with matching plugin chain",
            "Port the read path routes (GET /api/*)",
            "Wire up schema-first request validation",
            "Add integration tests against the Fastify instance",
            "Measure p99 on read path",
            "Port the write path routes",
            "Remove legacy Express handlers",
            "Run full integration suite on unified Fastify",
            "Deploy to staging with 10% traffic shadow",
            "Promote to 100% traffic",
            "Schedule HTTP/2 follow-up pass",
        ],
        "auth-refactor": [
            "Extract JWT signing into `auth/jwt.py`",
            "Extract JWT verification into `auth/jwt.py`",
            "Write unit tests for JWT module",
            "Extract session lifecycle into `auth/sessions.py`",
            "Write session integration tests",
            "Update 3 routes that used the hidden fallback",
            "Drop the compatibility shim",
            "Document the new module boundary",
        ],
        "docs-site-migration": [
            "Set up VitePress scaffold",
            "Port the quickstart + install pages",
            "Port the API reference pages (auto-generated)",
            "Port the 11 MDX pages to .md",
            "Wire up Algolia search",
            "Test URL redirects",
        ],
        "kafka-consumer-fix": [
            "Reproduce the rebalance storm in staging",
            "Switch StatefulSet consumers to static group membership",
            "Design the Deployment consumer path",
            "Run 3-broker-restart validation",
        ],
        "circuit-breaker-tuning": [
            "Measure baseline per-upstream timeout rate",
            "Implement per-upstream breaker",
            "Add half-open probe logic",
            "Raise failure threshold from 3 to 8",
            "Staging rollout with canary",
            "Production rollout",
        ],
        "ml-feature-store-poc": [
            "Define 5 representative features",
            "Build POC in Feast",
            "Build POC in Tecton",
            "Measure offline/online parity",
            "Measure serving latency p99",
            "Measure integration cost",
            "Write up comparison",
            "Present recommendation to ML team",
        ],
    }
    items = tasks_by_project[name]
    assert len(items) == total, f"{name}: task list length {len(items)} != {total}"
    lines = [f"# {name.replace('-', ' ').title()} - Tasks", ""]
    if project.get("remaining"):
        lines.append(f"**Remaining:** {project['remaining']}")
        lines.append("")
    if project.get("summary"):
        lines.append(f"**Summary:** {project['summary']}")
        lines.append("")
    for i, item in enumerate(items, start=1):
        mark = "x" if i <= done else " "
        lines.append(f"- [{mark}] {i}. {item}")
    lines.append("")
    return "\n".join(lines)


# =============================================================================
# Seeders
# =============================================================================

def init_schema(conn: sqlite3.Connection) -> None:
    """Create schema via orbit_db's SCHEMA_SQL plus the claude_session_cache table.

    We import orbit_db so the schema stays in sync with the canonical source.
    """
    # Import here so the script can fail fast if orbit_db is missing.
    from orbit_db import SCHEMA_SQL  # type: ignore[import-not-found]

    conn.executescript(SCHEMA_SQL)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS claude_session_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT UNIQUE NOT NULL,
            file_path TEXT NOT NULL,
            date TEXT NOT NULL,
            hour INTEGER NOT NULL,
            cwd TEXT,
            git_branch TEXT,
            project_path TEXT,
            message_count INTEGER DEFAULT 0,
            tool_call_count INTEGER DEFAULT 0,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            duration_seconds INTEGER DEFAULT 0,
            first_event_time TEXT,
            last_event_time TEXT,
            file_mtime REAL NOT NULL,
            cached_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_claude_session_date ON claude_session_cache(date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_claude_session_hour ON claude_session_cache(date, hour)")
    conn.commit()


def seed_repositories(conn: sqlite3.Connection, demo_home: Path) -> dict[str, int]:
    """Insert the 4 demo repositories and return {short_name: repo_id}."""
    repo_ids: dict[str, int] = {}
    for short_name in REPO_SHORTNAMES:
        path = str(demo_home / "projects" / short_name)
        cur = conn.execute(
            "INSERT INTO repositories (path, short_name, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (path, short_name, iso(NOW - timedelta(days=30)), iso(NOW)),
        )
        repo_ids[short_name] = cur.lastrowid  # type: ignore[assignment]
        # Create the directory so any path-resolving code does not choke
        (demo_home / "projects" / short_name).mkdir(parents=True, exist_ok=True)
    conn.commit()
    return repo_ids


def seed_tasks(
    conn: sqlite3.Connection, repo_ids: dict[str, int]
) -> dict[str, int]:
    """Insert project tasks. Return {name: task_id}."""
    task_ids: dict[str, int] = {}
    for p in PROJECTS:
        status = p["status"]
        full_path = f"{status}/{p['name']}"
        created_at = iso(NOW - timedelta(days=p["started_days_ago"]))
        if status == "active":
            last_worked_on = iso(NOW - timedelta(hours=p["last_active_hours_ago"]))
            completed_at = None
        else:
            last_worked_on = iso(NOW - timedelta(days=p["completed_days_ago"]))
            completed_at = last_worked_on
        cur = conn.execute(
            """INSERT INTO tasks (repo_id, name, full_path, status, type, tags,
                                   priority, jira_key, branch, created_at, updated_at,
                                   completed_at, last_worked_on)
               VALUES (?, ?, ?, ?, 'coding', '[]', ?, ?, ?, ?, ?, ?, ?)""",
            (
                repo_ids[p["repo"]],
                p["name"],
                full_path,
                status,
                p["priority"],
                p["jira_key"],
                p["branch"],
                created_at,
                iso(NOW),
                completed_at,
                last_worked_on,
            ),
        )
        task_ids[p["name"]] = cur.lastrowid  # type: ignore[assignment]
    conn.commit()
    return task_ids


def seed_heartbeats_and_sessions(
    conn: sqlite3.Connection, task_ids: dict[str, int]
) -> None:
    """For each project's pattern, insert heartbeats + aggregated sessions."""
    for p in PROJECTS:
        pattern = PATTERNS[p["pattern"]]
        task_id = task_ids[p["name"]]
        for day_offset, start_hour, duration_min in pattern:
            day = NOW + timedelta(days=day_offset)
            start = day.replace(
                hour=start_hour, minute=0, second=0, microsecond=0
            )
            end = start + timedelta(minutes=duration_min)
            # Heartbeat cadence: roughly one every 2 minutes during the session
            heartbeat_count = max(1, duration_min // 2)
            session_id = f"demo-{p['name']}-{day.date()}-{start_hour}"
            # Insert the aggregated session (what the dashboard primarily reads)
            conn.execute(
                """INSERT INTO sessions (task_id, session_id, start_time, end_time,
                                          duration_seconds, heartbeat_count)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    task_id,
                    session_id,
                    iso(start),
                    iso(end),
                    duration_min * 60,
                    heartbeat_count,
                ),
            )
            # Insert heartbeats spread evenly across the session so the raw
            # data is consistent with the aggregated session
            for i in range(heartbeat_count):
                ts = start + timedelta(
                    seconds=int(i * duration_min * 60 / heartbeat_count)
                )
                conn.execute(
                    """INSERT INTO heartbeats (task_id, timestamp, session_id, processed)
                       VALUES (?, ?, ?, 1)""",
                    (task_id, iso(ts), session_id),
                )
    conn.commit()


CLAUDE_SESSIONS = [
    # Tracked sessions: cwd matches a repo AND session_id is linked to an
    # orbit session, mirroring how production hooks share the Claude Code
    # session UUID between orbit heartbeats and JSONL transcripts.
    {"session_id": "demo-claude-001", "repo": "api", "task": "api-gateway-rewrite",
     "day_offset": 0, "hour": 10, "duration": 2700, "messages": 21, "tools": 18},
    {"session_id": "demo-claude-002", "repo": "api", "task": "auth-refactor",
     "day_offset": -1, "hour": 13, "duration": 4500, "messages": 34, "tools": 31},
    {"session_id": "demo-claude-003", "repo": "docs", "task": "docs-site-migration",
     "day_offset": -2, "hour": 11, "duration": 2100, "messages": 14, "tools": 12},
    # Untracked sessions: cwd has no matching repo and no linked orbit session.
    {"session_id": "demo-claude-untracked-001", "untracked_cwd": "scratch/experiments",
     "day_offset": -1, "hour": 20, "duration": 1800, "messages": 11, "tools": 6},
    {"session_id": "demo-claude-untracked-002", "untracked_cwd": "Downloads",
     "day_offset": 0, "hour": 8, "duration": 900, "messages": 5, "tools": 3},
]


def _write_session_jsonl(
    jsonl_path: Path,
    cwd: str,
    start_local: datetime,
    duration_seconds: int,
    message_pairs: int,
    tool_count: int,
) -> None:
    """Write a minimal JSONL transcript the dashboard parser can ingest.

    The parser counts user/assistant messages, sums input+output tokens from
    `message.usage`, counts `tool_use` entries in `message.content`, and uses
    the spread of timestamps to compute active duration. Events are spaced
    so consecutive gaps stay under the parser's 5-minute idle threshold.
    """
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)

    # astimezone() on a naive datetime tags it with the local tz; the
    # round-trip through parse_timestamp's own astimezone() is then a no-op.
    start = start_local.astimezone()
    interval = duration_seconds / (message_pairs * 2 - 1)

    lines: list[str] = []
    for i in range(message_pairs):
        user_ts = start + timedelta(seconds=int(i * 2 * interval))
        lines.append(json.dumps({
            "type": "user",
            "uuid": f"{jsonl_path.stem}-u-{i:04d}",
            "timestamp": user_ts.isoformat(),
            "cwd": cwd,
            "gitBranch": "main",
        }))
        asst_ts = start + timedelta(seconds=int((i * 2 + 1) * interval))
        content = [
            {"type": "tool_use", "id": f"toolu_{j:03d}", "name": "Read"}
            for j in range(tool_count if i == 0 else 0)
        ]
        lines.append(json.dumps({
            "type": "assistant",
            "uuid": f"{jsonl_path.stem}-a-{i:04d}",
            "timestamp": asst_ts.isoformat(),
            "cwd": cwd,
            "message": {
                "usage": {"input_tokens": 1200, "output_tokens": 450},
                "content": content,
            },
        }))

    jsonl_path.write_text("\n".join(lines) + "\n")


def seed_claude_jsonl_files(
    conn: sqlite3.Connection, demo_home: Path, task_ids: dict[str, int]
) -> None:
    """Write JSONL transcripts and link tracked entries to orbit sessions.

    The dashboard's `refresh_claude_session_cache` will discover the JSONL
    files on first read of the activity API, parse them, and populate
    `claude_session_cache` itself. We do not pre-populate the cache table
    because direct inserts are orphaned: the refresh loop only iterates
    files found via `get_jsonl_files_for_date`.

    For tracked entries we additionally insert an orbit `sessions` row
    keyed by the same session_id so the dashboard's untracked-vs-tracked
    LEFT JOIN classifies them correctly.
    """
    repo_paths = {short: str(demo_home / "projects" / short) for short in REPO_SHORTNAMES}

    for e in CLAUDE_SESSIONS:
        if "repo" in e:
            cwd = repo_paths[e["repo"]]
        else:
            cwd = str(demo_home / e["untracked_cwd"])
            Path(cwd).mkdir(parents=True, exist_ok=True)

        day = NOW + timedelta(days=e["day_offset"])
        start_local = day.replace(hour=e["hour"], minute=0, second=0, microsecond=0)
        end_local = start_local + timedelta(seconds=e["duration"])

        # Production format: ~/.claude/projects/<cwd-with-slashes-as-dashes>/
        encoded_cwd = cwd.replace("/", "-")
        jsonl_path = demo_home / ".claude" / "projects" / encoded_cwd / f"{e['session_id']}.jsonl"

        _write_session_jsonl(
            jsonl_path,
            cwd=cwd,
            start_local=start_local,
            duration_seconds=e["duration"],
            message_pairs=e["messages"],
            tool_count=e["tools"],
        )

        # Link tracked Claude sessions to orbit sessions so the dashboard's
        # session_id LEFT JOIN classifies them as tracked, not untracked.
        if "task" in e:
            conn.execute(
                """INSERT INTO sessions (task_id, session_id, start_time, end_time,
                                          duration_seconds, heartbeat_count)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    task_ids[e["task"]],
                    e["session_id"],
                    iso(start_local),
                    iso(end_local),
                    e["duration"],
                    max(1, e["duration"] // 120),
                ),
            )
    conn.commit()


def seed_auto_execution(
    conn: sqlite3.Connection, task_ids: dict[str, int]
) -> None:
    """Insert one completed orbit-auto run on kafka-consumer-fix with a
    realistic iteration log stream so the DAG visualization lights up."""
    task_id = task_ids["kafka-consumer-fix"]
    started = NOW - timedelta(hours=30)
    completed = started + timedelta(minutes=42)
    cur = conn.execute(
        """INSERT INTO auto_executions (task_id, started_at, completed_at,
                status, mode, worker_count, total_subtasks,
                completed_subtasks, failed_subtasks)
           VALUES (?, ?, ?, 'completed', 'parallel', 2, 4, 4, 0)""",
        (task_id, iso(started), iso(completed)),
    )
    exec_id = cur.lastrowid

    # Log lines for the DAG execution
    log_entries = [
        (0, None, None, "info",    "Starting orbit-auto execution on kafka-consumer-fix"),
        (2, None, None, "info",    "Parsed 4 subtasks from tasks.md"),
        (3, None, None, "info",    "Dependency graph: 1 -> 2, 1 -> 3, {2,3} -> 4"),
        (5, None, None, "info",    "Dispatching 2 workers"),
        (8, 1, "2",  "info",    "Worker 1 picked up subtask 2 (Switch to static membership)"),
        (9, 2, "3",  "info",    "Worker 2 picked up subtask 3 (Design Deployment path)"),
        (180, 1, "2", "success", "Subtask 2 complete (3m 0s, 8 tool calls)"),
        (420, 2, "3", "success", "Subtask 3 complete (6m 51s, 14 tool calls)"),
        (425, 1, "4", "info",    "Worker 1 picked up subtask 4 (3-broker-restart validation)"),
        (2400, 1, "4", "success", "Subtask 4 complete (33m 0s, 19 tool calls)"),
        (2405, None, None, "info",    "All subtasks complete"),
        (2520, None, None, "success", "Execution finished in 42m 00s"),
    ]
    for offset_sec, worker, subtask, level, msg in log_entries:
        ts = started + timedelta(seconds=offset_sec)
        conn.execute(
            """INSERT INTO auto_execution_logs (execution_id, timestamp, worker_id,
                    subtask_id, level, message)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (exec_id, iso(ts), worker, subtask, level, msg),
        )
    conn.commit()


# =============================================================================
# Orbit file writers
# =============================================================================

def write_orbit_files(demo_home: Path) -> None:
    """Create plan.md, context.md, tasks.md, and prompt files under
    $HOME/.claude/orbit/<status>/<name>/.
    """
    for p in PROJECTS:
        status_dir = "active" if p["status"] == "active" else "completed"
        task_dir = demo_home / ".claude" / "orbit" / status_dir / p["name"]
        task_dir.mkdir(parents=True, exist_ok=True)

        (task_dir / f"{p['name']}-plan.md").write_text(PLANS[p["name"]])

        # Inject a `## Description` section after the H1 title so the dashboard's
        # parse_orbit_progress can populate the Active Projects description column.
        ctx_lines = CONTEXTS[p["name"]].splitlines()
        ctx = "\n".join(
            ctx_lines[:2] + ["## Description", "", p["description"], ""] + ctx_lines[2:]
        )
        (task_dir / f"{p['name']}-context.md").write_text(ctx)

        (task_dir / f"{p['name']}-tasks.md").write_text(make_tasks_file(p))

        # Prompt files with YAML frontmatter drive the auto DAG edges.
        # Without them the D3 force simulation has no forceLink and nodes scatter.
        deps_map = p.get("prompt_deps") or {}
        if deps_map:
            prompts_dir = task_dir / "prompts"
            prompts_dir.mkdir(parents=True, exist_ok=True)
            for task_num in range(1, p["tasks_total"] + 1):
                deps = deps_map.get(task_num, [])
                (prompts_dir / f"task-{task_num:02d}-prompt.md").write_text(
                    f"---\ndepends_on: {json.dumps(deps)}\n---\n"
                )


# =============================================================================
# Main
# =============================================================================

def main() -> None:
    demo_home = safety_check()
    print(f"Seeding demo orbit installation at HOME={demo_home}")

    claude_dir = demo_home / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)

    db_path = claude_dir / "tasks.db"
    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(str(db_path))
    try:
        init_schema(conn)
        print("  schema initialized")

        repo_ids = seed_repositories(conn, demo_home)
        print(f"  seeded {len(repo_ids)} repositories")

        task_ids = seed_tasks(conn, repo_ids)
        print(f"  seeded {len(task_ids)} tasks ({sum(1 for p in PROJECTS if p['status']=='active')} active, {sum(1 for p in PROJECTS if p['status']=='completed')} completed)")

        seed_heartbeats_and_sessions(conn, task_ids)
        hb_count = conn.execute("SELECT count(*) FROM heartbeats").fetchone()[0]
        sess_count = conn.execute("SELECT count(*) FROM sessions").fetchone()[0]
        print(f"  seeded {hb_count} heartbeats across {sess_count} sessions")

        seed_auto_execution(conn, task_ids)
        log_count = conn.execute("SELECT count(*) FROM auto_execution_logs").fetchone()[0]
        print(f"  seeded 1 orbit-auto execution with {log_count} log lines")

        seed_claude_jsonl_files(conn, demo_home, task_ids)
        tracked = sum(1 for e in CLAUDE_SESSIONS if "task" in e)
        untracked = len(CLAUDE_SESSIONS) - tracked
        print(f"  wrote {len(CLAUDE_SESSIONS)} Claude Code JSONL transcripts ({tracked} tracked, {untracked} untracked)")
    finally:
        conn.close()

    write_orbit_files(demo_home)
    print("  wrote orbit plan/context/tasks files for 6 projects")

    # Sync SQLite -> DuckDB so the dashboard has the analytics layer ready
    # on startup without a separate migration step. Use the same code path the
    # dashboard's lifespan startup uses (AnalyticsDB.sync_from_sqlite), which
    # lazily creates the DuckDB file + schema on first connect.
    print("  syncing SQLite -> DuckDB...")
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from lib.analytics_db import AnalyticsDB  # type: ignore[import-not-found]
    sync_result = AnalyticsDB().sync_from_sqlite()
    print(f"  sync result: {sync_result}")

    print()
    print("Demo data seeded successfully.")
    print()
    print("Next step - run the dashboard on port 8789 so it does not collide")
    print("with your real dashboard on 8787:")
    print()
    print(f"    HOME={demo_home} ORBIT_DASHBOARD_PORT=8789 \\")
    print("        python3.11 orbit-dashboard/server.py")
    print()
    print("Then open http://localhost:8789 and take screenshots.")
    print()
    print(f"To reset: rm -rf {demo_home}")


if __name__ == "__main__":
    main()

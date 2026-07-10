<!-- Generated 2026-06-28 by an ultracode multi-agent workflow (12 agents, 4 phases: understand/design/verify/synthesize), verified against live source. Run wf_ebb7e58f-e11. -->

# MissionCache Cross-Machine Project Sharing — Final Build Plan

Single executable spec. All `file:line` refer to `missioncache-db/missioncache_db/__init__.py` unless another file is named. Verified against the live source; adversarial verdicts folded in (DuckDB layering, wikilink over-capture, runtime repo classification, files[] enumeration, created_at, IntegrityError guard, MISSIONCACHE_ROOT override).

---

## 1. Goal, scope, non-goals

**Goal.** Let one user move or share a MissionCache project between two POSIX machines (macOS + Windows 11 WSL) without leaking machine-specific absolute paths or local integer ids. A project = its markdown tree under `~/.missioncache/active/<name>/` plus the logical `tasks` row. Export produces a portable bundle; import lands it on the other machine, reconciling every machine-specific reference through a per-machine path map and reporting what resolved, what needs mapping, and what is missing.

**In scope (v1).**
- `missioncache-db export <name>` → portable bundle (markdown + `missioncache.json` manifest).
- `~/.missioncache/machine.json` — per-machine, never-synced path map (repos by git remote, vaults by logical name, anchors like HOME).
- `missioncache-db import <bundle>` → places files, reconciles refs, name-keyed task upsert, DuckDB rebuild trigger, 3-bucket alignment report.
- `missioncache-db config set-path / list-paths` (+ `show`, `seed`) to seed/edit the map.

**Non-goals / v1 limitations (state in `--help` and docs).**
- **Time history never merges.** `heartbeats` / `sessions` / `auto_executions` FK to local `tasks.id` (`:494`, `:504`) and are excluded from the bundle. The manifest carries `time_total_seconds` as **display-only** origin metadata; the target reads 0 local time until new work accrues. Import never fabricates session rows.
- **No native-Windows (backslash) target.** Both ends are POSIX; only the HOME prefix and repo/vault roots differ. The `${HOME}`/anchor rewrite assumes `/`-rooted absolute paths.
- **No continuous sync command in v1.** The optional git-tracked-folder reconcile loop is a shell wrapper over `export --out <folder>` + `import <folder>` (see §10 Phase 4). The CLI must not preclude it; it ships later.
- **Embedded paths are report-only by default.** Import does not rewrite user markdown unless `--rewrite-paths` is passed (a documented opt-in, Phase 3).
- `~/.claude/hooks-state.db` (statusline/session pointers) is machine-local and untouched.

---

## 2. Architecture overview

```
  MACHINE A (macOS)                         MACHINE B (WSL)
  ┌──────────────────┐                      ┌──────────────────┐
  │ tasks.db (SQLite)│                      │ tasks.db (SQLite)│   local, never synced
  │ active/<name>/ md│                      │ active/<name>/ md│
  └────────┬─────────┘                      └────────▲─────────┘
           │ export <name>                           │ import <bundle>
           ▼                                          │
   <name>.missioncache-bundle/                        │
     missioncache.json   (logical metadata,           │
     files/<name>/...    portable identifiers)  ──────┘
                                                resolve through
   machine.json (A)  ── never travels            machine.json (B)  ── never travels
                                                       │
                                                       ▼
                                          alignment report: resolved / needs-mapping / missing
                                                       │
                                          best-effort POST localhost:8787/api/sync
                                                       ▼
                                            tasks.duckdb (B) rebuilt, not synced
```

Invariants:
- **DB stays local, files travel.** SQLite `tasks.db` is never copied. Only the markdown tree + manifest move.
- **DuckDB is derived, rebuilt not synced.** `AnalyticsDB.sync_from_sqlite()` (`missioncache-dashboard/.../lib/analytics_db.py:669`) re-reads `SELECT * FROM tasks` and upserts `ON CONFLICT(id)`. **It lives in `missioncache-dashboard`, which depends on `missioncache-db` — calling it directly inverts the layering.** Import triggers a rebuild only via best-effort `POST http://localhost:8787/api/sync` (real route: `server.py:2771`, POST), short timeout, swallow failure, add a "DuckDB refreshes on next dashboard start" note. Never blocks import.
- **Reconcile by name + canonical git remote, never by id.** `tasks.id`, `repo_id`, `parent_id` are local autoincrement; `repositories.path` is absolute + machine-specific (`UNIQUE`). Nothing portable keys on them.
- **`full_path` is already portable.** Stored relative to `MISSIONCACHE_ROOT` (`relative_path = str(task_dir.relative_to(MISSIONCACHE_ROOT))` at `:1058`; root at `:68`). Landing dir on the target = `MISSIONCACHE_ROOT / full_path`, no rewrite.

---

## 3. Manifest format — `missioncache.json`

Carries the logical task row plus every reference as a **portable identifier**. Authoritative for DB fields — import never re-parses markdown for them (`_parse_task_metadata` at `:1119` reads only context/README, misses plan.md frontmatter, and its JIRA regex `\[([A-Z]+-\d+)\]` at `:1131` needs the bracket form).

**Note on overlap (verdict fix):** `jira_key` / `branch` / `pr_url` also appear inside the markdown. This is *intentional duplication with the DB as authoritative*; the "no double source of truth" property holds **because import treats the markdown copies as inert and never re-parses them**, not because they are absent. Document this in the import module.

### Schema

```jsonc
{
  "manifest_version": 1,                 // int; mismatch -> import refuses, no guess
  "kind": "missioncache-project-bundle",
  "generator": "missioncache-db/<pkg-version>",
  "exported_at": "2026-06-28T14:03:11+03:00",
  "exported_from": {                     // INFORMATIONAL; nothing resolves off this
    "host": "tbramis-mbp",
    "home": "/Users/tbrami",             // the prefix the tokenizer stripped on export (§6)
    "platform": "darwin"
  },
  "project": {
    "name": "finance-dashboard",         // tasks.name; dir + filename token; reconcile key
    "status": "active",                  // enum active|paused|completed|archived (CHECK :466-467)
    "type": "coding",                    // enum coding|non-coding (CHECK :468-469)
    "tags": ["dashboard", "finance"],    // tasks.tags (JSON-TEXT col :470); manifest is faithful source
    "priority": 2,                       // nullable int
    "jira_key": "GC-1234",               // nullable; pass-through (inert in markdown)
    "branch": "feature/finance-dash",    // nullable; pass-through
    "pr_url": "https://github.com/AkamaiETP/finance-dashboard/pull/42",
    "full_path": "active/finance-dashboard",  // portable; carry verbatim incl. global/ manual/ prefixes (:1224)
    "parent": null,                      // parent task NAME or null (never parent_id)
    "created_at": "2026-05-02T09:11:00", // carried; written on INSERT, MIN(existing,incoming) on UPDATE
    "time_total_seconds": 184320         // DISPLAY-ONLY origin metadata; not re-imported (§1)
  },
  "references": {
    "repo": { /* discriminated union, §6.1, or null */ },
    "vaults": [ /* §6.2 wikilink inventory, report-only */ ],
    "other_paths": [ /* §6.3 embedded-path inventory, report-only by default */ ]
  },
  "files": [ /* built from the ACTUAL recursive copy, §7; each {path, sha256} */ ]
}
```

### Full example

```json
{
  "manifest_version": 1,
  "kind": "missioncache-project-bundle",
  "generator": "missioncache-db/0.9.3",
  "exported_at": "2026-06-28T14:03:11+03:00",
  "exported_from": { "host": "tbramis-mbp", "home": "/Users/tbrami", "platform": "darwin" },
  "project": {
    "name": "finance-dashboard", "status": "active", "type": "coding",
    "tags": ["dashboard", "finance"], "priority": 2,
    "jira_key": "GC-1234", "branch": "feature/finance-dash",
    "pr_url": "https://github.com/AkamaiETP/finance-dashboard/pull/42",
    "full_path": "active/finance-dashboard", "parent": null,
    "created_at": "2026-05-02T09:11:00", "time_total_seconds": 184320
  },
  "references": {
    "repo": {
      "kind": "git",
      "remote": "github.com/akamaietp/finance-dashboard",
      "subpath": "", "worktree": false, "worktree_branch": null,
      "short_name": "finance-dashboard"
    },
    "vaults": [
      { "note": "finance-dashboard", "raw": "Hub: [[finance-dashboard]]", "source": "finance-dashboard-context.md:2", "confidence": "high" }
    ],
    "other_paths": [
      { "raw": "/Users/tbrami/.claude/plans/i-work.md", "token": "${HOME}/.claude/plans/i-work.md",
        "classification": "home-relative", "target_kind": "plan-file", "source": "finance-dashboard-context.md:7" },
      { "raw": "~/Documents/Obsidian/TomerWork/Resources/qa1-note-2026-05.md",
        "token": "${vault:TomerWork}/Resources/qa1-note-2026-05.md",
        "classification": "vault", "target_kind": "vault-note", "source": "finance-dashboard-context.md:30" }
    ]
  },
  "files": [
    { "path": "finance-dashboard/finance-dashboard-plan.md", "sha256": "3a7f…" },
    { "path": "finance-dashboard/finance-dashboard-context.md", "sha256": "91bc…" },
    { "path": "finance-dashboard/finance-dashboard-tasks.md", "sha256": "0d22…" },
    { "path": "finance-dashboard/research/02-data-pipeline-map.md", "sha256": "77aa…" },
    { "path": "finance-dashboard/prompts/task-01-prompt.md", "sha256": "ee10…" }
  ]
}
```

**Bundle layout** (directory; tar/zip is a transport wrapper):

```
<name>.missioncache-bundle/
  missioncache.json
  files/<name>/                # verbatim recursive copy of MISSIONCACHE_ROOT/<full_path>/
    <name>-plan.md  <name>-context.md  <name>-tasks.md   (or legacy bare plan/context/tasks.md)
    research/...                # arbitrary subdirs + loose .md are included
    prompts/task-NN-prompt.md
```

---

## 4. New CLI commands

All wire into the hand-rolled `main()` dispatch: `command = sys.argv[1]` (`:3182`), `db = TaskDB()` (`:3183`), one `try:` (`:3185`), flat `elif command == "..."` chain, terminal `else` printing `Unknown command` + `__doc__` (`:3964`), `finally: db.close()` (`:3969`). No argparse. Insert three new `elif` branches before `:3964`. Branches stay thin: parse `sys.argv`, call into the new modules, print JSON to stdout, human summary to stderr. Add a `Cross-Machine Sharing:` section to the module docstring (`:7-43`, the help text).

Flag-parse precedents to reuse: index-walk `while i < len(sys.argv)` (create-task `:3355-3371`) for value flags; `"--flag" in sys.argv` (get-task-by-name `:3626-3629`) for booleans. `import` as a string compared to `sys.argv[1]` is fine (never an identifier).

### 4.1 export

```
missioncache-db export <name> [--out <path>] [--no-time] [--json]
```
- `<name>` positional (project NAME). `--out <path>`: `.tgz`/`.tar.gz` → tarball; else a target directory; default `./<name>.missioncache-bundle/`. `--no-time` skips `process_heartbeats()` + time block (pure read-only). `--json` prints the manifest to stdout instead of the human summary.
- Body calls `portability.export_project(db, name, out=..., include_time=..., as_json=...)`, prints summary, prepends `warning:` lines to stderr (no git remote / worktree binding / unportable embedded path). Exit non-zero if name resolves to no row and no on-disk dir.

### 4.2 import

```
missioncache-db import <bundle> [--repo <path>] [--force] [--rewrite-paths] [--dry-run] [--json]
```
- `<bundle>`: directory or `.tar.gz`/`.zip` (extract to `tempfile.mkdtemp()` first, clean up on exit). `--repo <path>`: explicit local repo path, highest precedence over the map. `--force`: permit overwriting the **same** project's files when content differs and rebinding `repo_id` on remote change — never authorizes destroying a *different* project sharing the name (§5 conflict policy). `--rewrite-paths`: opt-in surgical rewrite of embedded paths in placed markdown (default: report-only). `--dry-run`: run the full pipeline, write nothing, print the report and the exit code a real run would produce. `--json`: emit the report dict instead of the human table.
- Body calls `portability.import_bundle(db, bundle, repo_override=..., force=..., rewrite=..., dry_run=...)`, prints the report, exits per §5.

### 4.3 config

Sub-dispatch on `sys.argv[2]`:

```
missioncache-db config set-path  <kind>:<name> <localpath>   # kind ∈ repo|vault|anchor
missioncache-db config list-paths [kind] [--json]
missioncache-db config show                                  # print live machine.json (or skeleton)
missioncache-db config seed [--dry-run]                      # pre-fill from local state (§6.4)
```
- **`set-path` colon-split on the FIRST colon only** (`kind, name = sys.argv[3].split(":", 1)`) — a git remote contains colons (`repo:git@github.com:owner/repo.git`). Reject `kind not in {repo,vault,anchor}`. For `kind == repo`, normalize `name` through `machine_map.remote_key()` before storing; print the canonical key actually stored. `localpath` is `Path(...).expanduser().resolve()`; if it does not exist, **warn but still store** (pre-seed before clone is valid).
- `list-paths --json` prints `machine_map.all_mappings()` (the dict import consumes); human form groups by section.

---

## 5. Resolver + alignment report

### Resolution order (the import pipeline)

Strict order; only steps 1-2 and 7 hard-fail.

1. **Validate bundle.** Manifest present + parseable; `manifest_version == 1`; `project.name` passes `validate_task_name()` (`:198`, `^[a-z0-9][a-z0-9-]*$`); `status`/`type` match the CHECK enums; `files/<name>/` exists. Fail → exit 1, nothing written.
2. **Classify collision** (below). Different-project name collision → exit 1. Identity is the stable `origin_uuid` when both the incoming bundle and the existing row carry one (different uuids → different project, aborted even with `--force`); when either side lacks a uuid (old bundle, or a pre-migration row left un-backfilled), it falls back to the repo-identity heuristic. Residual: a bundle that omits `origin_uuid` still falls to the heuristic, so a null-repo victim can be force-overwritten by a same-named uuid-less bundle — bounded to pre-feature bundles + `--force`, and recoverable via the swap backup.
3. **Place files** → `MISSIONCACHE_ROOT / full_path`, crash-safe (temp dir + `os.replace`, mirroring `lib/config.py:80-91`). CRLF→LF normalize `*.md`/`*.json` (WSL, §9).
4. **Resolve primary repo** (§6.1) → `repo_id` or NULL + report entry.
5. **Resolve vaults** (§6.2) → report entries.
6. **Resolve / optionally rewrite embedded paths** (§6.3) → report entries.
7. **Upsert task row by name** (§7 `upsert_imported_task`) — the DB write, wrapped against `UNIQUE(repo_id, full_path)` IntegrityError. Error → exit 1.
8. **Reconcile parent** by name → `parent_id` or needs-mapping.
9. **Time** → report-only (`time_origin_seconds`).
10. **Trigger DuckDB rebuild** → best-effort `POST localhost:8787/api/sync` (never a direct import).
11. **Emit report + exit.**

`--dry-run` runs 1-9 with writes suppressed, then 11.

> **Implementation note (shipped ordering).** The DB upsert (step 7) runs BEFORE file placement (step 3), not after. Reason: a `UNIQUE(repo_id, full_path)` conflict must abort with the filesystem untouched, so the row is written first; if placement then fails, `rollback_imported_task` deletes the just-created row (or restores the pre-image on an update). This preserves the "exit 1 = nothing committed" invariant in both directions, which the literal 3-then-7 order does not (a step-7 conflict after step-3 placement would strand placed files). See `TestImportPlacementFailureRollback`.

### 3-bucket alignment report

Every reference produces exactly one entry; nothing is dropped (the "nothing fails silently" guarantee). The `resolved` bucket always prints, even on exit 0.

| Bucket | Meaning | Example fix hint |
|---|---|---|
| **resolved** | local target produced AND exists on disk (repo mapped+present, vault note found, embedded path rewrites to an existing file); plus pass-through portables (`jira_key`/`pr_url`/`branch`, `local=null`) and the always-present `files`/`db-row` lines | — |
| **needs-mapping** | reference is portable/known but `machine.json` has no entry — no local path can be produced | `missioncache-db config set-path repo:<remote> <local-path> && re-import` |
| **missing** | a local path WAS produced (mapped, or HOME rewrite applied) but does not exist on disk | `path <local> absent; clone/create it then re-import` |

Entry shape: `{"bucket", "kind", "id", "local", "hint"}` where `kind ∈ files|db-row|repo|repo(worktree)|vault|parent|embedded-path|portable|time`.

**Exit codes (never silent):** `0` = all resolved (needs-mapping + missing both empty); `2` = imported successfully but ≥1 entry in needs-mapping/missing (the signal a sync script keys on); `1` = hard failure, nothing partially committed.

### Conflict / name-collision policy

`tasks.name` is NOT unique (only `UNIQUE(repo_id, full_path)` at `:480`). Identity is the `active/<name>` dir slot + the active row that claims it.

Detect: `existing = find_task_by_full_path("active/<name>")` (`:1184`, active-only) → fallback `get_task_by_name(name)` (`:1373`, active-preferred ordering `:1385`). Classify **same vs different project**: same iff incoming repo (canonical remote, or anchor name when non-git) equals the existing row's repo, OR both null/anchor-equal.

| dir/row state | classification | content == bundle | `--force` | action |
|---|---|---|---|---|
| neither present | — | — | — | **CREATE** |
| present | same | identical | any | **UPDATE** row; files no-op (idempotent re-import) |
| present | same | differs | absent | **ABORT this project**, exit 1 (never clobber local edits) |
| present | same | differs | present | **UPDATE**: overwrite files, UPDATE row, rebind repo_id if remote changed |
| present | different | — | absent | **ABORT**, exit 1, hint: rename one side |
| present | different | — | present | **ABORT anyway**, exit 1 (`--force` must not destroy an unrelated project) |

---

## 6. Reference kinds reconciled + portable identification

`machine.json` shape (per-machine, never synced; lives at `~/.missioncache/` root, structurally outside any sync folder; export hard-excludes it by name):

```json
{
  "version": 1,
  "repos":   { "github.com/akamaietp/logic-automation-python": "/home/tomer/work/.../logic-automation-python" },
  "vaults":  { "TomerWork": "/home/tomer/Obsidian/TomerWork" },
  "anchors": { "HOME": "/home/tomer", "work": "/home/tomer/work" }
}
```
`anchors.HOME` defaults to `str(Path.home())` even when absent, so HOME rewrites work on a fresh machine.

### 6.1 Repo binding (`tasks.repo_id → repositories.path`) — the primary link

**No git code exists in the repo today** (`subprocess` not imported; verdict-confirmed). Export shells out per repo row and **classifies at runtime by the actual `git remote get-url origin` exit** — do NOT hardcode which paths are git (verdict fix: `~/.claude` is itself a git repo `claude-config` on this machine; the Obsidian vault `repositories` row id 8 is missing on disk and at a different path than the rules assume). Tolerate a non-existent `repositories.path` → route to needs-mapping/missing, never crash.

Discriminated union on `kind`:
- **`git`**: `remote` (canonicalized, below), `subpath` (toplevel→path, usually `""`), `worktree` bool, `worktree_branch`, `short_name`. A linked worktree's `.git` is a FILE and its `origin` returns the PARENT remote → set `worktree:true`; import forces **needs-mapping** so a worktree project never silently collapses onto the parent checkout.
- **`anchor`**: non-git tracked folder (e.g. `~/work`). `anchor` logical name + `home_relative` fallback. Resolve via `machine.json.anchors`.
- **`home-relative`**: last resort — `home_relative` path under HOME, no logical match.
- **`null`**: `repo_id` NULL (non-coding task; legal, `create_task` passes None routinely).

Canonical remote (`machine_map.remote_key`, the single source of truth shared by export-write and import-lookup): strip scheme (`ssh|https|http|git://`), strip `git@`, scp-form `host:owner/repo` → `host/owner/repo`, drop `.git`, drop trailing `/`, lowercase **host only**. ssh/https/`.git` variants collapse to one key. `config set-path repo:` runs the same normalizer.

Import resolution (gate `add_repo` on existence — `add_repo` at `:930` is idempotent but has NO disk check and will insert a bogus path): `--repo` override → else `machine.json.repos[remote]` → verify path exists AND `git -C <path> remote get-url origin` equals the manifest remote → `add_repo(path)` → `repo_id` (resolved). Mapped-but-absent → NULL + **missing**. Unmapped → NULL + **needs-mapping**. `worktree:true` → **needs-mapping**. NULL repo_id still imports the row (`repo_id` is nullable `:462`) — a later `config set-path` + re-import (idempotent) binds it.

### 6.2 Vaults (`Hub:` / `[[wikilink]]`) — report-only, pass-through

Wikilink note name is logical/portable; the markdown is never rewritten. **Detection must strip code spans first (verdict fix):** the naive `\[\[([^\]]+)\]\]` over-captures bash `[[ -f "$X" ]]`, TOML `[[plugins."x"]]`, and code like `[[mm, aMonth, bMonth]]`. Rules:
1. Remove fenced (```` ``` ````) and inline (`` ` ``) code spans before scanning.
2. Anchor high-confidence on the `Hub:\s*\[\[([^\]]+)\]\]` line.
3. Validate the target as a note name: `^[A-Za-z0-9][\w .-]*$` — reject targets containing `$`, quotes, operators, commas, shell tokens. Loose `[[..]]` that pass the pattern get `confidence:"low"`.

Record `{note, raw, source: <file>:<line>, confidence}`. Import resolves each `note` by searching every `machine.json.vaults[*]` root for `<note>.md` → resolved / missing. Derive vault roots from `machine.json` / live DB, **not** the CLAUDE.md rule text (live vault path is `~/Documents/Obsidian/TomerWork`, differs from the rule's `~/Obsidian/TomerWork`).

### 6.3 Other embedded absolute paths — inventory, report-only by default

Many active trees embed literal `/Users/tbrami` or `~/` paths (plan files, vault notes by absolute path, worktree paths). **Most point at UNtracked repos** (not in the `repositories` table) — those tokenize as `${HOME}`/home-relative only, never `${repo:...}` (verdict fix: git-remote resolution rarely applies to embedded refs).

Tokenize longest-logical-root-first: a `repositories.path` that is a git repo with a remote → `${repo:<canonical-remote>}`; a configured vault root → `${vault:<logical>}`; otherwise bare HOME → `${HOME}`; no match and not under HOME → `unknown`. Record `{raw, token, classification (repo|vault|home-relative|unknown), target_kind (plan-file|vault-note|source-file|dir|path), source}`.

Default: do not mutate markdown (no false-positive replacements in prose). Import classifies each into the report buckets. `--rewrite-paths` (opt-in) re-expands tokens against the target `machine.json` and replaces the recorded `raw` substring in the placed file (exact literal replace, not a blind global rewrite).

### 6.4 `config seed` — best-effort pre-fill from PROVABLE local state

`anchors.HOME = str(Path.home())` always. For each `db.get_repos(active_only=False)` (`:971`): probe `git -C <path> remote get-url origin`; on success store `remote_key(url) → path` under `repos`, **skipping linked worktrees** (`--git-common-dir` ≠ `--git-dir`) to avoid collapsing the main repo's mapping. On non-zero exit, propose under `vaults` (if path contains `Obsidian/`) or `anchors`, keyed by `short_name`. Seed vault paths from live DB/settings, never the rule file. `--dry-run` writes nothing; prints `added`/`skipped`/`proposed`. Idempotent.

---

## 7. File-by-file change list

| File | Change |
|---|---|
| `missioncache-db/missioncache_db/__init__.py` `:67-68` | **Add env override** (shared prereq): `MISSIONCACHE_ROOT = Path(os.environ.get("MISSIONCACHE_ROOT", Path.home()/".missioncache"))`; `DB_PATH = MISSIONCACHE_ROOT / "tasks.db"`. Mirrors `SHADOW_TRACKED_FOLDER` (`:262`). Needed for import-into-fresh-root + tests. |
| `__init__.py` `:7-43` | Add `Cross-Machine Sharing:` docstring section (the `__doc__` help text). |
| `__init__.py` `~:3186-3964` | Three thin `elif` branches: `export`, `import`, `config` (sub-dispatch on `sys.argv[2]`). Parse argv, call modules, print JSON stdout / human stderr, `sys.exit(code)`. Add `import subprocess` (absent today) and `from missioncache_db import machine_map, portability` (lazy imports inside branches keep startup cheap). |
| `__init__.py` TaskDB | **Add `upsert_imported_task(...)`** — name-keyed direct-write upsert (full code below). Do NOT route import through `create_task` (writes `manual/`/`global/` full_path at `:1224`, lacks status/branch/pr_url/priority/parent/tags) or `_sync_task_from_dir`/scan (COALESCE-only update `:1073-1078` can't correct drift; cross-repo dedup `:1091-1098` skips a rebind). |
| **NEW** `missioncache_db/machine_map.py` | Module functions cloned from `lib/config.py` shape: `MACHINE_FILE = MISSIONCACHE_ROOT / "machine.json"`; tolerant `_read()` (missing/corrupt → `dict(_DEFAULTS)`); atomic `_write()` (tempfile in same dir + `os.replace`, `indent=2, sort_keys=True`, trailing newline); `remote_key(url)`; `resolve(kind, name)`; `record(kind, name, localpath)` (full-document RMW — never a shallow merge, dodging the `{**DEFAULTS, **file}` nested-clobber gotcha at `lib/config.py:71`); `all_mappings()`; `seed(db)`. |
| **NEW** `missioncache_db/portability.py` | `export_project(db, name, out, include_time, as_json) -> dict` and `import_bundle(db, bundle, repo_override, force, rewrite, dry_run) -> dict`. Holds the git helpers (`_git_remote`, `_normalize_remote` delegating to `machine_map.remote_key`, worktree detect), reference scanners (code-span-stripped wikilink + embedded-path tokenizer), `files[]` builder from the actual recursive copy minus exclusions (`*.lock`, `.DS_Store`, `*.bak`, `*.tmp`, `.git`), and the best-effort `POST /api/sync` trigger. **No `import missioncache_dashboard`** — layering preserved. Returns the report dict; the CLI owns all I/O. |
| **NEW** `missioncache-db/tests/test_machine_map.py` | `remote_key` variants; `record`→`resolve` round-trip per kind; `record` preserves other sections; `_read`/`_write` tolerance + atomicity; `set-path` colon-split. |
| **NEW** `missioncache-db/tests/test_portability.py` | §8 cases. |

`upsert_imported_task` (IntegrityError-guarded per verdict; carries `created_at`):

```python
def upsert_imported_task(self, name, full_path, *, repo_id, status, task_type,
                         tags, priority, jira_key, branch, pr_url,
                         parent_id, created_at) -> tuple["Task", str]:
    with self.connection() as conn:
        existing = conn.execute(
            "SELECT id, created_at FROM tasks WHERE full_path = ? "
            "AND status IN ('active','paused') ORDER BY id DESC", (full_path,)
        ).fetchone() or conn.execute(
            "SELECT id, created_at FROM tasks WHERE name = ? "
            "ORDER BY CASE WHEN status='active' THEN 0 ELSE 1 END, id DESC", (name,)
        ).fetchone()
        try:
            if existing:
                keep_created = min(filter(None, [existing["created_at"], created_at]),
                                   default=created_at)
                conn.execute(
                    "UPDATE tasks SET repo_id=?, status=?, type=?, tags=?, priority=?, "
                    "jira_key=?, branch=?, pr_url=?, parent_id=?, created_at=? WHERE id=?",
                    (repo_id, status, task_type, json.dumps(tags), priority,
                     jira_key, branch, pr_url, parent_id, keep_created, existing["id"]))
                conn.commit()
                return self.get_task(existing["id"]), "updated"
            cur = conn.execute(
                "INSERT INTO tasks (repo_id, name, full_path, parent_id, status, type, "
                "tags, priority, jira_key, branch, pr_url, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (repo_id, name, full_path, parent_id, status, task_type,
                 json.dumps(tags), priority, jira_key, branch, pr_url, created_at))
            conn.commit()
            return self.get_task(cur.lastrowid), "created"
        except sqlite3.IntegrityError as e:
            # UNIQUE(repo_id, full_path) collision with a different row -> surface, don't crash
            raise MissionCacheError(
                f"import conflict: (repo_id={repo_id}, {full_path}) already held by another row: {e}")
```

INSERT column shape mirrors `_sync_task_from_dir` (`:1101-1115`) extended with `type`/`tags`/`priority`/`created_at`. `tags` written `json.dumps(...)` to match the JSON-TEXT column (`:470`/`:1230`). The `trg_tasks_updated` trigger (`:540`) refreshes `updated_at` automatically.

---

## 8. Test plan

`pytest`, spec-derived (every assertion traces to the §3/§5 contract, never to current impl behavior — per `test-design.md`). Fixtures: `mc_root` (set `MISSIONCACHE_ROOT` env → `tmp_path/.missioncache`, fresh `TaskDB()` self-bootstraps schema+WAL on first `connection()` `:786-808`); second `mc_root_b` for machine B; `fake_repo` (`git init` + `git remote add origin git@github.com:AkamaiETP/logic-automation-python.git`); `seed_project`; `machine_json`.

| # | Case | Key assertions |
|---|---|---|
| 1 | **Round-trip** | B `get_task_by_name` returns row; status/jira/branch/tags match; 3 md files + `prompts/` + any `research/` subdir land under `mc_root_b/active/<name>/`; report `repo.bucket=="resolved"`, `action=="created"`, needs-mapping + missing empty. |
| 2 | **Path translation** | manifest `other_paths[]` carries the vault ref with `token=${vault:TomerWork}`; with `--rewrite-paths`, B's copied context.md holds the B-side absolute path; report `resolved[]` lists it with B target. Without `--rewrite-paths`, markdown unchanged, ref still classified. |
| 3 | **Name-collision** | different repo_id, no `--force` → conflict reported, NO write, exit 1. With `--force` → existing row UPDATED in place (`.id` preserved), repo_id rewritten. |
| 4 | **Non-git repo** | manifest `repo.kind=="anchor"`/`remote=null` + warning; import with no matching anchor → `needs-mapping`, task still imported `repo_id IS NULL`; then `config set-path vault:... ` + re-import → resolved, repo_id non-null. |
| 5 | **Missing-ref flagging** | mapped vault note absent on disk → `missing[]`; unmapped anchor → `needs-mapping[]` with exact hint `missioncache-db config set-path anchor:claude-plans <path>`; exit 2. |
| 6 | **Idempotent re-import** | import twice → exactly ONE row; `.id` unchanged; files identical; 2nd `action=="updated"`. Proves dedup bypass + same-row reuse. |
| 7 | **Field fidelity** | `type=="non-coding"`, `priority==3`, `tags==["x"]` survive (DB-only, absent from markdown). |
| 8 | **Subtask parent** | parent present → child `parent_id == B-local parent.id` (not A's id); parent absent → `parent_id IS NULL` + `needs-mapping`. |
| 9 | **EOL normalization** | CRLF bundle → files on disk LF-only. |
| 10 | **`/mnt/c` warning** | `MISSIONCACHE_ROOT` resolving under `/mnt/` → warning emitted, import still completes. |
| 11 | **Dry-run** | NO row, NO files written, report fully populated with same bucket classification. |
| 12 | **remote_key normalization** | export ssh form, B map keyed from https form → both normalize, repo resolves. Unit-test `remote_key()` across ssh/https/`.git`/trailing-slash. |
| 13 | **Time carried not seeded** | manifest `time_total_seconds>0`; report `time_origin_seconds` equals it; B has ZERO sessions (`get_task_time(id,"all")==0`). |
| 14 | **DuckDB layering** | import with no dashboard → succeeds, `notes[]` has the refresh line; source-scan asserts `portability.py` never `import missioncache_dashboard`. |
| 15 | **files[] enumeration + exclusion** (verdict) | a project with `research/` subdir + a `*.lock` + `.DS_Store` → `files[]` lists `research/*.md` with checksums, EXCLUDES `.lock`/`.DS_Store`/`.bak`. |
| 16 | **Wikilink over-capture guard** (verdict) | context.md containing bash `[[ -f "$X" ]]`, TOML `[[plugins."x"]]`, and a real `Hub: [[note]]` → only `note` recorded (high confidence); code-span matches dropped. |
| 17 | **Worktree binding** (verdict) | repo bound to a linked worktree → `worktree:true` + warning on export; import forces `needs-mapping`, does not bind the parent repo. |
| 18 | **created_at round-trip** (verdict) | new import preserves manifest `created_at`; re-import keeps the EARLIER of existing vs incoming. |

Out of scope for this component's tests: MCP-tool wrapping, slash-command UX, the git-sync reconcile loop (thin follow-ons).

---

## 9. WSL specifics + edge cases

- **`~/.missioncache` must be on WSL native fs, not `/mnt/c`.** DrvFs breaks SQLite WAL, is slow, has perms/inotify issues. On import, if `MISSIONCACHE_ROOT.resolve()` starts with `/mnt/`, emit a loud warning ("move to WSL native filesystem; SQLite WAL can corrupt on DrvFs"). Warn, do not hard-fail.
- **EOL normalization.** Bundles through Windows tools / git autocrlf can gain CRLF. Import normalizes CRLF→LF when writing `*.md`/`*.json` (keeps `Hub:`/`[[..]]`/frontmatter parsers stable, avoids spurious git-sync diffs). Export writes LF. `prompts/` binaries (none expected) copied byte-for-byte.
- **POSIX-to-POSIX only.** Only HOME prefix + repo/vault roots differ; no separator translation. Native-Windows backslash target out of scope (state in `--help`).
- **Case sensitivity.** `validate_task_name` forces lowercase (`:198`), removing macOS-insensitive / WSL-sensitive collision risk on the project dir.
- **Symlinked `.env` files** live in the REPO, not the project dir — not bundled; the repo (resolved by remote) brings its own. A markdown ref to an `.env` path falls into the normal buckets.
- **`add_repo` no disk check** (`:930-957`) — always gate on `Path.exists()` / `get_repo_by_path` (`:989`) before calling, else a bogus machine-specific path pollutes `repositories`.

---

## 10. Phased implementation (smallest shippable first)

**Phase 0 — enabling change (1 line + derive).** `MISSIONCACHE_ROOT`/`DB_PATH` env override (`:67-68`). Unblocks fresh-root import + all tests. Ship + verify existing suite green.

**Phase 1 — path map + config CLI.** `machine_map.py` (read/write/remote_key/resolve/record/all_mappings) + `config set-path`/`list-paths`/`show` branch + `test_machine_map.py`. Independently useful and fully testable with no export/import. Ship.

**Phase 2 — export.** `portability.export_project` + `export` CLI branch: project resolve, dir copy (recursive, with exclusions), runtime git classification, time total (`process_heartbeats` `:2016` then `get_task_time(id,"all")` `:2152`), code-span-safe reference scan, manifest write, `files[]` from actual copy. Tests: round-trip-export half of cases 1/4/15/16/17, plus the manifest-shape assertions. Ship — export alone gives a manual "copy the bundle, eyeball it" workflow.

**Phase 3 — import + alignment report.** `upsert_imported_task` + `portability.import_bundle` + `import` CLI branch: validate, collision classify, place files (atomic + LF), repo/vault/embedded resolution, name-keyed upsert (IntegrityError-guarded), parent reconcile, best-effort `POST /api/sync`, 3-bucket report, exit codes. `--rewrite-paths` opt-in. Full §8 suite. Ship — this completes the agreed export/import/path-map design.

**Phase 4 — continuous sync (later layer, no new CLI).** Document the shell loop: `export --out <git-folder>/<name>.missioncache-bundle` + commit/push on A; `git pull` + `import <git-folder>/<name>.missioncache-bundle` on B, keying a reconcile script on import exit code 2. Installer adds `machine.json`, `tasks.db*`, `tasks.duckdb` to that folder's `.gitignore`. Directory bundles + LF + checksummed `files[]` make it diff cleanly. No code beyond docs + the gitignore line.

**Phase 5 (optional) — MCP + slash-command wrappers.** Thin wrappers over the two CLI commands once the CLI path is proven.

---

### Verdict reconciliation summary (what changed vs the raw component specs)
- **DuckDB rebuild unified** on best-effort `POST localhost:8787/api/sync` (server.py:2771). The "call `AnalyticsDB.sync_from_sqlite()` directly" line from the import component is **dropped** — it inverts the `dashboard → db` dependency.
- **`upsert_imported_task` UPDATE wrapped** against `UNIQUE(repo_id, full_path)` IntegrityError → surfaced as a conflict, not a crash.
- **`created_at` round-trips**: added to INSERT, `min(existing, incoming)` on UPDATE (manifest and INSERT now agree; the earlier UNCERTAIN verdict resolved by carrying it).
- **"No double source of truth" reworded**: jira/branch/pr overlap markdown intentionally; the property holds because import never re-parses markdown for DB fields.
- **Wikilink detection hardened**: strip code spans, anchor on `Hub:`, strict note-name pattern — kills bash/TOML/code false positives.
- **Repo classification at runtime**, per row, by actual `git remote get-url origin` exit. No hardcoded "non-git anchor" list (`~/.claude` is a git repo here; vault row id 8 is missing on disk). Tolerate non-existent `repositories.path`.
- **`files[]` built from the actual recursive copy** (captures `research/`, loose dated `.md`), with an exclusion list (`*.lock`, `.DS_Store`, `*.bak`, `*.tmp`, `.git`).
- **Embedded paths to untracked repos** resolve via HOME/anchor only, documented — git-remote tokenization rarely applies.

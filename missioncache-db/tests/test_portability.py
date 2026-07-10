"""Tests for portability.export_project: Phase 2 (export) of cross-machine sharing.

Spec source: docs/cross-machine-sharing-plan.md sections 3 (manifest), 4.1 (CLI),
6 (reference kinds), 8 (test plan), 10 (phasing). Every assertion traces to that
contract, not to the implementation. Import (Phase 3) is out of scope here.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import missioncache_db
from missioncache_db import TaskDB, machine_map, portability

SSH_REMOTE = "git@github.com:AkamaiETP/logic-automation-python.git"
CANON_REMOTE = "github.com/AkamaiETP/logic-automation-python"


# ============ fixtures + helpers ============


@pytest.fixture
def mc(tmp_path, monkeypatch):
    """A throwaway MissionCache root + initialized DB.

    Patches the live module constant the way the plan's ``mc_root`` fixture
    prescribes (export reads ``missioncache_db.MISSIONCACHE_ROOT`` dynamically),
    and points machine_map at a machine.json under the same root. DB_PATH is
    left alone so the legacy-path guard keeps seeing the real install.
    """
    root = tmp_path / ".missioncache"
    (root / "active").mkdir(parents=True)
    monkeypatch.setattr(missioncache_db, "MISSIONCACHE_ROOT", root)
    monkeypatch.setattr(machine_map, "MACHINE_FILE", root / "machine.json")
    db = TaskDB(db_path=root / "tasks.db")
    db.initialize()
    return SimpleNamespace(root=root, db=db)


@pytest.fixture(autouse=True)
def _no_live_dashboard_sync(monkeypatch):
    """Pin the DuckDB-sync URL to a closed port so the suite never POSTs to a
    real dashboard running on this machine. Import's best-effort sync then always
    hits connection-refused (fast, deterministic); tests that assert the
    dashboard-absent note rely on exactly this. A test needing the reachable
    branch overrides _SYNC_URL itself and wins."""
    monkeypatch.setattr(portability, "_SYNC_URL", "http://127.0.0.1:9/api/sync")


def _git(path, *args, check=True):
    return subprocess.run(
        ["git", "-c", "user.email=t@e.com", "-c", "user.name=t", "-C", str(path), *args],
        capture_output=True, text=True, check=check,
    )


def _git_repo(path: Path, remote: str = SSH_REMOTE) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "remote", "add", "origin", remote], cwd=path, check=True)
    return path


def _committed_repo(path: Path, remote: str = SSH_REMOTE) -> Path:
    _git_repo(path, remote)
    _git(path, "commit", "--allow-empty", "-m", "init", "-q")
    return path


def _insert_task(db, *, name, full_path=None, repo_id=None, status="active",
                 task_type="coding", tags=None, priority=None, jira_key=None,
                 branch=None, pr_url=None, parent_id=None,
                 created_at="2026-05-02T09:11:00", origin_uuid=None,
                 category=None) -> int:
    # origin_uuid defaults to None so the repo-identity heuristic tests still
    # exercise the fallback path (a uuid on both sides would short-circuit it).
    full_path = full_path or f"active/{name}"
    with db.connection() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (repo_id, name, full_path, parent_id, status, type, "
            "tags, priority, jira_key, branch, pr_url, created_at, origin_uuid, "
            "category) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (repo_id, name, full_path, parent_id, status, task_type,
             json.dumps(tags or []), priority, jira_key, branch, pr_url,
             created_at, origin_uuid, category),
        )
        conn.commit()
        return cur.lastrowid


def _insert_session(db, task_id, duration):
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO sessions (task_id, start_time, end_time, duration_seconds, "
            "heartbeat_count) VALUES (?, '2026-05-02T10:00:00', '2026-05-02T10:02:00', ?, 1)",
            (task_id, duration),
        )
        conn.commit()


def _seed_files(mc, name, files):
    pdir = mc.root / "active" / name
    pdir.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        fp = pdir / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
    return pdir


def _default_md(name):
    return {
        f"{name}-plan.md": "# plan\n",
        f"{name}-context.md": f"# context\nHub: [[{name}]]\n",
        f"{name}-tasks.md": "# tasks\n- [ ] 1. do the thing\n",
    }


def _export(mc, name, **kw):
    return portability.export_project(mc.db, name, **kw)


# ============ manifest shape (§3) ============


class TestManifestShape:
    def test_core_fields(self, mc, tmp_path):
        name = "finance-dashboard"
        _seed_files(mc, name, _default_md(name))
        _insert_task(mc.db, name=name)
        m = _export(mc, name, out=str(tmp_path / "b"))["manifest"]

        assert m["manifest_version"] == 1
        assert m["kind"] == "missioncache-project-bundle"
        assert m["generator"].startswith("missioncache-db/")
        assert m["exported_from"]["home"] == str(Path.home())
        assert set(m["exported_from"]) == {"host", "home", "platform"}
        # exported_at is an ISO-8601 timestamp with an offset.
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}",
                        m["exported_at"])
        assert set(m["references"]) == {"repo", "vaults", "other_paths"}

    def test_project_block_mirrors_db_row(self, mc, tmp_path):
        name = "finance-dashboard"
        _seed_files(mc, name, _default_md(name))
        _insert_task(mc.db, name=name, status="paused", task_type="coding",
                     tags=["dashboard", "finance"], priority=2, jira_key="GC-1234",
                     branch="feature/x", pr_url="https://example/pr/1",
                     created_at="2026-05-02T09:11:00")
        p = _export(mc, name, out=str(tmp_path / "b"))["manifest"]["project"]

        assert p["name"] == name
        assert p["status"] == "paused"
        assert p["type"] == "coding"
        assert p["tags"] == ["dashboard", "finance"]
        assert p["priority"] == 2
        assert p["jira_key"] == "GC-1234"
        assert p["branch"] == "feature/x"
        assert p["pr_url"] == "https://example/pr/1"
        assert p["full_path"] == f"active/{name}"
        assert p["parent"] is None
        assert p["created_at"] == "2026-05-02T09:11:00"

    def test_files_have_path_and_checksum(self, mc, tmp_path):
        name = "proj"
        _seed_files(mc, name, _default_md(name))
        _insert_task(mc.db, name=name)
        out = tmp_path / "b"
        m = _export(mc, name, out=str(out))["manifest"]

        assert m["files"], "files[] must not be empty"
        for entry in m["files"]:
            assert entry["path"].startswith(f"{name}/")
            assert re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
            # checksum matches the file actually placed in the bundle
            placed = out / "files" / entry["path"]
            assert hashlib.sha256(placed.read_bytes()).hexdigest() == entry["sha256"]


# ============ round-trip export, case 1 ============


class TestRoundTripExport:
    def test_bundle_layout_and_all_files_present(self, mc, tmp_path):
        name = "finance-dashboard"
        files = _default_md(name)
        files["research/02-data-pipeline-map.md"] = "# map\n"
        files["prompts/task-01-prompt.md"] = "do task 1\n"
        _seed_files(mc, name, files)
        _insert_task(mc.db, name=name)

        out = tmp_path / "b"
        report = _export(mc, name, out=str(out))
        m = report["manifest"]

        assert (out / "missioncache.json").is_file()
        assert (out / "files" / name).is_dir()
        bundle_paths = {f["path"] for f in m["files"]}
        assert bundle_paths == {
            f"{name}/{name}-plan.md",
            f"{name}/{name}-context.md",
            f"{name}/{name}-tasks.md",
            f"{name}/research/02-data-pipeline-map.md",
            f"{name}/prompts/task-01-prompt.md",
        }
        assert report["file_count"] == 5
        # files[] is sorted by path (deterministic for git-folder diffing)
        assert [f["path"] for f in m["files"]] == sorted(f["path"] for f in m["files"])

    def test_git_repo_resolved_to_canonical_remote(self, mc, tmp_path):
        name = "finance-dashboard"
        _seed_files(mc, name, _default_md(name))
        repo = _committed_repo(tmp_path / "repo")
        rid = mc.db.add_repo(str(repo))
        _insert_task(mc.db, name=name, repo_id=rid)

        report = _export(mc, name, out=str(tmp_path / "b"))
        repo_ref = report["manifest"]["references"]["repo"]

        assert repo_ref["kind"] == "git"
        assert repo_ref["remote"] == CANON_REMOTE
        assert repo_ref["worktree"] is False
        assert repo_ref["subpath"] == ""
        assert repo_ref["short_name"]
        assert report["warnings"] == []


# ============ files[] enumeration + exclusion, case 15 ============


class TestFilesEnumeration:
    def test_subdirs_included_junk_excluded(self, mc, tmp_path):
        name = "proj"
        files = _default_md(name)
        files["research/notes.md"] = "# r\n"
        files["build.lock"] = "lock"
        files[".DS_Store"] = "junk"
        files["draft.bak"] = "bak"
        files["scratch.tmp"] = "tmp"
        _seed_files(mc, name, files)
        _insert_task(mc.db, name=name)

        out = tmp_path / "b"
        m = _export(mc, name, out=str(out))["manifest"]
        paths = {f["path"] for f in m["files"]}

        assert f"{name}/research/notes.md" in paths
        for junk in ("build.lock", ".DS_Store", "draft.bak", "scratch.tmp"):
            assert f"{name}/{junk}" not in paths
            assert not (out / "files" / name / junk).exists()  # not copied either


# ============ wikilink over-capture guard, case 16 ============


class TestWikilinkScan:
    def test_only_real_note_recorded_high_confidence(self, mc, tmp_path):
        name = "proj"
        context = (
            "# context\n"
            "Hub: [[my-note]]\n"
            "```bash\n"
            'if [[ -f "$X" ]]; then echo hi; fi\n'
            "```\n"
            'inline `[[plugins."x"]]` should be ignored\n'
            "[[mm, aMonth, bMonth]] is code not a note\n"
        )
        files = _default_md(name)
        files[f"{name}-context.md"] = context
        _seed_files(mc, name, files)
        _insert_task(mc.db, name=name)

        vaults = _export(mc, name, out=str(tmp_path / "b"))["manifest"]["references"]["vaults"]
        notes = [(v["note"], v["confidence"]) for v in vaults]

        assert ("my-note", "high") in notes
        assert all(n != "plugins" for n, _ in notes)
        assert all("," not in n for n, _ in notes)  # comma list rejected
        assert all('"' not in n and "$" not in n for n, _ in notes)

    def test_loose_valid_wikilink_low_confidence(self, mc, tmp_path):
        name = "proj"
        files = _default_md(name)
        files[f"{name}-context.md"] = "# context\nsee [[Other Note]] for details\n"
        _seed_files(mc, name, files)
        _insert_task(mc.db, name=name)

        vaults = _export(mc, name, out=str(tmp_path / "b"))["manifest"]["references"]["vaults"]
        assert ("Other Note", "low") in [(v["note"], v["confidence"]) for v in vaults]


# ============ embedded path tokenization, case 2 ============


class TestEmbeddedPaths:
    def test_home_path_tokenized(self, mc, tmp_path):
        name = "proj"
        home_path = f"{Path.home()}/Documents/plan-notes/i-work.md"
        files = _default_md(name)
        files[f"{name}-context.md"] = f"# context\nplan lives at {home_path}\n"
        _seed_files(mc, name, files)
        _insert_task(mc.db, name=name)

        op = _export(mc, name, out=str(tmp_path / "b"))["manifest"]["references"]["other_paths"]
        match = [e for e in op if e["raw"] == home_path]
        assert len(match) == 1
        assert match[0]["token"] == "${HOME}/Documents/plan-notes/i-work.md"
        assert match[0]["classification"] == "home-relative"

    def test_vault_path_tokenized(self, mc, tmp_path):
        name = "proj"
        vault_root = tmp_path / "Obsidian" / "TomerWork"
        vault_root.mkdir(parents=True)
        machine_map.record("vault", "TomerWork", str(vault_root))
        vault_path = f"{vault_root}/Resources/qa-note-2026-05.md"
        files = _default_md(name)
        files[f"{name}-context.md"] = f"# context\nref {vault_path}\n"
        _seed_files(mc, name, files)
        _insert_task(mc.db, name=name)

        op = _export(mc, name, out=str(tmp_path / "b"))["manifest"]["references"]["other_paths"]
        match = [e for e in op if e["raw"] == vault_path]
        assert len(match) == 1
        assert match[0]["token"] == "${vault:TomerWork}/Resources/qa-note-2026-05.md"
        assert match[0]["classification"] == "vault"
        assert match[0]["target_kind"] == "vault-note"

    def test_repo_path_tokenized(self, mc, tmp_path):
        name = "proj"
        repo = _committed_repo(tmp_path / "checkout")
        mc.db.add_repo(str(repo))
        embedded = f"{repo}/src/module.py"
        files = _default_md(name)
        files[f"{name}-context.md"] = f"# context\nedit {embedded}\n"
        _seed_files(mc, name, files)
        _insert_task(mc.db, name=name)

        op = _export(mc, name, out=str(tmp_path / "b"))["manifest"]["references"]["other_paths"]
        match = [e for e in op if e["raw"] == embedded]
        assert len(match) == 1
        assert match[0]["token"] == f"${{repo:{CANON_REMOTE}}}/src/module.py"
        assert match[0]["classification"] == "repo"

    def test_system_path_dropped_but_home_path_kept(self, mc, tmp_path):
        # Discriminating: a real HOME path AND a system path on the same line.
        # The scanner must capture the reconcilable one and drop the system one,
        # not return [] for both (which a no-op scanner would also satisfy).
        name = "proj"
        home_path = f"{Path.home()}/notes/keep.md"
        files = _default_md(name)
        files[f"{name}-context.md"] = (
            f"# context\nrun /usr/bin/env then open {home_path}\n"
        )
        _seed_files(mc, name, files)
        _insert_task(mc.db, name=name)

        op = _export(mc, name, out=str(tmp_path / "b"))["manifest"]["references"]["other_paths"]
        raws = [e["raw"] for e in op]
        assert home_path in raws  # reconcilable path captured
        assert all("/usr/bin/env" not in r for r in raws)  # system path dropped


# ============ worktree binding, case 17 ============


class TestWorktree:
    def test_linked_worktree_flagged_and_warned(self, mc, tmp_path):
        name = "proj"
        _seed_files(mc, name, _default_md(name))
        main = _committed_repo(tmp_path / "main")
        wt = tmp_path / "wt"
        _git(main, "worktree", "add", str(wt), "-b", "feature", "-q")
        rid = mc.db.add_repo(str(wt))
        _insert_task(mc.db, name=name, repo_id=rid)

        report = _export(mc, name, out=str(tmp_path / "b"))
        repo_ref = report["manifest"]["references"]["repo"]

        assert repo_ref["kind"] == "git"
        assert repo_ref["worktree"] is True
        assert repo_ref["worktree_branch"] == "feature"
        assert any("worktree" in w for w in report["warnings"])


# ============ non-git repo, case 4 (export half) ============


class TestNonGitRepo:
    def test_non_git_repo_is_anchor_with_warning(self, mc, tmp_path):
        name = "proj"
        _seed_files(mc, name, _default_md(name))
        plain = tmp_path / "plain-dir"
        plain.mkdir()
        rid = mc.db.add_repo(str(plain))
        _insert_task(mc.db, name=name, repo_id=rid)

        report = _export(mc, name, out=str(tmp_path / "b"))
        repo_ref = report["manifest"]["references"]["repo"]

        assert repo_ref["kind"] == "anchor"
        assert any("no 'origin' git remote" in w for w in report["warnings"])

    def test_non_coding_task_has_null_repo(self, mc, tmp_path):
        name = "proj"
        _seed_files(mc, name, _default_md(name))
        _insert_task(mc.db, name=name, task_type="non-coding", repo_id=None)

        m = _export(mc, name, out=str(tmp_path / "b"))["manifest"]
        assert m["references"]["repo"] is None


# ============ remote_key normalization in manifest, case 12 ============


class TestRemoteKeyInManifest:
    def test_ssh_remote_canonicalized_no_scheme_or_dotgit(self, mc, tmp_path):
        name = "proj"
        _seed_files(mc, name, _default_md(name))
        repo = _committed_repo(tmp_path / "repo", remote="git@github.com:Owner/Repo.git")
        rid = mc.db.add_repo(str(repo))
        _insert_task(mc.db, name=name, repo_id=rid)

        remote = _export(mc, name, out=str(tmp_path / "b"))["manifest"]["references"]["repo"]["remote"]
        assert remote == "github.com/Owner/Repo"
        assert "git@" not in remote and not remote.endswith(".git")


# ============ time carried not seeded, case 13 (export half) ============


class TestTime:
    def test_time_total_matches_db(self, mc, tmp_path):
        name = "proj"
        _seed_files(mc, name, _default_md(name))
        tid = _insert_task(mc.db, name=name)
        _insert_session(mc.db, tid, 120)

        m = _export(mc, name, out=str(tmp_path / "b"), include_time=True)["manifest"]
        # 120 traces to the inserted session duration (the §3 contract), not to
        # re-deriving the value through the same get_task_time the export calls.
        assert m["project"]["time_total_seconds"] == 120

    def test_no_time_flag_yields_zero(self, mc, tmp_path):
        name = "proj"
        _seed_files(mc, name, _default_md(name))
        tid = _insert_task(mc.db, name=name)
        _insert_session(mc.db, tid, 120)

        m = _export(mc, name, out=str(tmp_path / "b"), include_time=False)["manifest"]
        assert m["project"]["time_total_seconds"] == 0

    def test_no_time_does_not_call_process_heartbeats(self, mc, tmp_path, monkeypatch):
        # --no-time promises no DB writes; process_heartbeats (which writes
        # session rows) must be skipped, not just produce a zero in the output.
        name = "proj"
        _seed_files(mc, name, _default_md(name))
        _insert_task(mc.db, name=name)
        calls = []
        monkeypatch.setattr(mc.db, "process_heartbeats", lambda: calls.append(1))

        _export(mc, name, out=str(tmp_path / "b"), include_time=False)
        assert calls == []  # skipped under --no-time
        _export(mc, name, out=str(tmp_path / "c"), include_time=True)
        assert calls == [1]  # called once when time is included


# ============ output modes (§4.1) ============


class TestOutputModes:
    def test_tarball_output(self, mc, tmp_path):
        name = "finance-dashboard"
        _seed_files(mc, name, _default_md(name))
        _insert_task(mc.db, name=name)
        tgz = tmp_path / "out" / f"{name}.tgz"

        report = _export(mc, name, out=str(tgz))
        assert report["bundle_path"] == str(tgz)
        assert tgz.is_file()
        with tarfile.open(tgz) as tar:
            members = tar.getnames()
        assert f"{name}.missioncache-bundle/missioncache.json" in members
        assert any(m.startswith(f"{name}.missioncache-bundle/files/{name}/") for m in members)

    def test_default_out_dir_in_cwd(self, mc, tmp_path, monkeypatch):
        name = "proj"
        _seed_files(mc, name, _default_md(name))
        _insert_task(mc.db, name=name)
        monkeypatch.chdir(tmp_path)

        report = _export(mc, name)  # out=None -> ./<name>.missioncache-bundle
        assert (tmp_path / f"{name}.missioncache-bundle" / "missioncache.json").is_file()
        assert Path(report["bundle_path"]).name == f"{name}.missioncache-bundle"


# ============ errors + dir-only export ============


class TestErrors:
    def test_unknown_project_raises(self, mc, tmp_path):
        with pytest.raises(ValueError):
            _export(mc, "does-not-exist", out=str(tmp_path / "b"))

    def test_on_disk_dir_without_db_row_exports_minimal(self, mc, tmp_path):
        name = "orphan-proj"
        _seed_files(mc, name, _default_md(name))  # files but no tasks row

        report = _export(mc, name, out=str(tmp_path / "b"))
        p = report["manifest"]["project"]
        assert p["name"] == name
        assert p["status"] == "active"
        assert p["type"] == "coding"
        assert p["time_total_seconds"] == 0
        assert p["full_path"] == f"active/{name}"
        assert any("no tasks row" in w for w in report["warnings"])

    def test_source_files_never_mutated(self, mc, tmp_path):
        name = "proj"
        files = _default_md(name)
        files["research/note.md"] = "# r\n"
        files["prompts/task-01-prompt.md"] = "do 1\n"
        pdir = _seed_files(mc, name, files)
        _insert_task(mc.db, name=name)

        def _tree_hashes():
            return {
                p.relative_to(pdir).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in pdir.rglob("*") if p.is_file()
            }

        before = _tree_hashes()
        _export(mc, name, out=str(tmp_path / "b"))
        assert _tree_hashes() == before  # entire source tree byte-identical


# ============ scanner edge cases (regression for review findings) ============


class TestScannerEdgeCases:
    def test_sibling_dir_not_miscaptured_as_repo(self):
        # A reference to sibling `foobar` must not be sliced down to repo `foo`.
        home = "/h"
        roots = [("/h/work/foo", "${repo:github.com/x/foo}", "repo")]
        pat = portability._embedded_pattern(home, roots)
        matches = list(pat.finditer("see /h/work/foobar/x.md here"))
        assert len(matches) == 1
        raw = matches[0].group(0).rstrip(portability._PATH_STRIP_CHARS)
        assert raw == "/h/work/foobar/x.md"
        token, classification = portability._tokenize_path(raw, home, roots)
        assert token == "${HOME}/work/foobar/x.md"
        assert classification == "home-relative"

    def test_real_repo_path_still_tokenizes_as_repo(self):
        home = "/h"
        roots = [("/h/work/foo", "${repo:github.com/x/foo}", "repo")]
        pat = portability._embedded_pattern(home, roots)
        raw = list(pat.finditer("edit /h/work/foo/src/a.py now"))[0].group(0)
        token, classification = portability._tokenize_path(raw, home, roots)
        assert token == "${repo:github.com/x/foo}/src/a.py"
        assert classification == "repo"

    def test_interior_space_path_dropped_not_truncated(self, mc, tmp_path):
        # A path with an interior space must be DROPPED, never reported truncated
        # to "${HOME}/Documents/My".
        name = "proj"
        spaced = f"{Path.home()}/Documents/My Notes/plan.md"
        files = _default_md(name)
        files[f"{name}-context.md"] = f"# context\nplan at {spaced} today\n"
        _seed_files(mc, name, files)
        _insert_task(mc.db, name=name)

        op = _export(mc, name, out=str(tmp_path / "b"))["manifest"]["references"]["other_paths"]
        assert all("/Documents/My" not in e["raw"] for e in op)  # no truncated form

    def test_two_paths_one_line_trailing_one_kept(self, mc, tmp_path):
        # `cp <a> <b>`: a ends at a space (dropped), b ends the line (kept). This
        # covers both directions of the conservative rule on one realistic line -
        # the exact case the old slash heuristic got wrong (it dropped both).
        name = "proj"
        a = f"{Path.home()}/a.md"
        b = f"{Path.home()}/b.md"
        files = _default_md(name)
        files[f"{name}-context.md"] = f"# context\ncp {a} {b}\n"
        _seed_files(mc, name, files)
        _insert_task(mc.db, name=name)

        op = _export(mc, name, out=str(tmp_path / "bundle"))["manifest"]["references"]["other_paths"]
        raws = [e["raw"] for e in op]
        assert a not in raws  # followed by a space -> dropped (conservative)
        assert b in raws      # ends the line -> kept


# ============ symlink + secret exclusion (security findings) ============


class TestSymlinkAndSecret:
    def test_symlinked_file_skipped_and_warned(self, mc, tmp_path):
        name = "proj"
        pdir = _seed_files(mc, name, _default_md(name))
        secret = tmp_path / "outside-secret.txt"
        secret.write_text("SENSITIVE-CONTENT")
        (pdir / "leak.md").symlink_to(secret)
        _insert_task(mc.db, name=name)

        out = tmp_path / "b"
        report = _export(mc, name, out=str(out))
        assert not (out / "files" / name / "leak.md").exists()
        assert all(
            "SENSITIVE-CONTENT" not in p.read_text()
            for p in (out / "files" / name).rglob("*")
            if p.is_file()
        )
        assert any("symlink" in w for w in report["warnings"])

    def test_secret_file_skipped_and_warned(self, mc, tmp_path):
        name = "proj"
        pdir = _seed_files(mc, name, _default_md(name))
        (pdir / ".env").write_text("API_TOKEN=supersecret")
        (pdir / "server.pem").write_text("-----BEGIN KEY-----")
        _insert_task(mc.db, name=name)

        out = tmp_path / "b"
        report = _export(mc, name, out=str(out))
        manifest_paths = {f["path"] for f in report["manifest"]["files"]}
        assert f"{name}/.env" not in manifest_paths
        assert f"{name}/server.pem" not in manifest_paths
        assert not (out / "files" / name / ".env").exists()
        assert not (out / "files" / name / "server.pem").exists()
        assert all(
            "supersecret" not in p.read_text()
            for p in (out / "files" / name).rglob("*") if p.is_file()
        )
        assert any(".env" in w and "secret" in w for w in report["warnings"])

    def test_uppercase_secret_file_skipped(self, mc, tmp_path):
        # Case variants must not bypass the denylist (fnmatch is case-sensitive).
        name = "proj"
        pdir = _seed_files(mc, name, _default_md(name))
        (pdir / ".ENV").write_text("TOKEN=x")
        (pdir / "SERVER.PEM").write_text("-----BEGIN KEY-----")
        _insert_task(mc.db, name=name)

        out = tmp_path / "b"
        report = _export(mc, name, out=str(out))
        manifest_paths = {f["path"] for f in report["manifest"]["files"]}
        assert f"{name}/.ENV" not in manifest_paths
        assert f"{name}/SERVER.PEM" not in manifest_paths

    def test_symlinked_dir_skipped_and_warned(self, mc, tmp_path):
        name = "proj"
        pdir = _seed_files(mc, name, _default_md(name))
        outside = tmp_path / "outside-dir"
        outside.mkdir()
        (outside / "leak.md").write_text("LEAK-DIR-CONTENT")
        (pdir / "linkdir").symlink_to(outside, target_is_directory=True)
        _insert_task(mc.db, name=name)

        out = tmp_path / "b"
        report = _export(mc, name, out=str(out))
        assert not (out / "files" / name / "linkdir").exists()
        assert all(
            "LEAK-DIR-CONTENT" not in p.read_text()
            for p in (out / "files" / name).rglob("*") if p.is_file()
        )
        assert any("symlinked directory" in w for w in report["warnings"])


# ============ re-export staleness (Codex + silent-failure finding) ============


class TestReExportStaleness:
    def test_deleted_file_removed_on_reexport_to_same_dir(self, mc, tmp_path):
        name = "proj"
        files = _default_md(name)
        files["research/old.md"] = "# old\n"
        pdir = _seed_files(mc, name, files)
        _insert_task(mc.db, name=name)
        out = tmp_path / "bundle"

        _export(mc, name, out=str(out))
        assert (out / "files" / name / "research" / "old.md").exists()

        (pdir / "research" / "old.md").unlink()  # remove from source
        report = _export(mc, name, out=str(out))  # re-export to SAME dir

        assert not (out / "files" / name / "research" / "old.md").exists()
        assert all("old.md" not in f["path"] for f in report["manifest"]["files"])


# ============ name validation / path traversal (security finding) ============


class TestNameValidation:
    def test_traversal_name_rejected(self, mc, tmp_path):
        # match= pins the failure to the validation error; without it the test
        # would pass even if validate_task_name were deleted, because the
        # downstream "no files" guard raises ValueError too.
        with pytest.raises(ValueError, match="lowercase"):
            _export(mc, "../../../etc", out=str(tmp_path / "b"))

    def test_invalid_name_rejected_before_fs_access(self, mc, tmp_path):
        # Seed a dir that WOULD export, so "no files" cannot be the reason; the
        # "lowercase" match then proves validation fires before the is_dir check.
        pdir = mc.root / "active" / "Bad_Name"
        pdir.mkdir()
        for rel, content in _default_md("Bad_Name").items():
            (pdir / rel).write_text(content)
        with pytest.raises(ValueError, match="lowercase"):
            _export(mc, "Bad_Name", out=str(tmp_path / "b"))


# ============ repo classification matrix (test-analyst gap) ============


class TestClassifyRepo:
    # Note: a dangling repo_id (the `repo is None` guard in _classify_repo) is
    # not constructible - the tasks.repo_id -> repositories.id FK forbids it - so
    # that branch is defensive only and has no test.

    def test_missing_on_disk_repo_under_home_is_home_relative(self, mc):
        gone = Path.home() / "definitely-not-a-real-dir-xyz123"
        rid = mc.db.add_repo(str(gone))
        task = mc.db.get_task(_insert_task(mc.db, name="proj", repo_id=rid))
        ref, warns = portability._classify_repo(mc.db, task)
        assert ref["kind"] == "home-relative"
        assert any("missing on disk" in w for w in warns)

    def test_missing_on_disk_repo_outside_home_is_anchor(self, mc, tmp_path):
        gone = tmp_path / "gone-repo"  # under tmp, not HOME
        rid = mc.db.add_repo(str(gone))
        task = mc.db.get_task(_insert_task(mc.db, name="proj", repo_id=rid))
        ref, warns = portability._classify_repo(mc.db, task)
        assert ref["kind"] == "anchor"
        assert any("missing on disk" in w for w in warns)

    def test_local_path_origin_is_anchor_not_fake_remote(self, mc, tmp_path):
        repo = _committed_repo(tmp_path / "r", remote="/tmp/some/local/mirror")
        rid = mc.db.add_repo(str(repo))
        task = mc.db.get_task(_insert_task(mc.db, name="proj", repo_id=rid))
        ref, warns = portability._classify_repo(mc.db, task)
        assert ref["kind"] == "anchor"
        assert any("local path" in w for w in warns)

    def test_file_url_origin_is_anchor_not_fake_remote(self, mc, tmp_path):
        # file:// is a URL but addresses the local filesystem - must not become
        # a portable git key.
        repo = _committed_repo(tmp_path / "r", remote="file:///tmp/mirror/repo")
        rid = mc.db.add_repo(str(repo))
        task = mc.db.get_task(_insert_task(mc.db, name="proj", repo_id=rid))
        ref, warns = portability._classify_repo(mc.db, task)
        assert ref["kind"] == "anchor"
        assert any("local path" in w for w in warns)

    def test_git_not_on_path_warns_distinctly(self, mc, tmp_path, monkeypatch):
        repo = _committed_repo(tmp_path / "r")
        rid = mc.db.add_repo(str(repo))
        task = mc.db.get_task(_insert_task(mc.db, name="proj", repo_id=rid))
        monkeypatch.setattr(portability.shutil, "which", lambda _: None)
        ref, warns = portability._classify_repo(mc.db, task)
        assert ref["kind"] in ("anchor", "home-relative")
        assert any("git not found" in w for w in warns)


# ============ CLI branch (test-analyst critical gap) ============


@pytest.fixture
def cli_root(tmp_path):
    """A real on-disk MissionCache root the export CLI can run against.

    The CLI subprocess resolves its root from the MISSIONCACHE_ROOT env var, so
    the project must live as real files + a real tasks.db (the in-process `mc`
    monkeypatch does not cross the process boundary).
    """
    root = tmp_path / "mc-cli"
    (root / "active").mkdir(parents=True)
    db = TaskDB(db_path=root / "tasks.db")
    db.initialize()
    name = "cli-proj"
    pdir = root / "active" / name
    pdir.mkdir()
    for rel, content in _default_md(name).items():
        (pdir / rel).write_text(content)
    _insert_task(db, name=name)
    # A dir-only project (no DB row) to exercise the warning -> stderr path.
    dironly = root / "active" / "dir-only"
    dironly.mkdir()
    for rel, content in _default_md("dir-only").items():
        (dironly / rel).write_text(content)
    return root, name


def _run_cli(root, *args):
    env = {**os.environ, "MISSIONCACHE_ROOT": str(root)}
    code = "from missioncache_db import main; main()"
    return subprocess.run(
        [sys.executable, "-c", code, *args],
        capture_output=True, text=True, env=env,
    )


class TestCLI:
    def test_not_found_exits_1_with_stderr_message(self, cli_root, tmp_path):
        root, _ = cli_root
        r = _run_cli(root, "export", "no-such-proj", "--out", str(tmp_path / "b"))
        assert r.returncode == 1
        assert "export failed" in r.stderr

    def test_json_flag_prints_manifest_to_stdout(self, cli_root, tmp_path):
        root, name = cli_root
        r = _run_cli(root, "export", name, "--out", str(tmp_path / "b"),
                     "--no-time", "--json")
        assert r.returncode == 0
        data = json.loads(r.stdout)  # stdout is the manifest, nothing else
        assert data["manifest_version"] == 1
        assert data["project"]["name"] == name

    def test_warnings_go_to_stderr_not_stdout(self, cli_root, tmp_path):
        root, _ = cli_root
        r = _run_cli(root, "export", "dir-only", "--out", str(tmp_path / "b"))
        assert r.returncode == 0
        assert "warning:" in r.stderr
        assert "no tasks row" in r.stderr
        assert "warning:" not in r.stdout

    def test_usage_when_no_name(self, cli_root):
        root, _ = cli_root
        r = _run_cli(root, "export")
        assert r.returncode == 1
        assert "Usage" in r.stdout  # the usage line prints to stdout


@pytest.fixture
def cli_import_env(tmp_path, monkeypatch):
    """Real on-disk bundles + a fresh machine-B root for the import CLI subprocess.

    Bundles are built in-process (export just writes files); the monkeypatched
    MISSIONCACHE_ROOT only affects this process, while the import subprocess reads
    its own root from the MISSIONCACHE_ROOT env var (`_run_cli`).
    """
    root_a = tmp_path / "A" / ".missioncache"
    (root_a / "active").mkdir(parents=True)
    db_a = TaskDB(db_path=root_a / "tasks.db")
    db_a.initialize()
    mc_a = SimpleNamespace(root=root_a, db=db_a)
    _activate(monkeypatch, mc_a)  # export reads the module MISSIONCACHE_ROOT

    clean = _build_bundle(mc_a, "clean-proj", tmp_path / "clean-bundle")
    ref_files = _plain_md("ref-proj")
    ref_files["ref-proj-context.md"] = "# context\nHub: [[gone]]\n"
    unresolved = _build_bundle(
        mc_a, "ref-proj", tmp_path / "ref-bundle", files=ref_files
    )
    # A corrupt bundle: valid tree, unsupported manifest_version.
    import shutil as _sh
    corrupt = tmp_path / "corrupt-bundle"
    _sh.copytree(clean, corrupt)
    man_p = corrupt / "missioncache.json"
    man = json.loads(man_p.read_text())
    man["manifest_version"] = 999
    man_p.write_text(json.dumps(man))

    root_b = tmp_path / "B" / ".missioncache"
    (root_b / "active").mkdir(parents=True)
    TaskDB(db_path=root_b / "tasks.db").initialize()
    return SimpleNamespace(
        root_b=root_b, clean=clean, unresolved=unresolved, corrupt=corrupt
    )


class TestImportCLI:
    """The import CLI branch: real process exit codes are the contract a sync
    script keys on (sys.exit(report['exit_code'])), so assert returncode, not
    just the in-process report dict."""

    def test_clean_import_exits_0(self, cli_import_env):
        e = cli_import_env
        r = _run_cli(e.root_b, "import", str(e.clean))
        assert r.returncode == 0
        assert "imported 'clean-proj'" in r.stdout

    def test_unresolved_ref_exits_2(self, cli_import_env):
        e = cli_import_env
        r = _run_cli(e.root_b, "import", str(e.unresolved))
        assert r.returncode == 2  # imported-with-gaps: the sync-script signal
        assert "needs mapping" in r.stdout

    def test_corrupt_bundle_exits_1_with_stderr(self, cli_import_env):
        e = cli_import_env
        r = _run_cli(e.root_b, "import", str(e.corrupt))
        assert r.returncode == 1
        assert "import failed" in r.stderr
        assert "manifest_version" in r.stderr

    def test_json_flag_prints_report(self, cli_import_env):
        e = cli_import_env
        r = _run_cli(e.root_b, "import", str(e.clean), "--json")
        assert r.returncode == 0
        report = json.loads(r.stdout)  # stdout is the report, nothing else
        assert report["name"] == "clean-proj"
        assert report["exit_code"] == 0

    def test_repo_flag_without_path_exits_1(self, cli_import_env):
        e = cli_import_env
        r = _run_cli(e.root_b, "import", str(e.clean), "--repo")
        assert r.returncode == 1
        assert "--repo requires a path argument" in r.stderr

    def test_usage_when_no_bundle(self, cli_import_env):
        e = cli_import_env
        r = _run_cli(e.root_b, "import")
        assert r.returncode == 1
        assert "Usage: missioncache-db import" in r.stdout


# ============ source-dir guards (security) ============


class TestSourceDirGuards:
    def test_symlinked_project_dir_rejected(self, mc, tmp_path):
        # active/<name> itself a symlink to an outside dir -> refused (os.walk
        # would follow the top node and bundle out-of-tree content).
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.md").write_text("LEAK")
        link = mc.root / "active" / "linked-proj"
        link.symlink_to(outside, target_is_directory=True)
        _insert_task(mc.db, name="linked-proj")

        with pytest.raises(ValueError, match="symlink"):
            _export(mc, "linked-proj", out=str(tmp_path / "b"))

    def test_out_path_that_is_a_file_rejected(self, mc, tmp_path):
        name = "proj"
        _seed_files(mc, name, _default_md(name))
        _insert_task(mc.db, name=name)
        existing = tmp_path / "afile"
        existing.write_text("x")
        with pytest.raises(ValueError, match="not a directory"):
            _export(mc, name, out=str(existing))


# ============ parent-name resolution in the manifest (§3) ============


class TestParentResolution:
    def test_parent_name_carried_not_id(self, mc, tmp_path):
        # A child task's parent is exported as the parent's NAME, not its id.
        parent_dir = mc.root / "active" / "parent-proj"
        parent_dir.mkdir()
        for rel, content in _default_md("parent-proj").items():
            (parent_dir / rel).write_text(content)
        ptid = _insert_task(mc.db, name="parent-proj")

        name = "child-proj"
        _seed_files(mc, name, _default_md(name))
        _insert_task(mc.db, name=name, parent_id=ptid)

        m = _export(mc, name, out=str(tmp_path / "b"))["manifest"]
        assert m["project"]["parent"] == "parent-proj"


# ============ walk errors surfaced, not silently swallowed (SF1) ============


class TestWalkErrors:
    def test_unreadable_subtree_warned_not_silent(self, mc, tmp_path):
        if os.getuid() == 0:
            pytest.skip("root bypasses directory permissions")
        name = "proj"
        pdir = _seed_files(mc, name, _default_md(name))
        blocked = pdir / "blocked"
        blocked.mkdir()
        (blocked / "x.md").write_text("hidden")
        os.chmod(blocked, 0o000)
        try:
            report = _export(mc, name, out=str(tmp_path / "b"))
        finally:
            os.chmod(blocked, 0o755)  # restore so tmp cleanup works
        assert any("omitted from bundle" in w for w in report["warnings"])


# ===========================================================================
# Import (Phase 3): place files, reconcile refs, name-keyed upsert, 3-bucket
# alignment report. Spec: docs/cross-machine-sharing-plan.md sections 5/6/7/9
# and the §8 test plan. Every assertion traces to that contract.
# ===========================================================================


HTTPS_REMOTE = "https://github.com/AkamaiETP/logic-automation-python"


@pytest.fixture
def mc_b(tmp_path):
    """A second throwaway MissionCache root + DB - the import target (machine B).

    Its DB path is explicit, so it stays bound to B regardless of which root the
    live module constant points at. Tests activate B (repoint the module
    constants) right before calling import_bundle.
    """
    root = tmp_path / "B" / ".missioncache"
    (root / "active").mkdir(parents=True)
    db = TaskDB(db_path=root / "tasks.db")
    db.initialize()
    return SimpleNamespace(root=root, db=db)


def _activate(monkeypatch, machine):
    """Point the live module constants at ``machine``'s root (export/import read
    them dynamically)."""
    monkeypatch.setattr(missioncache_db, "MISSIONCACHE_ROOT", machine.root)
    monkeypatch.setattr(machine_map, "MACHINE_FILE", machine.root / "machine.json")


def _plain_md(name):
    """Markdown with no Hub: line and no embedded paths (clean round-trip)."""
    return {
        f"{name}-plan.md": "# plan\n",
        f"{name}-context.md": "# context\nplain notes here\n",
        f"{name}-tasks.md": "# tasks\n- [ ] 1. do the thing\n",
    }


def _build_bundle(mc, name, dest, *, files=None, **task_kw):
    """Seed files + a task row on machine A and export a bundle. Returns its path."""
    _seed_files(mc, name, files if files is not None else _plain_md(name))
    _insert_task(mc.db, name=name, **task_kw)
    return _export(mc, name, out=str(dest))["bundle_path"]


def _import(machine, monkeypatch, bundle, **kw):
    _activate(monkeypatch, machine)
    return portability.import_bundle(machine.db, bundle, **kw)


def _buckets(report):
    return (report["resolved"], report["needs_mapping"], report["missing"])


def _rehash_bundle(bundle):
    """Recompute the manifest files[] checksums from the current bundle files.

    A real second export re-hashes; a test that hand-edits a bundle file must do
    the same or the import-side integrity check (which verifies files[] against
    the bundle) will reject it as corrupt. Hashes the EOL-normalized bytes, the
    same way export records and import verifies them.
    """
    bp = Path(bundle)
    man_path = bp / "missioncache.json"
    man = json.loads(man_path.read_text())
    files_root = bp / "files"
    for f in man["files"]:
        data = (files_root / f["path"]).read_bytes()
        if f["path"].lower().endswith((".md", ".json")):
            data = data.replace(b"\r\n", b"\n")
        f["sha256"] = hashlib.sha256(data).hexdigest()
    man_path.write_text(json.dumps(man))


def _stable_home(monkeypatch, home):
    """Pin Path.home() so anchor-vs-home-relative classification is deterministic.

    Repo-classification calls Path.home(); on CI where TMPDIR sits under $HOME a
    tmp repo would classify home-relative instead of anchor. Pinning home to a
    dir that is never a parent of the test's tmp repos removes that fragility.
    """
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))


# ============ case 1: round-trip ============


class TestImportRoundTrip:
    def test_full_round_trip_resolves(self, mc, mc_b, tmp_path, monkeypatch):
        name = "finance-dashboard"
        repo_a = _committed_repo(tmp_path / "A-repo")
        rid = mc.db.add_repo(str(repo_a))
        files = _plain_md(name)
        files["research/02-map.md"] = "# map\n"
        files["prompts/task-01-prompt.md"] = "do it\n"
        bundle = _build_bundle(
            mc, name, tmp_path / "bundle", files=files, repo_id=rid,
            status="paused", tags=["dashboard", "finance"], priority=2,
            jira_key="GC-1234", branch="feature/x", pr_url="https://e/pr/1",
        )
        # machine B has the same remote at a different local path, mapped.
        repo_b = _committed_repo(tmp_path / "B-repo")
        _activate(monkeypatch, mc_b)
        machine_map.record("repo", SSH_REMOTE, str(repo_b))

        report = portability.import_bundle(mc_b.db, bundle)

        assert report["exit_code"] == 0
        assert report["action"] == "created"
        assert report["needs_mapping"] == []
        assert report["missing"] == []
        row = mc_b.db.get_task_by_name(name)
        assert row is not None
        assert row.status == "paused"
        assert row.jira_key == "GC-1234"
        assert row.branch == "feature/x"
        assert row.tags == ["dashboard", "finance"]
        # 3 md + research/ + prompts/ all land under B's active/<name>/
        landing = mc_b.root / "active" / name
        assert (landing / f"{name}-plan.md").is_file()
        assert (landing / "research" / "02-map.md").is_file()
        assert (landing / "prompts" / "task-01-prompt.md").is_file()
        repo_entries = [e for e in report["resolved"] if e["kind"] == "repo"]
        assert repo_entries and repo_entries[0]["bucket"] == "resolved"
        assert row.repo_id is not None


# ============ category portability ============


class TestCategoryPortability:
    """category rides the bundle like origin_uuid does (the lockstep invariant
    on _IMPORT_TASK_UPDATE_SQL)."""

    def test_export_manifest_carries_category(self, mc, tmp_path):
        name = "categorized-export"
        _seed_files(mc, name, _plain_md(name))
        _insert_task(mc.db, name=name, category="ui")
        bundle = _export(mc, name, out=str(tmp_path / "bundle"))["bundle_path"]
        man = json.loads((Path(bundle) / "missioncache.json").read_text())
        assert man["project"]["category"] == "ui"

    def test_category_round_trips_on_import(self, mc, mc_b, tmp_path, monkeypatch):
        name = "categorized-trip"
        bundle = _build_bundle(mc, name, tmp_path / "bundle", category="infra")
        report = _import(mc_b, monkeypatch, bundle)
        assert report["exit_code"] == 0
        assert mc_b.db.get_task_by_name(name).category == "infra"

    def test_unknown_bundle_category_imports_as_null_with_warning(
        self, mc, mc_b, tmp_path, monkeypatch
    ):
        """A bundle is untrusted input: an out-of-taxonomy category degrades
        to uncategorized instead of failing the import or storing garbage."""
        name = "hostile-category"
        bundle = _build_bundle(mc, name, tmp_path / "bundle")
        man_path = Path(bundle) / "missioncache.json"
        man = json.loads(man_path.read_text())
        man["project"]["category"] = "<img src=x onerror=alert(1)>"
        man_path.write_text(json.dumps(man))

        report = _import(mc_b, monkeypatch, bundle)

        assert report["exit_code"] == 0
        assert mc_b.db.get_task_by_name(name).category is None
        assert any("taxonomy" in w for w in report["warnings"])

    def test_null_bundle_category_preserves_local_on_reimport(
        self, mc, mc_b, tmp_path, monkeypatch
    ):
        """An incoming NULL is ambiguous (pre-category bundle), so a re-import
        must not clear a category set locally on the target machine."""
        name = "locally-categorized"
        bundle = _build_bundle(mc, name, tmp_path / "bundle")  # no category
        report = _import(mc_b, monkeypatch, bundle)
        assert report["exit_code"] == 0
        local = mc_b.db.get_task_by_name(name)
        assert local.category is None
        mc_b.db.set_task_category(local.id, "docs")

        report = _import(mc_b, monkeypatch, bundle, force=True)

        assert report["exit_code"] == 0
        assert mc_b.db.get_task_by_name(name).category == "docs"


# ============ case 11: dry-run writes nothing ============


class TestImportDryRun:
    def test_dry_run_writes_nothing_but_reports(self, mc, mc_b, tmp_path, monkeypatch):
        name = "proj"
        bundle = _build_bundle(mc, name, tmp_path / "bundle")
        report = _import(mc_b, monkeypatch, bundle, dry_run=True)

        assert mc_b.db.get_task_by_name(name) is None
        assert not (mc_b.root / "active" / name).exists()
        resolved, _, _ = _buckets(report)
        # report is still fully populated (files, db-row, time lines present)
        kinds = {e["kind"] for e in resolved}
        assert {"files", "db-row", "time"} <= kinds


# ============ case 6: idempotent re-import ============


class TestImportIdempotent:
    def test_re_import_is_one_row_updated(self, mc, mc_b, tmp_path, monkeypatch):
        name = "proj"
        bundle = _build_bundle(mc, name, tmp_path / "bundle")

        r1 = _import(mc_b, monkeypatch, bundle)
        assert r1["action"] == "created"
        first_id = mc_b.db.get_task_by_name(name).id

        r2 = _import(mc_b, monkeypatch, bundle)
        assert r2["action"] == "updated"
        assert mc_b.db.get_task_by_name(name).id == first_id
        with mc_b.db.connection() as conn:
            count = conn.execute(
                "SELECT COUNT(*) c FROM tasks WHERE name = ?", (name,)
            ).fetchone()["c"]
        assert count == 1


# ============ placement failure rolls the DB write back ============


class TestImportPlacementFailureRollback:
    """A failed _place_files must not leave the committed upsert behind -
    exit 1 promises nothing was committed (the Codex 2026-07-10 finding)."""

    @staticmethod
    def _break_placement(monkeypatch):
        def boom(src_dir, landing_dir):
            raise OSError("disk full")
        monkeypatch.setattr(portability, "_place_files", boom)

    def test_created_row_removed_when_placement_fails(
        self, mc, mc_b, tmp_path, monkeypatch
    ):
        name = "proj"
        bundle = _build_bundle(mc, name, tmp_path / "bundle")
        self._break_placement(monkeypatch)

        report = _import(mc_b, monkeypatch, bundle)

        assert report["exit_code"] == 1
        assert any("file placement failed" in e for e in report["errors"])
        assert any("rolled back" in n for n in report["notes"])
        assert report["task_id"] is None
        assert mc_b.db.get_task_by_name(name) is None
        assert not (mc_b.root / "active" / name).exists()

    def test_updated_row_restored_when_placement_fails(
        self, mc, mc_b, tmp_path, monkeypatch
    ):
        name = "proj"
        bundle = _build_bundle(
            mc, name, tmp_path / "bundle", status="paused", jira_key="GC-1"
        )
        r1 = _import(mc_b, monkeypatch, bundle)
        assert r1["action"] == "created"
        pre_row = mc_b.db.get_task_by_name(name)

        # a real second export from A with changed DB fields AND changed files
        _activate(monkeypatch, mc)
        with mc.db.connection() as conn:
            conn.execute(
                "UPDATE tasks SET jira_key='GC-2', status='active' WHERE name=?",
                (name,),
            )
            conn.commit()
        (mc.root / "active" / name / f"{name}-context.md").write_text(
            "# context\nchanged on A\n"
        )
        bundle2 = _export(mc, name, out=str(tmp_path / "bundle2"))["bundle_path"]

        self._break_placement(monkeypatch)
        r2 = _import(mc_b, monkeypatch, bundle2, force=True)

        assert r2["exit_code"] == 1
        row = mc_b.db.get_task_by_name(name)
        assert row.id == pre_row.id
        assert row.jira_key == pre_row.jira_key      # pre-image restored
        assert row.status == pre_row.status
        # the previously placed tree is untouched (staging swap never ran)
        landed = mc_b.root / "active" / name / f"{name}-context.md"
        assert landed.read_text() == "# context\nplain notes here\n"

    def test_rollback_failure_warns_but_keeps_placement_error(
        self, mc, mc_b, tmp_path, monkeypatch
    ):
        name = "proj"
        bundle = _build_bundle(mc, name, tmp_path / "bundle")
        self._break_placement(monkeypatch)

        def raise_locked(*a, **k):
            raise RuntimeError("db locked")
        monkeypatch.setattr(mc_b.db, "rollback_imported_task", raise_locked)

        report = _import(mc_b, monkeypatch, bundle)

        assert report["exit_code"] == 1
        assert any("file placement failed" in e for e in report["errors"])
        assert any("could NOT roll back" in w for w in report["warnings"])


# ============ case 7: field fidelity (DB-only fields survive) ============


class TestImportFieldFidelity:
    def test_db_only_fields_survive(self, mc, mc_b, tmp_path, monkeypatch):
        name = "proj"
        bundle = _build_bundle(
            mc, name, tmp_path / "bundle",
            task_type="non-coding", priority=3, tags=["x"],
        )
        _import(mc_b, monkeypatch, bundle)
        row = mc_b.db.get_task_by_name(name)
        assert row.task_type == "non-coding"
        assert row.priority == 3
        assert row.tags == ["x"]


# ============ case 3: name-collision + force ============


class TestImportCollision:
    def test_same_project_differs_aborts_without_force(self, mc, mc_b, tmp_path, monkeypatch):
        name = "proj"
        bundle = _build_bundle(mc, name, tmp_path / "bundle")
        _import(mc_b, monkeypatch, bundle)
        before_id = mc_b.db.get_task_by_name(name).id

        # a real second export with changed content: edit the file AND re-hash
        # the manifest so the bundle stays self-consistent (not "corrupt").
        ctx = Path(bundle) / "files" / name / f"{name}-context.md"
        ctx.write_text("# context\nEDITED on A\n")
        _rehash_bundle(bundle)

        report = _import(mc_b, monkeypatch, bundle)
        assert report["exit_code"] == 1
        assert report["errors"]
        # nothing changed: row id same, on-disk file still the original content
        assert mc_b.db.get_task_by_name(name).id == before_id
        landing_ctx = mc_b.root / "active" / name / f"{name}-context.md"
        assert "EDITED on A" not in landing_ctx.read_text()

    def test_same_project_differs_force_updates_in_place(self, mc, mc_b, tmp_path, monkeypatch):
        name = "proj"
        bundle = _build_bundle(mc, name, tmp_path / "bundle")
        _import(mc_b, monkeypatch, bundle)
        before_id = mc_b.db.get_task_by_name(name).id

        ctx = Path(bundle) / "files" / name / f"{name}-context.md"
        ctx.write_text("# context\nEDITED on A\n")
        _rehash_bundle(bundle)

        report = _import(mc_b, monkeypatch, bundle, force=True)
        assert report["action"] == "updated"
        assert mc_b.db.get_task_by_name(name).id == before_id  # .id preserved
        landing_ctx = mc_b.root / "active" / name / f"{name}-context.md"
        assert "EDITED on A" in landing_ctx.read_text()  # files overwritten

    def test_different_project_same_name_never_clobbered_even_force(
        self, mc, mc_b, tmp_path, monkeypatch
    ):
        name = "proj"
        # Bundle 1: project from remote X.
        repo_x = _committed_repo(tmp_path / "repoX", remote=SSH_REMOTE)
        rid_x = mc.db.add_repo(str(repo_x))
        b1 = _build_bundle(mc, name, tmp_path / "b1", repo_id=rid_x)
        # Map X on B and import it so a concrete bound row exists.
        repo_xb = _committed_repo(tmp_path / "repoXB", remote=SSH_REMOTE)
        _activate(monkeypatch, mc_b)
        machine_map.record("repo", SSH_REMOTE, str(repo_xb))
        portability.import_bundle(mc_b.db, b1)
        bound_id = mc_b.db.get_task_by_name(name).id
        bound_repo = mc_b.db.get_task_by_name(name).repo_id
        assert bound_repo is not None

        # Bundle 2: a DIFFERENT project, same name, from remote Y. Re-activate A
        # so the export reads machine A's root (importing b1 switched it to B).
        _activate(monkeypatch, mc)
        other_remote = "git@github.com:AkamaiETP/some-other-repo.git"
        repo_y = _committed_repo(tmp_path / "repoY", remote=other_remote)
        rid_y = mc.db.add_repo(str(repo_y))
        # build under a fresh A-machine name slot then rename in manifest is hard;
        # instead seed a second project dir + row and export it.
        b2 = _build_bundle(mc, name + "-y", tmp_path / "b2", repo_id=rid_y)
        # Rewrite the manifest so it claims the SAME project name/full_path.
        man_path = Path(b2) / "missioncache.json"
        man = json.loads(man_path.read_text())
        man["project"]["name"] = name
        man["project"]["full_path"] = f"active/{name}"
        # the files dir + files[] paths must match the claimed name, and the
        # manifest must stay self-consistent (else it fails the integrity check
        # before reaching collision classification - the wrong reason).
        os.rename(Path(b2) / "files" / (name + "-y"), Path(b2) / "files" / name)
        for f in man["files"]:
            f["path"] = f["path"].replace(f"{name}-y/", f"{name}/", 1)
        man_path.write_text(json.dumps(man))
        _rehash_bundle(b2)

        report = portability.import_bundle(mc_b.db, b2, force=True)
        assert report["exit_code"] == 1  # force must NOT destroy a different project
        assert report["errors"]
        # original row untouched
        still = mc_b.db.get_task_by_name(name)
        assert still.id == bound_id
        assert still.repo_id == bound_repo

    def test_different_project_same_name_aborts_without_force(
        self, mc, mc_b, tmp_path, monkeypatch
    ):
        # The §5 conflict table lists different-project-collision as ABORT both
        # with AND without --force. The force variant is above; this pins the
        # force-absent row of the table (the classifier returns abort_different
        # before --force is even consulted).
        name = "proj"
        repo_x = _committed_repo(tmp_path / "repoX", remote=SSH_REMOTE)
        rid_x = mc.db.add_repo(str(repo_x))
        b1 = _build_bundle(mc, name, tmp_path / "b1", repo_id=rid_x)
        repo_xb = _committed_repo(tmp_path / "repoXB", remote=SSH_REMOTE)
        _activate(monkeypatch, mc_b)
        machine_map.record("repo", SSH_REMOTE, str(repo_xb))
        portability.import_bundle(mc_b.db, b1)
        bound_id = mc_b.db.get_task_by_name(name).id

        _activate(monkeypatch, mc)
        other_remote = "git@github.com:AkamaiETP/some-other-repo.git"
        repo_y = _committed_repo(tmp_path / "repoY", remote=other_remote)
        rid_y = mc.db.add_repo(str(repo_y))
        b2 = _build_bundle(mc, name + "-y", tmp_path / "b2", repo_id=rid_y)
        man_path = Path(b2) / "missioncache.json"
        man = json.loads(man_path.read_text())
        man["project"]["name"] = name
        man["project"]["full_path"] = f"active/{name}"
        os.rename(Path(b2) / "files" / (name + "-y"), Path(b2) / "files" / name)
        for f in man["files"]:
            f["path"] = f["path"].replace(f"{name}-y/", f"{name}/", 1)
        man_path.write_text(json.dumps(man))
        _rehash_bundle(b2)

        report = portability.import_bundle(mc_b.db, b2)  # NO force
        assert report["exit_code"] == 1
        assert any("DIFFERENT project" in e for e in report["errors"])
        assert mc_b.db.get_task_by_name(name).id == bound_id


# ============ case 4: non-git repo -> anchor needs-mapping then resolves ============


class TestImportNonGitRepo:
    def test_anchor_needs_mapping_then_resolves(self, mc, mc_b, tmp_path, monkeypatch):
        name = "proj"
        plain_a = tmp_path / "plainA"
        plain_a.mkdir()  # a non-git tracked folder
        rid = mc.db.add_repo(str(plain_a))
        bundle = _build_bundle(mc, name, tmp_path / "bundle", repo_id=rid)
        man = json.loads((Path(bundle) / "missioncache.json").read_text())
        assert man["references"]["repo"]["kind"] == "anchor"

        # First import: no anchor mapped -> needs-mapping, row imported repo_id NULL.
        r1 = _import(mc_b, monkeypatch, bundle)
        repo_nm = [e for e in r1["needs_mapping"] if e["kind"] == "repo"]
        assert repo_nm and "config set-path anchor:plainA" in repo_nm[0]["hint"]
        assert mc_b.db.get_task_by_name(name).repo_id is None

        # Map the anchor (same basename) and re-import -> resolved, repo_id bound.
        plain_b = tmp_path / "B" / "plainA"
        plain_b.mkdir(parents=True)
        machine_map.record("anchor", "plainA", str(plain_b))
        r2 = _import(mc_b, monkeypatch, bundle)
        repo_res = [e for e in r2["resolved"] if e["kind"] == "repo"]
        assert repo_res and repo_res[0]["bucket"] == "resolved"
        assert mc_b.db.get_task_by_name(name).repo_id is not None


# ============ case 5: missing-ref flagging + exit 2 ============


class TestImportMissingRefs:
    def test_missing_vault_and_unmapped_anchor(self, mc, mc_b, tmp_path, monkeypatch):
        name = "proj"
        plain_a = tmp_path / "claude-plans"
        plain_a.mkdir()
        rid = mc.db.add_repo(str(plain_a))
        files = _plain_md(name)
        files[f"{name}-context.md"] = "# context\nHub: [[somenote]]\n"
        bundle = _build_bundle(mc, name, tmp_path / "bundle", files=files, repo_id=rid)

        # B: map a vault root that does NOT contain somenote.md; leave anchor unmapped.
        vault_b = tmp_path / "B" / "vault"
        vault_b.mkdir(parents=True)
        _activate(monkeypatch, mc_b)
        machine_map.record("vault", "TomerWork", str(vault_b))

        report = portability.import_bundle(mc_b.db, bundle)
        assert report["exit_code"] == 2
        vault_miss = [e for e in report["missing"] if e["kind"] == "vault"]
        assert vault_miss and vault_miss[0]["id"] == "somenote"
        repo_nm = [e for e in report["needs_mapping"] if e["kind"] == "repo"]
        assert repo_nm and "config set-path anchor:claude-plans" in repo_nm[0]["hint"]

    def test_vault_wikilink_resolves_to_mapped_root(self, mc, mc_b, tmp_path, monkeypatch):
        # Happy path: the note exists under a mapped vault root -> resolved bucket,
        # exit 0. This is the found-branch of _resolve_vaults / _find_note.
        name = "proj"
        files = _plain_md(name)
        files[f"{name}-context.md"] = "# context\nHub: [[somenote]]\n"
        bundle = _build_bundle(mc, name, tmp_path / "bundle", files=files)

        vault_b = tmp_path / "B" / "vault"
        (vault_b / "sub").mkdir(parents=True)
        (vault_b / "sub" / "somenote.md").write_text("# note\n")
        _activate(monkeypatch, mc_b)
        machine_map.record("vault", "TomerWork", str(vault_b))

        report = portability.import_bundle(mc_b.db, bundle)
        assert report["exit_code"] == 0
        vault_ok = [e for e in report["resolved"] if e["kind"] == "vault"]
        assert vault_ok and vault_ok[0]["id"] == "somenote"
        assert vault_ok[0]["local"] == str(vault_b / "sub" / "somenote.md")


# ============ case 2: embedded path translation + --rewrite-paths ============


class TestImportPathTranslation:
    def _vault_bundle(self, mc, monkeypatch, tmp_path):
        name = "proj"
        vault_a = tmp_path / "vaultA"
        (vault_a / "Resources").mkdir(parents=True)
        (vault_a / "Resources" / "qa.md").write_text("note\n")
        _activate(monkeypatch, mc)
        machine_map.record("vault", "TomerWork", str(vault_a))
        ref = f"{vault_a}/Resources/qa.md"
        files = _plain_md(name)
        files[f"{name}-context.md"] = f"# context\nvault note: {ref}\n"
        bundle = _build_bundle(mc, name, tmp_path / "bundle", files=files)
        man = json.loads((Path(bundle) / "missioncache.json").read_text())
        toks = [op["token"] for op in man["references"]["other_paths"]]
        assert any(t == "${vault:TomerWork}/Resources/qa.md" for t in toks)
        return name, bundle

    def test_rewrite_paths_rewrites_to_b_path(self, mc, mc_b, tmp_path, monkeypatch):
        name, bundle = self._vault_bundle(mc, monkeypatch, tmp_path)
        vault_b = tmp_path / "B" / "vaultB"
        (vault_b / "Resources").mkdir(parents=True)
        (vault_b / "Resources" / "qa.md").write_text("note\n")
        _activate(monkeypatch, mc_b)
        machine_map.record("vault", "TomerWork", str(vault_b))

        report = portability.import_bundle(mc_b.db, bundle, rewrite=True)
        ctx = (mc_b.root / "active" / name / f"{name}-context.md").read_text()
        assert str(vault_b) in ctx
        assert str(tmp_path / "vaultA") not in ctx
        emb = [e for e in report["resolved"] if e["kind"] == "embedded-path"]
        assert emb

    def test_without_rewrite_markdown_unchanged_but_classified(
        self, mc, mc_b, tmp_path, monkeypatch
    ):
        name, bundle = self._vault_bundle(mc, monkeypatch, tmp_path)
        vault_b = tmp_path / "B" / "vaultB"
        (vault_b / "Resources").mkdir(parents=True)
        (vault_b / "Resources" / "qa.md").write_text("note\n")
        _activate(monkeypatch, mc_b)
        machine_map.record("vault", "TomerWork", str(vault_b))

        report = portability.import_bundle(mc_b.db, bundle, rewrite=False)
        ctx = (mc_b.root / "active" / name / f"{name}-context.md").read_text()
        # markdown untouched: still holds A's path verbatim
        assert str(tmp_path / "vaultA") in ctx
        emb = [e for e in report["resolved"] if e["kind"] == "embedded-path"]
        assert emb  # still classified/reported


# ============ case 8: subtask parent reconcile ============


class TestImportParent:
    def test_parent_present_binds_local_id(self, mc, mc_b, tmp_path, monkeypatch):
        # parent + child on A
        _seed_files(mc, "parent-proj", _plain_md("parent-proj"))
        ptid = _insert_task(mc.db, name="parent-proj")
        parent_bundle = _export(mc, "parent-proj", out=str(tmp_path / "pb"))["bundle_path"]
        child_bundle = _build_bundle(
            mc, "child-proj", tmp_path / "cb", parent_id=ptid
        )
        man = json.loads((Path(child_bundle) / "missioncache.json").read_text())
        assert man["project"]["parent"] == "parent-proj"

        # B: import parent first, then child -> child.parent_id == B parent.id
        _import(mc_b, monkeypatch, parent_bundle)
        b_parent_id = mc_b.db.get_task_by_name("parent-proj").id
        report = _import(mc_b, monkeypatch, child_bundle)
        child = mc_b.db.get_task_by_name("child-proj")
        assert child.parent_id == b_parent_id
        assert all(e["kind"] != "parent" for e in report["needs_mapping"])

    def test_parent_absent_needs_mapping_null(self, mc, mc_b, tmp_path, monkeypatch):
        _seed_files(mc, "parent-proj", _plain_md("parent-proj"))
        ptid = _insert_task(mc.db, name="parent-proj")
        child_bundle = _build_bundle(mc, "child-proj", tmp_path / "cb", parent_id=ptid)

        report = _import(mc_b, monkeypatch, child_bundle)
        child = mc_b.db.get_task_by_name("child-proj")
        assert child.parent_id is None
        parent_nm = [e for e in report["needs_mapping"] if e["kind"] == "parent"]
        assert parent_nm and parent_nm[0]["id"] == "parent-proj"


# ============ case 9: EOL normalization ============


class TestImportEolNormalization:
    def test_crlf_bundle_lands_lf(self, mc, mc_b, tmp_path, monkeypatch):
        name = "proj"
        bundle = _build_bundle(mc, name, tmp_path / "bundle")
        # CRLF-ify the SAME content (a transit munge), so the EOL-normalized
        # integrity check still matches; only line endings change.
        f = Path(bundle) / "files" / name / f"{name}-context.md"
        original = f.read_bytes()
        f.write_bytes(original.replace(b"\n", b"\r\n"))

        _import(mc_b, monkeypatch, bundle)
        landed = (mc_b.root / "active" / name / f"{name}-context.md").read_bytes()
        assert b"\r" not in landed
        assert landed == original  # content preserved, only EOL normalized


# ============ case 10: /mnt DrvFs warning, import still completes ============


class TestImportDrvfsWarning:
    def test_drvfs_warns_but_completes(self, mc, mc_b, tmp_path, monkeypatch):
        name = "proj"
        bundle = _build_bundle(mc, name, tmp_path / "bundle")
        # Force the DrvFs detection on without an actual /mnt mount.
        monkeypatch.setattr(portability, "_under_drvfs", lambda root: True)
        report = _import(mc_b, monkeypatch, bundle)
        assert any("DrvFs" in w for w in report["warnings"])
        assert mc_b.db.get_task_by_name(name) is not None  # still completed


# ============ case 13: time carried, not seeded ============


class TestImportTimeNotSeeded:
    def test_origin_time_reported_zero_sessions(self, mc, mc_b, tmp_path, monkeypatch):
        name = "proj"
        _seed_files(mc, name, _plain_md(name))
        tid = _insert_task(mc.db, name=name)
        _insert_session(mc.db, tid, 184320)
        bundle = _export(mc, name, out=str(tmp_path / "bundle"))["bundle_path"]
        man = json.loads((Path(bundle) / "missioncache.json").read_text())
        assert man["project"]["time_total_seconds"] == 184320

        report = _import(mc_b, monkeypatch, bundle)
        assert report["time_origin_seconds"] == 184320
        b_id = mc_b.db.get_task_by_name(name).id
        assert mc_b.db.get_task_time(b_id, "all") == 0  # B starts at zero


# ============ case 14: DuckDB layering ============


class TestImportDuckDbLayering:
    def test_no_dashboard_succeeds_with_note(self, mc, mc_b, tmp_path, monkeypatch):
        name = "proj"
        bundle = _build_bundle(mc, name, tmp_path / "bundle")
        # point the sync poke at a closed port so the dashboard is "unreachable"
        monkeypatch.setattr(portability, "_SYNC_URL", "http://127.0.0.1:9/api/sync")
        report = _import(mc_b, monkeypatch, bundle)
        assert mc_b.db.get_task_by_name(name) is not None
        assert any("DuckDB" in n for n in report["notes"])

    def test_portability_never_imports_dashboard(self):
        src = Path(portability.__file__).read_text()
        assert "import missioncache_dashboard" not in src
        assert "from missioncache_dashboard" not in src


# ============ case 17: worktree binding forced to needs-mapping ============


class TestImportWorktreeBinding:
    def test_worktree_ref_forces_needs_mapping(self, mc, mc_b, tmp_path, monkeypatch):
        name = "proj"
        repo_a = _committed_repo(tmp_path / "A-repo")
        rid = mc.db.add_repo(str(repo_a))
        bundle = _build_bundle(mc, name, tmp_path / "bundle", repo_id=rid)
        # mark the repo ref as a linked worktree in the manifest
        man_path = Path(bundle) / "missioncache.json"
        man = json.loads(man_path.read_text())
        man["references"]["repo"]["worktree"] = True
        man_path.write_text(json.dumps(man))
        # even with the remote mapped, a worktree ref must not bind the parent
        repo_b = _committed_repo(tmp_path / "B-repo")
        _activate(monkeypatch, mc_b)
        machine_map.record("repo", SSH_REMOTE, str(repo_b))

        report = portability.import_bundle(mc_b.db, bundle)
        wt = [e for e in report["needs_mapping"] if e["kind"] == "repo(worktree)"]
        assert wt
        assert mc_b.db.get_task_by_name(name).repo_id is None


# ============ case 18: created_at round-trip (min on update) ============


class TestImportCreatedAt:
    def test_created_at_preserved_and_min_on_update(self, mc, mc_b, tmp_path, monkeypatch):
        name = "proj"
        bundle = _build_bundle(
            mc, name, tmp_path / "bundle", created_at="2026-05-02T09:11:00"
        )
        _import(mc_b, monkeypatch, bundle)
        assert mc_b.db.get_task_by_name(name).created_at == "2026-05-02T09:11:00"

        # re-import with a LATER created_at + differing content + force:
        # the EARLIER (existing) created_at is kept.
        man_path = Path(bundle) / "missioncache.json"
        man = json.loads(man_path.read_text())
        man["project"]["created_at"] = "2026-08-01T00:00:00"
        man_path.write_text(json.dumps(man))
        (Path(bundle) / "files" / name / f"{name}-plan.md").write_text("# plan v2\n")
        _rehash_bundle(bundle)
        _import(mc_b, monkeypatch, bundle, force=True)
        assert mc_b.db.get_task_by_name(name).created_at == "2026-05-02T09:11:00"


# ============ validation hard-failures (§5 step 1) ============


class TestImportValidation:
    def test_bad_manifest_version_aborts(self, mc, mc_b, tmp_path, monkeypatch):
        name = "proj"
        bundle = _build_bundle(mc, name, tmp_path / "bundle")
        man_path = Path(bundle) / "missioncache.json"
        man = json.loads(man_path.read_text())
        man["manifest_version"] = 999
        man_path.write_text(json.dumps(man))
        report = _import(mc_b, monkeypatch, bundle)
        assert report["exit_code"] == 1
        assert any("manifest_version" in e for e in report["errors"])
        assert mc_b.db.get_task_by_name(name) is None

    def test_missing_bundle_aborts(self, mc_b, tmp_path, monkeypatch):
        report = _import(mc_b, monkeypatch, str(tmp_path / "nope.bundle"))
        assert report["exit_code"] == 1
        assert report["errors"]


# ============ case 12: remote_key normalization across ssh/https ============


class TestImportRemoteKeyNormalization:
    def test_ssh_export_https_map_resolves(self, mc, mc_b, tmp_path, monkeypatch):
        name = "proj"
        repo_a = _committed_repo(tmp_path / "A-repo", remote=SSH_REMOTE)
        rid = mc.db.add_repo(str(repo_a))
        bundle = _build_bundle(mc, name, tmp_path / "bundle", repo_id=rid)
        # B repo also ssh origin, but the map is keyed from the HTTPS form.
        repo_b = _committed_repo(tmp_path / "B-repo", remote=SSH_REMOTE)
        _activate(monkeypatch, mc_b)
        machine_map.record("repo", HTTPS_REMOTE, str(repo_b))  # normalizes to canon
        report = portability.import_bundle(mc_b.db, bundle)
        repo_res = [e for e in report["resolved"] if e["kind"] == "repo"]
        assert repo_res and repo_res[0]["bucket"] == "resolved"
        assert mc_b.db.get_task_by_name(name).repo_id is not None


# ============ tarball + zip transport ============


class TestImportArchiveTransport:
    def test_tarball_round_trip(self, mc, mc_b, tmp_path, monkeypatch):
        name = "proj"
        _build_bundle(mc, name, tmp_path / "dummy")  # seeds files + row on A
        tgz = _export(mc, name, out=str(tmp_path / "proj.tgz"))["bundle_path"]
        assert tgz.endswith(".tgz")
        report = _import(mc_b, monkeypatch, tgz)
        assert report["name"] == name
        assert mc_b.db.get_task_by_name(name) is not None
        assert (mc_b.root / "active" / name / f"{name}-plan.md").is_file()


# ===========================================================================
# Phase-3 review hardening: criticals, security guards, and the coverage gaps
# the adversarial review surfaced. Spec: docs/cross-machine-sharing-plan.md.
# ===========================================================================


def _edit_manifest(bundle, mutate):
    """Apply ``mutate(manifest_dict)`` and write it back (no re-hash)."""
    mp = Path(bundle) / "missioncache.json"
    man = json.loads(mp.read_text())
    mutate(man)
    mp.write_text(json.dumps(man))


# ---- C1 + security: hostile / malformed full_path, archive traversal ----


class TestImportFullPathGuard:
    def test_full_path_dot_rejected_data_dir_intact(self, mc, mc_b, tmp_path, monkeypatch):
        # full_path "." would make landing == root and the swap rmtree the dir.
        name = "proj"
        bundle = _build_bundle(mc, name, tmp_path / "bundle")
        _edit_manifest(bundle, lambda m: m["project"].__setitem__("full_path", "."))
        report = _import(mc_b, monkeypatch, bundle)
        assert report["exit_code"] == 1
        assert report["errors"]
        assert mc_b.db.get_task_by_name(name) is None
        assert (mc_b.root / "tasks.db").exists()  # data dir not destroyed

    def test_full_path_reserved_name_rejected(self, mc, mc_b, tmp_path, monkeypatch):
        name = "proj"
        bundle = _build_bundle(mc, name, tmp_path / "bundle")
        _edit_manifest(bundle, lambda m: m["project"].__setitem__("full_path", "tasks.db"))
        report = _import(mc_b, monkeypatch, bundle)
        assert report["exit_code"] == 1

    def test_full_path_outside_root_rejected(self, mc, mc_b, tmp_path, monkeypatch):
        name = "proj"
        bundle = _build_bundle(mc, name, tmp_path / "bundle")
        _edit_manifest(bundle, lambda m: m["project"].__setitem__("full_path", "../evil"))
        report = _import(mc_b, monkeypatch, bundle)
        assert report["exit_code"] == 1
        assert not (tmp_path / "B" / "evil").exists()

    def test_full_path_name_mismatch_rejected(self, mc, mc_b, tmp_path, monkeypatch):
        name = "proj"
        bundle = _build_bundle(mc, name, tmp_path / "bundle")
        _edit_manifest(bundle, lambda m: m["project"].__setitem__("full_path", "active/other"))
        report = _import(mc_b, monkeypatch, bundle)
        assert report["exit_code"] == 1

    def test_global_and_manual_prefixes_allowed(self, mc, mc_b, tmp_path, monkeypatch):
        # a non-coding project legitimately lands under global/<name>
        name = "proj"
        gdir = mc.root / "global" / name
        gdir.mkdir(parents=True)
        for rel, content in _plain_md(name).items():
            (gdir / rel).write_text(content)
        _insert_task(mc.db, name=name, task_type="non-coding", full_path=f"global/{name}")
        bundle = _export(mc, name, out=str(tmp_path / "bundle"))["bundle_path"]
        report = _import(mc_b, monkeypatch, bundle)
        assert report["exit_code"] in (0, 2)
        row = mc_b.db.get_task_by_name(name)
        assert row is not None and row.full_path == f"global/{name}"
        assert (mc_b.root / "global" / name / f"{name}-plan.md").is_file()


class TestImportArchiveTraversal:
    def test_tar_member_escaping_dest_rejected(self, mc_b, tmp_path, monkeypatch):
        import tarfile as _tar
        # craft a tarball whose member escapes the extraction dir
        evil = tmp_path / "evil.tgz"
        payload = tmp_path / "payload.txt"
        payload.write_text("pwned\n")
        with _tar.open(evil, "w:gz") as t:
            t.add(payload, arcname="../escaped.txt")
        report = _import(mc_b, monkeypatch, str(evil))
        assert report["exit_code"] == 1
        assert not (tmp_path / "escaped.txt").exists()

    def test_decompression_bomb_member_count_rejected(self, mc_b, tmp_path, monkeypatch):
        import tarfile as _tar
        evil = tmp_path / "bomb.tgz"
        f = tmp_path / "x.txt"
        f.write_text("x")
        monkeypatch.setattr(portability, "_MAX_EXTRACT_MEMBERS", 5)
        with _tar.open(evil, "w:gz") as t:
            for i in range(6):
                t.add(f, arcname=f"m{i}.txt")
        report = _import(mc_b, monkeypatch, str(evil))
        assert report["exit_code"] == 1
        assert any("members" in e for e in report["errors"])

    def test_decompression_bomb_size_rejected(self, mc_b, tmp_path, monkeypatch):
        import tarfile as _tar
        evil = tmp_path / "bomb2.tgz"
        big = tmp_path / "big.txt"
        big.write_bytes(b"0" * 2048)
        monkeypatch.setattr(portability, "_MAX_EXTRACT_BYTES", 1024)
        with _tar.open(evil, "w:gz") as t:
            t.add(big, arcname="big.txt")
        report = _import(mc_b, monkeypatch, str(evil))
        assert report["exit_code"] == 1
        assert any("uncompressed bytes" in e for e in report["errors"])

    def test_zip_symlink_entry_not_extracted(self, mc_b, tmp_path, monkeypatch):
        # A zip symlink entry must not survive extraction as a working symlink.
        # _safe_extract_zip skips it (symmetric with the tar extractor); even
        # without the skip, Python's zipfile writes it as a plain file, never a
        # symlink. Either way it cannot redirect a later rewrite out of tree.
        import zipfile as _zip
        import stat as _stat
        z = tmp_path / "link.zip"
        outside = tmp_path / "secret.txt"
        outside.write_text("SECRET\n")
        zi = _zip.ZipInfo("proj/notes.md")
        zi.external_attr = (_stat.S_IFLNK | 0o777) << 16
        with _zip.ZipFile(z, "w") as zf:
            # a valid manifest so the bundle reaches placement
            zf.writestr("missioncache.json", json.dumps({
                "manifest_version": 1, "kind": "missioncache-project-bundle",
                "project": {
                    "name": "proj", "status": "active", "type": "coding",
                    "tags": [], "priority": None, "jira_key": None,
                    "branch": None, "pr_url": None, "full_path": "active/proj",
                    "parent": None, "created_at": None, "origin_uuid": None,
                    "time_total_seconds": 0,
                },
                "references": {"repo": None, "vaults": [], "other_paths": []},
                "files": [],
            }))
            zf.writestr(zi, str(outside))  # the symlink entry
        _import(mc_b, monkeypatch, str(z))
        # import succeeds (empty files[]) but no working symlink lands
        landed = mc_b.root / "active" / "proj" / "notes.md"
        assert not landed.is_symlink()


class TestImportDirBundleSymlinkGuard:
    def test_symlinked_files_top_rejected(self, mc_b, tmp_path, monkeypatch):
        # A directory bundle whose files/<name> top is a symlink to an out-of-tree
        # dir must be refused (the archive extractor's link guard doesn't run for
        # dir bundles, and an empty files[] skips the checksum guard).
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "leak.md").write_text("LEAK\n")
        bundle = tmp_path / "dir-bundle"
        (bundle / "files").mkdir(parents=True)
        (bundle / "files" / "proj").symlink_to(outside, target_is_directory=True)
        manifest = {
            "manifest_version": 1,
            "kind": "missioncache-project-bundle",
            "project": {
                "name": "proj", "status": "active", "type": "coding",
                "tags": [], "priority": None, "jira_key": None, "branch": None,
                "pr_url": None, "full_path": "active/proj", "parent": None,
                "created_at": None, "time_total_seconds": 0,
            },
            "references": {"repo": None, "vaults": [], "other_paths": []},
            "files": [],
        }
        (bundle / "missioncache.json").write_text(json.dumps(manifest))
        report = _import(mc_b, monkeypatch, str(bundle))
        assert report["exit_code"] == 1
        assert any("symlink" in e for e in report["errors"])
        assert mc_b.db.get_task_by_name("proj") is None
        assert not (mc_b.root / "active" / "proj" / "leak.md").exists()


class TestImportSwapFailureRestoresBackup:
    def test_failed_swap_in_restores_old_tree(self, tmp_path, monkeypatch):
        # If os.replace(staging -> dest) fails after dest was moved aside, the
        # old project dir must be restored, not left deleted.
        dest = tmp_path / "active" / "proj"
        dest.mkdir(parents=True)
        (dest / "keep.md").write_text("original\n")
        staging = tmp_path / "active" / ".proj.import-tmp-1"
        staging.mkdir()
        (staging / "new.md").write_text("new\n")

        real_replace = os.replace
        calls = {"n": 0}

        def flaky_replace(src, dst):
            calls["n"] += 1
            if calls["n"] == 2:  # the staging -> dest swap-in
                raise OSError("simulated ENOSPC")
            return real_replace(src, dst)

        monkeypatch.setattr(portability.os, "replace", flaky_replace)
        with pytest.raises(OSError):
            portability._atomic_swap_dir(staging, dest)
        # dest survived with its ORIGINAL content
        assert dest.is_dir()
        assert (dest / "keep.md").read_text() == "original\n"


# ---- C2 fix: stable origin_uuid closes the force-clobber hole ----


class TestOriginUuidIdentity:
    def test_migration_adds_column_leaves_existing_row_null(self, tmp_path):
        # A pre-uuid DB gets the column added on initialize(), existing rows NULL
        # (never backfilled - a fresh per-machine uuid would false-mismatch).
        import sqlite3 as _sq
        dbp = tmp_path / "old.db"
        conn = _sq.connect(dbp)
        conn.execute(
            "CREATE TABLE tasks (id INTEGER PRIMARY KEY, repo_id INTEGER, "
            "name TEXT, full_path TEXT, parent_id INTEGER, status TEXT, "
            "type TEXT, tags TEXT, priority INTEGER, jira_key TEXT, branch TEXT, "
            "pr_url TEXT, created_at TEXT, updated_at TEXT, completed_at TEXT, "
            "archived_at TEXT, last_worked_on TEXT)"
        )
        conn.execute(
            "INSERT INTO tasks (id, name, full_path, status, type, tags, "
            "created_at, updated_at) VALUES "
            "(1,'old','active/old','active','coding','[]','2026-01-01','2026-01-01')"
        )
        conn.commit()
        conn.close()

        db = TaskDB(db_path=dbp)
        db.initialize()
        with db.connection() as c:
            cols = {r["name"] for r in c.execute("PRAGMA table_info(tasks)")}
        assert "origin_uuid" in cols
        assert db.get_task(1).origin_uuid is None

    def test_create_task_mints_uuid(self, mc):
        task = mc.db.create_task("some-meeting", task_type="non-coding")
        assert task.origin_uuid  # a real uuid string, not None/empty

    def test_export_carries_uuid_and_reimport_adopts_it(self, mc, mc_b, tmp_path, monkeypatch):
        name = "proj"
        a_uuid = "11111111-1111-4111-8111-111111111111"
        bundle = _build_bundle(mc, name, tmp_path / "bundle", origin_uuid=a_uuid)
        assert mc.db.get_task_by_name(name).origin_uuid == a_uuid
        man = json.loads((Path(bundle) / "missioncache.json").read_text())
        assert man["project"]["origin_uuid"] == a_uuid

        _import(mc_b, monkeypatch, bundle)
        b_row = mc_b.db.get_task_by_name(name)
        assert b_row.origin_uuid == a_uuid  # B adopts A's origin identity
        r2 = _import(mc_b, monkeypatch, bundle)  # re-import stays one row, matched
        assert r2["action"] == "updated"
        assert mc_b.db.get_task_by_name(name).id == b_row.id

    def test_different_uuid_same_name_aborts_even_with_force(self, mc, mc_b, tmp_path, monkeypatch):
        # The exact C2 repro, now CLOSED: two unrelated null-repo projects share a
        # name, differ in content, and --force must NOT clobber the victim.
        name = "proj"
        # Victim on B: a non-coding project (repo_id NULL) with its own uuid.
        _activate(monkeypatch, mc_b)
        victim = mc_b.db.create_task(name, task_type="non-coding")
        vdir = mc_b.root / "global" / name
        vdir.mkdir(parents=True)
        (vdir / f"{name}-context.md").write_text("# victim content\n")
        with mc_b.db.connection() as c:
            c.execute("UPDATE tasks SET full_path=? WHERE id=?",
                      (f"global/{name}", victim.id))
            c.commit()

        # Hostile/unrelated bundle from A: same name, DIFFERENT uuid, non-coding.
        _activate(monkeypatch, mc)
        attacker = mc.db.create_task(name, task_type="non-coding")
        assert attacker.origin_uuid != victim.origin_uuid
        adir = mc.root / "global" / name
        adir.mkdir(parents=True)
        for rel, content in _plain_md(name).items():
            (adir / rel).write_text(content)
        with mc.db.connection() as c:
            c.execute("UPDATE tasks SET full_path=? WHERE id=?",
                      (f"global/{name}", attacker.id))
            c.commit()
        bundle = _export(mc, name, out=str(tmp_path / "bundle"))["bundle_path"]

        report = _import(mc_b, monkeypatch, bundle, force=True)
        assert report["exit_code"] == 1
        assert any("DIFFERENT project" in e for e in report["errors"])
        # victim untouched: same row identity + original content
        still = mc_b.db.get_task_by_name(name)
        assert still.id == victim.id
        assert still.origin_uuid == victim.origin_uuid
        assert (vdir / f"{name}-context.md").read_text() == "# victim content\n"

    def test_uuidless_bundle_falls_back_to_heuristic(self, mc, mc_b, tmp_path, monkeypatch):
        # An old bundle (no origin_uuid) onto a null-repo row still imports via the
        # repo-identity fallback - backward compatibility for pre-feature bundles.
        name = "proj"
        bundle = _build_bundle(mc, name, tmp_path / "bundle")
        _edit_manifest(bundle, lambda m: m["project"].__setitem__("origin_uuid", None))
        report = _import(mc_b, monkeypatch, bundle)
        assert report["exit_code"] in (0, 2)
        assert mc_b.db.get_task_by_name(name) is not None


class TestImportNonGitReimport:
    def test_home_relative_reimport_is_idempotent(self, mc, mc_b, tmp_path, monkeypatch):
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        _stable_home(monkeypatch, fake_home)
        repo = fake_home / "notes"  # non-git folder under HOME -> home-relative
        repo.mkdir()
        name = "proj"
        rid = mc.db.add_repo(str(repo))
        bundle = _build_bundle(mc, name, tmp_path / "bundle", repo_id=rid)
        man = json.loads((Path(bundle) / "missioncache.json").read_text())
        assert man["references"]["repo"]["kind"] == "home-relative"

        _import(mc_b, monkeypatch, bundle)
        first_id = mc_b.db.get_task_by_name(name).id
        r2 = _import(mc_b, monkeypatch, bundle)  # 2nd import must NOT abort_different
        assert r2["action"] == "updated"
        assert not any("DIFFERENT project" in e for e in r2["errors"])
        assert mc_b.db.get_task_by_name(name).id == first_id

    def test_anchor_reimport_with_different_basename(self, mc, mc_b, tmp_path, monkeypatch):
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        _stable_home(monkeypatch, fake_home)
        repo_a = tmp_path / "work"  # non-git, NOT under home -> anchor "work"
        repo_a.mkdir()
        name = "proj"
        rid = mc.db.add_repo(str(repo_a))
        bundle = _build_bundle(mc, name, tmp_path / "bundle", repo_id=rid)
        # B maps the anchor to a path with a DIFFERENT basename (Mac work vs WSL dev)
        repo_b = tmp_path / "dev"
        repo_b.mkdir()
        _activate(monkeypatch, mc_b)
        machine_map.record("anchor", "work", str(repo_b))
        _import(mc_b, monkeypatch, bundle)  # 1: binds repo_id
        first_id = mc_b.db.get_task_by_name(name).id
        r2 = _import(mc_b, monkeypatch, bundle)  # 2
        r3 = _import(mc_b, monkeypatch, bundle)  # 3: was the break point
        assert r3["action"] == "updated"
        assert not any("DIFFERENT project" in e for e in r2["errors"] + r3["errors"])
        assert mc_b.db.get_task_by_name(name).id == first_id


# ---- C3: dir-only bundle (created_at None) imports cleanly ----


class TestImportDirOnlyBundle:
    def test_dir_only_export_imports(self, mc, mc_b, tmp_path, monkeypatch):
        name = "proj"
        _seed_files(mc, name, _plain_md(name))  # files on disk, NO db row
        bundle = _export(mc, name, out=str(tmp_path / "bundle"))["bundle_path"]
        man = json.loads((Path(bundle) / "missioncache.json").read_text())
        assert man["project"]["created_at"] is None

        report = _import(mc_b, monkeypatch, bundle)
        assert report["exit_code"] in (0, 2)
        assert not report["errors"]  # not the bogus "conflict" message
        row = mc_b.db.get_task_by_name(name)
        assert row is not None and row.created_at  # column default fired


# ---- I4: name-match with a different full_path aligns the row ----


class TestImportRowAlignment:
    def test_name_match_rewrites_full_path(self, mc, mc_b, tmp_path, monkeypatch):
        name = "proj"
        bundle = _build_bundle(mc, name, tmp_path / "bundle")  # full_path active/proj
        # B already has a same-named row at a DIFFERENT full_path
        _activate(monkeypatch, mc_b)
        _insert_task(mc_b.db, name=name, full_path=f"manual/{name}")
        report = portability.import_bundle(mc_b.db, bundle)
        assert report["exit_code"] in (0, 2)
        row = mc_b.db.get_task_by_name(name)
        assert row.full_path == f"active/{name}"  # aligned to where files landed


# ---- I6: restore a row whose files were deleted, no --force ----


class TestImportRestore:
    def test_missing_dir_restores_without_force(self, mc, mc_b, tmp_path, monkeypatch):
        name = "proj"
        bundle = _build_bundle(mc, name, tmp_path / "bundle")
        _import(mc_b, monkeypatch, bundle)
        landing = mc_b.root / "active" / name
        shutil.rmtree(landing)
        assert not landing.exists()
        report = _import(mc_b, monkeypatch, bundle)  # no force
        assert not report["errors"]
        assert (landing / f"{name}-plan.md").is_file()  # restored


# ---- I8: corrupt / missing bundle file rejected ----


class TestImportIntegrity:
    def test_tampered_file_rejected(self, mc, mc_b, tmp_path, monkeypatch):
        name = "proj"
        bundle = _build_bundle(mc, name, tmp_path / "bundle")
        # edit a file WITHOUT re-hashing -> manifest checksum no longer matches
        (Path(bundle) / "files" / name / f"{name}-plan.md").write_text("tampered\n")
        report = _import(mc_b, monkeypatch, bundle)
        assert report["exit_code"] == 1
        assert any("checksum mismatch" in e for e in report["errors"])
        assert mc_b.db.get_task_by_name(name) is None

    def test_missing_listed_file_rejected(self, mc, mc_b, tmp_path, monkeypatch):
        name = "proj"
        bundle = _build_bundle(mc, name, tmp_path / "bundle")
        (Path(bundle) / "files" / name / f"{name}-plan.md").unlink()
        report = _import(mc_b, monkeypatch, bundle)
        assert report["exit_code"] == 1
        assert any("missing" in e for e in report["errors"])


# ---- I8 cross-cutting: a UNIQUE conflict at write time = exit 1, no files ----


class TestImportConflictNoPartialCommit:
    def test_integrity_conflict_exits_1_without_placing_files(
        self, mc, mc_b, tmp_path, monkeypatch
    ):
        name = "proj"
        repo_a = _committed_repo(tmp_path / "A-repo")
        rid = mc.db.add_repo(str(repo_a))
        bundle = _build_bundle(mc, name, tmp_path / "bundle", repo_id=rid)
        # B: a COMPLETED row already holds (repo_id, active/proj) under a DIFFERENT
        # name, so find_import_target misses it but the INSERT collides on UNIQUE.
        repo_b = _committed_repo(tmp_path / "B-repo")
        _activate(monkeypatch, mc_b)
        rid_b = mc_b.db.add_repo(str(repo_b))
        _insert_task(mc_b.db, name="zzz", full_path=f"active/{name}",
                     repo_id=rid_b, status="completed")
        machine_map.record("repo", SSH_REMOTE, str(repo_b))
        report = portability.import_bundle(mc_b.db, bundle)
        assert report["exit_code"] == 1
        assert any("conflict" in e for e in report["errors"])
        assert not (mc_b.root / "active" / name).exists()  # nothing placed


# ---- I2 / I3: placement never writes secrets or follows symlinks ----


class TestImportPlacementSafety:
    def test_secret_file_not_placed(self, mc, mc_b, tmp_path, monkeypatch):
        name = "proj"
        files = _plain_md(name)
        files[".env"] = "TOKEN=hunter2\n"
        bundle = _build_bundle(mc, name, tmp_path / "bundle", files=files)
        # export already drops .env; assert import would too even if present
        env_in_bundle = Path(bundle) / "files" / name / ".env"
        env_in_bundle.write_text("TOKEN=hunter2\n")  # plant it back, not in files[]
        report = _import(mc_b, monkeypatch, bundle)
        assert not (mc_b.root / "active" / name / ".env").exists()
        assert any("secret" in w for w in report["warnings"])

    def test_symlink_in_dir_bundle_not_followed(self, mc, mc_b, tmp_path, monkeypatch):
        name = "proj"
        bundle = _build_bundle(mc, name, tmp_path / "bundle")
        secret = tmp_path / "secret.txt"
        secret.write_text("PRIVATE\n")
        link = Path(bundle) / "files" / name / "notes.md"
        link.symlink_to(secret)  # not in files[], a dir-bundle symlink
        report = _import(mc_b, monkeypatch, bundle)
        placed = mc_b.root / "active" / name / "notes.md"
        assert not placed.exists() or "PRIVATE" not in placed.read_text()
        assert any("symlink" in w for w in report["warnings"])


# ---- test-analyst gaps: --repo override, repo branches, zip, embedded, vault ----


class TestImportRepoOverride:
    def test_override_wins_and_resolves(self, mc, mc_b, tmp_path, monkeypatch):
        name = "proj"
        repo_a = _committed_repo(tmp_path / "A-repo")
        rid = mc.db.add_repo(str(repo_a))
        bundle = _build_bundle(mc, name, tmp_path / "bundle", repo_id=rid)
        override = _committed_repo(tmp_path / "override-repo")
        report = _import(mc_b, monkeypatch, bundle, repo_override=str(override))
        repo_res = [e for e in report["resolved"] if e["kind"] == "repo"]
        assert repo_res and repo_res[0]["local"] == str(override)
        assert mc_b.db.get_task_by_name(name).repo_id is not None

    def test_override_missing_path(self, mc, mc_b, tmp_path, monkeypatch):
        name = "proj"
        bundle = _build_bundle(mc, name, tmp_path / "bundle")
        report = _import(mc_b, monkeypatch, bundle,
                         repo_override=str(tmp_path / "nope"))
        assert any(e["kind"] == "repo" for e in report["missing"])


class TestImportRepoBranches:
    def test_remote_mismatch_needs_mapping(self, mc, mc_b, tmp_path, monkeypatch):
        name = "proj"
        repo_a = _committed_repo(tmp_path / "A-repo", remote=SSH_REMOTE)
        rid = mc.db.add_repo(str(repo_a))
        bundle = _build_bundle(mc, name, tmp_path / "bundle", repo_id=rid)
        # B maps the manifest remote to a repo whose origin is something else
        other = _committed_repo(tmp_path / "B-repo",
                                remote="git@github.com:AkamaiETP/wrong.git")
        _activate(monkeypatch, mc_b)
        machine_map.record("repo", SSH_REMOTE, str(other))
        report = portability.import_bundle(mc_b.db, bundle)
        assert any(e["kind"] == "repo" for e in report["needs_mapping"])
        assert mc_b.db.get_task_by_name(name).repo_id is None

    def test_mapped_but_absent_missing(self, mc, mc_b, tmp_path, monkeypatch):
        name = "proj"
        repo_a = _committed_repo(tmp_path / "A-repo", remote=SSH_REMOTE)
        rid = mc.db.add_repo(str(repo_a))
        bundle = _build_bundle(mc, name, tmp_path / "bundle", repo_id=rid)
        _activate(monkeypatch, mc_b)
        machine_map.record("repo", SSH_REMOTE, str(tmp_path / "gone"))
        report = portability.import_bundle(mc_b.db, bundle)
        assert any(e["kind"] == "repo" for e in report["missing"])


class TestImportZipTransport:
    def test_zip_round_trip(self, mc, mc_b, tmp_path, monkeypatch):
        import zipfile as _zip
        name = "proj"
        bundle = _build_bundle(mc, name, tmp_path / "bundle")
        zpath = tmp_path / "proj.zip"
        with _zip.ZipFile(zpath, "w") as zf:
            for p in Path(bundle).rglob("*"):
                if p.is_file():
                    zf.write(p, arcname=str(p.relative_to(Path(bundle).parent)))
        report = _import(mc_b, monkeypatch, str(zpath))
        assert report["name"] == name
        assert mc_b.db.get_task_by_name(name) is not None


class TestImportEmbeddedBuckets:
    def test_embedded_needs_mapping(self, mc, mc_b, tmp_path, monkeypatch):
        name = "proj"
        # craft an embedded ref token that B has no mapping for
        files = _plain_md(name)
        bundle = _build_bundle(mc, name, tmp_path / "bundle", files=files)
        _edit_manifest(bundle, lambda m: m["references"]["other_paths"].append({
            "raw": "x", "token": "${repo:github.com/x/y}/a.md",
            "classification": "repo", "target_kind": "source-file",
            "source": f"{name}-context.md:1",
        }))
        report = _import(mc_b, monkeypatch, bundle)
        nm = [e for e in report["needs_mapping"] if e["kind"] == "embedded-path"]
        assert nm and "config set-path repo:github.com/x/y" in nm[0]["hint"]

    def test_embedded_missing(self, mc, mc_b, tmp_path, monkeypatch):
        name = "proj"
        bundle = _build_bundle(mc, name, tmp_path / "bundle")
        _edit_manifest(bundle, lambda m: m["references"]["other_paths"].append({
            "raw": "x", "token": "${HOME}/definitely/absent/a.md",
            "classification": "home-relative", "target_kind": "source-file",
            "source": f"{name}-context.md:1",
        }))
        fake_home = tmp_path / "home"; fake_home.mkdir()
        _stable_home(monkeypatch, fake_home)
        report = _import(mc_b, monkeypatch, bundle)
        miss = [e for e in report["missing"] if e["kind"] == "embedded-path"]
        assert miss


class TestImportVaultNoRoots:
    def test_no_vault_roots_needs_mapping(self, mc, mc_b, tmp_path, monkeypatch):
        name = "proj"
        files = _plain_md(name)
        files[f"{name}-context.md"] = "# context\nHub: [[somenote]]\n"
        bundle = _build_bundle(mc, name, tmp_path / "bundle", files=files)
        report = _import(mc_b, monkeypatch, bundle)  # B has no vault roots
        nm = [e for e in report["needs_mapping"] if e["kind"] == "vault"]
        assert nm and "set-path vault" in nm[0]["hint"]


# ---- manifest validation branches ----


class TestImportManifestValidation:
    def test_kind_mismatch_rejected(self, mc, mc_b, tmp_path, monkeypatch):
        name = "proj"
        bundle = _build_bundle(mc, name, tmp_path / "bundle")
        _edit_manifest(bundle, lambda m: m.__setitem__("kind", "something-else"))
        report = _import(mc_b, monkeypatch, bundle)
        assert report["exit_code"] == 1
        assert any("kind" in e for e in report["errors"])

    def test_invalid_status_rejected(self, mc, mc_b, tmp_path, monkeypatch):
        name = "proj"
        bundle = _build_bundle(mc, name, tmp_path / "bundle")
        _edit_manifest(bundle, lambda m: m["project"].__setitem__("status", "weird"))
        report = _import(mc_b, monkeypatch, bundle)
        assert report["exit_code"] == 1
        assert any("status" in e for e in report["errors"])

    def test_invalid_name_rejected(self, mc, mc_b, tmp_path, monkeypatch):
        name = "proj"
        bundle = _build_bundle(mc, name, tmp_path / "bundle")
        _edit_manifest(bundle, lambda m: m["project"].__setitem__("name", "../etc"))
        report = _import(mc_b, monkeypatch, bundle)
        assert report["exit_code"] == 1
        assert any("invalid project name" in e for e in report["errors"])


# ---- I9 / I10: rewrite boundary safety + failure demotion ----


class TestRewriteEmbedded:
    def test_prefix_collision_not_corrupted(self, tmp_path):
        d = tmp_path / "land"
        d.mkdir()
        f = d / "ctx.md"
        f.write_text("see /h/work and /h/work-backup/y here\n")
        status = portability._rewrite_embedded(d, "ctx.md:1", "/h/work", "/NEW")
        assert status == "rewritten"
        text = f.read_text()
        assert "/h/work-backup/y" in text  # longer path intact
        assert "/NEW and" in text  # shorter path rewritten

    def test_traversal_source_refuses_and_does_not_write(self, tmp_path):
        # A crafted manifest source escaping the landing dir must not be written.
        land = tmp_path / "land"
        land.mkdir()
        (land / "ctx.md").write_text("token /old here\n")
        victim = tmp_path / "victim.txt"
        victim.write_text("/old\n")
        status = portability._rewrite_embedded(
            land, "../victim.txt:1", "/old", "/PWNED"
        )
        assert status == "failed"
        assert victim.read_text() == "/old\n"  # untouched

    def test_symlink_source_refuses_and_does_not_write(self, tmp_path):
        # A symlink inside the landing dir pointing outside must not be followed.
        land = tmp_path / "land"
        land.mkdir()
        victim = tmp_path / "victim.txt"
        victim.write_text("/old\n")
        (land / "ctx.md").symlink_to(victim)
        status = portability._rewrite_embedded(land, "ctx.md:1", "/old", "/PWNED")
        assert status == "failed"
        assert victim.read_text() == "/old\n"  # untouched

    def test_failed_rewrite_demotes_entry(self, mc, mc_b, tmp_path, monkeypatch):
        name = "proj"
        vault_a = tmp_path / "vaultA"
        (vault_a / "R").mkdir(parents=True)
        (vault_a / "R" / "n.md").write_text("x\n")
        _activate(monkeypatch, mc)
        machine_map.record("vault", "V", str(vault_a))
        files = _plain_md(name)
        files[f"{name}-context.md"] = f"# context\nref {vault_a}/R/n.md\n"
        bundle = _build_bundle(mc, name, tmp_path / "bundle", files=files)
        vault_b = tmp_path / "vaultB"
        (vault_b / "R").mkdir(parents=True)
        (vault_b / "R" / "n.md").write_text("x\n")
        _activate(monkeypatch, mc_b)
        machine_map.record("vault", "V", str(vault_b))
        monkeypatch.setattr(portability, "_rewrite_embedded",
                            lambda *a, **k: "failed")
        report = portability.import_bundle(mc_b.db, bundle, rewrite=True)
        # the embedded entry is demoted out of resolved, exit reflects it
        assert any(e["kind"] == "embedded-path" for e in report["missing"])
        assert report["exit_code"] == 2


# ---- dry-run contract tightening + _under_drvfs unit + created_at min ----


class TestImportMisc:
    def test_dry_run_exit_code_and_flag(self, mc, mc_b, tmp_path, monkeypatch):
        name = "proj"
        bundle = _build_bundle(mc, name, tmp_path / "bundle")
        report = _import(mc_b, monkeypatch, bundle, dry_run=True)
        assert report["dry_run"] is True
        assert report["exit_code"] == 0  # clean bundle would fully resolve

    def test_under_drvfs_detection(self):
        assert portability._under_drvfs(Path("/mnt/c/Users/x/.missioncache")) is True
        assert portability._under_drvfs(Path("/home/x/.missioncache")) is False

    def test_created_at_mixed_separator_keeps_earlier(self, mc_b, tmp_path, monkeypatch):
        _activate(monkeypatch, mc_b)
        # existing space-form earlier; incoming T-form later -> earlier kept
        _insert_task(mc_b.db, name="proj", created_at="2026-05-02 09:11:00")
        task, action = mc_b.db.upsert_imported_task(
            "proj", "active/proj", repo_id=None, status="active",
            task_type="coding", tags=[], priority=None, jira_key=None,
            branch=None, pr_url=None, parent_id=None,
            created_at="2026-08-01T00:00:00",
        )
        assert action == "updated"
        assert task.created_at == "2026-05-02 09:11:00"


# ---- test-depth polish on existing happy-path tests ----


class TestImportReportFidelity:
    def test_missing_refs_still_imports_row(self, mc, mc_b, tmp_path, monkeypatch):
        name = "proj"
        files = _plain_md(name)
        files[f"{name}-context.md"] = "# context\nHub: [[gone]]\n"
        bundle = _build_bundle(mc, name, tmp_path / "bundle", files=files)
        report = _import(mc_b, monkeypatch, bundle)
        # an unresolved vault ref does NOT hard-fail: the row still imports,
        # exit is 2 (imported-with-gaps), and the ref lands in needs_mapping.
        assert mc_b.db.get_task_by_name(name) is not None
        assert report["exit_code"] == 2
        assert any(
            e["kind"] == "vault" and e["id"] == "gone"
            for e in report["needs_mapping"]
        )

    def test_parent_id_is_b_local_not_a(self, mc, mc_b, tmp_path, monkeypatch):
        # force A and B parent ids to diverge so the equality is load-bearing
        _seed_files(mc, "parent-proj", _plain_md("parent-proj"))
        ptid = _insert_task(mc.db, name="parent-proj")
        parent_bundle = _export(mc, "parent-proj", out=str(tmp_path / "pb"))["bundle_path"]
        child_bundle = _build_bundle(mc, "child-proj", tmp_path / "cb", parent_id=ptid)
        _activate(monkeypatch, mc_b)
        _insert_task(mc_b.db, name="filler")  # bump B ids so parent.id != A's ptid
        _insert_task(mc_b.db, name="filler2")
        portability.import_bundle(mc_b.db, parent_bundle)
        b_parent_id = mc_b.db.get_task_by_name("parent-proj").id
        assert b_parent_id != ptid  # the divergence the test relies on
        portability.import_bundle(mc_b.db, child_bundle)
        assert mc_b.db.get_task_by_name("child-proj").parent_id == b_parent_id

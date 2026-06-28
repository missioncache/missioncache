"""Tests for portability.export_project: Phase 2 (export) of cross-machine sharing.

Spec source: docs/cross-machine-sharing-plan.md sections 3 (manifest), 4.1 (CLI),
6 (reference kinds), 8 (test plan), 10 (phasing). Every assertion traces to that
contract, not to the implementation. Import (Phase 3) is out of scope here.
"""

import hashlib
import json
import os
import re
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
                 created_at="2026-05-02T09:11:00") -> int:
    full_path = full_path or f"active/{name}"
    with db.connection() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (repo_id, name, full_path, parent_id, status, type, "
            "tags, priority, jira_key, branch, pr_url, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (repo_id, name, full_path, parent_id, status, task_type,
             json.dumps(tags or []), priority, jira_key, branch, pr_url, created_at),
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

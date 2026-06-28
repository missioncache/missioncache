"""Tests for machine_map: the per-machine path map for cross-machine sharing.

Spec source: docs/cross-machine-sharing-plan.md (manifest + path-map contract).
Every assertion traces to that contract, not to the implementation.
"""

import json
import subprocess
from pathlib import Path

import pytest

from missioncache_db import machine_map


@pytest.fixture
def machine_file(tmp_path, monkeypatch):
    """Point machine_map at a throwaway machine.json (never the real one)."""
    f = tmp_path / "machine.json"
    monkeypatch.setattr(machine_map, "MACHINE_FILE", f)
    return f


def _git_repo(path: Path, remote: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "remote", "add", "origin", remote], cwd=path, check=True)
    return path


# ============ remote_key canonicalization ============


class TestRemoteKey:
    @pytest.mark.parametrize("url,expected", [
        ("git@github.com:Owner/Repo.git", "github.com/Owner/Repo"),       # scp form
        ("https://github.com/Owner/Repo.git", "github.com/Owner/Repo"),    # https + .git
        ("https://github.com/Owner/Repo", "github.com/Owner/Repo"),        # https no .git
        ("ssh://git@github.com/Owner/Repo.git", "github.com/Owner/Repo"),  # ssh url
        ("ssh://git@github.com:22/Owner/Repo.git", "github.com/Owner/Repo"),  # ssh with port
        ("git://github.com/Owner/Repo.git", "github.com/Owner/Repo"),      # git proto
        ("https://github.com/Owner/Repo/", "github.com/Owner/Repo"),       # trailing slash
        ("  git@github.com:Owner/Repo.git  ", "github.com/Owner/Repo"),    # whitespace
    ])
    def test_variants_collapse_to_one_key(self, url, expected):
        assert machine_map.remote_key(url) == expected

    def test_host_lowercased_path_case_preserved(self):
        # GitHub repo paths are case-sensitive; only the host is lowercased.
        assert machine_map.remote_key("https://GitHub.com/Owner/Repo") == "github.com/Owner/Repo"

    def test_ssh_and_https_of_same_repo_match(self):
        assert (machine_map.remote_key("git@github.com:a/b.git")
                == machine_map.remote_key("https://github.com/a/b"))

    def test_scp_numeric_owner_preserved_and_matches_https(self):
        # A numeric first path segment is an owner, not an ssh port, on scp form.
        assert machine_map.remote_key("git@github.com:123/Repo.git") == "github.com/123/Repo"
        assert (machine_map.remote_key("git@github.com:123/Repo.git")
                == machine_map.remote_key("https://github.com/123/Repo"))

    def test_embedded_credential_stripped(self):
        # A token in the URL must not leak into the key (or machine.json).
        assert machine_map.remote_key("https://user:ghp_TOKEN@github.com/Owner/Repo") == "github.com/Owner/Repo"
        assert machine_map.remote_key("https://user@github.com/Owner/Repo") == "github.com/Owner/Repo"


# ============ set-path colon split (CLI contract) ============


class TestSetPathColonSplit:
    def test_split_on_first_colon_only(self):
        # The CLI runs sys.argv[3].split(":", 1); a remote URL keeps its colons.
        kind, name = "repo:git@github.com:Owner/Repo.git".split(":", 1)
        assert kind == "repo"
        assert name == "git@github.com:Owner/Repo.git"
        assert machine_map.remote_key(name) == "github.com/Owner/Repo"


# ============ record / resolve round-trips ============


class TestRecordResolve:
    def test_repo_roundtrip_normalizes_and_resolves_by_raw_or_key(self, machine_file):
        key = machine_map.record("repo", "git@github.com:Owner/Repo.git", "/local/repo")
        assert key == "github.com/Owner/Repo"
        assert machine_map.resolve("repo", "https://github.com/Owner/Repo") == "/local/repo"
        assert machine_map.resolve("repo", "github.com/Owner/Repo") == "/local/repo"

    def test_vault_roundtrip(self, machine_file):
        machine_map.record("vault", "TomerWork", "/obsidian/TomerWork")
        assert machine_map.resolve("vault", "TomerWork") == "/obsidian/TomerWork"

    def test_anchor_roundtrip(self, machine_file):
        machine_map.record("anchor", "work", "/home/me/work")
        assert machine_map.resolve("anchor", "work") == "/home/me/work"

    def test_anchor_home_falls_back_to_live_home_when_unset(self, machine_file):
        assert machine_map.resolve("anchor", "HOME") == str(Path.home())

    def test_unmapped_returns_none(self, machine_file):
        assert machine_map.resolve("repo", "github.com/x/y") is None
        assert machine_map.resolve("vault", "Nope") is None

    def test_record_preserves_other_sections(self, machine_file):
        machine_map.record("repo", "git@github.com:a/b.git", "/r")
        machine_map.record("vault", "V", "/v")
        machine_map.record("anchor", "A", "/a")
        m = machine_map.all_mappings()
        assert m["repos"] == {"github.com/a/b": "/r"}
        assert m["vaults"] == {"V": "/v"}
        assert m["anchors"] == {"A": "/a"}


# ============ read/write tolerance + atomicity ============


class TestReadWrite:
    def test_missing_file_returns_defaults(self, machine_file):
        assert machine_map.all_mappings() == {"version": 1, "repos": {}, "vaults": {}, "anchors": {}}

    def test_corrupt_json_returns_defaults(self, machine_file):
        machine_file.write_text("{not valid json")
        m = machine_map.all_mappings()
        assert m["repos"] == {} and m["version"] == 1

    def test_non_dict_returns_defaults(self, machine_file):
        machine_file.write_text("[1, 2, 3]")
        assert machine_map.all_mappings()["repos"] == {}

    def test_partial_file_fills_missing_sections_without_clobbering(self, machine_file):
        machine_file.write_text(json.dumps({"repos": {"github.com/a/b": "/r"}}))
        m = machine_map.all_mappings()
        assert m["repos"] == {"github.com/a/b": "/r"}  # preserved
        assert m["vaults"] == {}                        # filled
        assert m["anchors"] == {}                        # filled

    def test_write_sorted_with_trailing_newline(self, machine_file):
        machine_map.record("vault", "Z", "/z")
        machine_map.record("vault", "A", "/a")
        text = machine_file.read_text()
        assert text.endswith("\n")
        assert text.index('"A"') < text.index('"Z"')  # sort_keys

    def test_no_tmp_file_left_behind(self, machine_file):
        machine_map.record("anchor", "x", "/x")
        assert list(machine_file.parent.glob(".machine.*.tmp")) == []


# ============ git helpers + seed ============


class TestGitHelpersAndSeed:
    def test_git_remote_reads_origin(self, tmp_path):
        repo = _git_repo(tmp_path / "r", "git@github.com:Owner/Repo.git")
        assert machine_map._git_remote(str(repo)) == "git@github.com:Owner/Repo.git"

    def test_git_remote_none_for_non_repo(self, tmp_path):
        assert machine_map._git_remote(str(tmp_path)) is None

    def test_seed_empty_db_sets_only_home_anchor(self, machine_file, task_db):
        result = machine_map.seed(task_db)
        assert result["added"] == []
        assert result["proposed"] == []
        assert machine_map.all_mappings()["anchors"]["HOME"] == str(Path.home())

    def test_seed_dry_run_writes_nothing(self, machine_file, task_db):
        machine_map.seed(task_db, dry_run=True)
        assert not machine_file.exists()

    def test_seed_maps_git_repo_by_remote_key(self, machine_file, task_db, tmp_path):
        repo = _git_repo(tmp_path / "proj", "https://github.com/Owner/Proj.git")
        task_db.add_repo(str(repo))
        result = machine_map.seed(task_db)
        resolved = machine_map.resolve("repo", "github.com/Owner/Proj")
        assert resolved is not None
        assert Path(resolved).name == "proj"
        assert ("repo", "github.com/Owner/Proj", resolved) in result["added"]

    def test_seed_duplicate_remote_keeps_first_flags_rest(self, machine_file, task_db, tmp_path):
        # Two clones of one remote (ssh + https forms): map one, flag the other.
        _git_repo(tmp_path / "clone-a", "git@github.com:Owner/Dup.git")
        _git_repo(tmp_path / "clone-b", "https://github.com/Owner/Dup.git")
        task_db.add_repo(str(tmp_path / "clone-a"))
        task_db.add_repo(str(tmp_path / "clone-b"))
        result = machine_map.seed(task_db)
        mapped = [a for a in result["added"] if a[1] == "github.com/Owner/Dup"]
        assert len(mapped) == 1  # exactly one wins, not last-wins silently
        assert any("already mapped to" in reason for _, reason in result["skipped"])
        assert machine_map.resolve("repo", "github.com/Owner/Dup") is not None


# ============ empty-env override safety ============


class TestEmptyEnvRoot:
    def test_empty_missioncache_root_falls_back_to_default_not_cwd(self):
        # A set-but-empty MISSIONCACHE_ROOT must not become Path("") == cwd.
        import importlib
        import os as _os
        prev = _os.environ.get("MISSIONCACHE_ROOT")
        _os.environ["MISSIONCACHE_ROOT"] = ""
        try:
            importlib.reload(machine_map)
            assert machine_map.MACHINE_FILE == Path.home() / ".missioncache" / "machine.json"
        finally:
            if prev is None:
                _os.environ.pop("MISSIONCACHE_ROOT", None)
            else:
                _os.environ["MISSIONCACHE_ROOT"] = prev
            importlib.reload(machine_map)

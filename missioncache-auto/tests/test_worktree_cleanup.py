"""Regression tests for WorktreeManager cleanup preserving dirty worktrees.

Worktree removal uses `git worktree remove --force`, which discards any
uncommitted work. cleanup_with_results must therefore skip worktrees that
still hold real changes (e.g. a worker whose auto-commit failed), while still
removing worktrees whose only leftover is a copied .env* file.
"""

import subprocess

from missioncache_auto.worktree import WorktreeManager


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True, text=True)


def _init_repo(path):
    _git(["init"], path)
    _git(["config", "user.email", "t@e.com"], path)
    _git(["config", "user.name", "test"], path)
    (path / "app.py").write_text("v1\n")
    _git(["add", "app.py"], path)
    _git(["commit", "-m", "init"], path)


def test_cleanup_preserves_worktree_with_uncommitted_changes(tmp_path):
    _init_repo(tmp_path)
    mgr = WorktreeManager(tmp_path, "sample", num_workers=1)
    wt = mgr.create_worktrees()[0]

    # Worker left a real, uncommitted change behind (e.g. commit failed).
    (wt / "app.py").write_text("uncommitted work\n")

    results = mgr.merge_all()  # no commits ahead -> nothing merged
    mgr.cleanup_with_results(results)

    assert wt.exists()  # preserved so the work can be recovered
    branches = subprocess.run(
        ["git", "branch"], cwd=tmp_path, capture_output=True, text=True
    ).stdout
    assert "missioncache-auto/sample/worker-0" in branches


def test_cleanup_removes_clean_worktree_with_copied_env(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / ".env").write_text("SECRET=1\n")  # copied into each worktree
    mgr = WorktreeManager(tmp_path, "sample", num_workers=1)
    wt = mgr.create_worktrees()[0]

    # Only the copied .env is present (untracked) - not real work to preserve.
    assert (wt / ".env").exists()

    results = mgr.merge_all()
    mgr.cleanup_with_results(results)

    assert not wt.exists()  # .env-only worktree is safe to remove

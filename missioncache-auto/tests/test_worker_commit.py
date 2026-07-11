"""Tests for missioncache_auto.worker.git_commit_task auto-commit behavior."""

import subprocess

from missioncache_auto.worker import git_commit_task


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)


def _init_repo(path):
    _git(["init"], path)
    _git(["config", "user.email", "t@example.com"], path)
    _git(["config", "user.name", "test"], path)
    (path / "app.py").write_text("v1\n")
    _git(["add", "app.py"], path)
    _git(["commit", "-m", "init"], path)


def test_auto_commit_excludes_env_files(tmp_path):
    repo = tmp_path
    _init_repo(repo)

    # Simulate a worker task: a real code edit plus .env* files that worktree
    # setup copied in (potential secrets, not gitignored here).
    (repo / "app.py").write_text("v2\n")
    (repo / ".env").write_text("SECRET=abc\n")
    (repo / ".env.local").write_text("LOCAL=xyz\n")

    prompt_file = tmp_path / "task-01-prompt.md"  # need not exist
    committed, msg = git_commit_task("01", prompt_file, repo)

    assert committed is True
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=repo, capture_output=True, text=True
    ).stdout.split()
    assert "app.py" in tracked
    assert ".env" not in tracked
    assert ".env.local" not in tracked


def test_auto_commit_stages_normal_changes(tmp_path):
    repo = tmp_path
    _init_repo(repo)

    # A normal (non-env) tracked-file change must still be committed.
    (repo / "app.py").write_text("v2\n")
    prompt_file = tmp_path / "task-01-prompt.md"
    committed, msg = git_commit_task("01", prompt_file, repo)

    assert committed is True
    committed_content = subprocess.run(
        ["git", "show", "HEAD:app.py"], cwd=repo, capture_output=True, text=True
    ).stdout
    assert "v2" in committed_content


def test_auto_commit_excludes_nested_env_files(tmp_path):
    repo = tmp_path
    _init_repo(repo)

    # A nested .env must be excluded too - the root-anchored exclude glob alone
    # would stage sub/.env (secrets leak). A real nested file must still commit.
    (repo / "app.py").write_text("v2\n")
    sub = repo / "sub"
    sub.mkdir()
    (sub / ".env").write_text("NESTED=secret\n")
    (sub / "module.py").write_text("code\n")

    prompt_file = tmp_path / "task-01-prompt.md"
    committed, msg = git_commit_task("01", prompt_file, repo)

    assert committed is True
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=repo, capture_output=True, text=True
    ).stdout.split()
    assert "app.py" in tracked
    assert "sub/module.py" in tracked
    assert "sub/.env" not in tracked


def test_auto_commit_includes_untracked_new_files(tmp_path):
    repo = tmp_path
    _init_repo(repo)

    # A task that produces ONLY new (untracked) files must still be committed.
    # The old `git diff --quiet` pre-check missed untracked files entirely.
    (repo / "newmod.py").write_text("code\n")
    prompt_file = tmp_path / "task-01-prompt.md"
    committed, msg = git_commit_task("01", prompt_file, repo)

    assert committed is True
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=repo, capture_output=True, text=True
    ).stdout.split()
    assert "newmod.py" in tracked


def test_auto_commit_skips_when_only_env_files_present(tmp_path):
    repo = tmp_path
    _init_repo(repo)

    # Only excluded .env* files changed - the pre-check applies the same
    # exclusion, so this is a clean "nothing to commit" skip, not a failed
    # (empty) commit.
    (repo / ".env").write_text("SECRET=1\n")
    prompt_file = tmp_path / "task-01-prompt.md"
    committed, msg = git_commit_task("01", prompt_file, repo)

    assert committed is False
    assert msg == ""

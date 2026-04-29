"""Integration tests for git-root resolution at MCP-tool boundaries.

The slash command guidance to resolve cwd via ``git rev-parse --show-toplevel``
is unenforceable - models can skip the bash step, and non-Claude MCP clients
have no equivalent. ``create_orbit_files`` and ``set_task_repo`` resolve
server-side instead, with an explicit ``resolve_git_root=False`` opt-out for
monorepo sub-package use cases. These tests exercise the wrapper layer
end-to-end with a temp SQLite + ``ORBIT_ROOT`` so the resolution behavior
is locked in.
"""

from __future__ import annotations

import asyncio
import pathlib

import pytest

from mcp_orbit import db as db_module
from mcp_orbit import tools_docs, tools_tracking


@pytest.fixture
def isolated_orbit(tmp_path, monkeypatch):
    """Bind ORBIT_ROOT and DB to a temp dir; reset the TaskDB singleton.

    Also guards against test environments where ``tmp_path`` is itself
    nested under a directory with ``.git`` - the walker would climb out
    of the test fixture and the assertions below would compare against
    the wrong path. Skip cleanly in that case.
    """
    walker = tmp_path.resolve()
    while walker != walker.parent:
        if (walker / ".git").exists():
            pytest.skip(
                f"tmp_path ancestor {walker} contains .git; cannot test "
                "git-root resolution in isolation"
            )
        walker = walker.parent

    orbit_root = tmp_path / ".orbit"
    orbit_root.mkdir()
    db_path = tmp_path / "tasks.db"

    from mcp_orbit import config, orbit

    monkeypatch.setattr(config.settings, "orbit_root", orbit_root)
    monkeypatch.setattr(config.settings, "db_path", db_path)
    monkeypatch.setattr(orbit, "settings", config.settings)
    # Force a fresh TaskDB next get_db() call.
    monkeypatch.setattr(db_module, "_db", None)

    return tmp_path


def _make_git_repo(repo_path: pathlib.Path) -> pathlib.Path:
    """Create a fake git repo by mkdir-ing ``repo_path/.git/``."""
    repo_path.mkdir(parents=True, exist_ok=True)
    (repo_path / ".git").mkdir(exist_ok=True)
    return repo_path


# ── create_orbit_files ───────────────────────────────────────────────────


class TestCreateOrbitFilesGitRootResolution:
    def test_subdir_of_git_repo_resolves_to_root(self, isolated_orbit):
        """Caller passes a subdir of a git repo; tool registers the repo at
        the git root, not at the subdir.

        This is the steering-template-improvements bug from 2026-04-28:
        cwd was ~/.claude/commands (subdir), got registered as
        ``commands`` instead of ``.claude``.
        """
        repo_root = _make_git_repo(isolated_orbit / "myrepo")
        subdir = repo_root / "deep" / "nested"
        subdir.mkdir(parents=True)

        result = asyncio.run(
            tools_docs.create_orbit_files(
                repo_path=str(subdir),
                project_name="my-project",
                description="test project",
            )
        )

        assert result.get("success") is True
        assert result["repo_path"] == str(repo_root.resolve())

    def test_git_root_passed_directly_registers_unchanged(self, isolated_orbit):
        """When the caller already passes the git root, registration uses
        that path verbatim (modulo ``Path.resolve()``)."""
        repo_root = _make_git_repo(isolated_orbit / "myrepo")

        result = asyncio.run(
            tools_docs.create_orbit_files(
                repo_path=str(repo_root),
                project_name="my-project",
                description="test project",
            )
        )

        assert result.get("success") is True
        assert result["repo_path"] == str(repo_root.resolve())

    def test_non_git_path_passes_through(self, isolated_orbit):
        """Non-git directories stay supported - the contract that
        ``repo_path`` accepts any directory is preserved. The fallback
        path equals the input (after resolve())."""
        non_git = isolated_orbit / "plain_workspace"
        non_git.mkdir()

        result = asyncio.run(
            tools_docs.create_orbit_files(
                repo_path=str(non_git),
                project_name="non-git-project",
                description="test",
            )
        )

        assert result.get("success") is True
        assert result["repo_path"] == str(non_git.resolve())

    def test_empty_repo_path_raises_with_default_resolve(self, isolated_orbit):
        """Codex P2 finding: with resolve_git_root=True (default), an empty
        repo_path used to silently resolve to the MCP server's cwd via
        ``Path("").expanduser().resolve()``, bypassing the empty-string
        guard in ``_validate_path``. Validation now runs on the raw input
        before resolution so the error fires at the boundary."""
        result = asyncio.run(
            tools_docs.create_orbit_files(
                repo_path="",
                project_name="empty-path-rejected",
                description="test",
            )
        )
        assert result.get("error") is True
        assert "empty" in result.get("message", "").lower()

    def test_null_byte_repo_path_raises_with_default_resolve(self, isolated_orbit):
        """Mirror guard for null bytes in the raw input."""
        result = asyncio.run(
            tools_docs.create_orbit_files(
                repo_path="/tmp/path\x00evil",
                project_name="null-byte-rejected",
                description="test",
            )
        )
        assert result.get("error") is True
        assert "null" in result.get("message", "").lower()

    def test_resolve_git_root_false_keeps_subdir(self, isolated_orbit):
        """Monorepo opt-out: caller passes a subdir of a git repo with
        ``resolve_git_root=False`` and the subdir IS the registered
        project boundary. This is the path for sub-packages within a
        monorepo where each package is its own orbit project."""
        repo_root = _make_git_repo(isolated_orbit / "monorepo")
        sub_package = repo_root / "packages" / "auth-service"
        sub_package.mkdir(parents=True)

        result = asyncio.run(
            tools_docs.create_orbit_files(
                repo_path=str(sub_package),
                project_name="auth-service-project",
                description="test",
                resolve_git_root=False,
            )
        )

        assert result.get("success") is True
        assert result["repo_path"] == str(sub_package)


# ── set_task_repo ────────────────────────────────────────────────────────


class TestSetTaskRepoGitRootResolution:
    def test_rebind_resolves_subdir_to_git_root(self, isolated_orbit):
        """``/orbit:go`` mismatch flow rebinding via ``set_task_repo`` must
        resolve to the git root the same way ``create_orbit_files`` does,
        otherwise the bug recurs at a different entry point.

        Set up directly via the DB layer to avoid the orbit-files
        scan-by-repo-path coupling that ``create_orbit_files`` exercises;
        this test only cares about the resolution behavior of the
        ``set_task_repo`` wrapper.
        """
        repo_root = _make_git_repo(isolated_orbit / "myrepo")
        subdir = repo_root / "deep" / "nested"
        subdir.mkdir(parents=True)

        db = db_module.get_db()
        repo_id = db.add_repo(str(repo_root))
        task = db.create_task(name="rebind-test", repo_id=repo_id)

        result = asyncio.run(
            tools_tracking.set_task_repo(
                task_id=task.id,
                repo_path=str(subdir),  # caller passes subdir, not git root
            )
        )

        # The repo at the resolved git-root path is already registered
        # (we registered repo_root above) so the rebind succeeds. The
        # task's repo binding stays the same (already at repo_root).
        assert "error" not in result
        assert result["repo_path"] == str(repo_root.resolve())

    def test_set_task_repo_empty_path_raises_with_default_resolve(self, isolated_orbit):
        """Codex P2 finding mirror for set_task_repo: empty repo_path
        used to silently resolve to the MCP server's cwd. Now raises."""
        repo_root = _make_git_repo(isolated_orbit / "myrepo")
        db = db_module.get_db()
        repo_id = db.add_repo(str(repo_root))
        task = db.create_task(name="empty-path-rebind", repo_id=repo_id)

        result = asyncio.run(
            tools_tracking.set_task_repo(task_id=task.id, repo_path="")
        )
        assert result.get("error") is True
        assert "empty" in result.get("message", "").lower()

    def test_resolve_git_root_false_uses_subdir_directly(self, isolated_orbit):
        """Monorepo opt-out for set_task_repo: caller passes a subdir
        with ``resolve_git_root=False`` and the rebind targets the
        subdir directly. The subdir must already be registered."""
        repo_root = _make_git_repo(isolated_orbit / "monorepo")
        sub_package = repo_root / "packages" / "auth-service"
        sub_package.mkdir(parents=True)

        db = db_module.get_db()
        # Pre-register both: the original repo_root (where the task
        # currently lives) and the sub_package (rebind target).
        original_repo_id = db.add_repo(str(repo_root))
        sub_package_repo_id = db.add_repo(str(sub_package))
        task = db.create_task(name="monorepo-rebind", repo_id=original_repo_id)

        result = asyncio.run(
            tools_tracking.set_task_repo(
                task_id=task.id,
                repo_path=str(sub_package),
                resolve_git_root=False,
            )
        )

        assert "error" not in result
        assert result["repo_path"] == str(sub_package)
        assert result["repo_id"] == sub_package_repo_id
        assert result["changed"] is True

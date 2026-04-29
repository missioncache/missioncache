"""Tests for helper functions - no I/O, no mocking."""

from pathlib import Path

import pytest

from mcp_orbit.errors import ValidationError
from mcp_orbit.helpers import _resolve_to_git_root, _validate_path


class TestValidatePath:
    def test_empty_path(self):
        """Empty string raises ValidationError."""
        with pytest.raises(ValidationError, match="cannot be empty"):
            _validate_path("")

    def test_null_bytes(self):
        """Path with null bytes raises ValidationError."""
        with pytest.raises(ValidationError, match="null bytes"):
            _validate_path("/some/path\x00evil")

    def test_valid_path(self):
        """Valid path returns resolved Path object."""
        result = _validate_path("/tmp/test")
        assert isinstance(result, Path)
        assert result == Path("/tmp/test").resolve()

    def test_must_be_under_pass(self, tmp_path):
        """Path within required root passes validation."""
        child = tmp_path / "sub" / "file.txt"
        result = _validate_path(str(child), must_be_under=tmp_path)
        assert result == child.resolve()

    def test_must_be_under_fail(self, tmp_path):
        """Path outside required root raises ValidationError."""
        outside = Path("/tmp/outside_dir/file.txt")
        with pytest.raises(ValidationError, match="must be within"):
            _validate_path(str(outside), must_be_under=tmp_path)


class TestResolveToGitRoot:
    """Walks parents looking for ``.git`` to enforce git-root capture
    server-side. Mirrors ``git rev-parse --show-toplevel`` semantics
    so /orbit:new and /orbit:go capture the same path regardless of
    cwd within a repo."""

    @pytest.fixture(autouse=True)
    def _isolated_tmp_path(self, tmp_path):
        """Skip if any ancestor of tmp_path has .git, otherwise the
        walker climbs out of the test fixture and assertions below
        compare against the wrong path."""
        walker = tmp_path.resolve()
        while walker != walker.parent:
            if (walker / ".git").exists():
                pytest.skip(
                    f"tmp_path ancestor {walker} contains .git; cannot "
                    "test git-root walker in isolation"
                )
            walker = walker.parent

    def test_returns_self_when_path_is_git_root(self, tmp_path):
        (tmp_path / ".git").mkdir()
        assert _resolve_to_git_root(str(tmp_path)) == str(tmp_path)

    def test_walks_up_to_git_root_from_subdir(self, tmp_path):
        (tmp_path / ".git").mkdir()
        sub = tmp_path / "src" / "deep"
        sub.mkdir(parents=True)
        assert _resolve_to_git_root(str(sub)) == str(tmp_path)

    def test_handles_dot_git_as_file_for_submodules(self, tmp_path):
        """Submodules have a ``.git`` file (not directory) pointing at
        the parent's git dir. ``Path.exists()`` returns True for both,
        so both are correctly recognized as git roots."""
        (tmp_path / ".git").write_text("gitdir: ../.git/modules/sub")
        sub = tmp_path / "subdir"
        sub.mkdir()
        assert _resolve_to_git_root(str(sub)) == str(tmp_path)

    def test_falls_back_to_path_when_no_git_ancestor(self, tmp_path):
        """Non-git directories stay supported - the bug fix doesn't
        change the contract that ``repo_path`` may be any directory."""
        sub = tmp_path / "non_git_workspace"
        sub.mkdir()
        assert _resolve_to_git_root(str(sub)) == str(sub)

    def test_resolves_symlinks_before_walking(self, tmp_path):
        """A symlink to a subdir of a git repo lands on the real path."""
        (tmp_path / ".git").mkdir()
        real = tmp_path / "real" / "deep"
        real.mkdir(parents=True)
        link = tmp_path / "link"
        link.symlink_to(real)
        assert _resolve_to_git_root(str(link)) == str(tmp_path)

    def test_returns_first_git_ancestor_innermost_wins(self, tmp_path):
        """When nested git roots exist (e.g., monorepo with a submodule
        worktree underneath), the closest ancestor wins. Matches
        ``git rev-parse --show-toplevel`` semantics."""
        (tmp_path / ".git").mkdir()
        inner = tmp_path / "inner"
        inner.mkdir()
        (inner / ".git").mkdir()
        sub = inner / "deeper"
        sub.mkdir()
        assert _resolve_to_git_root(str(sub)) == str(inner)

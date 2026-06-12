"""Tests for the git branch and worktree directory name templates.

The ``missioncache-auto/`` branch prefix and the ``missioncache-auto-<task>-w<id>``
worktree directory name are the two literal strings that a mechanical
rename sweep is most likely to brush over without anyone noticing:

- ``WorktreeManager._branch_name`` returns
  ``f"missioncache-auto/{self.task_name}/worker-{worker_id}"``
- ``WorktreeManager._worktree_path`` returns
  ``<project_root>/.claude/worktrees/missioncache-auto-<task>-w<id>``

If either literal silently changes, downstream git operations
(``git worktree add``, ``git merge``, the conflict-branch report) all
still pass their unit tests because they don't pin the string. These
tests exercise the smallest unit that builds each name - calling the
private formatters directly - so no git subprocess is invoked.
"""

from pathlib import Path

from missioncache_auto.worktree import WorktreeManager


def _manager(tmp_path: Path, task_name: str = "sample-task") -> WorktreeManager:
    """Build a WorktreeManager without touching git.

    The constructor stores arguments and initializes an empty dict;
    nothing shells out, so a bare instance is enough to call the
    private name-formatters under test.
    """
    return WorktreeManager(
        project_root=tmp_path,
        task_name=task_name,
        num_workers=3,
    )


class TestBranchName:
    def test_starts_with_missioncache_auto_prefix(self, tmp_path):
        mgr = _manager(tmp_path)
        assert mgr._branch_name(0).startswith("missioncache-auto/")

    def test_exact_format(self, tmp_path):
        """Branch name is the exact literal the merge step references.

        ``_merge_branch`` interpolates ``info.branch`` into commit
        messages and ``git log`` ranges; if the format drifts, those
        commands still run but against the wrong branch name.
        """
        mgr = _manager(tmp_path, task_name="sample-task")
        assert mgr._branch_name(0) == "missioncache-auto/sample-task/worker-0"
        assert mgr._branch_name(7) == "missioncache-auto/sample-task/worker-7"

    def test_task_name_passes_through_verbatim(self, tmp_path):
        """The task_name segment is whatever the caller passed - no
        sanitization. A rename that injects a transformation here would
        break existing worktree resumption."""
        mgr = _manager(tmp_path, task_name="my-feature-123")
        assert mgr._branch_name(2) == "missioncache-auto/my-feature-123/worker-2"


class TestWorktreePath:
    def test_directory_name_format(self, tmp_path):
        """Directory name is ``missioncache-auto-<task>-w<id>`` directly under
        ``<project_root>/.claude/worktrees/``. This is the only place the
        dashed form of the prefix appears, so the rename sweep needs to
        catch it independently of the slash-form in _branch_name."""
        mgr = _manager(tmp_path, task_name="sample-task")
        path = mgr._worktree_path(0)
        assert path.name == "missioncache-auto-sample-task-w0"
        assert path == (
            tmp_path / ".claude" / "worktrees" / "missioncache-auto-sample-task-w0"
        )

    def test_worker_id_in_suffix(self, tmp_path):
        mgr = _manager(tmp_path, task_name="sample-task")
        assert mgr._worktree_path(5).name == "missioncache-auto-sample-task-w5"

    def test_parent_is_claude_worktrees(self, tmp_path):
        """The ``.claude/worktrees/`` parent path is referenced by
        cleanup_with_results when removing worktrees. Pin it so a
        rename to ``.orbit/worktrees/`` (or anywhere else) is caught."""
        mgr = _manager(tmp_path, task_name="any-task")
        path = mgr._worktree_path(0)
        assert path.parent == tmp_path / ".claude" / "worktrees"

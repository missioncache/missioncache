"""
Git worktree management for orbit-auto parallel execution.

Creates isolated worktrees for each worker and merges changes back
after execution completes.
"""

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class WorktreeInfo:
    """Information about a created worktree."""

    worker_id: int
    path: Path
    branch: str


class WorktreeManager:
    """
    Manages git worktrees for parallel worker isolation.

    Lifecycle:
    1. create_worktrees() - before workers start
    2. Workers use worktree paths as working directories
    3. merge_all() - after all workers finish
    4. cleanup_with_results() - remove worktrees, keep conflict branches
    """

    def __init__(
        self,
        project_root: Path,
        task_name: str,
        num_workers: int,
    ) -> None:
        self.project_root = project_root
        self.task_name = task_name
        self.num_workers = num_workers
        self.worktrees: dict[int, WorktreeInfo] = {}

    def _branch_name(self, worker_id: int) -> str:
        return f"orbit-auto/{self.task_name}/worker-{worker_id}"

    def _worktree_path(self, worker_id: int) -> Path:
        return (
            self.project_root
            / ".claude"
            / "worktrees"
            / f"orbit-auto-{self.task_name}-w{worker_id}"
        )

    def create_worktrees(self) -> dict[int, Path]:
        """
        Create a worktree for each worker.

        Returns:
            Mapping of worker_id -> worktree_path
        """
        result: dict[int, Path] = {}

        for worker_id in range(self.num_workers):
            wt_path = self._worktree_path(worker_id)
            branch = self._branch_name(worker_id)

            # Clean up stale worktree from interrupted run
            if wt_path.exists():
                self._remove_worktree(wt_path)
                self._delete_branch(branch, force=True)

            wt_path.parent.mkdir(parents=True, exist_ok=True)

            subprocess.run(
                ["git", "worktree", "add", "-b", branch, str(wt_path), "HEAD"],
                cwd=self.project_root,
                capture_output=True,
                text=True,
                check=True,
            )

            self._copy_env_files(wt_path)

            self.worktrees[worker_id] = WorktreeInfo(
                worker_id=worker_id,
                path=wt_path,
                branch=branch,
            )
            result[worker_id] = wt_path

        return result

    def _copy_env_files(self, worktree_path: Path) -> None:
        """Copy .env* files from project root to worktree."""
        for env_file in self.project_root.glob(".env*"):
            if env_file.is_file():
                shutil.copy2(env_file, worktree_path / env_file.name)

    def merge_all(self) -> list[dict]:
        """
        Merge all worktree branches back to the current branch.

        Merges sequentially in worker order. Skips worktrees with
        no new commits. Reports conflicts without failing.

        Returns:
            List of dicts with keys: worker_id, branch, status, message.
            Status is one of: "merged", "no_changes", "conflict".
        """
        original_branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=self.project_root,
            capture_output=True,
            text=True,
        ).stdout.strip()

        results = []
        for worker_id in sorted(self.worktrees.keys()):
            info = self.worktrees[worker_id]
            results.append(self._merge_branch(info, original_branch))
        return results

    def _merge_branch(self, info: WorktreeInfo, target_branch: str) -> dict:
        """Merge a single worktree branch into the target branch."""
        # Check if branch has commits ahead
        diff_result = subprocess.run(
            ["git", "log", f"{target_branch}..{info.branch}", "--oneline"],
            cwd=self.project_root,
            capture_output=True,
            text=True,
        )

        if not diff_result.stdout.strip():
            return {
                "worker_id": info.worker_id,
                "branch": info.branch,
                "status": "no_changes",
                "message": "No commits to merge",
            }

        commit_count = len(diff_result.stdout.strip().splitlines())

        merge_result = subprocess.run(
            [
                "git",
                "merge",
                info.branch,
                "--no-edit",
                "-m",
                f"Merge orbit-auto worker {info.worker_id} ({self.task_name})",
            ],
            cwd=self.project_root,
            capture_output=True,
            text=True,
        )

        if merge_result.returncode == 0:
            return {
                "worker_id": info.worker_id,
                "branch": info.branch,
                "status": "merged",
                "message": f"Merged {commit_count} commit(s)",
            }

        # Abort the failed merge
        subprocess.run(
            ["git", "merge", "--abort"],
            cwd=self.project_root,
            capture_output=True,
        )
        return {
            "worker_id": info.worker_id,
            "branch": info.branch,
            "status": "conflict",
            "message": f"Merge conflict - branch '{info.branch}' preserved for manual resolution",
        }

    def cleanup_with_results(self, merge_results: list[dict]) -> None:
        """
        Clean up worktrees, keeping branches that had conflicts.

        Args:
            merge_results: Results from merge_all()
        """
        conflict_branches = {r["branch"] for r in merge_results if r["status"] == "conflict"}

        for info in self.worktrees.values():
            self._remove_worktree(info.path)
            if info.branch not in conflict_branches:
                self._delete_branch(info.branch)

    def _remove_worktree(self, path: Path) -> None:
        """Remove a git worktree."""
        subprocess.run(
            ["git", "worktree", "remove", str(path), "--force"],
            cwd=self.project_root,
            capture_output=True,
        )
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=self.project_root,
            capture_output=True,
        )

    def _delete_branch(self, branch: str, force: bool = False) -> None:
        """Delete a local branch. Uses safe -d by default, -D if force=True."""
        flag = "-D" if force else "-d"
        result = subprocess.run(
            ["git", "branch", flag, branch],
            cwd=self.project_root,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 and not force:
            # Branch has unmerged commits - warn instead of silently failing
            print(
                f"Warning: branch '{branch}' has unmerged commits. Preserving for manual review.",
                file=sys.stderr,
            )

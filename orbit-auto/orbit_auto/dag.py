"""
DAG (Directed Acyclic Graph) builder and analyzer for task dependencies.

Parses dependency information from prompt YAML frontmatter and builds
a directed acyclic graph for parallel task execution.
"""

from collections import defaultdict
from pathlib import Path

from orbit_auto.models import Task, TaskStatus


class CycleDetectedError(Exception):
    """Raised when a cycle is detected in the dependency graph."""

    pass


class DAG:
    """
    Directed Acyclic Graph for task dependency management.

    Supports:
    - Building from prompt files with YAML frontmatter
    - Cycle detection
    - Topological sorting
    - Wave computation for parallel execution
    - Critical path analysis
    """

    def __init__(self) -> None:
        self._adjacency: dict[str, list[str]] = {}
        self._titles: dict[str, str] = {}

    def add_task(self, task_id: str, dependencies: list[str], title: str = "") -> None:
        """Add a task with its dependencies to the graph."""
        self._adjacency[task_id] = list(dependencies)
        if title:
            self._titles[task_id] = title

    @property
    def tasks(self) -> list[str]:
        """Get all task IDs in sorted order."""
        return sorted(self._adjacency.keys())

    @property
    def task_count(self) -> int:
        """Get the total number of tasks."""
        return len(self._adjacency)

    def get_dependencies(self, task_id: str) -> list[str]:
        """Get dependencies for a specific task."""
        return self._adjacency.get(task_id, [])

    def get_title(self, task_id: str) -> str:
        """Get the title for a task."""
        return self._titles.get(task_id, f"Task {task_id}")

    @classmethod
    def build_from_prompts(cls, prompts_dir: Path) -> "DAG":
        """Build a DAG from prompt files in a directory."""
        dag = cls()
        prompt_files = sorted(prompts_dir.glob("task-*-prompt.md"))

        if not prompt_files:
            raise ValueError(f"No prompt files found in {prompts_dir}")

        for prompt_file in prompt_files:
            task_id = _get_task_id(prompt_file)
            if task_id is None:
                continue

            deps = _get_dependencies(prompt_file, task_id)
            title = _get_task_title(prompt_file)
            dag.add_task(task_id, deps, title or "")

        return dag

    @classmethod
    def build_from_adjacency_list(cls, adjacency: dict[str, list[str]]) -> "DAG":
        """Build a DAG from an adjacency list dictionary."""
        dag = cls()
        for task_id, deps in adjacency.items():
            dag.add_task(task_id, deps)
        return dag

    def detect_cycles(self) -> bool:
        """
        Detect cycles in the dependency graph using DFS.

        Returns True if no cycles found, raises CycleDetectedError if cycle found.
        """
        visited: set[str] = set()
        rec_stack: set[str] = set()
        path: list[str] = []

        def dfs(node: str) -> bool:
            visited.add(node)
            rec_stack.add(node)
            path.append(node)

            for dep in self._adjacency.get(node, []):
                if dep not in visited:
                    if not dfs(dep):
                        return False
                elif dep in rec_stack:
                    cycle_path = " -> ".join(path) + f" -> {dep}"
                    raise CycleDetectedError(f"Cycle detected: {cycle_path}")

            rec_stack.remove(node)
            path.pop()
            return True

        for task in self._adjacency:
            if task not in visited:
                if not dfs(task):
                    return False

        return True

    def topological_sort(self) -> list[str]:
        """Perform topological sort using Kahn's algorithm."""
        # Compute in-degrees
        in_degree = {task: len(deps) for task, deps in self._adjacency.items()}

        # Find nodes with no dependencies
        queue = sorted([t for t, d in in_degree.items() if d == 0])
        result: list[str] = []

        while queue:
            current = queue.pop(0)
            result.append(current)

            # For each task that depends on current
            for task, deps in self._adjacency.items():
                if current in deps:
                    in_degree[task] -= 1
                    if in_degree[task] == 0:
                        queue.append(task)
                        queue.sort()  # Maintain sorted order for determinism

        return result

    def get_waves(self) -> list[dict[str, list[str] | int]]:
        """
        Compute execution waves - groups of tasks that can run in parallel.

        Returns list of wave dicts with 'wave' (int) and 'tasks' (list[str]) keys.
        """
        task_wave: dict[str, int] = {}

        def get_wave(task: str) -> int:
            if task in task_wave:
                return task_wave[task]

            deps = self._adjacency.get(task, [])
            if not deps:
                task_wave[task] = 1
                return 1

            max_dep_wave = max(get_wave(d) for d in deps)
            wave = max_dep_wave + 1
            task_wave[task] = wave
            return wave

        # Compute waves for all tasks
        for task in self._adjacency:
            get_wave(task)

        # Group by wave
        wave_tasks: dict[int, list[str]] = defaultdict(list)
        for task, wave in task_wave.items():
            wave_tasks[wave].append(task)

        # Build result
        result = []
        for w in sorted(wave_tasks.keys()):
            tasks = sorted(wave_tasks[w])
            result.append({"wave": w, "tasks": tasks})

        return result

    def get_critical_path(self) -> tuple[int, list[str]]:
        """
        Find the critical path (longest dependency chain).

        Returns (length, path) tuple.
        """
        longest_path: dict[str, int] = {}
        path_via: dict[str, str | None] = {}

        def compute_longest(task: str) -> int:
            if task in longest_path:
                return longest_path[task]

            deps = self._adjacency.get(task, [])
            if not deps:
                longest_path[task] = 1
                path_via[task] = None
                return 1

            max_len = 0
            max_dep: str | None = None
            for dep in deps:
                dep_len = compute_longest(dep)
                if dep_len > max_len:
                    max_len = dep_len
                    max_dep = dep

            length = max_len + 1
            longest_path[task] = length
            path_via[task] = max_dep
            return length

        # Find task with longest path
        max_length = 0
        end_task: str | None = None
        for task in self._adjacency:
            length = compute_longest(task)
            if length > max_length:
                max_length = length
                end_task = task

        # Reconstruct path
        path: list[str] = []
        current: str | None = end_task
        while current:
            path.insert(0, current)
            current = path_via.get(current)

        return max_length, path

    def get_ready_tasks(
        self,
        completed: set[str],
        in_progress: set[str],
    ) -> list[str]:
        """Get all tasks that are ready to run (dependencies satisfied)."""
        ready = []
        for task in self._adjacency:
            if task in completed or task in in_progress:
                continue

            deps = self._adjacency.get(task, [])
            if all(dep in completed for dep in deps):
                ready.append(task)

        return sorted(ready)

    def deps_satisfied(self, task_id: str, completed: set[str]) -> bool:
        """Check if all dependencies for a task are satisfied."""
        deps = self._adjacency.get(task_id, [])
        return all(dep in completed for dep in deps)

    def get_wave_counts(self) -> dict[str, int | list[dict[str, int | str]]]:
        """Get task counts per wave."""
        waves = self.get_waves()
        return {
            "total": len(self._adjacency),
            "waves": [{"wave": w["wave"], "count": len(w["tasks"])} for w in waves],
        }

    def to_adjacency_file(self, output_file: Path) -> None:
        """Write adjacency list to file."""
        with open(output_file, "w") as f:
            for task_id in sorted(self._adjacency.keys()):
                deps = self._adjacency[task_id]
                deps_csv = ",".join(deps)
                f.write(f"{task_id}:{deps_csv}\n")

    @classmethod
    def from_adjacency_file(cls, adjacency_file: Path) -> "DAG":
        """Load DAG from adjacency list file."""
        dag = cls()
        with open(adjacency_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                task_id, deps_str = line.split(":", 1)
                deps = [d for d in deps_str.split(",") if d]
                dag.add_task(task_id, deps)
        return dag


def _get_task_id(prompt_file: Path) -> str | None:
    """Extract task_id from prompt file's YAML frontmatter."""
    import re

    if not prompt_file.exists():
        return None

    content = prompt_file.read_text()
    match = re.search(r'^task_id:\s*["\']?([^"\'\n]+)["\']?', content, re.MULTILINE)
    return match.group(1).strip() if match else None


def _get_dependencies(prompt_file: Path, task_id: str) -> list[str]:
    """Extract dependencies from prompt file's YAML frontmatter."""
    import re

    if not prompt_file.exists():
        return []

    content = prompt_file.read_text()
    match = re.search(r"^dependencies:\s*\[(.*?)\]", content, re.MULTILINE)

    if not match:
        # No dependencies field - check for implicit dependency
        num_match = re.match(r"^0*(\d+)$", task_id)
        if num_match:
            num = int(num_match.group(1))
            if num > 1:
                return [f"{num - 1:02d}"]
        return []

    # Parse array: ["01", "03"] -> ["01", "03"]
    deps_str = match.group(1)
    if not deps_str.strip():
        return []

    deps = re.findall(r'["\']([^"\']+)["\']', deps_str)
    return [d.strip() for d in deps if d.strip()]


def _get_task_title(prompt_file: Path) -> str | None:
    """Extract task_title from prompt file's YAML frontmatter."""
    import re

    if not prompt_file.exists():
        return None

    content = prompt_file.read_text()
    match = re.search(r'^task_title:\s*["\']?([^"\'\n]+)["\']?', content, re.MULTILINE)
    return match.group(1).strip() if match else None

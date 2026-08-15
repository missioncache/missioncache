"""Worker must survive pickling.

On Windows the only multiprocessing start method is "spawn", which pickles the
target and its args to send to the child. parallel.py constructs a Worker and
immediately hands worker.run to multiprocessing.Process, so a Worker that
cannot pickle would break parallel mode on Windows only - invisible on macOS
(spawn default since 3.8, but this repo's CI does not run parallel mode) and
Linux (fork, no pickling). This test pins the invariant so a future field (an
open DB connection, a lock, a file handle) fails loudly here instead.
"""

# pickle is safe here: every object round-tripped is one this test constructs
# itself (never untrusted input). Picklability is exactly the invariant under test.
import pickle
from pathlib import Path

from missioncache_auto.models import Visibility
from missioncache_auto.worker import Worker


def _make_worker(**overrides) -> Worker:
    kwargs = dict(
        worker_id=0,
        task_name="demo",
        project_root=Path("/tmp/proj"),
        state_dir=Path("/tmp/state"),
        prompts_dir=Path("/tmp/prompts"),
        adjacency_file=Path("/tmp/adjacency.txt"),
        logs_dir=Path("/tmp/logs"),
        max_retries=2,
        task_timeout=1800,
        visibility=Visibility.NONE,
        execution_id=None,
        enable_review=False,
        spec_review_only=False,
        auto_commit=False,
        tdd_mode=False,
    )
    kwargs.update(overrides)
    return Worker(**kwargs)


def test_worker_round_trips_through_pickle():
    worker = _make_worker()
    restored = pickle.loads(pickle.dumps(worker))
    assert restored.worker_id == worker.worker_id
    assert restored.task_name == worker.task_name


def test_worker_with_execution_id_round_trips():
    """execution_id triggers the lazy _WorkerDBLogger path; the DB connection is
    lazy (None at construction), so the Worker still pickles."""
    worker = _make_worker(execution_id=42)
    restored = pickle.loads(pickle.dumps(worker))
    assert restored.execution_id == 42


def test_worker_run_is_pickable_as_the_spawn_target():
    """multiprocessing.Process(target=worker.run) pickles the bound method."""
    worker = _make_worker()
    restored = pickle.loads(pickle.dumps(worker.run))
    assert callable(restored)

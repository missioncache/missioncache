"""Integration tests for orbit-auto StateManager.

Tests use tmp_path for the state directory and real file I/O.
"""

import pytest

from orbit_auto.dag import DAG
from orbit_auto.models import TaskStatus
from orbit_auto.state import StateManager


@pytest.fixture
def state_dir(tmp_path):
    """Temporary directory for state files."""
    d = tmp_path / "state"
    d.mkdir()
    return d


@pytest.fixture
def sm(state_dir):
    """StateManager backed by a temporary directory."""
    return StateManager(state_dir)


@pytest.fixture
def no_deps_dag():
    """DAG where all tasks have no dependencies."""
    return DAG.build_from_adjacency_list({"01": [], "02": [], "03": []})


@pytest.fixture
def linear_dag():
    """DAG: 01 -> 02 -> 03."""
    return DAG.build_from_adjacency_list(
        {"01": [], "02": ["01"], "03": ["02"]}
    )


# ── init ──────────────────────────────────────────────────────────────────


class TestInit:
    def test_creates_state_file(self, sm):
        """init creates the state.json file."""
        sm.init(["01", "02"])
        assert sm.state_file.exists()

    def test_with_pre_completed(self, sm):
        """init marks pre-completed tasks as COMPLETED."""
        state = sm.init(["01", "02", "03"], pre_completed={"01"})
        assert state.tasks["01"].status == TaskStatus.COMPLETED
        assert state.tasks["02"].status == TaskStatus.PENDING
        assert state.tasks["03"].status == TaskStatus.PENDING


# ── claim_task ────────────────────────────────────────────────────────────


class TestClaimTask:
    def test_claim_respects_deps(self, sm, linear_dag):
        """claim_task only returns tasks whose dependencies are satisfied."""
        sm.init(["01", "02", "03"])

        # Only 01 should be claimable (02 depends on 01, 03 depends on 02)
        claimed = sm.claim_task(worker_id=1, dag=linear_dag)
        assert claimed == "01"

        # 02 should not be claimable yet (01 is IN_PROGRESS, not COMPLETED)
        claimed2 = sm.claim_task(worker_id=2, dag=linear_dag)
        assert claimed2 is None

    def test_claim_returns_none_when_empty(self, sm, no_deps_dag):
        """claim_task returns None when all tasks are claimed or done."""
        sm.init(["01"])
        sm.claim_task(worker_id=1, dag=no_deps_dag)  # claims 01

        result = sm.claim_task(worker_id=2, dag=no_deps_dag)
        assert result is None


# ── complete_task / fail_task ─────────────────────────────────────────────


class TestCompleteAndFail:
    def test_complete_task(self, sm, no_deps_dag):
        """complete_task sets status to COMPLETED."""
        sm.init(["01"])
        sm.claim_task(worker_id=1, dag=no_deps_dag)
        sm.complete_task("01")

        state = sm.read()
        assert state.tasks["01"].status == TaskStatus.COMPLETED

    def test_fail_task_with_message(self, sm, no_deps_dag):
        """fail_task sets status to FAILED and stores error_message."""
        sm.init(["01"])
        sm.claim_task(worker_id=1, dag=no_deps_dag)
        sm.fail_task("01", error_message="something broke")

        state = sm.read()
        assert state.tasks["01"].status == TaskStatus.FAILED
        assert state.tasks["01"].error_message == "something broke"


# ── release_task ──────────────────────────────────────────────────────────


class TestReleaseTask:
    def test_release_under_max_retries(self, sm, no_deps_dag):
        """release_task returns 'released' and resets to PENDING when under max_retries."""
        sm.init(["01"])
        sm.claim_task(worker_id=1, dag=no_deps_dag)  # attempts becomes 1

        result = sm.release_task("01", max_retries=3)
        assert result == "released"

        state = sm.read()
        assert state.tasks["01"].status == TaskStatus.PENDING

    def test_release_at_max_retries(self, sm, no_deps_dag):
        """release_task returns 'max_retries_reached' and marks FAILED at limit."""
        sm.init(["01"])
        # Claim 3 times (attempts = 1, then release, then claim again...)
        # Simpler: claim once (attempts=1), then set max_retries=1
        sm.claim_task(worker_id=1, dag=no_deps_dag)  # attempts = 1

        result = sm.release_task("01", max_retries=1)
        assert result == "max_retries_reached"

        state = sm.read()
        assert state.tasks["01"].status == TaskStatus.FAILED


# ── get_progress / is_complete ────────────────────────────────────────────


class TestProgress:
    def test_get_progress(self, sm, no_deps_dag):
        """get_progress returns (completed, total) counts."""
        sm.init(["01", "02", "03"])
        sm.claim_task(worker_id=1, dag=no_deps_dag)
        sm.complete_task("01")

        completed, total = sm.get_progress()
        assert completed == 1
        assert total == 3

    def test_is_complete(self, sm, no_deps_dag):
        """is_complete returns True only when no PENDING/IN_PROGRESS tasks remain."""
        sm.init(["01"])
        assert not sm.is_complete()

        sm.claim_task(worker_id=1, dag=no_deps_dag)
        assert not sm.is_complete()

        sm.complete_task("01")
        assert sm.is_complete()


# ── sync_to_tasks_md ─────────────────────────────────────────────────────


class TestSyncToTasksMd:
    def test_sync_marks_checkboxes(self, sm, no_deps_dag, tmp_path):
        """sync_to_tasks_md converts '- [ ] N.' to '- [x] N.' for completed tasks."""
        tasks_md = tmp_path / "tasks.md"
        tasks_md.write_text(
            "# Tasks\n\n"
            "- [ ] 1. First task\n"
            "- [ ] 2. Second task\n"
            "- [ ] 3. Third task\n"
        )

        sm.init(["01", "02", "03"])
        sm.claim_task(worker_id=1, dag=no_deps_dag)
        sm.complete_task("01")
        sm.claim_task(worker_id=1, dag=no_deps_dag)
        sm.complete_task("02")

        sm.sync_to_tasks_md(tasks_md)

        content = tasks_md.read_text()
        assert "- [x] 1." in content
        assert "- [x] 2." in content
        assert "- [ ] 3." in content

"""Tests for the TaskDB planning layer (plans, agent executions, dependencies).

The contract source is mcp-server/src/mcp_missioncache/tools_planning.py: these
methods implement exactly the interface those tools call.
"""

import pytest

from missioncache_db import TaskDB


@pytest.fixture
def db(tmp_path):
    """TaskDB backed by a temporary SQLite database."""
    db_path = tmp_path / "test.db"
    db = TaskDB(db_path=db_path)
    db.initialize()
    yield db
    db.close()


class TestPlans:
    def test_create_and_get_roundtrip(self, db):
        plan_id = db.create_plan("my plan", None, {"key": "value"})
        plan = db.get_plan(plan_id)
        assert plan["id"] == plan_id
        assert plan["name"] == "my plan"
        assert plan["status"] == "draft"
        assert plan["metadata"] == {"key": "value"}
        assert plan["started_at"] is None
        assert plan["completed_at"] is None

    def test_create_without_metadata(self, db):
        plan_id = db.create_plan("bare plan")
        plan = db.get_plan(plan_id)
        assert plan["metadata"] is None
        assert plan["task_id"] is None

    def test_create_with_task_id(self, db):
        task = db.create_task("planning-target", task_type="non-coding")
        plan_id = db.create_plan("linked", task.id)
        assert db.get_plan(plan_id)["task_id"] == task.id

    def test_get_missing_plan_returns_none(self, db):
        assert db.get_plan(9999) is None

    def test_update_status_to_running_stamps_started_at(self, db):
        plan_id = db.create_plan("p")
        plan = db.update_plan_status(plan_id, "running")
        assert plan["status"] == "running"
        assert plan["started_at"] is not None
        assert plan["completed_at"] is None

    @pytest.mark.parametrize("final", ["completed", "failed"])
    def test_update_status_to_final_stamps_completed_at(self, db, final):
        plan_id = db.create_plan("p")
        plan = db.update_plan_status(plan_id, final)
        assert plan["status"] == final
        assert plan["completed_at"] is not None

    def test_update_plan_keyword_status(self, db):
        # complete_plan calls update_plan(plan_id, status=...) directly
        plan_id = db.create_plan("p")
        plan = db.update_plan(plan_id, status="completed")
        assert plan["status"] == "completed"
        assert plan["completed_at"] is not None

    def test_update_plan_no_fields_returns_current(self, db):
        plan_id = db.create_plan("p")
        assert db.update_plan(plan_id)["status"] == "draft"

    def test_update_missing_plan_returns_none(self, db):
        assert db.update_plan(9999, status="completed") is None


class TestAgentExecutions:
    def test_register_and_list_ordered(self, db):
        plan_id = db.create_plan("p")
        db.add_agent_execution(plan_id=plan_id, agent_id="02", agent_name="b", prompt="p2")
        db.add_agent_execution(plan_id=plan_id, agent_id="01", agent_name="a", prompt="p1")
        agents = db.get_plan_agents(plan_id)
        assert [a["agent_id"] for a in agents] == ["01", "02"]
        assert all(a["status"] == "pending" for a in agents)
        assert agents[0]["agent_name"] == "a"
        assert agents[0]["prompt"] == "p1"
        assert agents[0]["max_attempts"] == 3

    def test_empty_plan_has_no_agents(self, db):
        plan_id = db.create_plan("p")
        assert db.get_plan_agents(plan_id) == []

    def test_update_by_plan_and_agent_id(self, db):
        plan_id = db.create_plan("p")
        db.add_agent_execution(plan_id=plan_id, agent_id="01")
        agent = db.update_agent_execution(plan_id=plan_id, agent_id="01", status="running")
        assert agent["status"] == "running"
        assert agent["started_at"] is not None
        agent = db.update_agent_execution(
            plan_id=plan_id, agent_id="01", status="completed",
            output={"result": "done"},
        )
        assert agent["status"] == "completed"
        assert agent["completed_at"] is not None
        assert agent["result"] == '{"result": "done"}'

    def test_update_failed_with_error(self, db):
        plan_id = db.create_plan("p")
        db.add_agent_execution(plan_id=plan_id, agent_id="01")
        agent = db.update_agent_execution(
            plan_id=plan_id, agent_id="01", status="failed", error="boom",
        )
        assert agent["status"] == "failed"
        assert agent["error_message"] == "boom"

    def test_update_missing_agent_returns_none(self, db):
        plan_id = db.create_plan("p")
        assert db.update_agent_execution(plan_id=plan_id, agent_id="99", status="failed") is None

    def test_running_transition_counts_an_attempt(self, db):
        plan_id = db.create_plan("p")
        db.add_agent_execution(plan_id=plan_id, agent_id="01")
        agent = db.update_agent_execution(plan_id=plan_id, agent_id="01", status="running")
        assert agent["attempt_count"] == 1
        agent = db.update_agent_execution(plan_id=plan_id, agent_id="01", status="completed")
        assert agent["attempt_count"] == 1

    def test_duplicate_agent_registration_rejected(self, db):
        import sqlite3

        plan_id = db.create_plan("p")
        db.add_agent_execution(plan_id=plan_id, agent_id="01")
        with pytest.raises(sqlite3.IntegrityError):
            db.add_agent_execution(plan_id=plan_id, agent_id="01")


class TestAgentDependencies:
    def test_add_and_get_sorted(self, db):
        plan_id = db.create_plan("p")
        db.add_agent_dependency(plan_id, "03", "02")
        db.add_agent_dependency(plan_id, "03", "01")
        assert db.get_agent_dependencies(plan_id, "03") == ["01", "02"]

    def test_duplicate_edge_is_idempotent(self, db):
        plan_id = db.create_plan("p")
        first = db.add_agent_dependency(plan_id, "02", "01")
        second = db.add_agent_dependency(plan_id, "02", "01")
        assert first == second
        assert db.get_agent_dependencies(plan_id, "02") == ["01"]

    def test_self_dependency_raises(self, db):
        plan_id = db.create_plan("p")
        with pytest.raises(ValueError):
            db.add_agent_dependency(plan_id, "01", "01")

    def test_no_dependencies_returns_empty(self, db):
        plan_id = db.create_plan("p")
        assert db.get_agent_dependencies(plan_id, "01") == []


class TestSchemaMigration:
    def test_existing_db_gains_plan_tables_on_reopen(self, tmp_path):
        """A DB created before the planning tables self-migrates on open."""
        import sqlite3

        db_path = tmp_path / "old.db"
        db = TaskDB(db_path=db_path)
        db.initialize()
        db.close()

        # Strip the planning tables to simulate a pre-upgrade DB
        conn = sqlite3.connect(db_path)
        for table in ("agent_dependencies", "agent_executions", "plans"):
            conn.execute(f"DROP TABLE {table}")
        conn.commit()
        conn.close()

        db = TaskDB(db_path=db_path)
        plan_id = db.create_plan("post-migration plan")
        assert db.get_plan(plan_id)["name"] == "post-migration plan"
        db.close()

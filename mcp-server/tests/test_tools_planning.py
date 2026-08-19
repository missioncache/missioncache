"""Tests for the planning MCP tools (plans, agents, dependencies, spawn).

Spec source: the tool docstrings in tools_planning.py - the workflow is
create_plan -> register_agent_execution -> get_ready_agents /
spawn_parallel_agents -> update_agent_status -> complete_plan. Ready
semantics per get_ready_agents: pending agents whose dependencies are all
completed; failed dependencies do NOT block.

Async wrappers called via ``asyncio.run`` (same pattern as test_tools_pm.py).
"""

import asyncio

import pytest

from mcp_missioncache import tools_planning


@pytest.fixture
def plan_id(isolated_orbit):
    """A draft plan in an isolated DB."""
    result = asyncio.run(tools_planning.create_plan(name="test plan"))
    assert not result.get("error"), result
    return result["plan_id"]


def _register(plan_id, agent_id, dependencies=None):
    return asyncio.run(tools_planning.register_agent_execution(
        plan_id=plan_id,
        agent_id=agent_id,
        agent_name=f"agent {agent_id}",
        prompt=f"do thing {agent_id}",
        dependencies=dependencies,
    ))


class TestCreatePlan:
    def test_create_returns_draft(self, isolated_orbit):
        result = asyncio.run(tools_planning.create_plan(name="p"))
        assert result["status"] == "draft"
        assert isinstance(result["plan_id"], int)

    def test_invalid_metadata_json(self, isolated_orbit):
        result = asyncio.run(tools_planning.create_plan(name="p", metadata="{not json"))
        assert result["error"] is True
        assert result["code"] == "VALIDATION_ERROR"

    def test_unknown_task_id(self, isolated_orbit):
        result = asyncio.run(tools_planning.create_plan(name="p", task_id=9999))
        assert result["error"] is True


class TestRegisterAgent:
    def test_register_with_dependencies(self, plan_id):
        _register(plan_id, "01")
        result = _register(plan_id, "02", dependencies=["01"])
        assert result["agent_id"] == "02"
        assert result["dependencies"] == ["01"]
        assert len(result["dependency_records"]) == 1


class TestUpdateAgentStatus:
    def test_invalid_status_rejected(self, plan_id):
        _register(plan_id, "01")
        result = asyncio.run(tools_planning.update_agent_status(
            plan_id=plan_id, agent_id="01", status="bogus"))
        assert result["error"] is True
        assert result["code"] == "VALIDATION_ERROR"

    def test_unknown_agent(self, plan_id):
        result = asyncio.run(tools_planning.update_agent_status(
            plan_id=plan_id, agent_id="99", status="running"))
        assert result["error"] is True
        assert result["code"] == "NOT_FOUND"

    def test_running_agent_moves_plan_to_running(self, plan_id):
        _register(plan_id, "01")
        result = asyncio.run(tools_planning.update_agent_status(
            plan_id=plan_id, agent_id="01", status="running"))
        assert result["updated"] is True
        status = asyncio.run(tools_planning.get_plan_status(plan_id))
        assert status["plan"]["status"] == "running"

    def test_all_completed_moves_plan_to_completed(self, plan_id):
        _register(plan_id, "01")
        _register(plan_id, "02")
        for agent in ("01", "02"):
            asyncio.run(tools_planning.update_agent_status(
                plan_id=plan_id, agent_id=agent, status="completed",
                result="done"))
        status = asyncio.run(tools_planning.get_plan_status(plan_id))
        assert status["plan"]["status"] == "completed"
        assert status["summary"]["completed"] == 2

    def test_any_failed_moves_plan_to_failed(self, plan_id):
        _register(plan_id, "01")
        _register(plan_id, "02")
        asyncio.run(tools_planning.update_agent_status(
            plan_id=plan_id, agent_id="01", status="completed"))
        asyncio.run(tools_planning.update_agent_status(
            plan_id=plan_id, agent_id="02", status="failed",
            error_message="boom"))
        status = asyncio.run(tools_planning.get_plan_status(plan_id))
        assert status["plan"]["status"] == "failed"
        assert status["summary"]["failed"] == 1


class TestGetPlanStatus:
    def test_unknown_plan(self, isolated_orbit):
        result = asyncio.run(tools_planning.get_plan_status(9999))
        assert result["error"] is True
        assert result["code"] == "NOT_FOUND"

    def test_summary_counts(self, plan_id):
        _register(plan_id, "01")
        _register(plan_id, "02", dependencies=["01"])
        status = asyncio.run(tools_planning.get_plan_status(plan_id))
        assert status["summary"]["total"] == 2
        assert status["summary"]["pending"] == 2
        assert len(status["agents"]) == 2


class TestGetReadyAgents:
    def test_no_dependency_is_ready(self, plan_id):
        _register(plan_id, "01")
        _register(plan_id, "02", dependencies=["01"])
        result = asyncio.run(tools_planning.get_ready_agents(plan_id))
        assert [a["agent_id"] for a in result["ready_agents"]] == ["01"]

    def test_dependent_becomes_ready_after_completion(self, plan_id):
        _register(plan_id, "01")
        _register(plan_id, "02", dependencies=["01"])
        asyncio.run(tools_planning.update_agent_status(
            plan_id=plan_id, agent_id="01", status="completed"))
        result = asyncio.run(tools_planning.get_ready_agents(plan_id))
        assert [a["agent_id"] for a in result["ready_agents"]] == ["02"]

    def test_failed_dependency_does_not_unblock(self, plan_id):
        # A failed dependency is not "completed", so the dependent stays
        # un-ready (the plan completes partially, per the tool docstring).
        _register(plan_id, "01")
        _register(plan_id, "02", dependencies=["01"])
        asyncio.run(tools_planning.update_agent_status(
            plan_id=plan_id, agent_id="01", status="failed"))
        result = asyncio.run(tools_planning.get_ready_agents(plan_id))
        assert result["ready_agents"] == []

    def test_empty_plan(self, plan_id):
        result = asyncio.run(tools_planning.get_ready_agents(plan_id))
        assert result == {"ready_agents": [], "count": 0}

    def test_unknown_plan(self, isolated_orbit):
        result = asyncio.run(tools_planning.get_ready_agents(9999))
        assert result["error"] is True


class TestSpawnParallelAgents:
    def test_task_calls_carry_callback_instructions(self, plan_id):
        _register(plan_id, "01")
        _register(plan_id, "02")
        result = asyncio.run(tools_planning.spawn_parallel_agents(plan_id))
        assert result["ready_count"] == 2
        assert len(result["task_calls"]) == 2
        call = result["task_calls"][0]
        assert call["subagent_type"] == "general-purpose"
        assert call["run_in_background"] is True
        assert "update_agent_status" in call["prompt"]
        assert f"plan_id={plan_id}" in call["prompt"]

    def test_nothing_ready(self, plan_id):
        _register(plan_id, "02", dependencies=["01"])
        result = asyncio.run(tools_planning.spawn_parallel_agents(plan_id))
        assert result["task_calls"] == []
        assert result["ready_count"] == 0

    def test_unknown_plan(self, isolated_orbit):
        result = asyncio.run(tools_planning.spawn_parallel_agents(9999))
        assert result["error"] is True


class TestCompletePlan:
    def test_complete(self, plan_id):
        result = asyncio.run(tools_planning.complete_plan(plan_id))
        assert result["completed"] is True
        status = asyncio.run(tools_planning.get_plan_status(plan_id))
        assert status["plan"]["status"] == "completed"
        assert status["plan"]["completed_at"] is not None

    def test_invalid_final_status(self, plan_id):
        result = asyncio.run(tools_planning.complete_plan(plan_id, status="draft"))
        assert result["error"] is True
        assert result["code"] == "VALIDATION_ERROR"

    def test_unknown_plan(self, isolated_orbit):
        result = asyncio.run(tools_planning.complete_plan(9999))
        assert result["error"] is True


class TestFullFlow:
    def test_three_agents_one_dependency_end_to_end(self, plan_id):
        """The whole advertised workflow in one pass."""
        _register(plan_id, "01")
        _register(plan_id, "02")
        _register(plan_id, "03", dependencies=["01", "02"])

        spawn = asyncio.run(tools_planning.spawn_parallel_agents(plan_id))
        assert spawn["ready_count"] == 2

        for agent in ("01", "02"):
            asyncio.run(tools_planning.update_agent_status(
                plan_id=plan_id, agent_id=agent, status="running"))
            asyncio.run(tools_planning.update_agent_status(
                plan_id=plan_id, agent_id=agent, status="completed",
                result=f"agent {agent} done"))

        spawn = asyncio.run(tools_planning.spawn_parallel_agents(plan_id))
        assert spawn["ready_count"] == 1
        assert "03" in spawn["task_calls"][0]["prompt"]

        asyncio.run(tools_planning.update_agent_status(
            plan_id=plan_id, agent_id="03", status="completed", result="done"))

        status = asyncio.run(tools_planning.get_plan_status(plan_id))
        assert status["plan"]["status"] == "completed"
        assert status["summary"] == {
            "total": 3, "pending": 0, "blocked": 0,
            "running": 0, "completed": 3, "failed": 0,
        }

        final = asyncio.run(tools_planning.complete_plan(plan_id))
        assert final["completed"] is True

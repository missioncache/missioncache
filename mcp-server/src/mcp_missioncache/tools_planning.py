"""Parallel execution planning MCP tools - plans, agents, dependencies."""

import json
import logging
from typing import Annotated

from pydantic import Field

from .app import mcp
from .db import get_db
from .errors import OrbitError, TaskNotFoundError

logger = logging.getLogger(__name__)


# =============================================================================
# HELPERS
# =============================================================================


def _update_plan_counters(plan_id: int) -> None:
    """Update plan status based on agent statuses.

    Determines plan status from current agent statuses:
    - If any agent is running -> plan is running
    - If all agents are done (completed/failed) -> plan is completed (or failed if any failed)
    """
    db = get_db()
    agents = db.get_plan_agents(plan_id)

    if not agents:
        return  # No agents registered yet

    completed = sum(1 for a in agents if a["status"] == "completed")
    failed = sum(1 for a in agents if a["status"] == "failed")
    total = len(agents)

    # Determine plan status based on agent statuses
    all_done = all(a["status"] in ("completed", "failed", "skipped") for a in agents)
    any_running = any(a["status"] == "running" for a in agents)

    if all_done and total > 0:
        new_status = "failed" if failed > 0 else "completed"
        db.update_plan_status(plan_id, new_status)
    elif any_running:
        # Only transition to running if not already running
        plan = db.get_plan(plan_id)
        if plan and plan.get("status") != "running":
            db.update_plan_status(plan_id, "running")


# =============================================================================
# TOOLS
# =============================================================================


@mcp.tool()
async def create_plan(
    name: Annotated[str, Field(description="Plan name/description")],
    task_id: Annotated[
        int | None, Field(description="Optional associated task ID")
    ] = None,
    metadata: Annotated[
        str | None, Field(description="Optional JSON metadata string")
    ] = None,
) -> dict:
    """
    Create a new execution plan for parallel agents.

    Plans are containers for agent executions. Create a plan first,
    then register agents with register_agent_execution.
    """
    db = get_db()

    try:
        # Parse metadata if provided
        metadata_dict = None
        if metadata:
            try:
                metadata_dict = json.loads(metadata)
            except json.JSONDecodeError as e:
                return {
                    "error": True,
                    "code": "VALIDATION_ERROR",
                    "message": f"Invalid JSON metadata: {e}",
                }

        # Validate task_id if provided
        if task_id:
            task = db.get_task(task_id)
            if not task:
                raise TaskNotFoundError(task_id)

        plan_id = db.create_plan(name, task_id, metadata_dict)

        return {
            "plan_id": plan_id,
            "name": name,
            "task_id": task_id,
            "status": "draft",
        }

    except OrbitError as e:
        return e.to_dict()
    except Exception as e:
        logger.exception("Error creating plan")
        return {"error": True, "message": str(e)}


@mcp.tool()
async def register_agent_execution(
    plan_id: Annotated[int, Field(description="Plan ID to register agent with")],
    agent_id: Annotated[str, Field(description="Agent identifier (e.g., '01', '02')")],
    agent_name: Annotated[str, Field(description="Human-readable agent name")],
    prompt: Annotated[str, Field(description="The prompt/task for this agent")],
    dependencies: Annotated[
        list[str] | None, Field(description="List of agent_ids this agent depends on")
    ] = None,
    max_attempts: Annotated[int, Field(description="Maximum retry attempts")] = 3,
) -> dict:
    """
    Register an agent for execution in a plan.

    Agents are executed in parallel unless they have dependencies.
    Dependencies are specified as a list of agent_ids that must complete first.
    """
    db = get_db()

    try:
        # Create the agent execution record
        exec_id = db.add_agent_execution(
            plan_id=plan_id,
            agent_id=agent_id,
            agent_name=agent_name,
            prompt=prompt,
            max_attempts=max_attempts,
        )

        # Add dependencies if specified
        dependency_ids = []
        if dependencies:
            for dep in dependencies:
                dep_id = db.add_agent_dependency(plan_id, agent_id, dep)
                dependency_ids.append(dep_id)

        return {
            "execution_id": exec_id,
            "plan_id": plan_id,
            "agent_id": agent_id,
            "agent_name": agent_name,
            "dependencies": dependencies or [],
            "dependency_records": dependency_ids,
            "max_attempts": max_attempts,
        }

    except Exception as e:
        logger.exception("Error registering agent execution")
        return {"error": True, "message": str(e)}


@mcp.tool()
async def update_agent_status(
    plan_id: Annotated[int, Field(description="Plan ID")],
    agent_id: Annotated[str, Field(description="Agent identifier (e.g., '01', '02')")],
    status: Annotated[
        str, Field(description="New status: pending, running, completed, failed")
    ],
    result: Annotated[
        str | None, Field(description="Result text (for completed status)")
    ] = None,
    error_message: Annotated[
        str | None, Field(description="Error message (for failed status)")
    ] = None,
) -> dict:
    """
    Update agent execution status. Called by subagents to report progress.

    Status transitions: pending -> running -> completed|failed
    Timestamps are set automatically on status transitions.
    Plan counters are updated after each status change.
    """
    db = get_db()

    try:
        # Validate status
        valid_statuses = ("pending", "running", "completed", "failed", "blocked")
        if status not in valid_statuses:
            return {
                "error": True,
                "code": "VALIDATION_ERROR",
                "message": f"Invalid status '{status}'. Must be one of: {', '.join(valid_statuses)}",
            }

        # Find execution by plan_id + agent_id
        agents = db.get_plan_agents(plan_id)
        execution = next((a for a in agents if a["agent_id"] == agent_id), None)

        if not execution:
            return {
                "error": True,
                "code": "NOT_FOUND",
                "message": f"Agent '{agent_id}' not found in plan {plan_id}",
            }

        # Build output dict if result provided
        output = {"result": result} if result else None

        # Call update_agent_execution to update by plan_id + agent_id
        db.update_agent_execution(
            plan_id=plan_id,
            agent_id=agent_id,
            status=status,
            output=output,
            error=error_message,
        )

        # Sync plan counters
        _update_plan_counters(plan_id)

        return {
            "updated": True,
            "plan_id": plan_id,
            "agent_id": agent_id,
            "status": status,
        }

    except Exception as e:
        logger.exception("Error updating agent status")
        return {"error": True, "message": str(e)}


@mcp.tool()
async def get_plan_status(
    plan_id: Annotated[int, Field(description="Plan ID")],
) -> dict:
    """
    Get plan status with agent breakdown.

    Returns the plan details, all agent executions, and a summary
    with counts by status (pending, running, completed, failed).
    """
    db = get_db()

    try:
        plan = db.get_plan(plan_id)
        if not plan:
            return {
                "error": True,
                "code": "NOT_FOUND",
                "message": f"Plan {plan_id} not found",
            }

        agents = db.get_plan_agents(plan_id)

        # Build summary counts
        summary = {
            "total": len(agents),
            "pending": sum(1 for a in agents if a["status"] == "pending"),
            "blocked": sum(1 for a in agents if a["status"] == "blocked"),
            "running": sum(1 for a in agents if a["status"] == "running"),
            "completed": sum(1 for a in agents if a["status"] == "completed"),
            "failed": sum(1 for a in agents if a["status"] == "failed"),
        }

        return {
            "plan": plan,
            "agents": agents,
            "summary": summary,
        }

    except Exception as e:
        logger.exception("Error getting plan status")
        return {"error": True, "message": str(e)}


@mcp.tool()
async def get_ready_agents(
    plan_id: Annotated[int, Field(description="Plan ID")],
) -> dict:
    """
    Get agents ready to execute (pending with all dependencies completed).

    An agent is ready if:
    - Its status is "pending"
    - It has no dependencies, OR all its dependencies are "completed"

    Note: Failed dependencies do NOT block - the plan may still complete partially.
    """
    db = get_db()

    try:
        plan = db.get_plan(plan_id)
        if not plan:
            return {
                "error": True,
                "code": "NOT_FOUND",
                "message": f"Plan {plan_id} not found",
            }

        agents = db.get_plan_agents(plan_id)

        # Handle empty plan
        if not agents:
            return {"ready_agents": [], "count": 0}

        ready = []
        for agent in agents:
            # Only consider pending agents
            if agent["status"] != "pending":
                continue

            # Get this agent's dependencies
            deps = db.get_agent_dependencies(plan_id, agent["agent_id"])

            # Check if all dependencies are completed
            # (Note: no deps = immediately ready, failed deps don't block)
            if not deps:
                all_deps_complete = True
            else:
                all_deps_complete = all(
                    any(
                        a["agent_id"] == dep and a["status"] == "completed"
                        for a in agents
                    )
                    for dep in deps
                )

            if all_deps_complete:
                ready.append(
                    {
                        "agent_id": agent["agent_id"],
                        "agent_name": agent["agent_name"],
                        "prompt": agent["prompt"],
                        "execution_id": agent["id"],
                    }
                )

        return {"ready_agents": ready, "count": len(ready)}

    except Exception as e:
        logger.exception("Error getting ready agents")
        return {"error": True, "message": str(e)}


@mcp.tool()
async def spawn_parallel_agents(
    plan_id: Annotated[int, Field(description="Plan ID to spawn agents for")],
    subagent_type: Annotated[
        str, Field(description="Task tool subagent_type to use")
    ] = "general-purpose",
) -> dict:
    """
    Get instructions for spawning ready agents in parallel using Task tool.

    Returns task_calls array that can be used to invoke multiple Task tools
    in a SINGLE message for parallel execution. Each agent prompt includes
    the update_agent_status callback for reporting completion.

    The orchestrating Claude should:
    1. Call this tool to get task_calls
    2. Invoke all Task tools in ONE message (enables parallel execution)
    3. Each subagent will report back via update_agent_status when done
    """
    db = get_db()

    try:
        plan = db.get_plan(plan_id)
        if not plan:
            return {
                "error": True,
                "code": "NOT_FOUND",
                "message": f"Plan {plan_id} not found",
            }

        # Get ready agents using the existing function logic
        ready_result = await get_ready_agents(plan_id)

        if ready_result.get("error"):
            return ready_result

        ready_agents = ready_result.get("ready_agents", [])

        if not ready_agents:
            return {
                "message": "No agents ready to spawn",
                "ready_count": 0,
                "task_calls": [],
                "instructions": None,
            }

        # Generate Task tool invocation pattern for each ready agent
        task_calls = []
        for agent in ready_agents:
            agent_prompt = f"""You are executing agent {agent["agent_id"]} in plan {plan_id}.

{agent["prompt"]}

IMPORTANT: When done, call the MCP tool to report your status:
mcp__orbit__update_agent_status(
    plan_id={plan_id},
    agent_id="{agent["agent_id"]}",
    status="completed",  # or "failed" if you encountered errors
    result="<summary of what you did>"
)

If you fail, include error_message parameter describing what went wrong."""

            task_calls.append(
                {
                    "subagent_type": subagent_type,
                    "description": f"Execute: {agent['agent_name']}",
                    "prompt": agent_prompt,
                    "run_in_background": True,
                }
            )

        return {
            "ready_count": len(task_calls),
            "task_calls": task_calls,
            "instructions": f"Invoke {len(task_calls)} Task tool calls in a SINGLE message to run in parallel. Each agent will call update_agent_status when done.",
            "plan_id": plan_id,
            "plan_name": plan.get("name"),
        }

    except Exception as e:
        logger.exception("Error generating spawn instructions")
        return {"error": True, "message": str(e)}


@mcp.tool()
async def complete_plan(
    plan_id: Annotated[int, Field(description="Plan ID")],
    status: Annotated[
        str, Field(description="Final status: 'completed' or 'failed'")
    ] = "completed",
) -> dict:
    """
    Mark a plan as completed or failed.

    Sets the plan status and completed_at timestamp.
    Use this when all agents have finished (successfully or not).
    """
    db = get_db()

    try:
        # Validate status
        if status not in ("completed", "failed"):
            return {
                "error": True,
                "code": "VALIDATION_ERROR",
                "message": "Status must be 'completed' or 'failed'",
            }

        plan = db.get_plan(plan_id)
        if not plan:
            return {
                "error": True,
                "code": "NOT_FOUND",
                "message": f"Plan {plan_id} not found",
            }

        # Update the plan (completed_at is set automatically by update_plan)
        db.update_plan(plan_id, status=status)

        return {
            "plan_id": plan_id,
            "status": status,
            "completed": True,
        }

    except Exception as e:
        logger.exception("Error completing plan")
        return {"error": True, "message": str(e)}

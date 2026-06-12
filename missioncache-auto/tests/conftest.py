"""Shared fixtures and test data for orbit-auto tests."""

import pytest

from orbit_auto.dag import DAG


@pytest.fixture
def empty_dag() -> DAG:
    """An empty DAG with no tasks."""
    return DAG()


@pytest.fixture
def linear_dag() -> DAG:
    """A -> B -> C linear chain."""
    return DAG.build_from_adjacency_list(
        {"01": [], "02": ["01"], "03": ["02"]}
    )


@pytest.fixture
def diamond_dag() -> DAG:
    """Diamond: A -> B, A -> C, B -> D, C -> D."""
    return DAG.build_from_adjacency_list(
        {"01": [], "02": ["01"], "03": ["01"], "04": ["02", "03"]}
    )


@pytest.fixture
def independent_dag() -> DAG:
    """Three independent tasks with no dependencies."""
    return DAG.build_from_adjacency_list(
        {"01": [], "02": [], "03": []}
    )


@pytest.fixture
def tasks_md_content() -> str:
    """Standard tasks.md content for testing."""
    return (
        "# Tasks\n"
        "\n"
        "- [x] 1. Setup project structure\n"
        "- [ ] 2. Implement core logic\n"
        "- [ ] 3. Add tests\n"
        "- [ ] [WAIT] 4. Deploy to staging\n"
    )


@pytest.fixture
def prompt_yaml_content() -> str:
    """Standard prompt YAML content for testing."""
    return (
        "---\n"
        'task_id: "01"\n'
        'task_title: "Setup project"\n'
        'dependencies: ["02", "03"]\n'
        "agents:\n"
        "  - python-pro\n"
        "skills:\n"
        "  - pytest-patterns\n"
        "---\n"
        "Implement the setup step.\n"
        "\n"
        "<acceptance_criteria>\n"
        "- Project compiles\n"
        "</acceptance_criteria>\n"
    )

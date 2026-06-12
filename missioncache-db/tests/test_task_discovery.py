"""Integration tests for TaskDB.find_task_for_cwd.

Tests use a real SQLite database and tmp_path for orbit directory structure.
"""

import json
import os

import pytest

from orbit_db import TaskDB, ORBIT_ROOT


@pytest.fixture
def db(tmp_path, monkeypatch):
    """TaskDB with a temporary orbit root and SQLite database."""
    db_path = tmp_path / "test.db"
    orbit_root = tmp_path / "orbit"
    orbit_root.mkdir()
    (orbit_root / "active").mkdir()

    # Patch ORBIT_ROOT so find_task_for_cwd uses our tmp dir
    monkeypatch.setattr("orbit_db.ORBIT_ROOT", orbit_root)

    db = TaskDB(db_path=db_path)
    db.initialize()
    yield db
    db.close()


@pytest.fixture
def orbit_root(tmp_path):
    """Return the orbit root used by the db fixture."""
    return tmp_path / "orbit"


def _create_orbit_task(db, orbit_root, task_name):
    """Helper: create a task directory and insert a matching DB row."""
    task_dir = orbit_root / "active" / task_name
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / f"{task_name}-context.md").write_text("# Context")

    full_path = f"active/{task_name}"
    with db.connection() as conn:
        cursor = conn.execute(
            "INSERT INTO tasks (name, full_path, status, type) VALUES (?, ?, 'active', 'coding')",
            (task_name, full_path),
        )
        conn.commit()
        return cursor.lastrowid


# ── find_task_for_cwd ─────────────────────────────────────────────────────


class TestFindTaskForCwd:
    def test_exact_path_match(self, db, orbit_root):
        """Priority 3: cwd exactly at the orbit task directory matches."""
        _create_orbit_task(db, orbit_root, "my-project")
        task_dir = orbit_root / "active" / "my-project"

        found = db.find_task_for_cwd(str(task_dir))
        assert found is not None
        assert found.name == "my-project"

    def test_parent_directory_match(self, db, orbit_root):
        """Priority 3: cwd inside a subdirectory of the task dir still matches."""
        _create_orbit_task(db, orbit_root, "my-project")
        sub_dir = orbit_root / "active" / "my-project" / "src"
        sub_dir.mkdir(parents=True, exist_ok=True)

        found = db.find_task_for_cwd(str(sub_dir))
        assert found is not None
        assert found.name == "my-project"

    def test_no_match_returns_none(self, db, tmp_path):
        """find_task_for_cwd returns None when cwd doesn't match any task."""
        unrelated = tmp_path / "somewhere" / "else"
        unrelated.mkdir(parents=True, exist_ok=True)

        found = db.find_task_for_cwd(str(unrelated))
        assert found is None

    def test_session_project_match(self, db, orbit_root, tmp_path, monkeypatch):
        """Priority 2: per-session project file matches the task.

        When a session-project JSON exists with a projectName that maps to
        a task in a repo whose path is an ancestor of cwd, that task is returned.
        """
        # Create a repo and task associated with it
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        repo_id = db.add_repo(str(repo_dir))

        task_name = "session-proj"
        with db.connection() as conn:
            conn.execute(
                "INSERT INTO tasks (repo_id, name, full_path, status, type) VALUES (?, ?, ?, 'active', 'coding')",
                (repo_id, task_name, f"active/{task_name}"),
            )
            conn.commit()

        # Create per-session project file
        state_dir = tmp_path / "state"
        monkeypatch.setattr("orbit_db.Path", type(orbit_root))  # keep Path as is
        # We need to patch the state_dir location used inside find_task_for_cwd
        # The method constructs: Path.home() / ".claude" / "hooks" / "state"
        # Instead, write the session project file where the code looks for it
        hooks_state = tmp_path / ".claude" / "hooks" / "state" / "projects"
        hooks_state.mkdir(parents=True, exist_ok=True)

        session_id = "test-session-123"
        project_file = hooks_state / f"{session_id}.json"
        project_file.write_text(json.dumps({
            "projectName": task_name,
            "sessionId": session_id,
        }))

        # Monkeypatch Path.home() to point to our tmp_path
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        cwd = repo_dir / "src"
        cwd.mkdir(exist_ok=True)

        found = db.find_task_for_cwd(str(cwd), session_id=session_id)
        assert found is not None
        assert found.name == task_name

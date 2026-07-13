"""Integration tests for TaskDB.find_task_for_cwd.

Tests use a real SQLite database and tmp_path for MissionCache directory structure.
"""

import json
import os

import pytest

from missioncache_db import TaskDB, MISSIONCACHE_ROOT


@pytest.fixture
def db(tmp_path, monkeypatch):
    """TaskDB with a temporary MissionCache root and SQLite database."""
    db_path = tmp_path / "test.db"
    orbit_root = tmp_path / "orbit"
    orbit_root.mkdir()
    (orbit_root / "active").mkdir()

    # Patch MISSIONCACHE_ROOT so find_task_for_cwd uses our tmp dir
    monkeypatch.setattr("missioncache_db.MISSIONCACHE_ROOT", orbit_root)

    db = TaskDB(db_path=db_path)
    db.initialize()
    yield db
    db.close()


@pytest.fixture
def orbit_root(tmp_path):
    """Return the MissionCache root used by the db fixture."""
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
        """Priority 3: cwd exactly at the MissionCache task directory matches."""
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
        monkeypatch.setattr("missioncache_db.Path", type(orbit_root))  # keep Path as is
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


# ── fork reconcile on scan ────────────────────────────────────────────────


class TestForkReconcile:
    def _mk_project(self, orbit_root, name, context_body="# Context"):
        d = orbit_root / "active" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}-context.md").write_text(context_body)
        return d

    def test_scan_links_fork_header_to_parent(self, db, orbit_root, tmp_path):
        """A child whose context carries **Fork of:** <parent> gets parent_id
        set by the reconcile pass, regardless of discovery order."""
        self._mk_project(orbit_root, "parent-proj")
        self._mk_project(
            orbit_root,
            "a-child-proj",  # sorts BEFORE parent-proj: child discovered first
            "# a-child-proj - Context\n**Last Updated:** now\n**Fork of:** parent-proj\n\n## Description\n",
        )
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        repo_id = db.add_repo(repo_dir)

        db.scan_repo(repo_id)

        parent = db._resolve_task_by_name("parent-proj")
        child = db._resolve_task_by_name("a-child-proj")
        assert parent is not None and child is not None
        assert child.parent_id == parent.id

    def test_scan_reheals_nulled_parent(self, db, orbit_root, tmp_path):
        """The header is the durable source of truth: a nulled parent_id
        (ON DELETE SET NULL / import) is re-linked on the next scan."""
        self._mk_project(orbit_root, "parent-proj")
        self._mk_project(
            orbit_root,
            "child-proj",
            "# child-proj - Context\n**Fork of:** parent-proj\n\n## Description\n",
        )
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        repo_id = db.add_repo(repo_dir)
        db.scan_repo(repo_id)

        child = db._resolve_task_by_name("child-proj")
        db.set_task_parent(child.id, None)

        db.scan_repo(repo_id)
        rehealed = db._resolve_task_by_name("child-proj")
        parent = db._resolve_task_by_name("parent-proj")
        assert rehealed.parent_id == parent.id

    def test_self_fork_ignored(self, db, orbit_root, tmp_path):
        """A **Fork of:** line naming the project itself is ignored."""
        self._mk_project(
            orbit_root,
            "selfie",
            "# selfie - Context\n**Fork of:** selfie\n\n## Description\n",
        )
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        repo_id = db.add_repo(repo_dir)
        db.scan_repo(repo_id)

        task = db._resolve_task_by_name("selfie")
        assert task.parent_id is None

    def test_wikilink_form_accepted(self, db, orbit_root, tmp_path):
        """**Fork of:** [[parent-proj]] resolves the same as the plain form."""
        self._mk_project(orbit_root, "parent-proj")
        self._mk_project(
            orbit_root,
            "child-proj",
            "# child-proj - Context\n**Fork of:** [[parent-proj]]\n\n## Description\n",
        )
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        repo_id = db.add_repo(repo_dir)
        db.scan_repo(repo_id)

        child = db._resolve_task_by_name("child-proj")
        parent = db._resolve_task_by_name("parent-proj")
        assert child.parent_id == parent.id


class TestForkReconcileAdversarial:
    """Cases from the adversarial review: cycles, stale headers, ambiguity,
    body mentions, malformed links, legacy nested subtasks."""

    def _mk_project(self, orbit_root, name, context_body="# Context"):
        d = orbit_root / "active" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}-context.md").write_text(context_body)
        return d

    def _repo(self, db, tmp_path, name="repo"):
        repo_dir = tmp_path / name
        repo_dir.mkdir(exist_ok=True)
        return db.add_repo(repo_dir)

    def test_header_removal_clears_link(self, db, orbit_root, tmp_path):
        """Valid absence of the header clears a stale parent link."""
        self._mk_project(orbit_root, "parent-proj")
        child_dir = self._mk_project(
            orbit_root, "child-proj",
            "# child-proj - Context\n**Fork of:** parent-proj\n\n## Description\n",
        )
        repo_id = self._repo(db, tmp_path)
        db.scan_repo(repo_id)
        assert db._resolve_task_by_name("child-proj").parent_id is not None

        (child_dir / "child-proj-context.md").write_text(
            "# child-proj - Context\n\n## Description\n"
        )
        db.scan_repo(repo_id)
        assert db._resolve_task_by_name("child-proj").parent_id is None

    def test_nested_legacy_subtask_untouched(self, db, orbit_root, tmp_path):
        """Legacy nested subtasks never carry the header and must never be
        cleared by the reconcile pass."""
        parent_dir = self._mk_project(orbit_root, "parent-proj")
        sub = parent_dir / "sub-task"
        sub.mkdir()
        (sub / "context.md").write_text("# sub context")
        repo_id = self._repo(db, tmp_path)
        db.scan_repo(repo_id)

        subtask = db._resolve_task_by_name("sub-task")
        parent = db._resolve_task_by_name("parent-proj")
        assert subtask.parent_id == parent.id

        db.scan_repo(repo_id)  # second scan: reconcile must not clear it
        assert db._resolve_task_by_name("sub-task").parent_id == parent.id

    def test_body_and_fence_mentions_ignored_on_scan(self, db, orbit_root, tmp_path):
        """A Fork of line below the first section or inside a fence is not a
        header and must not reparent the task."""
        self._mk_project(orbit_root, "parent-proj")
        self._mk_project(
            orbit_root, "child-proj",
            "# child-proj - Context\n\n## Notes\n```\n**Fork of:** parent-proj\n```\n"
            "**Fork of:** parent-proj\n",
        )
        repo_id = self._repo(db, tmp_path)
        db.scan_repo(repo_id)
        assert db._resolve_task_by_name("child-proj").parent_id is None

    def test_malformed_half_wikilink_rejected(self, db, orbit_root, tmp_path):
        """`[[name` / `name]]` half-links do not resolve."""
        self._mk_project(orbit_root, "parent-proj")
        self._mk_project(
            orbit_root, "child-proj",
            "# child-proj - Context\n**Fork of:** [[parent-proj\n\n## Description\n",
        )
        repo_id = self._repo(db, tmp_path)
        db.scan_repo(repo_id)
        assert db._resolve_task_by_name("child-proj").parent_id is None

    def test_two_header_cycle_refused(self, db, orbit_root, tmp_path):
        """A forks B while B forks A: the second link is refused, no cycle
        forms, and both projects stay reachable in the hierarchy."""
        self._mk_project(
            orbit_root, "aaa-proj",
            "# aaa-proj - Context\n**Fork of:** bbb-proj\n\n## Description\n",
        )
        self._mk_project(
            orbit_root, "bbb-proj",
            "# bbb-proj - Context\n**Fork of:** aaa-proj\n\n## Description\n",
        )
        repo_id = self._repo(db, tmp_path)
        db.scan_repo(repo_id)

        aaa = db._resolve_task_by_name("aaa-proj")
        bbb = db._resolve_task_by_name("bbb-proj")
        links = [t.parent_id for t in (aaa, bbb)]
        assert links.count(None) >= 1, "cycle formed: both tasks have parents"
        hierarchy = db.get_active_tasks_hierarchical()
        reachable = {t.name for t in hierarchy["top_level"]}
        for kids in hierarchy["children"].values():
            reachable |= {t.name for t in kids}
        assert {"aaa-proj", "bbb-proj"} <= reachable

    def test_manual_task_name_collision_prefers_project_row(
        self, db, orbit_root, tmp_path
    ):
        """A manual/ task sharing the parent's name must not win over the
        canonical active/<name> project row."""
        db.create_task("parent-proj")  # full_path manual/parent-proj
        self._mk_project(orbit_root, "parent-proj")
        self._mk_project(
            orbit_root, "child-proj",
            "# child-proj - Context\n**Fork of:** parent-proj\n\n## Description\n",
        )
        repo_id = self._repo(db, tmp_path)
        db.scan_repo(repo_id)

        child = db._resolve_task_by_name("child-proj")
        parent = child and db.get_task(child.parent_id)
        assert parent is not None
        assert parent.full_path == "active/parent-proj"

    def test_fork_child_rename_allowed(self, db, orbit_root, tmp_path):
        """A flat fork child is a full project: rename must not hit the
        legacy subtask guard."""
        self._mk_project(orbit_root, "parent-proj")
        self._mk_project(
            orbit_root, "child-proj",
            "# child-proj - Context\n**Fork of:** parent-proj\n\n## Description\n",
        )
        repo_id = self._repo(db, tmp_path)
        db.scan_repo(repo_id)
        child = db._resolve_task_by_name("child-proj")
        assert child.parent_id is not None

        result = db.rename_task(child.id, "renamed-child")
        assert result["full_path"] == "active/renamed-child"

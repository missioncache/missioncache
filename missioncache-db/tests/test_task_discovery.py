"""Integration tests for TaskDB.find_task_for_cwd.

Tests use a real SQLite database and tmp_path for MissionCache directory structure.
"""

import json
import os
import pathlib

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


class TestExplicitBindingCwdIndependence:
    """The per-session binding file (written on /missioncache:load) names the
    project directly. cwd must not veto it: projects are routinely worked
    from outside their registered repo, and forks inherit the parent's
    repo_id. Current bindings carry taskId (durable identity, immune to name
    reuse); name-only bindings are the legacy shape and resolve by name with
    an ambiguity guard. Contract source: the load/save promise (state
    survives compaction for the loaded project, README + docs/hooks.md) -
    which the pre-compact and heartbeat hooks can only honor if resolution
    succeeds for the bound session.
    """

    def _repo_task(self, db, tmp_path, repo_name, task_name, status="active"):
        repo_dir = tmp_path / repo_name
        repo_dir.mkdir(exist_ok=True)
        repo_id = db.add_repo(str(repo_dir))
        with db.connection() as conn:
            cursor = conn.execute(
                "INSERT INTO tasks (repo_id, name, full_path, status, type)"
                " VALUES (?, ?, ?, ?, 'coding')",
                (repo_id, task_name, f"active/{task_name}", status),
            )
            conn.commit()
            task_id = cursor.lastrowid
        return repo_dir, task_id

    def _bind_session(self, tmp_path, session_id, task_name, task_id=None):
        hooks_state = tmp_path / ".claude" / "hooks" / "state" / "projects"
        hooks_state.mkdir(parents=True, exist_ok=True)
        payload = {"projectName": task_name, "sessionId": session_id}
        if task_id is not None:
            payload["taskId"] = task_id
        (hooks_state / f"{session_id}.json").write_text(json.dumps(payload))

    def _outside_cwd(self, tmp_path):
        outside = tmp_path / "unrelated" / "workdir"
        outside.mkdir(parents=True, exist_ok=True)
        return outside

    # ── taskId bindings (current shape) ───────────────────────────────

    def test_binding_with_task_id_resolves_by_id_not_name(
        self, db, tmp_path, monkeypatch
    ):
        """taskId is the durable identity: it wins even when the binding's
        projectName is stale (project renamed after binding was written)."""
        _repo, task_id = self._repo_task(db, tmp_path, "repo1", "real-proj")
        self._bind_session(
            tmp_path, "sess-id-1", "old-stale-name", task_id=task_id
        )
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)

        found = db.find_task_for_cwd(
            str(self._outside_cwd(tmp_path)), session_id="sess-id-1"
        )
        assert found is not None
        assert found.id == task_id
        assert found.name == "real-proj"

    def test_stale_task_id_does_not_fall_back_to_same_named_stranger(
        self, db, tmp_path, monkeypatch
    ):
        """The misroute hole: bound project completed, an UNRELATED project
        reuses the name. A dead taskId must resolve to None - never to the
        stranger - or snapshots and heartbeats route into the wrong project.
        """
        _r1, dead_id = self._repo_task(
            db, tmp_path, "repo1", "cleanup", status="completed"
        )
        self._repo_task(db, tmp_path, "repo2", "cleanup")  # the stranger
        self._bind_session(tmp_path, "sess-id-2", "cleanup", task_id=dead_id)
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)

        found = db.find_task_for_cwd(
            str(self._outside_cwd(tmp_path)), session_id="sess-id-2"
        )
        assert found is None, (
            "a dead taskId must not fall back to a same-named stranger"
        )

    def test_binding_with_paused_task_id_resolves(self, db, tmp_path, monkeypatch):
        """Paused projects are resumable - the binding stays resolvable."""
        _repo, task_id = self._repo_task(
            db, tmp_path, "repo1", "paused-proj", status="paused"
        )
        self._bind_session(tmp_path, "sess-id-3", "paused-proj", task_id=task_id)
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)

        found = db.find_task_for_cwd(
            str(self._outside_cwd(tmp_path)), session_id="sess-id-3"
        )
        assert found is not None
        assert found.id == task_id

    # ── legacy name-only bindings ─────────────────────────────────────

    def test_legacy_binding_resolves_outside_registered_repo(
        self, db, tmp_path, monkeypatch
    ):
        """The original bug: session bound to a project whose registered repo
        does not contain cwd got None back, so snapshots and heartbeats were
        lost. Name-only (legacy) bindings resolve via the global fallback."""
        self._repo_task(db, tmp_path, "repo1", "bound-proj")
        self._bind_session(tmp_path, "sess-1", "bound-proj")
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)

        found = db.find_task_for_cwd(
            str(self._outside_cwd(tmp_path)), session_id="sess-1"
        )
        assert found is not None
        assert found.name == "bound-proj"

    def test_legacy_binding_to_completed_task_returns_none(
        self, db, tmp_path, monkeypatch
    ):
        """The status filter is the discriminator the pre-compact sticky
        error depends on: a completed bound project must resolve to None,
        not snapshot into an archived project's files."""
        self._repo_task(db, tmp_path, "repo1", "done-proj", status="completed")
        self._bind_session(tmp_path, "sess-4", "done-proj")
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)

        found = db.find_task_for_cwd(
            str(self._outside_cwd(tmp_path)), session_id="sess-4"
        )
        assert found is None

    def test_legacy_binding_subtask_bare_name_resolves(
        self, db, tmp_path, monkeypatch
    ):
        """Subtask rows store a bare name, so a binding naming a subtask
        resolves via the fallback; the 'parent/subtask' form misses (matches
        the docstring's claimed contract)."""
        _repo, parent_id = self._repo_task(db, tmp_path, "repo1", "parent-p")
        with db.connection() as conn:
            conn.execute(
                "INSERT INTO tasks (repo_id, name, full_path, status, type, parent_id)"
                " SELECT repo_id, 'sub-p', 'active/parent-p/sub-p', 'active', 'coding', ?"
                " FROM tasks WHERE id = ?",
                (parent_id, parent_id),
            )
            conn.commit()
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        outside = self._outside_cwd(tmp_path)

        self._bind_session(tmp_path, "sess-5", "sub-p")
        found = db.find_task_for_cwd(str(outside), session_id="sess-5")
        assert found is not None
        assert found.name == "sub-p"
        assert found.parent_id == parent_id

        self._bind_session(tmp_path, "sess-6", "parent-p/sub-p")
        assert db.find_task_for_cwd(str(outside), session_id="sess-6") is None

    def test_ambiguous_name_outside_repos_returns_none(
        self, db, tmp_path, monkeypatch, capsys
    ):
        """Two active projects share the bound name and cwd disambiguates
        neither: refuse to guess rather than pick a repo arbitrarily - and
        leave the stderr breadcrumb every consumer inherits."""
        self._repo_task(db, tmp_path, "repo1", "dup-proj")
        self._repo_task(db, tmp_path, "repo2", "dup-proj")
        self._bind_session(tmp_path, "sess-2", "dup-proj")
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)

        found = db.find_task_for_cwd(
            str(self._outside_cwd(tmp_path)), session_id="sess-2"
        )
        assert found is None
        err = capsys.readouterr().err
        assert "ambiguous" in err and "dup-proj" in err

    def test_ambiguous_name_inside_repo_still_resolves_repo_scoped(
        self, db, tmp_path, monkeypatch
    ):
        """When cwd IS inside one of the same-named projects' repos, the
        repo-scoped lookup wins as before - the fallback never runs."""
        self._repo_task(db, tmp_path, "repo1", "dup-proj")
        repo2, _tid = self._repo_task(db, tmp_path, "repo2", "dup-proj")
        self._bind_session(tmp_path, "sess-3", "dup-proj")
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)

        found = db.find_task_for_cwd(str(repo2), session_id="sess-3")
        assert found is not None
        assert found.name == "dup-proj"
        repo2_id = db.get_repo_by_path(str(repo2)).id
        assert found.repo_id == repo2_id


class TestSessionBindingAccessors:
    """write_session_binding / read_session_binding own the binding file
    convention - every writer and reader goes through them (or matches
    their format exactly), so the shape is pinned here once."""

    def test_roundtrip_with_task_id(self, tmp_path, monkeypatch):
        from missioncache_db import read_session_binding, write_session_binding

        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        write_session_binding("sess-a", "proj-x", task_id=42)

        exists, data = read_session_binding("sess-a")
        assert exists is True
        assert data["projectName"] == "proj-x"
        assert data["taskId"] == 42
        assert data["sessionId"] == "sess-a"
        assert "updated" in data

    def test_roundtrip_without_task_id_omits_key(self, tmp_path, monkeypatch):
        from missioncache_db import read_session_binding, write_session_binding

        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        write_session_binding("sess-b", "proj-y")

        exists, data = read_session_binding("sess-b")
        assert exists is True
        assert "taskId" not in data

    def test_missing_file_reads_as_not_bound(self, tmp_path, monkeypatch):
        from missioncache_db import read_session_binding

        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        assert read_session_binding("never-bound") == (False, None)

    def test_corrupt_file_reads_as_exists_but_unreadable(
        self, tmp_path, monkeypatch
    ):
        from missioncache_db import read_session_binding, session_binding_path

        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        path = session_binding_path("sess-c")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")

        assert read_session_binding("sess-c") == (True, None)


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


class TestForkReconcileFileOrder:
    """The reconcile must certify the parent link off the SAME context file
    the readers (digest, statusline) render from - the prefixed name - so a
    stale legacy context.md cannot silently de-link a live fork."""

    def _repo(self, db, tmp_path):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir(exist_ok=True)
        return db.add_repo(repo_dir)

    def test_prefixed_file_wins_when_both_present(self, db, orbit_root, tmp_path):
        """Both context.md (no header) and <name>-context.md (with header)
        exist: the link is read off the prefixed file and set, NOT nulled."""
        (orbit_root / "active" / "parent-proj").mkdir(parents=True)
        (orbit_root / "active" / "parent-proj" / "parent-proj-context.md").write_text("# p")
        child = orbit_root / "active" / "child-proj"
        child.mkdir(parents=True)
        # Legacy unprefixed file has NO fork header...
        (child / "context.md").write_text("# child-proj - Context\n\n## Description\n")
        # ...the canonical prefixed file DOES.
        (child / "child-proj-context.md").write_text(
            "# child-proj - Context\n**Fork of:** parent-proj\n\n## Description\n"
        )
        repo_id = self._repo(db, tmp_path)
        db.scan_repo(repo_id)

        c = db._resolve_task_by_name("child-proj")
        parent = db._resolve_task_by_name("parent-proj")
        assert c.parent_id == parent.id, "link must come off the prefixed file"

    def test_prefixed_header_not_cleared_by_legacy_file(self, db, orbit_root, tmp_path):
        """A previously-linked fork with a header in the prefixed file must NOT
        be de-linked just because a header-less legacy context.md appears."""
        (orbit_root / "active" / "parent-proj").mkdir(parents=True)
        (orbit_root / "active" / "parent-proj" / "parent-proj-context.md").write_text("# p")
        child = orbit_root / "active" / "child-proj"
        child.mkdir(parents=True)
        (child / "child-proj-context.md").write_text(
            "# child-proj - Context\n**Fork of:** parent-proj\n\n## Description\n"
        )
        repo_id = self._repo(db, tmp_path)
        db.scan_repo(repo_id)
        assert db._resolve_task_by_name("child-proj").parent_id is not None

        # A legacy header-less file appears; re-scan must preserve the link.
        (child / "context.md").write_text("# child-proj - Context\n\n## Description\n")
        db.scan_repo(repo_id)
        assert db._resolve_task_by_name("child-proj").parent_id is not None


class TestForkRenameInteractions:
    """Renaming a fork child (a full project) must keep its parent link and
    header; the header remains the durable source of truth across rename."""

    def _repo(self, db, tmp_path):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir(exist_ok=True)
        return db.add_repo(repo_dir)

    def test_child_rename_preserves_link_and_header(self, db, orbit_root, tmp_path):
        (orbit_root / "active" / "parent-proj").mkdir(parents=True)
        (orbit_root / "active" / "parent-proj" / "parent-proj-context.md").write_text("# p")
        child = orbit_root / "active" / "child-proj"
        child.mkdir(parents=True)
        (child / "child-proj-context.md").write_text(
            "# child-proj - Context\n**Fork of:** parent-proj\n\n## Description\n"
        )
        repo_id = self._repo(db, tmp_path)
        db.scan_repo(repo_id)
        c = db._resolve_task_by_name("child-proj")
        parent = db._resolve_task_by_name("parent-proj")
        assert c.parent_id == parent.id

        db.rename_task(c.id, "renamed-child")

        renamed = db._resolve_task_by_name("renamed-child")
        assert renamed is not None
        assert renamed.parent_id == parent.id  # link survives the rename
        # The header moved with the file and still reconciles.
        db.scan_repo(repo_id)
        assert db._resolve_task_by_name("renamed-child").parent_id == parent.id


class TestForkReconcilePreservesOnUnreadable:
    """The 'absence of evidence is not evidence' contract: an unreadable
    context file must PRESERVE the link, never clear it."""

    def test_unreadable_context_preserves_link(self, db, orbit_root, tmp_path, monkeypatch):
        (orbit_root / "active" / "parent-proj").mkdir(parents=True)
        (orbit_root / "active" / "parent-proj" / "parent-proj-context.md").write_text("# p")
        child = orbit_root / "active" / "child-proj"
        child.mkdir(parents=True)
        (child / "child-proj-context.md").write_text(
            "# child-proj - Context\n**Fork of:** parent-proj\n\n## Description\n"
        )
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        repo_id = db.add_repo(repo_dir)
        db.scan_repo(repo_id)
        c = db._resolve_task_by_name("child-proj")
        assert c.parent_id is not None

        # Make the child's context unreadable, then re-scan: the link must
        # survive (OSError -> preserve), NOT be cleared as "validly absent".
        real_read_text = pathlib.Path.read_text

        def boom(self, *a, **k):
            if self.name == "child-proj-context.md":
                raise OSError("unreadable")
            return real_read_text(self, *a, **k)

        monkeypatch.setattr(pathlib.Path, "read_text", boom)
        db.scan_repo(repo_id)
        monkeypatch.undo()

        assert db._resolve_task_by_name("child-proj").parent_id is not None

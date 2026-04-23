"""Integration tests for session_start, pre_compact, and stop hooks.

Tests mock orbit_db and use tmp_path for file I/O.
"""

import json
import os
import re
from datetime import datetime
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ── session_start ─────────────────────────────────────────────────────────


class TestSessionStart:
    def test_find_task_for_cwd_integration(self, tmp_path, monkeypatch, capsys):
        """session_start calls find_task_for_cwd and outputs context for a match."""
        # Redirect Path.home() so state-file writes land in tmp_path, not
        # the real ~/.claude/hooks/state/ (prevents test pollution).
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        # Build a mock task
        mock_task = SimpleNamespace(
            id=1,
            name="my-task",
            status="active",
            jira_key=None,
            repo_id=10,
            full_path="active/my-task",
        )
        mock_repo = SimpleNamespace(short_name="my-repo", path="/fake/repo")

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = mock_task
        mock_db.get_repo.return_value = mock_repo
        mock_db.get_task_time.return_value = 0
        mock_db.format_duration.return_value = "0m"

        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-42")
        monkeypatch.setattr("os.getcwd", lambda: "/fake/repo")

        with patch.dict("sys.modules", {"orbit_db": MagicMock(TaskDB=lambda: mock_db)}):
            # Re-import to pick up mocked module
            import importlib
            import hooks.session_start as mod

            importlib.reload(mod)
            mod.main()

        output = capsys.readouterr().out
        assert "my-task" in output
        assert "Active Task Detected" in output

    def test_writes_pending_task_json(self, tmp_path, monkeypatch):
        """write_pending_task creates a valid JSON file."""
        state_dir = tmp_path / ".claude" / "hooks" / "state"
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        # Import after monkeypatch so Path.home() is patched
        import importlib
        import hooks.session_start as mod

        importlib.reload(mod)
        mod.write_pending_task("test-task", "/some/path")

        pending_file = state_dir / "pending-task.json"
        assert pending_file.exists()

        data = json.loads(pending_file.read_text())
        assert data["taskName"] == "test-task"
        assert data["cwd"] == "/some/path"
        assert "timestamp" in data

    def test_writes_cwd_session_pointer(self, tmp_path, monkeypatch):
        """write_cwd_session_pointer records {sessionId, cwd, updatedAt} keyed by cwd."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        fake_cwd = tmp_path / "some" / "repo"
        fake_cwd.mkdir(parents=True)
        monkeypatch.chdir(fake_cwd)

        import importlib
        import hooks.session_start as mod

        importlib.reload(mod)
        mod.write_cwd_session_pointer("abc-123")

        cwd_key = str(fake_cwd).replace("/", "-")
        pointer_file = tmp_path / ".claude" / "hooks" / "state" / "cwd-session" / f"{cwd_key}.json"
        assert pointer_file.exists()

        data = json.loads(pointer_file.read_text())
        assert data["sessionId"] == "abc-123"
        assert data["cwd"] == str(fake_cwd)
        assert "updatedAt" in data

    def test_cwd_session_pointer_skipped_when_no_session_id(self, tmp_path, monkeypatch):
        """Empty session_id is a no-op - no file created."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        monkeypatch.chdir(tmp_path)

        import importlib
        import hooks.session_start as mod

        importlib.reload(mod)
        mod.write_cwd_session_pointer("")

        pointer_dir = tmp_path / ".claude" / "hooks" / "state" / "cwd-session"
        # Directory should not be created when session_id is empty.
        assert not pointer_dir.exists()

    def test_outputs_context_message(self, tmp_path, monkeypatch, capsys):
        """session_start prints context including task name and status."""
        # Redirect Path.home() so state-file writes land in tmp_path, not
        # the real ~/.claude/hooks/state/ (prevents test pollution).
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        mock_task = SimpleNamespace(
            id=5,
            name="context-task",
            status="active",
            jira_key="PROJ-999",
            repo_id=1,
            full_path="active/context-task",
        )
        mock_repo = SimpleNamespace(short_name="repo", path="/repo")

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = mock_task
        mock_db.get_repo.return_value = mock_repo
        mock_db.get_task_time.return_value = 3600
        mock_db.format_duration.return_value = "1h 0m"

        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-99")
        monkeypatch.setattr("os.getcwd", lambda: "/repo")

        with patch.dict("sys.modules", {"orbit_db": MagicMock(TaskDB=lambda: mock_db)}):
            import importlib
            import hooks.session_start as mod

            importlib.reload(mod)
            mod.main()

        output = capsys.readouterr().out
        assert "context-task" in output
        assert "PROJ-999" in output
        assert "1h 0m" in output


# ── pre_compact ───────────────────────────────────────────────────────────


class TestPreCompact:
    def test_updates_context_timestamp(self, tmp_path, monkeypatch):
        """pre_compact updates the Last Updated timestamp in context.md."""
        # Set up task dir with a context file
        task_dir = tmp_path / "orbit" / "active" / "compact-task"
        task_dir.mkdir(parents=True)
        ctx_file = task_dir / "compact-task-context.md"
        ctx_file.write_text(
            "# Context\n\n**Last Updated:** 2025-01-01 00:00\n\n## Recent Changes\n\n- Old change\n"
        )

        mock_task = SimpleNamespace(
            id=1, name="compact-task", repo_id=1, full_path="active/compact-task"
        )
        mock_repo = SimpleNamespace(path=str(tmp_path / "orbit"))

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = mock_task
        mock_db.get_repo.return_value = mock_repo
        mock_db.process_heartbeats.return_value = 0

        monkeypatch.setattr("os.getcwd", lambda: str(task_dir))
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)

        with patch.dict("sys.modules", {"orbit_db": MagicMock(TaskDB=lambda: mock_db)}):
            import importlib
            import hooks.pre_compact as mod

            importlib.reload(mod)
            mod.main()

        content = ctx_file.read_text()
        # Old timestamp should be replaced
        assert "2025-01-01 00:00" not in content
        assert "**Last Updated:**" in content
        # Compaction note added
        assert "Auto-saved before compaction" in content


# ── stop ──────────────────────────────────────────────────────────────────


class TestStop:
    def _run_stop(self, monkeypatch, stdin_data, mock_db):
        """Helper to run stop.main() with given stdin and mock DB."""
        monkeypatch.setattr("sys.stdin", StringIO(json.dumps(stdin_data)))

        with patch.dict("sys.modules", {"orbit_db": MagicMock(TaskDB=lambda: mock_db)}):
            import importlib
            import hooks.stop as mod

            importlib.reload(mod)
            mod.main()

    def test_detects_edits_shows_reminder(self, tmp_path, monkeypatch, capsys):
        """stop shows orbit reminder when transcript contains Write/Edit tool uses."""
        # Create a fake transcript with edit tool uses
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            '{"type": "tool_use", "name": "Edit"}\n'
            '{"type": "tool_use", "name": "Write"}\n'
        )

        orbit_dir = tmp_path / ".claude" / "orbit" / "active" / "stop-task"
        orbit_dir.mkdir(parents=True)
        (orbit_dir / "stop-task-context.md").write_text("# Context")

        mock_task = SimpleNamespace(
            id=1, name="stop-task", full_path="active/stop-task"
        )
        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = mock_task

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        self._run_stop(
            monkeypatch,
            {"transcript_path": str(transcript), "cwd": str(tmp_path)},
            mock_db,
        )

        err = capsys.readouterr().err
        assert "stop-task" in err
        assert "orbit:save" in err.lower() or "Orbit Reminder" in err

    def test_no_reminder_when_no_edits(self, tmp_path, monkeypatch, capsys):
        """stop does not show reminder when transcript has no Write/Edit tool uses."""
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text('{"type": "tool_use", "name": "Read"}\n')

        mock_db = MagicMock()

        self._run_stop(
            monkeypatch,
            {"transcript_path": str(transcript), "cwd": str(tmp_path)},
            mock_db,
        )

        err = capsys.readouterr().err
        assert "Orbit Reminder" not in err


# ── task_tracker ──────────────────────────────────────────────────────────


class TestTaskTracker:
    """Tests for the UserPromptSubmit divergence detection hook."""

    def _setup_project(
        self,
        tmp_path: Path,
        monkeypatch,
        tasks_content: str,
        context_content: str,
        *,
        context_newer: bool = True,
    ) -> SimpleNamespace:
        """Create fake orbit project files under tmp_path's fake HOME.

        Points Path.home() at tmp_path so the hook's orbit_root resolution
        (~/.claude/orbit) lands in our sandbox. Returns a fake task object
        ready to be plugged into `mock_db.find_task_for_cwd.return_value`.
        """
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        orbit_dir = tmp_path / ".claude" / "orbit" / "active" / "fake-task"
        orbit_dir.mkdir(parents=True)

        tasks_file = orbit_dir / "fake-task-tasks.md"
        context_file = orbit_dir / "fake-task-context.md"
        tasks_file.write_text(tasks_content)
        context_file.write_text(context_content)

        # Force mtime ordering: context is always newer by default.
        if context_newer:
            os.utime(tasks_file, (1000, 1000))
            os.utime(context_file, (2000, 2000))
        else:
            os.utime(tasks_file, (2000, 2000))
            os.utime(context_file, (1000, 1000))

        return SimpleNamespace(
            id=1,
            name="fake-task",
            repo_id=1,
            full_path="active/fake-task",
        )

    def _run_tracker(self, monkeypatch, stdin_data, mock_db=None):
        """Helper to run task_tracker.main() with given stdin and mock DB."""
        monkeypatch.setattr("sys.stdin", StringIO(json.dumps(stdin_data)))

        module_patch = {"orbit_db": MagicMock(TaskDB=lambda: mock_db)}
        with patch.dict("sys.modules", module_patch):
            import importlib
            import hooks.task_tracker as mod

            importlib.reload(mod)
            mod.main()

    def test_no_active_project_silent(self, monkeypatch, capsys):
        """Returns silently when there's no orbit project for the cwd."""
        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = None

        self._run_tracker(
            monkeypatch,
            {"session_id": "s1", "cwd": "/tmp", "prompt": "hello"},
            mock_db,
        )

        out = capsys.readouterr().out
        assert out == ""

    def test_missing_tasks_file_silent(self, tmp_path, monkeypatch, capsys):
        """Returns silently when the tasks file doesn't exist."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        orbit_dir = tmp_path / ".claude" / "orbit" / "active" / "fake-task"
        orbit_dir.mkdir(parents=True)
        # Only create context file, no tasks file
        (orbit_dir / "fake-task-context.md").write_text("### Task 1: something")

        task = SimpleNamespace(
            id=1, name="fake-task", repo_id=1, full_path="active/fake-task"
        )

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = task

        self._run_tracker(
            monkeypatch,
            {"session_id": "s1", "cwd": str(tmp_path), "prompt": "hello"},
            mock_db,
        )

        assert capsys.readouterr().out == ""

    def test_missing_context_file_silent(self, tmp_path, monkeypatch, capsys):
        """Returns silently when the context file doesn't exist."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        orbit_dir = tmp_path / ".claude" / "orbit" / "active" / "fake-task"
        orbit_dir.mkdir(parents=True)
        (orbit_dir / "fake-task-tasks.md").write_text("- [ ] 1. Task one")

        task = SimpleNamespace(
            id=1, name="fake-task", repo_id=1, full_path="active/fake-task"
        )

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = task

        self._run_tracker(
            monkeypatch,
            {"session_id": "s1", "cwd": str(tmp_path), "prompt": "hello"},
            mock_db,
        )

        assert capsys.readouterr().out == ""

    def test_divergence_fires_regardless_of_mtime_order(
        self, tmp_path, monkeypatch, capsys
    ):
        """Warn on divergence even if tasks file was touched more recently.

        Motivation: a Claude session can mark one task complete (touching
        the tasks file) while leaving other tasks with context-file findings
        still unchecked. In this state, tasks_mtime > context_mtime but the
        divergence is still real.
        """
        task = self._setup_project(
            tmp_path,
            monkeypatch,
            tasks_content=(
                "- [x] 1. Done task\n"
                "- [ ] 2. Divergent task\n"
            ),
            context_content=(
                "### Task 1: Done task\nfindings\n"
                "### Task 2: Divergent task\nfindings\n"
            ),
            context_newer=False,  # tasks file is newer
        )

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = task

        self._run_tracker(
            monkeypatch,
            {"session_id": "s1", "cwd": str(tmp_path), "prompt": "hello"},
            mock_db,
        )

        out = capsys.readouterr().out
        assert "Task 2: Divergent task" in out
        assert "Task 1:" not in out

    def test_no_divergence_all_marked(self, tmp_path, monkeypatch, capsys):
        """No warning when every heading has a matching [x] in tasks file."""
        task = self._setup_project(
            tmp_path,
            monkeypatch,
            tasks_content="- [x] 1. First task\n",
            context_content="### Task 1: First task\nfindings\n",
        )

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = task

        self._run_tracker(
            monkeypatch,
            {"session_id": "s1", "cwd": str(tmp_path), "prompt": "hello"},
            mock_db,
        )

        assert capsys.readouterr().out == ""

    def test_single_divergence(self, tmp_path, monkeypatch, capsys):
        """Warns when context has a heading for an unchecked task."""
        task = self._setup_project(
            tmp_path,
            monkeypatch,
            tasks_content="- [ ] 2. Framework wiring review\n",
            context_content="### Task 2: Framework wiring review\ndetailed findings\n",
        )

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = task

        self._run_tracker(
            monkeypatch,
            {"session_id": "s1", "cwd": str(tmp_path), "prompt": "hello"},
            mock_db,
        )

        out = capsys.readouterr().out
        assert "Orbit task tracking divergence" in out
        assert "Task 2: Framework wiring review" in out
        assert "update_tasks_file" in out

    def test_multiple_divergence(self, tmp_path, monkeypatch, capsys):
        """Warns about all divergent tasks, not just one."""
        task = self._setup_project(
            tmp_path,
            monkeypatch,
            tasks_content=(
                "- [ ] 2. Framework review\n"
                "- [ ] 3. Helper review\n"
                "- [ ] 4. Templates review\n"
            ),
            context_content=(
                "### Task 2: Framework review\nfindings\n"
                "### Task 3: Helper review\nfindings\n"
                "### Task 4: Templates review\nfindings\n"
            ),
        )

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = task

        self._run_tracker(
            monkeypatch,
            {"session_id": "s1", "cwd": str(tmp_path), "prompt": "hello"},
            mock_db,
        )

        out = capsys.readouterr().out
        assert "Task 2: Framework review" in out
        assert "Task 3: Helper review" in out
        assert "Task 4: Templates review" in out

    def test_partial_divergence(self, tmp_path, monkeypatch, capsys):
        """Only warns about tasks that have headings AND are still unchecked."""
        task = self._setup_project(
            tmp_path,
            monkeypatch,
            tasks_content=(
                "- [x] 1. Done task\n"
                "- [ ] 2. Pending with heading\n"
                "- [ ] 3. Pending without heading\n"
            ),
            context_content=(
                "### Task 1: Done task\nfindings\n"
                "### Task 2: Pending with heading\nfindings\n"
            ),
        )

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = task

        self._run_tracker(
            monkeypatch,
            {"session_id": "s1", "cwd": str(tmp_path), "prompt": "hello"},
            mock_db,
        )

        out = capsys.readouterr().out
        assert "Task 2: Pending with heading" in out
        # Task 1 is done - not flagged
        assert "Task 1:" not in out
        # Task 3 has no heading - not flagged
        assert "Task 3:" not in out

    def test_skip_slash_command(self, monkeypatch, capsys):
        """Skips divergence check for slash commands."""
        mock_db = MagicMock()

        self._run_tracker(
            monkeypatch,
            {"session_id": "s1", "cwd": "/tmp", "prompt": "/orbit:save"},
            mock_db,
        )

        assert capsys.readouterr().out == ""
        # Should never have called the DB
        mock_db.find_task_for_cwd.assert_not_called()

    def test_skip_subagent(self, monkeypatch, capsys):
        """Skips divergence check when running in a subagent context."""
        mock_db = MagicMock()

        self._run_tracker(
            monkeypatch,
            {
                "session_id": "s1",
                "cwd": "/tmp",
                "prompt": "hello",
                "agent_id": "sub-42",
            },
            mock_db,
        )

        assert capsys.readouterr().out == ""
        mock_db.find_task_for_cwd.assert_not_called()

    def test_skip_empty_prompt(self, monkeypatch, capsys):
        """Skips divergence check for empty prompts."""
        mock_db = MagicMock()

        self._run_tracker(
            monkeypatch,
            {"session_id": "s1", "cwd": "/tmp", "prompt": "   "},
            mock_db,
        )

        assert capsys.readouterr().out == ""
        mock_db.find_task_for_cwd.assert_not_called()

    def test_heading_without_description_counts(
        self, tmp_path, monkeypatch, capsys
    ):
        """A bare `### Task N` heading (no colon) still triggers a warning."""
        task = self._setup_project(
            tmp_path,
            monkeypatch,
            tasks_content="- [ ] 5. Review thing\n",
            context_content="### Task 5\nsome findings without colon\n",
        )

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = task

        self._run_tracker(
            monkeypatch,
            {"session_id": "s1", "cwd": str(tmp_path), "prompt": "hello"},
            mock_db,
        )

        out = capsys.readouterr().out
        assert "Task 5: Review thing" in out

    def test_subtask_layout_divergence(self, tmp_path, monkeypatch, capsys):
        """Subtask directories use plain tasks.md/context.md (no prefix).

        Mirrors the layout that orbit_db's scan_repo treats as a subtask
        marker. Verifies the hook falls back to the non-prefixed filenames
        when the prefixed form is absent.
        """
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        # Subtask dir: active/parent-task/sub-task with plain tasks.md/context.md
        subtask_dir = (
            tmp_path / ".claude" / "orbit" / "active" / "parent-task" / "sub-task"
        )
        subtask_dir.mkdir(parents=True)
        (subtask_dir / "tasks.md").write_text(
            "- [x] 1. Done subtask item\n"
            "- [ ] 2. Divergent subtask item\n"
        )
        (subtask_dir / "context.md").write_text(
            "### Task 1: Done subtask item\nfindings\n"
            "### Task 2: Divergent subtask item\nfindings\n"
        )

        task = SimpleNamespace(
            id=2,
            name="sub-task",
            repo_id=1,
            full_path="active/parent-task/sub-task",
        )

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = task

        self._run_tracker(
            monkeypatch,
            {"session_id": "s1", "cwd": str(tmp_path), "prompt": "hello"},
            mock_db,
        )

        out = capsys.readouterr().out
        assert "Task 2: Divergent subtask item" in out
        # Task 1 is done - not flagged
        assert "Task 1:" not in out


# ── session_start task discipline reminder ────────────────────────────────


class TestSessionStartTaskDiscipline:
    """Verify the session_start hook includes the task tracking discipline reminder."""

    def test_output_includes_discipline_reminder(
        self, tmp_path, monkeypatch, capsys
    ):
        """session_start output mentions update_tasks_file and the TaskCreate anti-pattern."""
        # Redirect Path.home() to tmp_path so the hook's state-file writes
        # (pending-task.json, projects/<session>.json) land in our sandbox
        # instead of polluting the real ~/.claude/hooks/state/.
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        # Real on-disk task_dir so the `task_dir.exists()` check passes.
        repo_path = tmp_path / "repo"
        task_dir = repo_path / "active" / "my-task"
        task_dir.mkdir(parents=True)

        mock_task = SimpleNamespace(
            id=1,
            name="my-task",
            status="active",
            jira_key=None,
            repo_id=10,
            full_path="active/my-task",
        )
        mock_repo = SimpleNamespace(short_name="my-repo", path=str(repo_path))

        mock_db = MagicMock()
        mock_db.find_task_for_cwd.return_value = mock_task
        mock_db.get_repo.return_value = mock_repo
        mock_db.get_task_time.return_value = 0
        mock_db.format_duration.return_value = "0m"

        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-discipline-test")
        monkeypatch.setattr("os.getcwd", lambda: str(task_dir))

        with patch.dict(
            "sys.modules", {"orbit_db": MagicMock(TaskDB=lambda: mock_db)}
        ):
            import importlib
            import hooks.session_start as mod

            importlib.reload(mod)
            mod.main()

        output = capsys.readouterr().out
        assert "Task tracking discipline" in output
        assert "update_tasks_file" in output
        assert "TaskCreate" in output

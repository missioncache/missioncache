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
    def test_find_task_for_cwd_integration(self, monkeypatch, capsys):
        """session_start calls find_task_for_cwd and outputs context for a match."""
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

    def test_outputs_context_message(self, monkeypatch, capsys):
        """session_start prints context including task name and status."""
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

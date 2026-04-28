"""Tests for the per-session active orbit-task pointer.

Covers the read/write/clear primitives and the cross-session sweep used
by the auto-clear hook on update_tasks_file. Filesystem isolation via
``monkeypatch`` of ``Path.home()``; tests do not touch the real
``~/.claude/`` tree.
"""

from __future__ import annotations

import pathlib

import pytest

from mcp_orbit import active_task


def _state_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / ".claude" / "hooks" / "state" / "active-orbit-task"


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: tmp_path))
    # Re-bind STATE_DIR after the home patch since it was bound at import time.
    monkeypatch.setattr(active_task, "STATE_DIR", _state_dir(tmp_path))
    yield


class TestWriteAndReadPointer:
    def test_round_trip(self, tmp_path):
        path = active_task.write_pointer("sess-1", "orbit-public-release", ["54a"])
        assert path == _state_dir(tmp_path) / "sess-1.json"

        data = active_task.read_pointer("sess-1")
        assert data is not None
        assert data["project_name"] == "orbit-public-release"
        assert data["task_numbers"] == ["54a"]
        assert "updated" in data

    def test_replaces_existing(self):
        active_task.write_pointer("sess-1", "p1", ["8"])
        active_task.write_pointer("sess-1", "p1", ["54a", "54b"])

        data = active_task.read_pointer("sess-1")
        assert data["task_numbers"] == ["54a", "54b"]

    def test_creates_state_dir(self, tmp_path):
        assert not _state_dir(tmp_path).exists()
        active_task.write_pointer("sess-1", "p1", ["8"])
        assert _state_dir(tmp_path).is_dir()

    def test_atomic_via_tmp_rename(self, tmp_path):
        """Pointer write goes through ``.tmp`` then ``os.replace``; the .tmp
        file shouldn't linger after a successful write."""
        active_task.write_pointer("sess-1", "p1", ["8"])
        files = list(_state_dir(tmp_path).iterdir())
        assert [f.name for f in files] == ["sess-1.json"]


class TestReadPointerMissing:
    def test_returns_none_when_no_file(self):
        assert active_task.read_pointer("never-set") is None

    def test_returns_none_for_empty_session_id(self):
        assert active_task.read_pointer("") is None

    def test_returns_none_for_corrupt_json(self, tmp_path):
        sd = _state_dir(tmp_path)
        sd.mkdir(parents=True)
        (sd / "sess-1.json").write_text("not valid json")

        assert active_task.read_pointer("sess-1") is None


class TestClearPointer:
    def test_removes_existing_file(self):
        active_task.write_pointer("sess-1", "p1", ["8"])
        assert active_task.clear_pointer("sess-1") is True
        assert active_task.read_pointer("sess-1") is None

    def test_returns_false_when_missing(self):
        assert active_task.clear_pointer("never-set") is False

    def test_returns_false_for_empty_session_id(self):
        assert active_task.clear_pointer("") is False


class TestSessionIdValidation:
    """Reject session ids that would let a caller escape STATE_DIR.

    Defense-in-depth: the MCP layer already rejects empty session ids,
    but accepting any string here would mean ``session_id="../foo"``
    writes outside ``~/.claude/hooks/state/active-orbit-task/``.
    """

    @pytest.mark.parametrize(
        "bad_id",
        [
            "../foo",
            "foo/../bar",
            "/etc/passwd",
            "foo/bar",
            "foo\\bar",
            "foo\x00bar",
            "",
        ],
    )
    def test_write_pointer_rejects_unsafe(self, bad_id):
        with pytest.raises(ValueError):
            active_task.write_pointer(bad_id, "p", ["8"])

    @pytest.mark.parametrize("bad_id", ["../foo", "/etc/passwd", "foo/bar"])
    def test_read_pointer_returns_none_for_unsafe(self, bad_id):
        assert active_task.read_pointer(bad_id) is None

    @pytest.mark.parametrize("bad_id", ["../foo", "/etc/passwd", "foo/bar"])
    def test_clear_pointer_returns_false_for_unsafe(self, bad_id):
        assert active_task.clear_pointer(bad_id) is False

    def test_uuid_session_id_accepted(self):
        # Sanity check: real Claude Code session ids (UUIDs) pass validation.
        sid = "452c7ee3-3abe-46ac-8d67-fdc10bf95991"
        active_task.write_pointer(sid, "demo-project", ["8"])
        assert active_task.read_pointer(sid)["task_numbers"] == ["8"]


class TestRemoveTaskNumbersEverywhere:
    """Cross-session sweep: when items get marked [x] in tasks.md, drop them
    from every session's active-task pointer for that project."""

    def test_no_state_dir_returns_empty(self, tmp_path):
        # No directory exists yet -> sweep is a no-op.
        assert active_task.remove_task_numbers_everywhere("p1", ["8"]) == []

    def test_no_completed_numbers_short_circuits(self):
        active_task.write_pointer("sess-1", "p1", ["8"])
        # Empty list returns immediately; pointer untouched.
        assert active_task.remove_task_numbers_everywhere("p1", []) == []
        assert active_task.read_pointer("sess-1")["task_numbers"] == ["8"]

    def test_strips_matching_numbers_from_one_session(self):
        active_task.write_pointer("sess-1", "p1", ["54a", "54b", "54c"])
        affected = active_task.remove_task_numbers_everywhere("p1", ["54a", "54b"])

        assert affected == ["sess-1"]
        data = active_task.read_pointer("sess-1")
        assert data["task_numbers"] == ["54c"]

    def test_removes_pointer_when_set_drains_to_empty(self):
        active_task.write_pointer("sess-1", "p1", ["54a"])
        active_task.remove_task_numbers_everywhere("p1", ["54a"])

        assert active_task.read_pointer("sess-1") is None

    def test_isolates_by_project_name(self):
        """A pointer for project X is untouched when project Y completes work."""
        active_task.write_pointer("sess-1", "p1", ["54a"])
        active_task.write_pointer("sess-2", "p2", ["54a"])
        affected = active_task.remove_task_numbers_everywhere("p1", ["54a"])

        assert affected == ["sess-1"]
        # p2's pointer survives because the project name didn't match.
        assert active_task.read_pointer("sess-2")["task_numbers"] == ["54a"]

    def test_sweeps_all_matching_sessions(self):
        active_task.write_pointer("sess-1", "p1", ["54a", "8"])
        active_task.write_pointer("sess-2", "p1", ["54a"])
        active_task.write_pointer("sess-3", "p1", ["8"])

        affected = active_task.remove_task_numbers_everywhere("p1", ["54a"])
        assert sorted(affected) == ["sess-1", "sess-2"]
        assert active_task.read_pointer("sess-1")["task_numbers"] == ["8"]
        assert active_task.read_pointer("sess-2") is None
        assert active_task.read_pointer("sess-3")["task_numbers"] == ["8"]

    def test_skips_corrupt_pointer_files(self, tmp_path):
        sd = _state_dir(tmp_path)
        sd.mkdir(parents=True)
        (sd / "sess-corrupt.json").write_text("bad json")
        active_task.write_pointer("sess-good", "p1", ["54a"])

        # Sweep should not crash on the corrupt file.
        affected = active_task.remove_task_numbers_everywhere("p1", ["54a"])
        assert affected == ["sess-good"]

    def test_skips_when_no_change(self):
        """Pointer that doesn't reference completed numbers is left alone, and
        the session is NOT reported as affected (nothing to do)."""
        active_task.write_pointer("sess-1", "p1", ["54a"])
        affected = active_task.remove_task_numbers_everywhere("p1", ["8"])
        assert affected == []
        assert active_task.read_pointer("sess-1")["task_numbers"] == ["54a"]

    def test_drained_pointer_renders_as_hidden_even_if_unlink_fails(
        self, tmp_path, monkeypatch
    ):
        """If the unlink step fails after the set drains, the pointer is
        still inert (empty task_numbers) instead of holding stale data.

        Without this guarantee, a transient FS error could leave the
        statusline showing a completed task as active until the user
        manually re-set the pointer.
        """
        active_task.write_pointer("sess-1", "p1", ["54a"])

        original_unlink = pathlib.Path.unlink

        def _fail(self, *args, **kwargs):  # pragma: no cover - explicit fail
            raise OSError("simulated unlink failure")

        monkeypatch.setattr(pathlib.Path, "unlink", _fail)
        active_task.remove_task_numbers_everywhere("p1", ["54a"])
        monkeypatch.setattr(pathlib.Path, "unlink", original_unlink)

        # File still exists (unlink failed) but is now empty.
        data = active_task.read_pointer("sess-1")
        assert data is not None
        assert data["task_numbers"] == []

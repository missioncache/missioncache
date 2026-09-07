"""Tests for move_to_project - the cross-project split operation.

Spec source: a split is a MOVE, not a delete plus an add. The content ends
up in exactly one file, both sides record it, and nothing is silently
dropped. The lock discipline matters as much as the content: this is the
only operation that holds more than one project's file lock.
"""

import threading
from pathlib import Path

import pytest
from missioncache_db import context_health as ch

from mcp_missioncache.config import Settings
from mcp_missioncache.errors import ErrorCode, MissionCacheError
from mcp_missioncache.project_files import (
    BULLETS_REMOVE_FORBIDDEN,
    PROTECTED_SECTIONS,
    move_to_project,
    update_context_file,
)

SOURCE_CONTEXT = """# src - Context
**Last Updated:** 2026-01-01 00:00

## Description
the source

## Gotchas

- source-only gotcha
- ROLE: belongs to the other project

## Guild items handed to Lior (2026-08-16)

The four items and what came back.

## Waiting on

| What | Who | Since | Gates |
|------|-----|-------|-------|
| the tagging decision | Sa'ar | 2026-08-01 | the map |
| stays here | me | 2026-09-01 | next session |

## Next Steps

1. keep going

## Recent Changes
"""

SOURCE_TASKS = """# src - Tasks
**Last Updated:** 2026-01-01 00:00

## Phase 1

- [ ] 25. Stays here
- [ ] 26. Chase the three squad leads
- [x] 27. Already done, also moving
"""

TARGET_CONTEXT = """# dst - Context
**Last Updated:** 2026-01-01 00:00

## Description
the target

## Gotchas

- target gotcha

## Waiting on

| What | Who | Since | Gates |
|------|-----|-------|-------|

## Next Steps

1. TBD

## Recent Changes
"""

TARGET_TASKS = """# dst - Tasks
**Last Updated:** 2026-01-01 00:00

## Phase 1

- [ ] 1. Existing target task
"""


@pytest.fixture
def projects(tmp_path, monkeypatch):
    """Two real project directories under an isolated MISSIONCACHE_ROOT."""
    root = tmp_path / "missioncache"
    monkeypatch.setattr(
        "mcp_missioncache.project_files.settings", Settings(root=root)
    )
    for name, context, tasks in (
        ("src", SOURCE_CONTEXT, SOURCE_TASKS),
        ("dst", TARGET_CONTEXT, TARGET_TASKS),
    ):
        d = root / "active" / name
        d.mkdir(parents=True)
        (d / f"{name}-context.md").write_text(context)
        (d / f"{name}-tasks.md").write_text(tasks)
    return root


def _read(root, name, suffix):
    return (root / "active" / name / f"{name}-{suffix}.md").read_text()


# The guard lists, stated as the CONTRACT rather than read from the code.
# Parametrizing over the frozensets themselves is circular: deleting a name
# from the set also deletes its test case, so the mutation survives. These
# literals are what the docs promise; the tests below assert the code's sets
# equal them, so a name added or dropped in either place fails loudly.
EXPECTED_PROTECTED_SECTIONS = {
    "Description", "Gotchas", "Waiting on", "Next Steps", "Recent Changes",
    "Action Items", "Stakeholders", "Tickets",
}
EXPECTED_BULLETS_FORBIDDEN = {
    "Recent Changes", "Waiting on", "Action Items", "Stakeholders", "Tickets",
}


def test_the_protected_section_list_matches_the_documented_contract():
    assert set(PROTECTED_SECTIONS) == EXPECTED_PROTECTED_SECTIONS


def test_the_forbidden_bullets_list_matches_the_documented_contract():
    assert set(BULLETS_REMOVE_FORBIDDEN) == EXPECTED_BULLETS_FORBIDDEN


class TestMoveContent:
    def test_content_ends_up_in_exactly_one_file(self, projects):
        move_to_project(
            "src", "dst",
            sections=["Guild items handed to Lior (2026-08-16)"],
            bullets=[{"section": "Gotchas", "match": "ROLE:"}],
            waiting_on=["the tagging decision"],
        )
        src = _read(projects, "src", "context")
        dst = _read(projects, "dst", "context")

        # The section is gone as a SECTION, and its body with it. The move
        # record in Recent Changes still names it by design - both sides
        # record what moved - so assert on structure and body, not on the
        # heading text appearing nowhere.
        assert ch.extract_section(src, "Guild items handed to Lior (2026-08-16)") is None
        assert "The four items and what came back." not in src
        assert ch.extract_section(dst, "Guild items handed to Lior (2026-08-16)") is not None
        assert "The four items and what came back." in dst

        for moved in ("ROLE: belongs to", "the tagging decision"):
            assert moved in dst, f"{moved} never reached the target"
        assert "ROLE: belongs to" not in ch.extract_section(src, "Gotchas")
        assert not ch.parse_waiting_on(src) or all(
            "tagging decision" not in row["what"] for row in ch.parse_waiting_on(src)
        )

        # What was not named stays put.
        assert "source-only gotcha" in src
        assert "stays here" in src and "stays here" not in dst

    def test_a_moved_waiting_on_row_keeps_its_who_and_since(self, projects):
        move_to_project("src", "dst", waiting_on=["the tagging decision"])
        rows = ch.parse_waiting_on(_read(projects, "dst", "context"))
        assert len(rows) == 1
        assert rows[0]["who"] == "Sa'ar"
        assert rows[0]["since"] == "2026-08-01"
        assert rows[0]["gates"] == "the map"

    def test_a_moved_section_lands_above_waiting_on(self, projects):
        move_to_project("src", "dst", sections=["Guild items handed to Lior (2026-08-16)"])
        names = [e["name"] for e in ch.section_index(_read(projects, "dst", "context"))]
        assert names.index("Guild items handed to Lior (2026-08-16)") < names.index(
            "Waiting on"
        )

    def test_both_sides_record_the_move_and_the_link(self, projects):
        move_to_project(
            "src", "dst",
            sections=["Guild items handed to Lior (2026-08-16)"],
            note="splitting the room from the role",
        )
        src = _read(projects, "src", "context")
        dst = _read(projects, "dst", "context")
        assert "Moved to dst:" in src and "[[dst]]" in src
        assert "Moved in from src:" in dst and "[[src]]" in dst
        assert "splitting the room from the role" in src

    def test_the_move_record_does_not_break_the_section(self, projects):
        move_to_project("src", "dst", sections=["Guild items handed to Lior (2026-08-16)"])
        for name in ("src", "dst"):
            content = _read(projects, name, "context")
            assert len(ch.parse_recent_changes_subsections(content)) == 1
            assert ch.orphaned_recent_changes(content) == []

    def test_last_updated_is_refreshed_on_both_sides(self, projects):
        move_to_project("src", "dst", waiting_on=["the tagging decision"])
        for name in ("src", "dst"):
            assert "**Last Updated:** 2026-01-01 00:00" not in _read(
                projects, name, "context"
            )


class TestMoveTasks:
    def test_a_task_moves_and_is_renumbered_on_the_target(self, projects):
        result = move_to_project("src", "dst", tasks=[{"match": "26"}])
        assert result["tasks_moved"] == [{"from": "26", "to": "2"}]
        src = _read(projects, "src", "tasks")
        dst = _read(projects, "dst", "tasks")
        assert "- [ ] 26." not in src
        assert "~~26. Chase the three squad leads~~" in src
        assert "moved to dst as 2" in src
        assert "- [ ] 2. Chase the three squad leads (moved from src task 26)" in dst

    def test_a_completed_task_keeps_its_checkbox(self, projects):
        move_to_project("src", "dst", tasks=[{"match": "27"}])
        assert "- [x] 2. Already done" in _read(projects, "dst", "tasks")

    def test_the_freed_number_is_not_reused_on_the_source(self, projects):
        from mcp_missioncache.project_files import update_tasks_file

        move_to_project("src", "dst", tasks=[{"match": "27"}])
        tasks_file = projects / "active" / "src" / "src-tasks.md"
        update_tasks_file(str(tasks_file), new_tasks=["something new"])
        assert "- [ ] 28. something new" in tasks_file.read_text()


class TestMoveRefusals:
    def test_moving_to_itself_is_refused(self, projects):
        with pytest.raises(MissionCacheError) as exc:
            move_to_project("src", "src", waiting_on=["x"])
        assert exc.value.code == ErrorCode.VALIDATION_ERROR

    def test_an_empty_move_is_refused(self, projects):
        with pytest.raises(MissionCacheError) as exc:
            move_to_project("src", "dst")
        assert exc.value.code == ErrorCode.VALIDATION_ERROR

    def test_a_section_the_target_already_has_is_refused(self, projects):
        with pytest.raises(MissionCacheError) as exc:
            move_to_project("src", "dst", sections=["Gotchas"])
        assert exc.value.code == ErrorCode.VALIDATION_ERROR
        # Refused before any write: neither side changed.
        assert _read(projects, "src", "context") == SOURCE_CONTEXT
        assert _read(projects, "dst", "context") == TARGET_CONTEXT

    def test_moving_a_parent_with_children_is_refused(self, projects):
        tasks_file = projects / "active" / "src" / "src-tasks.md"
        tasks_file.write_text(SOURCE_TASKS + "- [ ] 25.1. Child of 25\n")
        with pytest.raises(MissionCacheError) as exc:
            move_to_project("src", "dst", tasks=[{"match": "25"}])
        assert "25.1" in str(exc.value)

    def test_nothing_that_matched_nothing_is_claimed_as_moved(self, projects):
        result = move_to_project(
            "src", "dst",
            sections=["No Such Section"],
            bullets=[{"section": "Gotchas", "match": "no such gotcha"}],
            waiting_on=["no such row"],
            tasks=[{"match": "999"}],
        )
        assert result["sections_moved"] == []
        assert result["bullets_moved"] == []
        assert result["waiting_on_moved"] == []
        assert result["tasks_moved"] == []
        assert len(result["unmatched"]) == 4


class TestMoveLocking:
    def test_the_move_does_not_deadlock_against_its_own_helpers(self, projects):
        """The move holds all four sidecar locks, then writes.

        POSIX flock blocks a second acquisition of the same lockfile from the
        same process, so a move that called the lock-taking wrappers under its
        own locks would hang forever with no error. Guard it with a timeout
        rather than trusting that a passing run proves it.
        """
        done = threading.Event()
        error: list[BaseException] = []

        def run():
            try:
                move_to_project("src", "dst", waiting_on=["the tagging decision"])
            except BaseException as e:  # noqa: BLE001 - reported to the assertion
                error.append(e)
            finally:
                done.set()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        assert done.wait(timeout=15), "move_to_project deadlocked on its own locks"
        assert not error, f"move raised {error[0]!r}"

    def test_every_affected_file_is_locked(self, projects, monkeypatch):
        """All four project files, through the shared multi-lock primitive.

        The dedup-and-sort ordering itself is filelock.sidecar_locks' contract
        and is tested there. What matters here is that the move routes through
        it and covers every file it writes - a tasks file written outside the
        lock set would race a peer's update_tasks_file.
        """
        from missioncache_db import filelock

        locked: list[str] = []
        real = filelock.sidecar_lock

        def recording(path):
            locked.append(Path(path).name)
            return real(path)

        monkeypatch.setattr(filelock, "sidecar_lock", recording)
        move_to_project(
            "src", "dst",
            waiting_on=["the tagging decision"],
            tasks=[{"match": "26"}],
        )
        assert sorted(locked) == [
            "dst-context.md",
            "dst-tasks.md",
            "src-context.md",
            "src-tasks.md",
        ]


class TestMoveThenRemove:
    def test_a_leftover_can_be_cleaned_up_through_the_tools(self, projects):
        """The documented recovery path for a half-applied move."""
        move_to_project("src", "dst", sections=["Guild items handed to Lior (2026-08-16)"])
        # Simulate the crash window: the target has it, the source got it back.
        src_ctx = projects / "active" / "src" / "src-context.md"
        src_ctx.write_text(SOURCE_CONTEXT)
        result = update_context_file(
            str(src_ctx),
            sections_remove=["Guild items handed to Lior (2026-08-16)"],
        )
        assert result["sections_removed"] == ["Guild items handed to Lior (2026-08-16)"]
        assert "Guild items" not in src_ctx.read_text()
        assert "Guild items" in _read(projects, "dst", "context")


class TestMoveValidatesProjectNames:
    """Both names are interpolated into a path under the data root."""

    @pytest.mark.parametrize(
        "bad", ["../escape", "a/b", "../../etc/passwd", "", "has space"]
    )
    def test_a_traversing_or_malformed_name_is_refused(self, projects, bad):
        for args in (("src", bad), (bad, "dst")):
            with pytest.raises(MissionCacheError) as exc:
                move_to_project(*args, waiting_on=["the tagging decision"])
            assert exc.value.code == ErrorCode.VALIDATION_ERROR
        # Refused before any read or write.
        assert _read(projects, "src", "context") == SOURCE_CONTEXT


class TestMoveValidatesBulletsAndSections:
    """A blank match is contained in every item, so it must not reach the mover."""

    @pytest.mark.parametrize(
        "entry",
        [
            {"section": "Gotchas", "match": ""},
            {"section": "Gotchas", "match": "   "},
            {"section": "", "match": "ROLE:"},
            {"match": "ROLE:"},
            {"section": "Gotchas"},
        ],
    )
    def test_a_blank_bullet_field_is_refused(self, projects, entry):
        with pytest.raises(MissionCacheError) as exc:
            move_to_project("src", "dst", bullets=[entry])
        assert exc.value.code == ErrorCode.VALIDATION_ERROR
        # Nothing moved: a blank match would otherwise take the FIRST item.
        assert _read(projects, "src", "context") == SOURCE_CONTEXT
        assert _read(projects, "dst", "context") == TARGET_CONTEXT

    def test_a_blank_section_heading_is_refused(self, projects):
        with pytest.raises(MissionCacheError) as exc:
            move_to_project("src", "dst", sections=["  "])
        assert exc.value.code == ErrorCode.VALIDATION_ERROR
        assert _read(projects, "src", "context") == SOURCE_CONTEXT


class TestSectionConflictIsCheckedUnderTheLock:
    def test_a_section_added_after_resolution_still_conflicts(self, projects, monkeypatch):
        """The conflict check must see the content it is about to write into.

        Checked before the locks, a concurrent writer can add the same
        section in the gap; the move then inserts a second copy and deletes
        the source's, leaving a target no later write can touch.
        """
        from missioncache_db import filelock

        dst_ctx = projects / "active" / "dst" / "dst-context.md"
        real_lock = filelock.sidecar_lock
        injected = {"done": False}

        def lock_then_inject(path):
            cm = real_lock(path)
            if not injected["done"] and Path(path) == dst_ctx:
                injected["done"] = True
                # A peer writes the section between name resolution and here.
                dst_ctx.write_text(
                    TARGET_CONTEXT.replace(
                        "## Waiting on",
                        "## Guild items handed to Lior (2026-08-16)\n\n"
                        "written by someone else\n\n## Waiting on",
                    )
                )
            return cm

        monkeypatch.setattr(filelock, "sidecar_lock", lock_then_inject)
        with pytest.raises(MissionCacheError) as exc:
            move_to_project(
                "src", "dst", sections=["Guild items handed to Lior (2026-08-16)"]
            )
        assert exc.value.code == ErrorCode.VALIDATION_ERROR
        # The source keeps its section rather than losing it to a target
        # that now has two.
        assert (
            ch.extract_section(
                _read(projects, "src", "context"),
                "Guild items handed to Lior (2026-08-16)",
            )
            is not None
        )
        assert _read(projects, "dst", "context").count(
            "## Guild items handed to Lior (2026-08-16)"
        ) == 1


class TestMoveHonoursTheSameProtections:
    """Moving out of a section is removing from it."""

    @pytest.mark.parametrize("name", sorted(EXPECTED_PROTECTED_SECTIONS))
    def test_a_protected_section_cannot_be_moved(self, projects, name):
        with pytest.raises(MissionCacheError) as exc:
            move_to_project("src", "dst", sections=[name])
        assert exc.value.code == ErrorCode.VALIDATION_ERROR
        assert _read(projects, "src", "context") == SOURCE_CONTEXT

    @pytest.mark.parametrize("section", sorted(EXPECTED_BULLETS_FORBIDDEN))
    def test_a_forbidden_bullet_section_cannot_be_moved(self, projects, section):
        with pytest.raises(MissionCacheError) as exc:
            move_to_project("src", "dst", bullets=[{"section": section, "match": "x"}])
        assert exc.value.code == ErrorCode.VALIDATION_ERROR
        assert _read(projects, "src", "context") == SOURCE_CONTEXT

    def test_a_multiline_section_name_is_refused(self, projects):
        attack = "Guild items handed to Lior (2026-08-16)\n\nbody\n\n## Waiting on"
        with pytest.raises(MissionCacheError) as exc:
            move_to_project("src", "dst", sections=[attack])
        assert exc.value.code == ErrorCode.VALIDATION_ERROR
        assert _read(projects, "src", "context") == SOURCE_CONTEXT
        assert _read(projects, "dst", "context") == TARGET_CONTEXT


class TestMovedBulletLandsInCanonicalPosition:
    def test_a_new_section_on_the_target_goes_above_recent_changes(self, projects):
        # append_to_section_body creates a missing section at EOF, which is
        # below Recent Changes and breaks the canonical order.
        dst = projects / "active" / "dst" / "dst-context.md"
        dst.write_text(TARGET_CONTEXT.replace("## Gotchas\n\n- target gotcha\n\n", ""))
        move_to_project("src", "dst", bullets=[{"section": "Gotchas", "match": "ROLE:"}])
        names = [e["name"] for e in ch.section_index(_read(projects, "dst", "context"))]
        assert "Gotchas" in names
        assert names.index("Gotchas") < names.index("Recent Changes")
        assert "ROLE: belongs to" in _read(projects, "dst", "context")


class TestAnEmptyMoveWritesNothing:
    def test_no_files_are_touched_when_nothing_matched(self, projects):
        result = move_to_project(
            "src", "dst",
            sections=["No Such Section"],
            waiting_on=["no such row"],
        )
        assert result["unmatched"] == ["section: No Such Section", "waiting_on: no such row"]
        # Both files byte-identical: no "Moved to dst: nothing matched" entry,
        # and no permanent Related-projects link between two projects that
        # never exchanged anything.
        assert _read(projects, "src", "context") == SOURCE_CONTEXT
        assert _read(projects, "dst", "context") == TARGET_CONTEXT
        assert "[[dst]]" not in _read(projects, "src", "context")
        assert "[[src]]" not in _read(projects, "dst", "context")

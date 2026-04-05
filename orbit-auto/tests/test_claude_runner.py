"""Tests for orbit_auto.claude_runner module."""

import time
from pathlib import Path

from orbit_auto.claude_runner import ClaudeRunner, StreamContext, build_generic_prompt
from orbit_auto.models import Visibility


class TestExtractTag:
    def test_extracts_promise_tag(self):
        runner = ClaudeRunner()
        text = "Some text <promise>COMPLETE</promise> more text"
        assert runner._extract_tag(text, "promise") == "COMPLETE"

    def test_extracts_multiline_tag(self):
        runner = ClaudeRunner()
        text = "<what_worked>\nRefactored the parser\nAdded tests\n</what_worked>"
        result = runner._extract_tag(text, "what_worked")
        assert "Refactored the parser" in result
        assert "Added tests" in result

    def test_returns_none_when_missing(self):
        runner = ClaudeRunner()
        text = "No tags here at all"
        assert runner._extract_tag(text, "promise") is None

    def test_returns_none_when_empty(self):
        runner = ClaudeRunner()
        text = "<learnings></learnings>"
        # start_idx == end_idx, so returns None
        assert runner._extract_tag(text, "learnings") is None


class TestBuildResult:
    def test_success_when_complete(self):
        runner = ClaudeRunner()
        ctx = StreamContext(start_time=time.time())
        ctx.accumulated_text = "<promise>COMPLETE</promise>"
        result = runner._build_result(ctx)
        assert result.success is True
        assert result.is_complete is True

    def test_success_when_what_worked(self):
        runner = ClaudeRunner()
        ctx = StreamContext(start_time=time.time())
        ctx.accumulated_text = "<what_worked>Parser works now</what_worked>"
        result = runner._build_result(ctx)
        assert result.success is True
        assert result.what_worked == "Parser works now"

    def test_not_success_when_blocked_even_with_what_worked(self):
        runner = ClaudeRunner()
        ctx = StreamContext(start_time=time.time())
        ctx.accumulated_text = (
            "<what_worked>Partial</what_worked>"
            "<blocker>WAITING_FOR_HUMAN</blocker>"
        )
        result = runner._build_result(ctx)
        assert result.success is False
        assert result.is_blocked is True

    def test_failure_when_no_signal(self):
        runner = ClaudeRunner()
        ctx = StreamContext(start_time=time.time())
        ctx.accumulated_text = "Just some text with no tags"
        result = runner._build_result(ctx)
        assert result.success is False


class TestBuildGenericPrompt:
    def test_includes_task_info(self, tmp_path):
        tasks_file = tmp_path / "tasks.md"
        context_file = tmp_path / "context.md"
        tasks_file.write_text("- [ ] 1. Do stuff\n")
        context_file.write_text("Context info\n")

        prompt = build_generic_prompt(
            task_number="1",
            task_title="Do stuff",
            tasks_file=tasks_file,
            context_file=context_file,
        )
        assert "Task 1: Do stuff" in prompt
        assert str(tasks_file) in prompt
        assert str(context_file) in prompt

    def test_includes_auto_log_when_exists(self, tmp_path):
        tasks_file = tmp_path / "tasks.md"
        context_file = tmp_path / "context.md"
        auto_log = tmp_path / "auto-log.md"
        tasks_file.write_text("")
        context_file.write_text("")
        auto_log.write_text("Previous run info\n")

        prompt = build_generic_prompt(
            task_number="1",
            task_title="Do stuff",
            tasks_file=tasks_file,
            context_file=context_file,
            auto_log=auto_log,
        )
        assert str(auto_log) in prompt

    def test_omits_auto_log_when_missing(self, tmp_path):
        tasks_file = tmp_path / "tasks.md"
        context_file = tmp_path / "context.md"
        tasks_file.write_text("")
        context_file.write_text("")

        prompt = build_generic_prompt(
            task_number="1",
            task_title="Do stuff",
            tasks_file=tasks_file,
            context_file=context_file,
            auto_log=None,
        )
        assert "Auto log" not in prompt

    def test_includes_output_tags_instructions(self, tmp_path):
        tasks_file = tmp_path / "tasks.md"
        context_file = tmp_path / "context.md"
        tasks_file.write_text("")
        context_file.write_text("")

        prompt = build_generic_prompt(
            task_number="1",
            task_title="Do stuff",
            tasks_file=tasks_file,
            context_file=context_file,
        )
        assert "<promise>COMPLETE</promise>" in prompt
        assert "<what_worked>" in prompt

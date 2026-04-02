"""
Claude CLI integration for Orbit Auto.

Handles invoking the Claude CLI, parsing streaming output,
and extracting structured results from responses.
"""

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from orbit_auto.models import ExecutionResult, Visibility


@dataclass
class StreamContext:
    """Context for stream processing."""

    start_time: float = field(default_factory=time.time)
    tool_count: int = 0
    files_modified: list[str] = field(default_factory=list)
    accumulated_text: str = ""


class ClaudeRunner:
    """
    Runs Claude CLI and processes output.

    Supports streaming output parsing, tool visibility modes,
    and extraction of learning tags from responses.
    """

    def __init__(
        self,
        visibility: Visibility = Visibility.VERBOSE,
        on_tool_use: Callable[[str, str], None] | None = None,
    ) -> None:
        """
        Initialize ClaudeRunner.

        Args:
            visibility: Output visibility level
            on_tool_use: Optional callback for tool use events (tool_name, display_info)
        """
        self.visibility = visibility
        self.on_tool_use = on_tool_use

    def run(
        self,
        prompt: str,
        working_dir: Path,
        print_output: bool = True,
        log_file: Path | None = None,
        timeout: int | None = None,
        session_name: str | None = None,
    ) -> ExecutionResult:
        """
        Run Claude with a prompt and parse the response.

        Args:
            prompt: The prompt to send to Claude
            working_dir: Working directory for the Claude process
            print_output: Whether to print tool visibility output
            log_file: Optional path to write full Claude output for debugging
            timeout: Max seconds to wait for completion (None = no limit)
            session_name: Optional display name for the session (--name flag)

        Returns:
            ExecutionResult with parsed response data
        """
        start_time = time.time()
        ctx = StreamContext(start_time=start_time)

        # Build command
        # Note: --verbose is required when using --print with --output-format=stream-json
        cmd = ["claude", "--print", "--output-format", "stream-json", "--verbose"]
        if session_name:
            cmd.extend(["--name", session_name])

        # Set up environment to signal autonomous execution
        # This allows hooks to skip when running in orbit-auto mode
        env = os.environ.copy()
        env["ORBIT_AUTO_MODE"] = "1"

        # Run Claude
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=working_dir,
            text=True,
            env=env,
        )

        # Send prompt (with optional timeout)
        try:
            stdout, stderr = process.communicate(input=prompt, timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()  # drain pipes after kill
            result = ExecutionResult(
                task_id="",
                success=False,
                output="",
                duration=time.time() - start_time,
            )
            result.cli_error = f"Task timed out after {timeout}s"
            if log_file:
                self._write_log_file(
                    log_file,
                    prompt,
                    stdout or "",
                    stderr or "",
                    result,
                    start_time,
                )
            return result

        # Process output
        result = self._process_output(stdout, ctx, print_output)
        result.duration = time.time() - start_time

        # Capture CLI errors from stderr (e.g., rate limits, auth errors, flag errors)
        if stderr and stderr.strip():
            # Filter out noise - only capture actual errors
            error_lines = [
                line
                for line in stderr.strip().split("\n")
                if line.strip() and not line.startswith("Warning:")
            ]
            if error_lines:
                result.cli_error = "\n".join(error_lines[:5])  # Limit to first 5 lines

        # Write log file if requested
        if log_file:
            self._write_log_file(log_file, prompt, stdout, stderr, result, start_time)

        return result

    def _write_log_file(
        self,
        log_file: Path,
        prompt: str,
        stdout: str,
        stderr: str,
        result: ExecutionResult,
        start_time: float,
    ) -> None:
        """Write worker execution log to file."""
        from datetime import datetime

        log_file.parent.mkdir(parents=True, exist_ok=True)

        with open(log_file, "w") as f:
            f.write("=== Orbit Auto Worker Log ===\n")
            f.write(f"Started: {datetime.fromtimestamp(start_time).isoformat()}\n")
            f.write(f"Duration: {result.duration:.1f}s\n")
            f.write("\n")

            f.write("--- PROMPT ---\n")
            f.write(prompt[:500] + "...\n" if len(prompt) > 500 else prompt + "\n")
            f.write("\n")

            f.write("--- RAW CLAUDE OUTPUT (stream-json) ---\n")
            f.write(stdout if stdout else "(no stdout)\n")
            f.write("\n")

            if stderr and stderr.strip():
                f.write("--- STDERR ---\n")
                f.write(stderr)
                f.write("\n")

            f.write("--- EXECUTION RESULT ---\n")
            f.write(f"Success: {result.success}\n")
            f.write(f"Tools used: {result.tools_used}\n")
            f.write(f"Files modified: {result.files_modified}\n")
            if result.cli_error:
                f.write(f"CLI error: {result.cli_error}\n")
            if result.what_worked:
                f.write(f"What worked: {result.what_worked}\n")
            if result.what_failed:
                f.write(f"What failed: {result.what_failed}\n")
            if result.is_blocked:
                f.write("Status: BLOCKED (waiting for human)\n")

    def _process_output(
        self,
        output: str,
        ctx: StreamContext,
        print_output: bool,
    ) -> ExecutionResult:
        """Process Claude's stream-json output."""
        for line in output.split("\n"):
            line = line.strip()
            if not line:
                continue

            # Parse tool use
            if '"type":"tool_use"' in line:
                self._handle_tool_use(line, ctx, print_output)

            # Capture text content
            if '"type":"assistant"' in line:
                text_content = self._extract_text_content(line)
                if text_content:
                    ctx.accumulated_text += text_content

            if '"type":"result"' in line:
                result_content = self._extract_result_content(line)
                if result_content:
                    ctx.accumulated_text += result_content

        # Build result
        return self._build_result(ctx)

    def _handle_tool_use(
        self,
        line: str,
        ctx: StreamContext,
        print_output: bool,
    ) -> None:
        """Handle a tool use event from the stream."""
        tool_name = self._extract_json_field(line, "name")
        if not tool_name:
            return

        ctx.tool_count += 1
        display_info = ""

        if self.visibility == Visibility.VERBOSE:
            display_info = self._get_verbose_info(line, tool_name, ctx)
        elif self.visibility == Visibility.MINIMAL:
            display_info = self._get_minimal_info(line, tool_name, ctx)

        if print_output and self.on_tool_use:
            self.on_tool_use(tool_name, display_info)

    def _get_verbose_info(self, line: str, tool_name: str, ctx: StreamContext) -> str:
        """Extract verbose display info for a tool call."""
        if tool_name in ("Read", "Write", "Edit"):
            file_path = self._extract_input_field(line, "file_path")
            if file_path and tool_name in ("Write", "Edit"):
                ctx.files_modified.append(file_path)
            return file_path or ""
        elif tool_name == "Bash":
            cmd = self._extract_input_field(line, "command")
            if cmd:
                # Strip ANSI escape sequences
                cmd = re.sub(r"\x1b\[[0-9;]*m", "", cmd)
                if len(cmd) > 50:
                    cmd = cmd[:47] + "..."
            return cmd or ""
        elif tool_name in ("Glob", "Grep"):
            return self._extract_input_field(line, "pattern") or ""
        return ""

    def _get_minimal_info(self, line: str, tool_name: str, ctx: StreamContext) -> str:
        """Extract minimal display info for a tool call."""
        if tool_name in ("Read", "Write", "Edit"):
            file_path = self._extract_input_field(line, "file_path")
            if file_path:
                if tool_name in ("Write", "Edit"):
                    ctx.files_modified.append(file_path)
                return Path(file_path).name
            return ""
        elif tool_name == "Bash":
            cmd = self._extract_input_field(line, "command")
            if cmd:
                cmd = re.sub(r"\x1b\[[0-9;]*m", "", cmd)
                return cmd.split()[0] if cmd.split() else ""
            return ""
        return ""

    def _extract_json_field(self, line: str, field: str) -> str:
        """Extract a field value from JSON line."""
        pattern = rf'"{field}":"([^"]*)"'
        match = re.search(pattern, line)
        return match.group(1) if match else ""

    def _extract_input_field(self, line: str, field: str) -> str:
        """Extract a field from the input object in a tool_use message."""
        # Try to parse as JSON
        try:
            data = json.loads(line)
            # Check nested path first
            if "message" in data and "content" in data["message"]:
                for item in data["message"]["content"]:
                    if item.get("type") == "tool_use" and "input" in item:
                        return item["input"].get(field, "")
            # Check top-level input
            if "input" in data:
                return data["input"].get(field, "")
        except json.JSONDecodeError:
            pass
        return ""

    def _extract_text_content(self, line: str) -> str:
        """Extract text content from an assistant message."""
        try:
            data = json.loads(line)
            if "message" in data and "content" in data["message"]:
                for item in data["message"]["content"]:
                    if item.get("type") == "text":
                        return item.get("text", "")
        except json.JSONDecodeError:
            pass
        return ""

    def _extract_result_content(self, line: str) -> str:
        """Extract result content from a result message."""
        try:
            data = json.loads(line)
            return data.get("result", "")
        except json.JSONDecodeError:
            pass
        return ""

    def _build_result(self, ctx: StreamContext) -> ExecutionResult:
        """Build ExecutionResult from accumulated context."""
        text = ctx.accumulated_text

        # Extract learning tags
        learnings = self._extract_tag(text, "learnings")
        what_worked = self._extract_tag(text, "what_worked")
        what_failed = self._extract_tag(text, "what_failed")
        dont_retry = self._extract_tag(text, "dont_retry")
        try_next = self._extract_tag(text, "try_next")
        pattern = self._extract_tag(text, "pattern_discovered")
        gotcha = self._extract_tag(text, "gotcha")

        # Check status signals
        is_complete = "<promise>COMPLETE</promise>" in text
        is_blocked = "<blocker>WAITING_FOR_HUMAN</blocker>" in text

        # Determine success - require explicit positive signal
        # Task is successful ONLY if:
        # 1. <promise>COMPLETE</promise> is present, OR
        # 2. <what_worked> tag is present (explicit success signal)
        # This prevents marking tasks complete when Claude crashes, rate limits, or outputs nothing
        success = is_complete or (what_worked is not None and not is_blocked)

        return ExecutionResult(
            task_id="",  # Set by caller
            success=success,
            output=text,
            duration=time.time() - ctx.start_time,
            tools_used=ctx.tool_count,
            files_modified=list(set(ctx.files_modified)),
            learnings=learnings,
            what_worked=what_worked,
            what_failed=what_failed,
            dont_retry=dont_retry,
            try_next=try_next,
            pattern_discovered=pattern,
            gotcha=gotcha,
            is_complete=is_complete,
            is_blocked=is_blocked,
        )

    def _extract_tag(self, text: str, tag: str) -> str | None:
        """Extract content between XML-style tags."""
        start_tag = f"<{tag}>"
        end_tag = f"</{tag}>"

        if start_tag not in text or end_tag not in text:
            return None

        start_idx = text.index(start_tag) + len(start_tag)
        end_idx = text.index(end_tag)

        if end_idx > start_idx:
            return text[start_idx:end_idx].strip()
        return None


def build_generic_prompt(
    task_number: str,
    task_title: str,
    tasks_file: Path,
    context_file: Path,
    auto_log: Path | None = None,
) -> str:
    """
    Build a generic prompt for tasks without optimized prompts.

    This replicates the prompt structure used by the bash implementation.
    """
    prompt_parts = [
        "You are working on an autonomous development task using Orbit Auto.",
        "",
        "## Current Task",
        f"Task {task_number}: {task_title}",
        "",
        "## Files to Reference",
        f"- Tasks file: {tasks_file}",
        f"- Context file: {context_file}",
    ]

    if auto_log and auto_log.exists():
        prompt_parts.append(f"- Auto log: {auto_log}")

    prompt_parts.extend(
        [
            "",
            "## Instructions",
            "1. Read the tasks file to understand the full task list",
            "2. Read the context file for project-specific information",
            "3. Complete the current task following the acceptance criteria",
            "4. DO NOT mark the task checkbox - orbit-auto handles task completion tracking",
            "",
            "## Output Tags (REQUIRED)",
            "",
            "**CRITICAL:** orbit-auto detects success via these tags. Without them, the task is marked FAILED.",
            "",
            "Always include:",
            "- <learnings>What you learned from this attempt</learnings>",
            "",
            "**On SUCCESS (REQUIRED for task completion):**",
            "- <what_worked>The approach that succeeded</what_worked>",
            "",
            "On FAILURE:",
            "- <what_failed>What went wrong</what_failed>",
            "- <dont_retry>What not to try again</dont_retry>",
            "- <try_next>What to try next</try_next>",
            "",
            "When ALL tasks are complete:",
            "- <run_summary>Summary of all work done</run_summary>",
            "- <promise>COMPLETE</promise>",
        ]
    )

    return "\n".join(prompt_parts)

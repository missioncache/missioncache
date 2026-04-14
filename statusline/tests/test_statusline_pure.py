"""Pure function tests for statusline.py.

No file I/O, no network, no mocking except monkeypatch for env vars
in _detect_subscription and _get_health_components.
"""

import json
import re
import time

import pytest

from statusline import (
    COLORS,
    RESET,
    _detect_subscription,
    _format_reset_time,
    _format_unix_reset,
    _get_health_components,
    _health_link,
    _item,
    _osc8_link,
    _join_items,
    _pad_line,
    _parse_stdin_rate_limits,
    _parse_task_progress,
    _parse_usage_response,
    _relative_time,
    display_width,
    parse_input,
)


# ============ display_width (5 tests) ============


class TestDisplayWidth:
    def test_ascii_string(self):
        assert display_width("hello") == 5
        assert display_width("") == 0
        assert display_width("abc 123") == 7

    def test_ansi_escape_codes_stripped(self):
        colored = "\033[38;2;180;140;100mhello\033[0m"
        assert display_width(colored) == 5
        # Multiple color codes
        multi = "\033[31mA\033[32mB\033[0m"
        assert display_width(multi) == 2

    def test_emoji_width_2(self):
        # Folder emoji U+1F4C1 is in range 0x1F300-0x1F9FF
        assert display_width("\U0001f4c1") == 2
        # Check mark U+2705 is in _EMOJI_SINGLES
        assert display_width("\u2705") == 2

    def test_vs16_variation_selector(self):
        # U+FE0F (VS16) should not add width by itself
        # Pencil U+270F is in _EMOJI_SINGLES, VS16 follows
        pencil_vs16 = "\u270f\ufe0f"
        assert display_width(pencil_vs16) == 2

    def test_zwj_sequences(self):
        # U+200D (ZWJ) is skipped entirely
        # Simple test: character + ZWJ + character
        # Each emoji is width 2, ZWJ is skipped
        s = "\u2764\u200d\U0001f525"  # heart ZWJ fire
        # heart (U+2764) in _EMOJI_SINGLES -> 2
        # ZWJ skipped
        # fire (U+1F525) in emoji range -> 2
        assert display_width(s) == 4


# ============ _relative_time (4 tests) ============


class TestRelativeTime:
    def test_seconds_ago(self):
        from datetime import datetime, timezone, timedelta

        ts = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
        result = _relative_time(ts)
        assert result.endswith("s ago")
        num = int(result.replace("s ago", ""))
        assert 28 <= num <= 32

    def test_minutes_ago(self):
        from datetime import datetime, timezone, timedelta

        ts = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        result = _relative_time(ts)
        assert result.endswith("m ago")
        assert result == "5m ago"

    def test_hours_ago(self):
        from datetime import datetime, timezone, timedelta

        ts = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        result = _relative_time(ts)
        assert result.endswith("h ago")
        assert result == "3h ago"

    def test_days_ago(self):
        from datetime import datetime, timezone, timedelta

        ts = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        result = _relative_time(ts)
        assert result.endswith("d ago")
        assert result == "2d ago"


# ============ _format_reset_time (3 tests) ============


class TestFormatResetTime:
    def test_valid_iso_timestamp(self):
        # Use a known timestamp: 2025-01-02T11:00:00Z (Thursday)
        result = _format_reset_time("2025-01-02T11:00:00Z")
        # Should be lowercase day + hour format like "thu 11am"
        assert re.match(r"^[a-z]{3} \d{1,2}[ap]m$", result), f"Got: {result}"

    def test_invalid_string(self):
        assert _format_reset_time("not-a-date") == "?"
        assert _format_reset_time("") == "?"

    def test_timezone_aware_conversion(self):
        # Two timestamps representing the same moment should produce the same output
        result1 = _format_reset_time("2025-06-15T12:00:00+00:00")
        result2 = _format_reset_time("2025-06-15T12:00:00Z")
        assert result1 == result2
        # Result should match the expected format
        assert re.match(r"^[a-z]{3} \d{1,2}[ap]m$", result1), f"Got: {result1}"


# ============ _format_unix_reset (3 tests) ============


class TestFormatUnixReset:
    def test_valid_unix_timestamp(self):
        # 1735815600 = 2025-01-02T11:00:00Z (Thursday)
        result = _format_unix_reset(1735815600)
        assert re.match(r"^[a-z]{3} \d{1,2}[ap]m$", result), f"Got: {result}"

    def test_invalid_none(self):
        assert _format_unix_reset(None) == "?"
        assert _format_unix_reset("bad") == "?"

    def test_timestamp_zero(self):
        # Epoch 0 = 1970-01-01T00:00:00Z (Thursday)
        result = _format_unix_reset(0)
        # Should produce a valid formatted time, not "?"
        assert re.match(r"^[a-z]{3} \d{1,2}[ap]m$", result), f"Got: {result}"


# ============ _parse_stdin_rate_limits (4 tests) ============


class TestParseStdinRateLimits:
    def test_full_data(self):
        data = {
            "five_hour": {"used_percentage": 42, "resets_at": 1735815600},
            "seven_day": {"used_percentage": 15, "resets_at": 1735815600},
            "seven_day_opus": {"used_percentage": 8, "resets_at": 1735815600},
        }
        result = _parse_stdin_rate_limits(data)
        assert result["session_pct"] == "42"
        assert result["weekly_pct"] == "15"
        assert result["opus_pct"] == "8"
        assert "session_reset" in result
        assert "weekly_reset" in result

    def test_empty_none(self):
        assert _parse_stdin_rate_limits(None) == {"is_max": True}
        assert _parse_stdin_rate_limits({}) == {"is_max": True}

    def test_partial_five_hour_only(self):
        data = {"five_hour": {"used_percentage": 30, "resets_at": 1735815600}}
        result = _parse_stdin_rate_limits(data)
        assert result["session_pct"] == "30"
        assert "weekly_pct" not in result
        assert "opus_pct" not in result

    def test_opus_zero_excluded(self):
        data = {
            "five_hour": {"used_percentage": 10, "resets_at": 1735815600},
            "seven_day_opus": {"used_percentage": 0, "resets_at": 1735815600},
        }
        result = _parse_stdin_rate_limits(data)
        assert "opus_pct" not in result


# ============ parse_input (6 tests) ============


class TestParseInput:
    def test_full_json(self):
        data = {
            "model": {"display_name": "Opus"},
            "context_window": {
                "used_percentage": 50,
                "context_window_size": 200000,
                "current_usage": {
                    "input_tokens": 10000,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "output_tokens": 5000,
                },
            },
            "cost": {"total_duration_ms": 120000, "total_cost_usd": 1.23},
            "session_id": "test-session",
            "workspace": {"git_worktree": {"name": "feat-branch"}},
            "rate_limits": {"five_hour": {"used_percentage": 20, "resets_at": 0}},
        }
        result = parse_input(json.dumps(data))
        assert result["model_name"] == "Opus"
        assert result["session_id"] == "test-session"
        assert result["ctx_percent"] == 69  # 50 + 19
        assert result["ctx_estimated"] is False
        assert result["worktree"] == {"name": "feat-branch"}

    def test_empty_invalid_json(self):
        result = parse_input("")
        assert result["model_name"] == "Claude"
        assert result["tokens_str"] == "0"
        # Empty input still gets SYSTEM_OVERHEAD_PERCENT (19%) added
        assert result["ctx_percent"] == 19
        assert result["ctx_estimated"] is True

        result2 = parse_input("{invalid json")
        assert result2["model_name"] == "Claude"

    def test_tokens_under_1k(self):
        data = {
            "context_window": {
                "current_usage": {
                    "input_tokens": 500,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "output_tokens": 100,
                }
            }
        }
        result = parse_input(json.dumps(data))
        assert result["tokens_str"] == "600"

    def test_tokens_k_threshold(self):
        data = {
            "context_window": {
                "current_usage": {
                    "input_tokens": 5000,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "output_tokens": 0,
                }
            }
        }
        result = parse_input(json.dumps(data))
        assert result["tokens_str"] == "5.0K"

    def test_tokens_m_threshold(self):
        data = {
            "context_window": {
                "current_usage": {
                    "input_tokens": 1_500_000,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "output_tokens": 0,
                }
            }
        }
        result = parse_input(json.dumps(data))
        assert result["tokens_str"] == "1.5M"

    def test_duration_formatting(self):
        # Under 60 minutes
        data = {"cost": {"total_duration_ms": 125000}}
        result = parse_input(json.dumps(data))
        assert result["duration_str"] == "2m 5s"

        # Over 60 minutes
        data2 = {"cost": {"total_duration_ms": 3_720_000}}  # 62 minutes
        result2 = parse_input(json.dumps(data2))
        assert result2["duration_str"] == "1h 2m"

    def test_cost_formatting(self):
        data = {"cost": {"total_cost_usd": 3.456}}
        result = parse_input(json.dumps(data))
        assert result["cost_str"] == "$3.46"

    def test_worktree_passthrough(self):
        data = {"workspace": {"git_worktree": {"name": "my-worktree", "path": "/some/path"}}}
        result = parse_input(json.dumps(data))
        assert result["worktree"] == {"name": "my-worktree", "path": "/some/path"}


# ============ _parse_usage_response (4 tests) ============


class TestParseUsageResponse:
    def test_full_api_response(self):
        data = {
            "five_hour": {"utilization": 42, "resets_at": "2025-01-02T11:00:00Z"},
            "seven_day": {"utilization": 15, "resets_at": "2025-01-05T00:00:00Z"},
            "seven_day_opus": {"utilization": 8, "resets_at": "2025-01-05T00:00:00Z"},
        }
        result = _parse_usage_response(data)
        assert result["session_pct"] == "42"
        assert result["weekly_pct"] == "15"
        assert result["opus_pct"] == "8"
        assert "session_reset" in result
        assert "weekly_reset" in result

    def test_empty_none_fields(self):
        assert _parse_usage_response({}) == {"is_max": True}
        assert _parse_usage_response(
            {"five_hour": None, "seven_day": None, "seven_day_opus": None}
        ) == {"is_max": True}

    def test_partial_five_hour_only(self):
        data = {"five_hour": {"utilization": 55, "resets_at": "2025-01-02T11:00:00Z"}}
        result = _parse_usage_response(data)
        assert result["session_pct"] == "55"
        assert "weekly_pct" not in result
        assert "opus_pct" not in result

    def test_opus_zero_excluded(self):
        data = {
            "five_hour": {"utilization": 10, "resets_at": "2025-01-02T11:00:00Z"},
            "seven_day_opus": {"utilization": 0, "resets_at": "2025-01-05T00:00:00Z"},
        }
        result = _parse_usage_response(data)
        assert "opus_pct" not in result


# ============ _detect_subscription (6 tests) ============


class TestDetectSubscription:
    ENV_VARS = [
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
    ]

    def _clear_env(self, monkeypatch):
        for var in self.ENV_VARS:
            monkeypatch.delenv(var, raising=False)

    def test_bedrock(self, monkeypatch):
        self._clear_env(monkeypatch)
        monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
        name, icon, color = _detect_subscription(None)
        assert name == "Bedrock"
        assert color == "mode_work"

    def test_vertex(self, monkeypatch):
        self._clear_env(monkeypatch)
        monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")
        name, icon, color = _detect_subscription(None)
        assert name == "Vertex AI"
        assert color == "mode_work"

    def test_foundry(self, monkeypatch):
        self._clear_env(monkeypatch)
        monkeypatch.setenv("CLAUDE_CODE_USE_FOUNDRY", "1")
        name, icon, color = _detect_subscription(None)
        assert name == "Foundry"
        assert color == "mode_work"

    def test_auth_token(self, monkeypatch):
        self._clear_env(monkeypatch)
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "some-token")
        name, icon, color = _detect_subscription(None)
        assert name == "API Gateway"
        assert color == "mode_work"

    def test_api_key(self, monkeypatch):
        self._clear_env(monkeypatch)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xxx")
        name, icon, color = _detect_subscription(None)
        assert name == "API Key"
        assert color == "mode_work"

    def test_oauth_with_usage(self, monkeypatch):
        self._clear_env(monkeypatch)
        usage = {"session_pct": "42", "weekly_pct": "10"}
        name, icon, color = _detect_subscription(usage)
        assert name == "claude.ai"
        assert color == "mode_personal"


# ============ _get_health_components (3 tests) ============


class TestGetHealthComponents:
    def test_no_env_var_defaults(self, monkeypatch):
        monkeypatch.delenv("STATUSLINE_HEALTH_SERVICES", raising=False)
        result = _get_health_components()
        names = set(result.values())
        assert names == {"Code", "Claude API"}

    def test_custom_services(self, monkeypatch):
        monkeypatch.setenv("STATUSLINE_HEALTH_SERVICES", "Code,claude.ai")
        result = _get_health_components()
        names = set(result.values())
        assert names == {"Code", "claude.ai"}

    def test_unknown_service_ignored(self, monkeypatch):
        monkeypatch.setenv("STATUSLINE_HEALTH_SERVICES", "Code,NonExistent")
        result = _get_health_components()
        names = set(result.values())
        assert names == {"Code"}


# ============ _health_link (1 test) ============


class TestHealthLink:
    def test_wraps_in_osc8_hyperlink(self):
        result = _health_link("Status OK")
        assert "Status OK" in result
        assert "https://status.claude.com" in result
        # OSC 8 format: ESC ]8;; URL ESC \ text ESC ]8;; ESC \
        assert "\033]8;;https://status.claude.com\033\\" in result
        assert result.endswith("\033]8;;\033\\")


# ============ _osc8_link (2 tests) ============


class TestOsc8Link:
    def test_wraps_text_in_osc8_hyperlink(self):
        result = _osc8_link("http://localhost:8787/#projects", "my-project")
        assert "my-project" in result
        assert "http://localhost:8787/#projects" in result
        assert "\033]8;;http://localhost:8787/#projects\033\\" in result
        assert result.endswith("\033]8;;\033\\")

    def test_strips_control_characters(self):
        result = _osc8_link("http://example.com", "bad\033name\x07here")
        assert "\033]8;;http://example.com\033\\" in result
        assert "badnamehere" in result
        assert "\x07" not in result.split("\033]8;;")[1].split("\033\\")[1]


# ============ _item (1 test) ============


class TestItem:
    def test_builds_colored_item(self):
        result = _item(COLORS["dir"], "\U0001f4c1", "Dir", "mydir")
        assert "Dir: mydir" in result
        assert result.startswith(COLORS["dir"])
        assert result.endswith(RESET)
        assert "\U0001f4c1" in result


# ============ _join_items / _pad_line (2 tests) ============


class TestJoinItemsPadLine:
    def test_join_items_empty(self):
        assert _join_items([], [], 24, 24) == ""

    def test_pad_line_adds_trailing_spaces(self):
        line = "hello"
        padded = _pad_line(line, 5, 20)
        assert len(padded) == 20
        assert padded == "hello" + " " * 15
        # No padding needed when already at max
        assert _pad_line(line, 20, 20) == "hello"


# ============ _parse_task_progress (orbit project progress bracket) ============


class TestParseTaskProgress:
    def test_normal_fraction(self):
        content = (
            "- [x] 1. done\n"
            "- [x] 2. also done\n"
            "- [x] 3. finished\n"
            "- [ ] 4. todo\n"
            "- [ ] 5. another\n"
        )
        assert _parse_task_progress(content) == "[3/5]"

    def test_all_complete(self):
        content = "- [x] 1. a\n- [x] 2. b\n- [x] 3. c\n- [x] 4. d\n- [x] 5. e\n"
        assert _parse_task_progress(content) == "[5/5]"

    def test_none_complete(self):
        content = "\n".join(f"- [ ] {i}. todo" for i in range(1, 8)) + "\n"
        assert _parse_task_progress(content) == "[0/7]"

    def test_template_placeholder(self):
        assert _parse_task_progress("- [ ] TBD") == "[TBD]"

    def test_template_placeholder_with_leading_whitespace(self):
        assert _parse_task_progress("  - [ ] TBD\n") == "[TBD]"

    def test_empty_content(self):
        assert _parse_task_progress("") == "[TBD]"

    def test_only_headings_and_prose(self):
        content = (
            "# My Project - Tasks\n"
            "\n"
            "**Status:** In Progress\n"
            "\n"
            "Some prose that is not a checklist.\n"
        )
        assert _parse_task_progress(content) == "[TBD]"

    def test_real_task_with_tbd_in_text(self):
        # A real task whose description happens to contain "TBD" should count
        # as a real task, not the placeholder.
        content = "- [ ] 1. TBD: figure out the auth flow"
        assert _parse_task_progress(content) == "[0/1]"

    def test_mixed_nesting_counted_flat(self):
        # All checklist items are counted regardless of indentation.
        content = (
            "- [x] 1. parent done\n"
            "  - [x] 1.1. child done\n"
            "  - [ ] 1.2. child todo\n"
            "- [ ] 2. parent todo\n"
        )
        assert _parse_task_progress(content) == "[2/4]"

    def test_completed_checkbox_case_insensitive(self):
        content = "- [X] upper\n- [x] lower\n- [ ] todo\n"
        assert _parse_task_progress(content) == "[2/3]"

    def test_asterisk_bullets_counted(self):
        content = "* [x] 1. done\n* [ ] 2. todo\n"
        assert _parse_task_progress(content) == "[1/2]"

    def test_uppercase_tbd_placeholder(self):
        assert _parse_task_progress("- [ ] tbd") == "[TBD]"
        assert _parse_task_progress("- [ ] TBD  ") == "[TBD]"

"""Pure function tests for statusline.py.

No file I/O, no network, no mocking except monkeypatch for env vars
in _detect_subscription and for the dashboard config path in
_load_statusline_config.
"""

import json
import re
import time

import pytest

import missioncache_dashboard.statusline as statusline
from missioncache_dashboard.statusline import (
    COLORS,
    ICONS,
    RESET,
    _SGR_RE,
    _apply_health_filters,
    _detect_subscription,
    _format_last_action,
    _format_reset_time,
    _format_unix_reset,
    _health_link,
    _is_model_notice,
    _item,
    _load_statusline_config,
    _osc8_link,
    _join_items,
    _pad_line,
    _parse_stdin_rate_limits,
    _parse_task_progress,
    _parse_usage_response,
    _relative_time,
    _render_effort_field,
    _truncate_to_width,
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
        # used_percentage already reflects the full context fill (system prompt
        # + tools are in current_usage), so it is used verbatim - no overhead add.
        assert result["ctx_percent"] == 50
        assert result["ctx_estimated"] is False
        assert result["worktree"] == {"name": "feat-branch"}

    def test_empty_invalid_json(self):
        result = parse_input("")
        assert result["model_name"] == "Claude"
        assert result["tokens_str"] == "\u21910/\u21930"
        # Empty input has no used_percentage, so the estimated path adds the
        # ctx_size * 0.19 overhead term to a zero token base -> 19%.
        assert result["ctx_percent"] == 19
        assert result["ctx_estimated"] is True

        result2 = parse_input("{invalid json")
        assert result2["model_name"] == "Claude"

    def test_tokens_under_1k(self):
        data = {
            "context_window": {
                "total_input_tokens": 500,
                "total_output_tokens": 100,
            }
        }
        result = parse_input(json.dumps(data))
        assert result["tokens_str"] == "\u2191500/\u2193100"

    def test_tokens_k_threshold(self):
        data = {
            "context_window": {
                "total_input_tokens": 5000,
                "total_output_tokens": 0,
            }
        }
        result = parse_input(json.dumps(data))
        assert result["tokens_str"] == "\u21915.0K/\u21930"

    def test_tokens_m_threshold(self):
        data = {
            "context_window": {
                "total_input_tokens": 1_500_000,
                "total_output_tokens": 0,
            }
        }
        result = parse_input(json.dumps(data))
        assert result["tokens_str"] == "\u21911.5M/\u21930"

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

    def test_empty_usage_dict_is_authenticated_not_free(self, monkeypatch):
        # A stdin rate_limits payload with no window data parses to {}. The
        # user is still authenticated, so they must get the personal styling,
        # NOT the gray free-tier icon.
        self._clear_env(monkeypatch)
        name, icon, color = _detect_subscription({})
        assert name == "claude.ai"
        assert icon == "✨"
        assert color == "mode_personal"

    def test_none_usage_stays_free_tier(self, monkeypatch):
        # No usage data at all (no OAuth token) still resolves to the free-tier
        # icon - the fix only rescues the empty-dict "authenticated" case.
        self._clear_env(monkeypatch)
        name, icon, color = _detect_subscription(None)
        assert name == "claude.ai"
        assert icon == "\U0001f464"
        assert color == "mode_free"


# ============ _load_statusline_config (4 tests) ============


class TestLoadStatuslineConfig:
    def test_missing_file_returns_defaults(self, tmp_path, monkeypatch):
        monkeypatch.setattr('missioncache_dashboard.statusline._DASHBOARD_CONFIG_FILE', tmp_path / 'missing.json')
        cfg = _load_statusline_config()
        assert cfg == {
            "codex": True,
            "subscription_usage": True,
            "subscription_type": True,
            "claude_status": True,
            "claude_status_services": ["Code", "Claude API"],
            "model_suspensions": False,
        }

    def test_partial_config_fills_defaults(self, tmp_path, monkeypatch):
        f = tmp_path / 'config.json'
        f.write_text(json.dumps({"statusline": {"codex": False}}))
        monkeypatch.setattr('missioncache_dashboard.statusline._DASHBOARD_CONFIG_FILE', f)
        cfg = _load_statusline_config()
        assert cfg["codex"] is False
        assert cfg["subscription_usage"] is True
        assert cfg["claude_status_services"] == ["Code", "Claude API"]

    def test_custom_services(self, tmp_path, monkeypatch):
        f = tmp_path / 'config.json'
        f.write_text(json.dumps({"statusline": {"claude_status_services": ["Code", "claude.ai"]}}))
        monkeypatch.setattr('missioncache_dashboard.statusline._DASHBOARD_CONFIG_FILE', f)
        cfg = _load_statusline_config()
        assert cfg["claude_status_services"] == ["Code", "claude.ai"]

    def test_bad_json_returns_defaults(self, tmp_path, monkeypatch):
        f = tmp_path / 'config.json'
        f.write_text("{not valid json")
        monkeypatch.setattr('missioncache_dashboard.statusline._DASHBOARD_CONFIG_FILE', f)
        cfg = _load_statusline_config()
        assert cfg["codex"] is True


# ============ _is_model_notice (model suspension classifier) ============


class TestIsModelNotice:
    def test_real_fable_suspension_name_is_a_notice(self):
        # The live status.claude.com incident the toggle exists to hide.
        name = "We've suspended access to Claude Mythos 5 and Claude Fable 5"
        assert _is_model_notice(name) is True

    def test_operational_outage_is_not_a_notice(self):
        # Genuine outages use "elevated errors" phrasing and must keep showing.
        assert _is_model_notice("Elevated errors on Claude API") is False
        assert _is_model_notice("Service disruption on Claude services") is False

    @pytest.mark.parametrize("name", [
        "Claude Fable 5 deprecated",
        "Sonnet 3.5 sunset on Dec 1",
        "Opus 3 retired from the API",
        "Model X is no longer available",
    ])
    def test_deprecation_wording_is_a_notice(self, name):
        assert _is_model_notice(name) is True

    def test_match_is_case_insensitive(self):
        assert _is_model_notice("WE HAVE SUSPENDED ACCESS") is True

    def test_keyword_only_in_body_still_matches(self):
        assert _is_model_notice("Status update", "this model is now deprecated") is True

    def test_no_keyword_is_not_a_notice(self):
        assert _is_model_notice("Increased latency on Code", "investigating") is False


# ============ _apply_health_filters (toggle + OK fallback) ============


class TestApplyHealthFilters:
    def test_notice_hidden_when_toggle_off(self, monkeypatch):
        monkeypatch.setitem(statusline.STATUSLINE_CONFIG, "model_suspensions", False)
        incidents = [
            {"service": "Code", "name": "outage", "is_model_notice": False},
            {"service": "Both", "name": "suspended", "is_model_notice": True},
        ]
        result = _apply_health_filters(incidents)
        assert result == [{"service": "Code", "name": "outage", "is_model_notice": False}]

    def test_notice_shown_when_toggle_on(self, monkeypatch):
        monkeypatch.setitem(statusline.STATUSLINE_CONFIG, "model_suspensions", True)
        incidents = [{"service": "Both", "name": "suspended", "is_model_notice": True}]
        assert _apply_health_filters(incidents) == incidents

    def test_hiding_only_incident_falls_back_to_ok(self, monkeypatch):
        monkeypatch.setitem(statusline.STATUSLINE_CONFIG, "model_suspensions", False)
        incidents = [{"service": "Both", "name": "suspended", "is_model_notice": True}]
        assert _apply_health_filters(incidents) == [{"service": "OK"}]

    def test_empty_input_is_ok(self, monkeypatch):
        monkeypatch.setitem(statusline.STATUSLINE_CONFIG, "model_suspensions", False)
        assert _apply_health_filters([]) == [{"service": "OK"}]

    def test_untagged_incident_is_kept(self, monkeypatch):
        # A cache written by an older version has no is_model_notice key; such
        # incidents must be treated as real and kept, never silently dropped.
        monkeypatch.setitem(statusline.STATUSLINE_CONFIG, "model_suspensions", False)
        incidents = [{"service": "Code", "name": "outage"}]
        assert _apply_health_filters(incidents) == incidents


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

    def test_pad_line_truncates_when_wider_than_max(self):
        # A line whose display width exceeds max_width is truncated (with an
        # ellipsis) instead of emitted verbatim, so it never wraps a narrow
        # terminal past the fixed-height status block.
        result = _pad_line("hello world", 11, 6)
        assert display_width(result) == 6
        assert "hello" in result
        assert "world" not in result


# ============ _truncate_to_width (narrow-terminal content truncation) ============


class TestTruncateToWidth:
    def test_line_within_width_unchanged(self):
        assert _truncate_to_width("hello", 10) == "hello"

    def test_plain_line_truncated_with_ellipsis(self):
        result = _truncate_to_width("hello world", 6)
        # 5 visible chars + 1 ellipsis cell = 6.
        assert display_width(result) == 6
        assert result.startswith("hello")
        assert result.endswith(RESET)
        assert "…" in result

    def test_ansi_codes_preserved_and_not_counted(self):
        colored = "\033[31mhello world\033[0m"
        result = _truncate_to_width(colored, 6)
        # Color code passes through without consuming width.
        assert "\033[31m" in result
        assert display_width(result) == 6
        assert "world" not in result

    def test_emoji_counted_as_two_cells(self):
        # Folder emoji (width 2) + text; truncating to 3 keeps the emoji (2)
        # plus the ellipsis (1) and drops the trailing text.
        result = _truncate_to_width("\U0001f4c1abcd", 3)
        assert display_width(result) == 3
        assert "\U0001f4c1" in result

    def test_zero_width_returns_empty(self):
        assert _truncate_to_width("anything", 0) == ""


# ============ _format_last_action (session_state last-prompt formatting) ============


class TestFormatLastAction:
    def test_empty_returns_empty(self):
        assert _format_last_action("") == ""

    def test_valid_iso_formats_month_day_time(self):
        assert _format_last_action("2025-01-02T14:05:00") == "Jan 2 14:05"

    def test_garbage_returns_empty(self):
        assert _format_last_action("not-a-timestamp") == ""


# ============ _SGR_RE (NO_COLOR color stripping) ============


class TestSgrColorStripping:
    def test_strips_color_sequences(self):
        colored = f"{COLORS['dir']}Dir: mydir{RESET}"
        assert _SGR_RE.sub("", colored) == "Dir: mydir"

    def test_leaves_osc8_hyperlinks_intact(self):
        # NO_COLOR is about color only - clickable OSC 8 links must survive.
        linked = _osc8_link("http://localhost:8787", "project")
        assert _SGR_RE.sub("", linked) == linked


# ============ _parse_task_progress (MissionCache project progress bracket) ============


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




# ============ _render_effort_field (Effort + Thinking merged field) ============


class TestRenderEffortField:
    def test_no_effort_returns_none(self):
        assert _render_effort_field(None, False) is None

    def test_thinking_on_without_effort_drops_signal(self):
        # Edge case: Claude Code emits thinking but not effort. We deliberately
        # drop the signal rather than rendering a half-merged field.
        assert _render_effort_field(None, True) is None

    def test_effort_with_thinking_off_uses_dart_icon(self):
        result = _render_effort_field("medium", False)
        assert result is not None
        assert ICONS["effort"] in result
        assert ICONS["thinking"] not in result
        assert "Effort: medium" in result

    def test_effort_with_thinking_on_uses_brain_icon(self):
        result = _render_effort_field("high", True)
        assert result is not None
        assert ICONS["thinking"] in result
        assert ICONS["effort"] not in result
        assert "Effort: high" in result

    def test_max_with_thinking_off_uses_dart(self):
        result = _render_effort_field("max", False)
        assert result is not None
        assert ICONS["effort"] in result
        assert ICONS["thinking"] not in result

    def test_max_with_thinking_on_uses_brain(self):
        result = _render_effort_field("max", True)
        assert result is not None
        assert ICONS["thinking"] in result
        assert ICONS["effort"] not in result

    @pytest.mark.parametrize("level,color_key", [
        ("low", "effort_low"),
        ("medium", "effort_medium"),
        ("high", "effort_high"),
        ("xhigh", "effort_xhigh"),
    ])
    def test_effort_color_per_level(self, level, color_key):
        result = _render_effort_field(level, False)
        assert result is not None
        assert COLORS[color_key] in result

    def test_unknown_effort_level_falls_back_to_medium_color(self):
        result = _render_effort_field("garbage", False)
        assert result is not None
        assert COLORS["effort_medium"] in result
        assert "Effort: garbage" in result


# ============ parse_input - effort/thinking extraction across CC versions ============


class TestParseInputEffortThinking:
    def _stdin(self, **overrides) -> str:
        # Minimal stdin shape that parse_input accepts. We only care about
        # effort/thinking extraction here; the other fields just need to
        # not crash the parser.
        base = {
            "model": {"display_name": "Sonnet 4.5"},
            "context_window": {"size": 200000, "used_percentage": 5},
            "cost": {"total_duration_ms": 1000, "total_cost_usd": 0.0},
            "session_id": "test",
            "version": "2.1.119",
        }
        base.update(overrides)
        return json.dumps(base)

    def test_full_v2_119_stdin_extracts_both_fields(self):
        info = parse_input(self._stdin(
            effort={"level": "high"},
            thinking={"enabled": True},
        ))
        assert info["effort_level"] == "high"
        assert info["thinking_enabled"] is True

    def test_old_claude_code_stdin_yields_none_and_false(self):
        # Pre-v2.1.119 Claude Code emits no effort or thinking fields.
        # parse_input must not crash and must return safe defaults.
        info = parse_input(self._stdin())
        assert info["effort_level"] is None
        assert info["thinking_enabled"] is False

    def test_partial_only_effort(self):
        info = parse_input(self._stdin(effort={"level": "low"}))
        assert info["effort_level"] == "low"
        assert info["thinking_enabled"] is False

    def test_partial_only_thinking(self):
        info = parse_input(self._stdin(thinking={"enabled": True}))
        assert info["effort_level"] is None
        assert info["thinking_enabled"] is True

    def test_null_effort_field_handled_safely(self):
        info = parse_input(self._stdin(effort=None))
        assert info["effort_level"] is None

    def test_null_thinking_field_handled_safely(self):
        info = parse_input(self._stdin(thinking=None))
        assert info["thinking_enabled"] is False

    def test_thinking_disabled_explicit_false(self):
        info = parse_input(self._stdin(thinking={"enabled": False}))
        assert info["thinking_enabled"] is False


# ── user addons ──────────────────────────────────────────────────────────


class TestParseAddonOutput:
    def test_plain_text(self):
        assert statusline._parse_addon_output("hello") == {"value": "hello"}

    def test_plain_text_stripped(self):
        assert statusline._parse_addon_output("  hi \n") == {"value": "hi"}

    def test_empty_and_none(self):
        assert statusline._parse_addon_output("") is None
        assert statusline._parse_addon_output("   ") is None
        assert statusline._parse_addon_output(None) is None

    def test_json_object_overrides(self):
        out = statusline._parse_addon_output(
            '{"value":"v","label":"L","icon":"I","color":"ctx","hidden":false}'
        )
        assert out == {"value": "v", "label": "L", "icon": "I", "color": "ctx", "hidden": False}

    def test_json_object_keeps_only_allowed_keys(self):
        out = statusline._parse_addon_output('{"value":"v","evil":"x"}')
        assert out == {"value": "v"}

    def test_non_brace_prefixed_output_is_plain_text(self):
        # Only {-prefixed output is parsed as JSON; a bare number/array is text.
        assert statusline._parse_addon_output("42") == {"value": "42"}
        assert statusline._parse_addon_output("[1,2]") == {"value": "[1,2]"}

    def test_ansi_and_control_chars_stripped(self):
        out = statusline._parse_addon_output("a\x1b[31mb\x07c")
        assert "\x1b" not in out["value"] and "\x07" not in out["value"]

    def test_invalid_json_object_falls_back_to_text(self):
        out = statusline._parse_addon_output('{not json')
        assert out == {"value": "{not json"}


def _addon(**kw):
    base = {"id": "a", "enabled": True, "label": "Lbl", "icon": ">",
            "color": "version", "command": ["echo", "x"], "ttl": 60, "timeout": 5,
            "placement": {"mode": "row", "group": "g", "order": 0, "target": None}}
    base.update(kw)
    return base


class TestRenderAddonCell:
    def test_value_renders_label_and_value(self):
        cell = statusline._render_addon_cell(_addon(), {"value": "3"})
        assert "Lbl: 3" in cell
        assert cell.endswith(RESET)

    def test_none_data_returns_none(self):
        assert statusline._render_addon_cell(_addon(), None) is None

    def test_hidden_returns_none(self):
        assert statusline._render_addon_cell(_addon(), {"value": "x", "hidden": True}) is None

    def test_empty_value_returns_none(self):
        assert statusline._render_addon_cell(_addon(), {"value": ""}) is None

    def test_label_icon_override(self):
        cell = statusline._render_addon_cell(_addon(), {"value": "v", "label": "New", "icon": "*"})
        assert "New: v" in cell and "*" in cell

    def test_disallowed_color_falls_back_to_addon_color(self):
        # override color not in the allowlist -> addon's own color-key is used
        cell = statusline._render_addon_cell(_addon(color="ctx"), {"value": "v", "color": "not-a-color"})
        assert cell.startswith(COLORS["ctx"])

    def test_control_chars_in_value_do_not_reach_rendered_cell(self):
        # Values reach _render_addon_cell already stripped by _parse_addon_output;
        # feed a real escape through the parse+render pipeline and confirm the
        # rendered cell carries only the framing color + RESET, no injected ESC.
        data = statusline._parse_addon_output("val\x1b[31;5mue")
        cell = statusline._render_addon_cell(_addon(color="version"), data)
        # The injected ESC byte is stripped; only the printable residue remains,
        # so the value can't start a real ANSI sequence.
        assert "val[31;5mue" in cell
        assert "\x1b[31" not in cell
        # Exactly the two framing escapes (the color prefix + RESET), no third.
        assert cell.count("\x1b") == 2
        assert cell.startswith(COLORS["version"]) and cell.endswith(RESET)


class TestLoadAddons:
    def _write(self, tmp_path, obj):
        p = tmp_path / "cfg.json"
        p.write_text(json.dumps(obj))
        return p

    def test_valid_enabled_only(self, tmp_path):
        p = self._write(tmp_path, {"statusline_addons": [
            _addon(id="one"),
            _addon(id="two", enabled=False),
        ]})
        addons = statusline._load_addons(p)
        assert [a["id"] for a in addons] == ["one"]

    def test_bad_id_rejected(self, tmp_path):
        p = self._write(tmp_path, {"statusline_addons": [
            {"id": "../evil", "command": ["echo", "x"]},
            {"id": "OK-no", "command": ["echo", "x"]},  # uppercase -> rejected
            _addon(id="good"),
        ]})
        assert [a["id"] for a in statusline._load_addons(p)] == ["good"]

    def test_empty_or_nonlist_command_rejected(self, tmp_path):
        p = self._write(tmp_path, {"statusline_addons": [
            {"id": "empty", "command": []},
            {"id": "nolist", "command": "echo x"},
            _addon(id="good"),
        ]})
        assert [a["id"] for a in statusline._load_addons(p)] == ["good"]

    def test_duplicate_ids_first_wins(self, tmp_path):
        p = self._write(tmp_path, {"statusline_addons": [
            _addon(id="dup", command=["echo", "1"]),
            _addon(id="dup", command=["echo", "2"]),
        ]})
        addons = statusline._load_addons(p)
        assert len(addons) == 1 and addons[0]["command"] == ["echo", "1"]

    def test_ttl_timeout_bounded(self, tmp_path):
        p = self._write(tmp_path, {"statusline_addons": [
            _addon(id="a", ttl=1, timeout=999),
        ]})
        a = statusline._load_addons(p)[0]
        assert a["ttl"] == 5 and a["timeout"] == 30

    def test_disallowed_color_defaults(self, tmp_path):
        p = self._write(tmp_path, {"statusline_addons": [_addon(id="a", color="bogus")]})
        assert statusline._load_addons(p)[0]["color"] == statusline.ADDON_DEFAULT_COLOR

    def test_malformed_file_returns_empty(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{not json")
        assert statusline._load_addons(p) == []

    def test_missing_key_returns_empty(self, tmp_path):
        p = self._write(tmp_path, {"other": 1})
        assert statusline._load_addons(p) == []


class TestAddonPlacement:
    def test_row_groups_distinct_and_ordered(self):
        addons = [
            _addon(id="a", placement={"mode": "row", "group": "z", "order": 10, "target": None}),
            _addon(id="b", placement={"mode": "row", "group": "a", "order": 5, "target": None}),
            _addon(id="c", placement={"mode": "row", "group": "z", "order": 1, "target": None}),
        ]
        # sorted by (min order in group, name): 'z' min order 1, 'a' order 5 -> z before a
        assert statusline._addon_row_groups(addons) == ["z", "a"]

    def test_append_only_yields_no_rows(self):
        addons = [_addon(id="a", placement={"mode": "append", "group": "a", "order": 0, "target": "vitals"})]
        assert statusline._addon_row_groups(addons) == []

    def test_groups_determined_by_config_not_data(self):
        # group list is identical whether or not fetch data is provided
        addons = [_addon(id="a", placement={"mode": "row", "group": "g", "order": 0, "target": None})]
        assert statusline._addon_row_groups(addons) == ["g"]

    def test_assemble_routes_row_and_append(self):
        addons = [
            _addon(id="r", label="R", placement={"mode": "row", "group": "g", "order": 0, "target": None}),
            _addon(id="ap", label="A", placement={"mode": "append", "group": "ap", "order": 0, "target": "vitals"}),
        ]
        values = {"r": {"value": "1"}, "ap": {"value": "2"}}
        rows, appends = statusline._assemble_addon_cells(addons, values)
        assert "g" in rows and len(rows["g"]) == 1
        assert appends["line_health"] and len(appends["line_health"]) == 1

    def test_assemble_omits_none_cells_but_group_survives(self):
        addons = [_addon(id="r", placement={"mode": "row", "group": "g", "order": 0, "target": None})]
        rows, appends = statusline._assemble_addon_cells(addons, {"r": None})
        # cell omitted from row_items ...
        assert rows == {}
        # ... but the group still counts as a line (height stays stable)
        assert statusline._addon_row_groups(addons) == ["g"]


class TestAddonReviewFixes:
    """Regression tests for the review-round hardening fixes."""

    def _write(self, tmp_path, addons):
        p = tmp_path / "cfg.json"
        p.write_text(json.dumps({"statusline_addons": addons}))
        return p

    def test_append_without_target_downgrades_to_row(self, tmp_path):
        # append + no/invalid target would render nothing; must fall back to row.
        p = self._write(tmp_path, [_addon(id="a", placement={"mode": "append", "target": "typo"})])
        a = statusline._load_addons(p)[0]
        assert a["placement"]["mode"] == "row"
        # and it now produces a row group so it actually renders
        assert statusline._addon_row_groups([a]) == [a["placement"]["group"]]

    def test_append_with_valid_target_stays_append(self, tmp_path):
        p = self._write(tmp_path, [_addon(id="a", placement={"mode": "append", "target": "vitals"})])
        a = statusline._load_addons(p)[0]
        assert a["placement"]["mode"] == "append" and a["placement"]["target"] == "vitals"

    def test_control_chars_stripped_from_static_label_icon(self, tmp_path):
        p = self._write(tmp_path, [_addon(id="a", label="L\x1b[31mX", icon="i\x07")])
        a = statusline._load_addons(p)[0]
        assert "\x1b" not in a["label"] and "\x07" not in a["icon"]
        # and the strip carries through to the rendered cell (plain-text output
        # uses the static label/icon)
        cell = statusline._render_addon_cell(a, {"value": "v"})
        assert "\x1b[31m" not in cell

    def test_trailing_newline_id_rejected(self, tmp_path):
        p = self._write(tmp_path, [
            {"id": "abc\n", "command": ["echo", "x"]},
            _addon(id="good"),
        ])
        assert [a["id"] for a in statusline._load_addons(p)] == ["good"]

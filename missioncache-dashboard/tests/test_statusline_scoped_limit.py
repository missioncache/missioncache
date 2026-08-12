"""Tests for the per-model weekly usage counter (`_parse_scoped_limit`).

Spec source: the usage API's own response, and the Claude desktop app's Usage
pane which renders the same numbers. A live payload captured while Fable was
capped, alongside what the app displayed for the same account at the same time:

    app:  Current session 57% | All models 44% | Fable 71% (resets Wed 9:59 AM)
    api:  five_hour 57.0      | seven_day 44.0 | limits[] -> weekly_scoped 71

The scoped entry is the ONLY place that number appears. Every top-level
per-model bucket (`seven_day_opus`, `seven_day_sonnet`, `cinder_cove`, ...) was
null in that same payload, which is why the counter reads `limits` instead.

Assertions trace to that contract, not to the parser's implementation.
"""

import missioncache_dashboard.statusline as mod


# Verbatim shape from the live API response, trimmed to the fields the parser
# reads. Kept as a literal so a change in the API's schema shows up as a test
# failure rather than as a silently empty counter.
LIVE_LIMITS = [
    {
        "kind": "session",
        "group": "session",
        "percent": 58,
        "severity": "normal",
        "resets_at": "2026-08-12T19:59:59.910779+00:00",
        "scope": None,
        "is_active": False,
    },
    {
        "kind": "weekly_all",
        "group": "weekly",
        "percent": 44,
        "severity": "normal",
        "resets_at": "2026-08-19T07:00:00.910803+00:00",
        "scope": None,
        "is_active": False,
    },
    {
        "kind": "weekly_scoped",
        "group": "weekly",
        "percent": 71,
        "severity": "normal",
        "resets_at": "2026-08-19T06:59:59.911034+00:00",
        "scope": {"model": {"id": None, "display_name": "Fable"}},
        "is_active": True,
    },
]


class TestParseScopedLimit:
    def test_reads_the_scoped_weekly_entry(self):
        got = mod._parse_scoped_limit(LIVE_LIMITS)
        assert got["scoped_label"] == "Fable"
        assert got["scoped_pct"] == "71"

    def test_label_comes_from_the_api_not_a_hardcoded_model(self):
        """The counter must follow whichever model the plan scopes, so a
        different display_name has to render as that name."""
        limits = [dict(LIVE_LIMITS[2], scope={"model": {"id": None, "display_name": "Mythos"}})]
        assert mod._parse_scoped_limit(limits)["scoped_label"] == "Mythos"

    def test_ignores_the_unscoped_weekly_and_session_entries(self):
        """44 and 58 belong to the existing weekly and session counters. Picking
        either here would silently duplicate them under a model's name."""
        got = mod._parse_scoped_limit(LIVE_LIMITS)
        assert got["scoped_pct"] not in ("44", "58")

    def test_none_when_no_model_is_scoped(self):
        """The normal state on a plan that caps no single model. None keeps the
        counter off the line entirely rather than rendering an empty label."""
        assert mod._parse_scoped_limit(LIVE_LIMITS[:2]) is None

    def test_none_when_the_scope_carries_no_display_name(self):
        """scope.model.id is null in practice, so display_name is the only
        usable key; without it there is nothing to label the counter with."""
        limits = [dict(LIVE_LIMITS[2], scope={"model": {"id": None, "display_name": None}})]
        assert mod._parse_scoped_limit(limits) is None

    def test_zero_percent_still_reports(self):
        """0% is a real reading, not a missing one. The Opus counter hides at
        zero; this one must not, or a freshly reset week looks like no cap."""
        limits = [dict(LIVE_LIMITS[2], percent=0)]
        assert mod._parse_scoped_limit(limits)["scoped_pct"] == "0"

    def test_survives_a_missing_or_malformed_limits_field(self):
        """The field is absent on older responses and on other plan types."""
        for bad in (None, {}, "limits", [], [None], [{"kind": "weekly_scoped"}]):
            assert mod._parse_scoped_limit(bad) is None


class TestScopedLimitInTheUsageResponse:
    def test_surfaces_through_the_full_parser(self):
        parsed = mod._parse_usage_response({
            "five_hour": {"utilization": 58.0, "resets_at": ""},
            "seven_day": {"utilization": 44.0, "resets_at": ""},
            "limits": LIVE_LIMITS,
        })
        assert parsed["scoped_label"] == "Fable"
        assert parsed["scoped_pct"] == "71"
        assert parsed["weekly_pct"] == "44", "the all-models counter is unchanged"

    def test_absent_scoped_entry_leaves_no_stray_keys(self):
        parsed = mod._parse_usage_response({
            "five_hour": {"utilization": 58.0, "resets_at": ""},
            "seven_day": {"utilization": 44.0, "resets_at": ""},
            "limits": LIVE_LIMITS[:2],
        })
        assert "scoped_label" not in parsed
        assert "scoped_pct" not in parsed

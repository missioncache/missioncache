"""Tests for missioncache_db.agenda.

Spec source: the agenda contract in the module docstring - two source kinds
(ics, command), a single-day RRULE hit test with a named out-of-scope list,
all-day DTEND treated as EXCLUSIVE, RECURRENCE-ID overrides replacing rather
than duplicating an instance, and the never-raises rule where every failure
becomes a source status instead of an exception.

No network and no clock dependence: ICS fixtures are strings and every query
names its day explicitly.
"""

import json
import os
import sys
from datetime import date
from pathlib import Path

import pytest

from missioncache_db import agenda


DAY = date(2026, 9, 8)  # a Tuesday


def _ics(*vevents: str) -> str:
    body = "\n".join(vevents)
    return f"BEGIN:VCALENDAR\nVERSION:2.0\n{body}\nEND:VCALENDAR\n"


def _vevent(**props: str) -> str:
    lines = ["BEGIN:VEVENT"]
    lines += [f"{k.replace('_', '-')}:{v}" for k, v in props.items()]
    lines.append("END:VEVENT")
    return "\n".join(lines)


def _parse(text: str, day: date = DAY):
    return agenda.parse_ics(text, day, calendar="Work", tz=agenda._resolve_zone("UTC"))


# ---------------------------------------------------------------------------
# Single events
# ---------------------------------------------------------------------------


class TestSingleEvents:
    def test_timed_event_on_the_day(self):
        events, notes = _parse(
            _ics(_vevent(UID="a", DTSTART="20260908T093000Z",
                         DTEND="20260908T100000Z", SUMMARY="AIP sync",
                         LOCATION="Webex"))
        )
        assert len(events) == 1
        assert events[0]["title"] == "AIP sync"
        assert events[0]["location"] == "Webex"
        assert events[0]["all_day"] is False
        assert events[0]["start"].startswith("2026-09-08T09:30")
        assert notes["skipped_rules"] == 0

    def test_event_on_another_day_is_not_returned(self):
        events, _ = _parse(
            _ics(_vevent(UID="a", DTSTART="20260909T093000Z",
                         DTEND="20260909T100000Z", SUMMARY="Tomorrow"))
        )
        assert events == []

    def test_duration_stands_in_for_a_missing_dtend(self):
        events, _ = _parse(
            _ics(_vevent(UID="a", DTSTART="20260908T090000Z",
                         DURATION="PT1H30M", SUMMARY="Long one"))
        )
        assert events[0]["end"].startswith("2026-09-08T10:30")

    def test_cancelled_events_are_skipped(self):
        events, _ = _parse(
            _ics(_vevent(UID="a", DTSTART="20260908T090000Z",
                         DTEND="20260908T093000Z", SUMMARY="Off",
                         STATUS="CANCELLED"))
        )
        assert events == []

    def test_summary_text_is_unescaped(self):
        events, _ = _parse(
            _ics(_vevent(UID="a", DTSTART="20260908T090000Z",
                         DTEND="20260908T093000Z",
                         SUMMARY=r"Review\, then merge\; ship"))
        )
        assert events[0]["title"] == "Review, then merge; ship"

    def test_folded_lines_are_rejoined(self):
        raw = (
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:a\n"
            "DTSTART:20260908T090000Z\nDTEND:20260908T093000Z\n"
            "SUMMARY:A very long meeting\n  title that was folded\n"
            "END:VEVENT\nEND:VCALENDAR\n"
        )
        events, _ = _parse(raw)
        assert events[0]["title"] == "A very long meeting title that was folded"


class TestAllDayEvents:
    def test_dtend_is_exclusive_so_a_one_day_event_ends_the_same_day(self):
        """The single most common way an ICS consumer is silently wrong."""
        events, _ = _parse(
            _ics(_vevent(UID="a", DTSTART="20260908", DTEND="20260909",
                         SUMMARY="Ilya on vacation"))
        )
        assert events[0]["all_day"] is True
        assert events[0]["start"] == "2026-09-08"
        assert events[0]["end"] == "2026-09-08"

    def test_multi_day_all_day_event_keeps_its_last_covered_day(self):
        events, _ = _parse(
            _ics(_vevent(UID="a", DTSTART="20260908", DTEND="20260911",
                         SUMMARY="Conference"))
        )
        assert events[0]["end"] == "2026-09-10"

    def test_value_date_parameter_is_honored(self):
        raw = (
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:a\n"
            "DTSTART;VALUE=DATE:20260908\nDTEND;VALUE=DATE:20260909\n"
            "SUMMARY:Holiday\nEND:VEVENT\nEND:VCALENDAR\n"
        )
        events, _ = _parse(raw)
        assert events[0]["all_day"] is True
        assert events[0]["end"] == "2026-09-08"


# ---------------------------------------------------------------------------
# Recurrence
# ---------------------------------------------------------------------------


class TestRecurrence:
    def test_daily_rule_lands_on_a_later_day(self):
        events, _ = _parse(
            _ics(_vevent(UID="a", DTSTART="20260901T090000Z",
                         DTEND="20260901T093000Z", SUMMARY="Standup",
                         RRULE="FREQ=DAILY"))
        )
        assert len(events) == 1
        # The occurrence is shifted onto the queried day, not left at DTSTART.
        assert events[0]["start"].startswith("2026-09-08T09:00")

    def test_daily_interval_skips_the_off_days(self):
        rule = _vevent(UID="a", DTSTART="20260901T090000Z",
                       DTEND="20260901T093000Z", SUMMARY="Every other",
                       RRULE="FREQ=DAILY;INTERVAL=2")
        # Sep 1 + 7 days is an odd offset, so it must NOT hit.
        assert _parse(_ics(rule), date(2026, 9, 8))[0] == []
        assert len(_parse(_ics(rule), date(2026, 9, 9))[0]) == 1

    def test_weekly_byday_hits_only_the_listed_weekdays(self):
        rule = _vevent(UID="a", DTSTART="20260901T090000Z",
                       DTEND="20260901T093000Z", SUMMARY="Guild",
                       RRULE="FREQ=WEEKLY;BYDAY=TU,TH")
        assert len(_parse(_ics(rule), date(2026, 9, 8))[0]) == 1   # Tuesday
        assert len(_parse(_ics(rule), date(2026, 9, 10))[0]) == 1  # Thursday
        assert _parse(_ics(rule), date(2026, 9, 9))[0] == []       # Wednesday

    def test_biweekly_skips_the_intervening_week(self):
        rule = _vevent(UID="a", DTSTART="20260901T090000Z",
                       DTEND="20260901T093000Z", SUMMARY="Biweekly",
                       RRULE="FREQ=WEEKLY;INTERVAL=2;BYDAY=TU")
        assert len(_parse(_ics(rule), date(2026, 9, 1))[0]) == 1
        assert _parse(_ics(rule), date(2026, 9, 8))[0] == []
        assert len(_parse(_ics(rule), date(2026, 9, 15))[0]) == 1

    def test_monthly_bymonthday(self):
        rule = _vevent(UID="a", DTSTART="20260608T090000Z",
                       DTEND="20260608T093000Z", SUMMARY="Monthly",
                       RRULE="FREQ=MONTHLY;BYMONTHDAY=8")
        assert len(_parse(_ics(rule), date(2026, 9, 8))[0]) == 1
        assert _parse(_ics(rule), date(2026, 9, 9))[0] == []

    def test_count_stops_the_series(self):
        rule = _vevent(UID="a", DTSTART="20260901T090000Z",
                       DTEND="20260901T093000Z", SUMMARY="Three only",
                       RRULE="FREQ=DAILY;COUNT=3")
        assert len(_parse(_ics(rule), date(2026, 9, 3))[0]) == 1
        assert _parse(_ics(rule), date(2026, 9, 8))[0] == []

    def test_until_stops_the_series(self):
        rule = _vevent(UID="a", DTSTART="20260901T090000Z",
                       DTEND="20260901T093000Z", SUMMARY="Bounded",
                       RRULE="FREQ=DAILY;UNTIL=20260905T000000Z")
        assert len(_parse(_ics(rule), date(2026, 9, 4))[0]) == 1
        assert _parse(_ics(rule), date(2026, 9, 8))[0] == []

    def test_exdate_removes_one_occurrence(self):
        raw = (
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:a\n"
            "DTSTART:20260901T090000Z\nDTEND:20260901T093000Z\n"
            "SUMMARY:Standup\nRRULE:FREQ=DAILY\n"
            "EXDATE:20260908T090000Z\nEND:VEVENT\nEND:VCALENDAR\n"
        )
        assert _parse(raw, date(2026, 9, 8))[0] == []
        assert len(_parse(raw, date(2026, 9, 9))[0]) == 1

    def test_recurrence_id_override_replaces_rather_than_duplicates(self):
        """A moved instance must show once, at its new time."""
        raw = _ics(
            _vevent(UID="a", DTSTART="20260901T090000Z", DTEND="20260901T093000Z",
                    SUMMARY="Standup", RRULE="FREQ=DAILY"),
            "BEGIN:VEVENT\nUID:a\nRECURRENCE-ID:20260908T090000Z\n"
            "DTSTART:20260908T140000Z\nDTEND:20260908T143000Z\n"
            "SUMMARY:Standup moved\nEND:VEVENT",
        )
        events, _ = _parse(raw, date(2026, 9, 8))
        assert len(events) == 1
        assert events[0]["title"] == "Standup moved"
        assert events[0]["start"].startswith("2026-09-08T14:00")


class TestUnsupportedRulesAreCountedNotDropped:
    """A silent gap reads as 'nothing scheduled', which is the wrong answer."""

    def test_yearly_is_reported(self):
        events, notes = _parse(
            _ics(_vevent(UID="a", DTSTART="20200908T090000Z",
                         DTEND="20200908T093000Z", SUMMARY="Birthday",
                         RRULE="FREQ=YEARLY"))
        )
        assert events == []
        assert notes["skipped_rules"] == 1

    def test_ordinal_weekday_is_reported(self):
        events, notes = _parse(
            _ics(_vevent(UID="a", DTSTART="20260602T090000Z",
                         DTEND="20260602T093000Z", SUMMARY="Second Tuesday",
                         RRULE="FREQ=MONTHLY;BYDAY=2TU"))
        )
        assert events == []
        assert notes["skipped_rules"] == 1

    def test_weekly_byday_with_an_ordinal_is_reported(self):
        events, notes = _parse(
            _ics(_vevent(UID="a", DTSTART="20260901T090000Z",
                         DTEND="20260901T093000Z", SUMMARY="Odd",
                         RRULE="FREQ=WEEKLY;BYDAY=2TU"))
        )
        assert events == []
        assert notes["skipped_rules"] == 1


class TestTimeZones:
    def test_unknown_tzid_falls_back_and_is_reported(self):
        """Outlook exports carry Windows zone names that zoneinfo cannot load."""
        raw = (
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:a\n"
            "DTSTART;TZID=Israel Standard Time:20260908T090000\n"
            "DTEND;TZID=Israel Standard Time:20260908T093000\n"
            "SUMMARY:Outlook\nEND:VEVENT\nEND:VCALENDAR\n"
        )
        events, notes = _parse(raw)
        assert len(events) == 1
        assert notes["tz_fallbacks"] == ["Israel Standard Time"]

    def test_known_tzid_is_converted_into_the_configured_zone(self):
        """A foreign TZID is read correctly and then RE-STATED in our zone.

        `_parse` configures UTC, so 09:00 Jerusalem must come back as 06:00Z:
        the same instant, on the reader's own wall clock. Printing the source
        zone's own numbers is how a 09:00Z meeting shows as 09:00 to someone
        three hours away.
        """
        raw = (
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:a\n"
            "DTSTART;TZID=Asia/Jerusalem:20260908T090000\n"
            "DTEND;TZID=Asia/Jerusalem:20260908T093000\n"
            "SUMMARY:Local\nEND:VEVENT\nEND:VCALENDAR\n"
        )
        events, notes = _parse(raw)
        assert notes["tz_fallbacks"] == []
        assert events[0]["start"] == "2026-09-08T06:00:00+00:00"
        assert events[0]["end"] == "2026-09-08T06:30:00+00:00"

    def test_utc_event_is_restated_in_a_non_utc_configured_zone(self):
        """The direction the UTC-configured tests cannot see."""
        raw = (
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:a\n"
            "DTSTART:20260908T090000Z\nDTEND:20260908T100000Z\n"
            "SUMMARY:Zulu\nEND:VEVENT\nEND:VCALENDAR\n"
        )
        events, _ = agenda.parse_ics(
            raw, DAY, calendar="Work", tz=agenda._resolve_zone("Asia/Jerusalem")
        )
        assert events[0]["start"] == "2026-09-08T12:00:00+03:00"
        assert events[0]["end"] == "2026-09-08T13:00:00+03:00"

    def test_unresolvable_tzid_falls_back_to_the_configured_zone(self):
        """The fallback is OBSERVED, not just counted.

        A UTC-configured fixture cannot see this: the fallback zone and the
        output zone would be the same, so any offset assertion passes whether
        the fallback happened or not.
        """
        raw = (
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:a\n"
            "DTSTART;TZID=Israel Standard Time:20260908T090000\n"
            "DTEND;TZID=Israel Standard Time:20260908T093000\n"
            "SUMMARY:Outlook\nEND:VEVENT\nEND:VCALENDAR\n"
        )
        events, notes = agenda.parse_ics(
            raw, DAY, calendar="Work", tz=agenda._resolve_zone("Asia/Jerusalem")
        )
        assert notes["tz_fallbacks"] == ["Israel Standard Time"]
        # Read AS Jerusalem local, so the wall-clock numbers survive.
        assert events[0]["start"] == "2026-09-08T09:00:00+03:00"


# ---------------------------------------------------------------------------
# agenda_for: sources, failure handling, ordering
# ---------------------------------------------------------------------------


@pytest.fixture
def rooted(tmp_path, monkeypatch):
    """Point the cache at a temp data root, never the developer's own."""
    import missioncache_db

    monkeypatch.setattr(missioncache_db, "MISSIONCACHE_ROOT", tmp_path / "root")
    return tmp_path


class TestAgendaFor:
    def test_no_sources_is_not_configured_and_not_an_error(self, rooted):
        result = agenda.agenda_for(DAY, config={"sources": []})
        assert result["configured"] is False
        assert result["events"] == []
        assert result["sources"] == []

    def test_disabled_sources_do_not_count_as_configured(self, rooted):
        result = agenda.agenda_for(
            DAY, config={"sources": [{"name": "X", "kind": "ics",
                                      "location": "/nope.ics", "enabled": False}]}
        )
        assert result["configured"] is False

    def test_file_source_is_read(self, rooted, tmp_path):
        path = tmp_path / "cal.ics"
        path.write_text(
            _ics(_vevent(UID="a", DTSTART="20260908T090000Z",
                         DTEND="20260908T093000Z", SUMMARY="From file"))
        )
        result = agenda.agenda_for(
            DAY, config={"sources": [{"name": "Work", "kind": "ics",
                                      "location": str(path)}]}
        )
        assert result["configured"] is True
        assert [e["title"] for e in result["events"]] == ["From file"]
        assert result["sources"][0]["status"] == "ok"
        assert result["sources"][0]["count"] == 1

    def test_a_broken_source_reports_and_does_not_raise(self, rooted, tmp_path):
        """A calendar problem must never be why a brief fails to render."""
        good = tmp_path / "good.ics"
        good.write_text(
            _ics(_vevent(UID="a", DTSTART="20260908T090000Z",
                         DTEND="20260908T093000Z", SUMMARY="Survives"))
        )
        result = agenda.agenda_for(
            DAY,
            config={"sources": [
                {"name": "Broken", "kind": "ics", "location": str(tmp_path / "missing.ics")},
                {"name": "Good", "kind": "ics", "location": str(good)},
            ]},
        )
        statuses = {s["name"]: s["status"] for s in result["sources"]}
        assert statuses == {"Broken": "error", "Good": "ok"}
        assert [e["title"] for e in result["events"]] == ["Survives"]
        assert result["sources"][0]["error"]

    def test_glob_matching_nothing_is_an_error_with_a_reason(self, rooted, tmp_path):
        result = agenda.agenda_for(
            DAY, config={"sources": [{"name": "G", "kind": "ics",
                                      "location": str(tmp_path / "*.ics")}]}
        )
        assert result["sources"][0]["status"] == "error"
        assert "no files matched" in result["sources"][0]["error"]

    def test_glob_reads_every_match(self, rooted, tmp_path):
        for index, title in enumerate(["One", "Two"]):
            (tmp_path / f"c{index}.ics").write_text(
                _ics(_vevent(UID=f"u{index}", DTSTART="20260908T090000Z",
                             DTEND="20260908T093000Z", SUMMARY=title))
            )
        result = agenda.agenda_for(
            DAY, config={"sources": [{"name": "G", "kind": "ics",
                                      "location": str(tmp_path / "*.ics")}]}
        )
        assert sorted(e["title"] for e in result["events"]) == ["One", "Two"]

    def test_all_day_events_sort_above_timed_ones(self, rooted, tmp_path):
        path = tmp_path / "cal.ics"
        path.write_text(
            _ics(
                _vevent(UID="a", DTSTART="20260908T130000Z",
                        DTEND="20260908T140000Z", SUMMARY="Afternoon"),
                _vevent(UID="b", DTSTART="20260908T090000Z",
                        DTEND="20260908T093000Z", SUMMARY="Morning"),
                _vevent(UID="c", DTSTART="20260908", DTEND="20260909",
                        SUMMARY="Vacation"),
            )
        )
        result = agenda.agenda_for(
            DAY, config={"sources": [{"name": "W", "kind": "ics",
                                      "location": str(path)}]}
        )
        assert [e["title"] for e in result["events"]] == [
            "Vacation", "Morning", "Afternoon"
        ]

    def test_skipped_rules_surface_on_the_source(self, rooted, tmp_path):
        path = tmp_path / "cal.ics"
        path.write_text(
            _ics(_vevent(UID="a", DTSTART="20200908T090000Z",
                         DTEND="20200908T093000Z", SUMMARY="Birthday",
                         RRULE="FREQ=YEARLY"))
        )
        result = agenda.agenda_for(
            DAY, config={"sources": [{"name": "W", "kind": "ics",
                                      "location": str(path)}]}
        )
        assert result["sources"][0]["skipped_rules"] == 1


class TestCommandSource:
    def _script(self, tmp_path, body: str) -> list:
        """An argv that runs ``body`` as Python. Returns the whole argv, not a path.

        These used to be `/bin/sh` scripts, which Windows cannot execute at
        all: CreateProcess needs a real executable, so every test here died
        with WinError 193 instead of exercising the command source. Running
        the current interpreter against a script file behaves the same on
        every platform, and ``sys.executable`` is absolute, which is what the
        source itself demands of argv[0].
        """
        path = tmp_path / "cal.py"
        path.write_text(body)
        return [sys.executable, str(path)]

    def test_json_output_becomes_events(self, rooted, tmp_path):
        payload = json.dumps([
            {"start": "2026-09-08T09:30:00+03:00", "end": "2026-09-08T10:00:00+03:00",
             "title": "From command", "location": "Webex"}
        ])
        script = self._script(tmp_path, f"print({payload!r})")
        result = agenda.agenda_for(
            DAY, config={"sources": [{"name": "Team", "kind": "command",
                                      "command": script}]}
        )
        assert result["sources"][0]["status"] == "ok"
        assert result["events"][0]["title"] == "From command"

    def test_events_key_wrapper_is_accepted(self, rooted, tmp_path):
        payload = json.dumps({"events": [
            {"start": "2026-09-08", "title": "Wrapped", "all_day": True}
        ]})
        script = self._script(tmp_path, f"print({payload!r})")
        result = agenda.agenda_for(
            DAY, config={"sources": [{"name": "Team", "kind": "command",
                                      "command": script}]}
        )
        assert result["events"][0]["title"] == "Wrapped"

    def test_date_placeholder_is_substituted(self, rooted, tmp_path):
        script = self._script(
            tmp_path,
            'import sys, json\n'
            'print(json.dumps([{"start": sys.argv[1], "title": "echoed"}]))\n',
        )
        result = agenda.agenda_for(
            DAY, config={"sources": [{"name": "Team", "kind": "command",
                                      "command": script + ["{date}"]}]}
        )
        assert result["events"][0]["start"] == "2026-09-08"

    def test_relative_program_is_refused_before_execution(self, rooted):
        result = agenda.agenda_for(
            DAY, config={"sources": [{"name": "Team", "kind": "command",
                                      "command": ["my-cal", "--json"]}]}
        )
        assert result["sources"][0]["status"] == "error"
        assert "absolute path" in result["sources"][0]["error"]

    def test_non_zero_exit_is_reported_not_raised(self, rooted, tmp_path):
        script = self._script(
            tmp_path,
            'import sys\nprint("auth token expired", file=sys.stderr)\nsys.exit(2)\n',
        )
        result = agenda.agenda_for(
            DAY, config={"sources": [{"name": "Team", "kind": "command",
                                      "command": script}]}
        )
        assert result["sources"][0]["status"] == "error"
        assert "exit 2" in result["sources"][0]["error"]
        assert "auth token expired" in result["sources"][0]["error"]

    def test_non_json_output_is_reported(self, rooted, tmp_path):
        script = self._script(tmp_path, 'print("not json")\n')
        result = agenda.agenda_for(
            DAY, config={"sources": [{"name": "Team", "kind": "command",
                                      "command": script}]}
        )
        assert result["sources"][0]["status"] == "error"
        assert "JSON" in result["sources"][0]["error"]


class TestConfigReading:
    def test_missing_file_yields_defaults(self, monkeypatch, tmp_path):
        monkeypatch.setattr(agenda, "_config_file", lambda: tmp_path / "nope.json")
        assert agenda.read_calendar_config() == agenda.DEFAULT_CALENDAR

    def test_invalid_json_yields_defaults(self, monkeypatch, tmp_path):
        path = tmp_path / "cfg.json"
        path.write_text("{not json")
        monkeypatch.setattr(agenda, "_config_file", lambda: path)
        assert agenda.read_calendar_config()["sources"] == []

    def test_calendar_section_is_merged_over_defaults(self, monkeypatch, tmp_path):
        path = tmp_path / "cfg.json"
        path.write_text(json.dumps({
            "jira_urls": {},
            "calendar": {"sources": [{"name": "W", "kind": "ics", "location": "/x.ics"}],
                         "timezone": "Asia/Jerusalem"},
        }))
        monkeypatch.setattr(agenda, "_config_file", lambda: path)
        config = agenda.read_calendar_config()
        assert config["timezone"] == "Asia/Jerusalem"
        assert config["cache_ttl_seconds"] == 900  # default survives
        assert len(config["sources"]) == 1

    def test_non_list_sources_is_normalized_away(self, monkeypatch, tmp_path):
        path = tmp_path / "cfg.json"
        path.write_text(json.dumps({"calendar": {"sources": "oops"}}))
        monkeypatch.setattr(agenda, "_config_file", lambda: path)
        assert agenda.read_calendar_config()["sources"] == []


class TestCache:
    def test_cache_lives_under_the_data_root(self, rooted, tmp_path):
        """Never a hardcoded ~/.missioncache - the root override must win."""
        assert str(agenda._cache_dir()).startswith(str(tmp_path / "root"))

    def test_file_sources_are_not_cached(self, rooted, tmp_path):
        path = tmp_path / "cal.ics"
        path.write_text(
            _ics(_vevent(UID="a", DTSTART="20260908T090000Z",
                         DTEND="20260908T093000Z", SUMMARY="First"))
        )
        config = {"sources": [{"name": "W", "kind": "ics", "location": str(path)}]}
        agenda.agenda_for(DAY, config=config)
        path.write_text(
            _ics(_vevent(UID="a", DTSTART="20260908T090000Z",
                         DTEND="20260908T093000Z", SUMMARY="Second"))
        )
        result = agenda.agenda_for(DAY, config=config)
        assert result["events"][0]["title"] == "Second"
        assert result["sources"][0]["cached"] is False

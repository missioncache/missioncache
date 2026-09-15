"""Calendar agenda for the MissionCache brief - read-only, stdlib-only.

Answers one question: "what is on the calendar for this day?". The brief
command and the dashboard both ask it, so it lives here in missioncache-db
rather than in the dashboard: the CLI and the MCP server import from this
package, never the reverse.

Two source kinds, both configured by hand under the ``calendar`` key of
``~/.claude/missioncache-dashboard-config.json``:

  * ``ics``     - a URL, a file path, or a glob. One parser serves all three.
  * ``command`` - an argv list that prints JSON events on stdout. This is the
                  escape hatch for wiring a tool MissionCache must not depend
                  on. It is an argv LIST and runs with ``shell=False``,
                  because this is config-file content executed by the
                  dashboard process - the same threat model as statusline
                  addons, and it gets the same rule: ``command[0]`` must be an
                  absolute path that exists.

This module only READS the config file. The dashboard's ``lib/config.py``
owns the single atomic write path to it, the way ``pm_items`` already splits
those responsibilities.

Nothing here raises. A dead URL, a timeout, garbage ICS, a command that exits
non-zero: each lands in its source's entry in the ``sources`` list with
``status="error"`` and a short reason. A calendar problem must never be why a
brief or a dashboard fails to render, so ``agenda_for`` wraps each source in a
broad except on purpose.

``configured: False`` (no enabled sources) is NOT an error and callers must
treat it differently: render nothing at all, rather than an empty or broken
calendar block.

TWO THINGS THAT SILENTLY BITE ICS CONSUMERS, both handled here:

  * ``DTEND`` on an all-day event is EXCLUSIVE. A one-day event ends the
    following midnight. We subtract a day so ``end`` means the last day the
    event actually covers, which is what every reader assumes it means.
  * A ``RECURRENCE-ID`` override is a single moved instance of a recurring
    series. Without pulling its date out of the master's occurrence set you
    show the meeting twice, at the old time and the new one.

RRULE is a single-day HIT TEST, not an expander. The only question ever asked
is "does this rule land on date D", and answering just that is what keeps this
module small. Supported: ``FREQ=DAILY|WEEKLY|MONTHLY``, ``INTERVAL``,
``COUNT``, ``UNTIL``, ``BYDAY`` (weekly), ``BYMONTHDAY`` (monthly), ``EXDATE``.
Deliberately out of scope: ``FREQ=YEARLY``, sub-daily frequencies, ordinal
weekdays (``MONTHLY;BYDAY=2TU``), ``BYSETPOS``, ``BYMONTH``, ``BYWEEKNO``,
``RDATE``, ``WKST``, ``VTIMEZONE``-defined zones, ``VALARM``, attendees.
An unsupported rule increments ``skipped_rules`` instead of vanishing - the
difference between "3 rules were not understood" and a standup that is just
mysteriously missing.

WINDOWS AND TIME ZONES: stdlib ``zoneinfo`` reads the system IANA database,
which Windows does not ship. Every ``TZID`` therefore raises
ZoneInfoNotFoundError there and falls back to the local zone, and the source
records the fallback. This package cannot fix that itself: its pyproject
carries a hard INVARIANT of no runtime dependencies, because the plugin hooks
import it under ``uv run --no-project``, which installs nothing. The
dashboard, which already has dependencies, carries ``tzdata`` for its own
process; a Windows user driving the CLI directly can ``pip install tzdata``
in the environment they run it from.
"""

import hashlib
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from glob import glob
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# The schema lives here, not in the dashboard, so there is exactly one owner.
# The dashboard's get_calendar_config() delegates to read_calendar_config().
DEFAULT_CALENDAR: dict[str, Any] = {
    "sources": [],
    "cache_ttl_seconds": 900,
    "timezone": None,
}

FETCH_TIMEOUT_SECONDS = 10
DEFAULT_COMMAND_TIMEOUT = 10
CACHE_DIR_NAME = "agenda-cache"

# A bounded walk is only used for COUNT, which is rare. The cap stops a
# malformed rule from spinning; a real recurring meeting never approaches it.
_OCCURRENCE_CAP = 5000

_SUPPORTED_FREQ = {"DAILY", "WEEKLY", "MONTHLY"}
_WEEKDAY = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
_DURATION_RE = re.compile(
    r"^[+-]?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$"
)
# 20MB. A year of a busy calendar is well under 1MB.
_MAX_FETCH_BYTES = 20 * 1024 * 1024

_GLOB_CHARS = ("*", "?", "[")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _config_file() -> Path:
    """The dashboard's config file, which missioncache-db reads but never writes.

    Same file and same read-only relationship as ``pm_items.jira_url_for``.
    """
    return Path.home() / ".claude" / "missioncache-dashboard-config.json"


def read_calendar_config() -> dict[str, Any]:
    """The ``calendar`` section merged over DEFAULT_CALENDAR. Never raises.

    A missing file, invalid JSON, or a ``calendar`` key holding something
    other than an object all yield the defaults, which means an empty
    ``sources`` list, which means ``configured: False`` downstream.
    """
    section: dict[str, Any] = {}
    try:
        with open(_config_file(), encoding="utf-8") as handle:
            raw = json.load(handle)
        if isinstance(raw, dict) and isinstance(raw.get("calendar"), dict):
            section = raw["calendar"]
    except (OSError, json.JSONDecodeError, ValueError):
        pass

    merged = dict(DEFAULT_CALENDAR)
    merged.update(section)

    sources = merged.get("sources")
    merged["sources"] = (
        [s for s in sources if isinstance(s, dict)] if isinstance(sources, list) else []
    )
    try:
        merged["cache_ttl_seconds"] = max(0, int(merged.get("cache_ttl_seconds") or 0))
    except (TypeError, ValueError):
        merged["cache_ttl_seconds"] = DEFAULT_CALENDAR["cache_ttl_seconds"]
    tz_name = merged.get("timezone")
    merged["timezone"] = tz_name if isinstance(tz_name, str) and tz_name else None
    return merged


def _resolve_zone(name: Optional[str]):
    """Named IANA zone, else this machine's local zone. Never raises."""
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            pass
    return datetime.now().astimezone().tzinfo or timezone.utc


def _zone_label(zone) -> str:
    return getattr(zone, "key", None) or str(zone)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def _cache_dir() -> Path:
    """Under the data root, never a hardcoded ~/.missioncache.

    Late import so a test that monkeypatches ``missioncache_db.MISSIONCACHE_ROOT``
    is honored: binding it at import time would snapshot the real home
    directory and quietly read the developer's own data.
    """
    import missioncache_db

    return missioncache_db.MISSIONCACHE_ROOT / CACHE_DIR_NAME


def _cache_path(source_key: str) -> Path:
    digest = hashlib.sha1(source_key.encode("utf-8")).hexdigest()
    return _cache_dir() / f"{digest}.json"


def _cache_read(path: Path) -> tuple[Optional[str], Optional[float]]:
    """Return ``(body, age_seconds)``, or ``(None, None)`` when unusable."""
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        body = payload.get("body")
        fetched_at = float(payload.get("fetched_at", 0))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None, None
    if not isinstance(body, str):
        return None, None
    return body, max(0.0, time.time() - fetched_at)


def _cache_write(path: Path, body: str) -> None:
    import missioncache_db

    missioncache_db.atomic_write_json(path, {"fetched_at": time.time(), "body": body})


# ---------------------------------------------------------------------------
# ICS lexing
# ---------------------------------------------------------------------------


def _unfold(text: str) -> str:
    """Undo RFC 5545 line folding: a newline followed by one space or tab."""
    return re.sub(r"\n[ \t]", "", text.replace("\r\n", "\n").replace("\r", "\n"))


def _split_property(line: str):
    """``NAME;PARAM=v:value`` -> ``(NAME, {PARAM: v}, value)``, or None.

    The split is on the first colon OUTSIDE double quotes, because a quoted
    parameter value may legally contain one (``TZID="GMT+02:00"``).
    """
    in_quotes = False
    cut = -1
    for index, char in enumerate(line):
        if char == '"':
            in_quotes = not in_quotes
        elif char == ":" and not in_quotes:
            cut = index
            break
    if cut < 0:
        return None
    head, value = line[:cut], line[cut + 1 :]
    pieces = head.split(";")
    name = pieces[0].strip().upper()
    params: dict[str, str] = {}
    for piece in pieces[1:]:
        if "=" in piece:
            key, val = piece.split("=", 1)
            params[key.strip().upper()] = val.strip().strip('"')
    return name, params, value


def _unescape(value: str) -> str:
    r"""RFC 5545 TEXT unescaping: ``\n`` ``\N`` ``\,`` ``\;`` ``\\``."""
    out: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            nxt = value[index + 1]
            out.append("\n" if nxt in ("n", "N") else nxt)
            index += 2
        else:
            out.append(char)
            index += 1
    return "".join(out)


def _parse_duration(value: str) -> Optional[timedelta]:
    match = _DURATION_RE.match(value.strip())
    if not match:
        return None
    weeks, days, hours, minutes, seconds = (int(g or 0) for g in match.groups())
    delta = timedelta(
        weeks=weeks, days=days, hours=hours, minutes=minutes, seconds=seconds
    )
    return -delta if value.strip().startswith("-") else delta


def _parse_datetime(value: str, params: dict[str, str], zone):
    """-> ``(value, is_date, tz_fallback_name)``.

    ``is_date`` marks an all-day value. ``tz_fallback_name`` is the TZID we
    could not resolve, so the caller can report it rather than swallow it -
    on Windows this fires for every TZID and the user needs to know why the
    times look wrong.
    """
    raw = value.strip()
    if not raw:
        return None, False, None
    if params.get("VALUE", "").upper() == "DATE" or (
        len(raw) == 8 and "T" not in raw
    ):
        try:
            return datetime.strptime(raw, "%Y%m%d").date(), True, None
        except ValueError:
            return None, False, None
    fmt = "%Y%m%dT%H%M%SZ" if raw.endswith("Z") else "%Y%m%dT%H%M%S"
    try:
        parsed = datetime.strptime(raw, fmt)
    except ValueError:
        return None, False, None
    if raw.endswith("Z"):
        return parsed.replace(tzinfo=timezone.utc), False, None
    tzid = params.get("TZID")
    fallback = None
    target = zone
    if tzid:
        try:
            target = ZoneInfo(tzid)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            fallback = tzid
    return parsed.replace(tzinfo=target), False, fallback


def _as_date(value) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else None


# ---------------------------------------------------------------------------
# RRULE: does this rule land on `day`?
# ---------------------------------------------------------------------------


def _parse_rule(value: str) -> dict[str, str]:
    rule: dict[str, str] = {}
    for part in value.split(";"):
        if "=" in part:
            key, val = part.split("=", 1)
            rule[key.strip().upper()] = val.strip()
    return rule


def _byday_weekdays(raw: str) -> Optional[set[int]]:
    """``MO,WE,FR`` -> weekday numbers. None when an ordinal is present.

    ``2TU`` (second Tuesday) is out of scope, and returning None is how the
    caller learns to count the rule as not understood rather than silently
    treating it as every Tuesday.
    """
    days: set[int] = set()
    for token in raw.split(","):
        token = token.strip().upper()
        if token not in _WEEKDAY:
            return None
        days.add(_WEEKDAY[token])
    return days or None


def _parse_until(raw: Optional[str]) -> Optional[date]:
    if not raw:
        return None
    text = raw.strip().rstrip("Z")
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _occurrence_index(start: date, rule_freq: str, interval: int,
                      weekdays: Optional[set[int]], monthdays: Optional[set[int]],
                      target: date) -> Optional[int]:
    """0-based index of ``target`` in the series, for COUNT only.

    Bounded walk. COUNT is rare enough that a closed form for every branch
    would be more code than it saves, and the cap keeps a malformed rule from
    spinning forever.
    """
    seen = 0
    if rule_freq == "DAILY":
        delta = (target - start).days
        return delta // interval if delta >= 0 else None
    if rule_freq == "WEEKLY":
        week_start = start - timedelta(days=start.weekday())
        cursor = week_start
        guard = 0
        while cursor <= target and guard < _OCCURRENCE_CAP:
            guard += 1
            for offset in range(7):
                current = cursor + timedelta(days=offset)
                if current < start or current > target:
                    continue
                if current.weekday() in (weekdays or {start.weekday()}):
                    if current == target:
                        return seen
                    seen += 1
            cursor += timedelta(weeks=interval)
        return None
    if rule_freq == "MONTHLY":
        year, month = start.year, start.month
        guard = 0
        while guard < _OCCURRENCE_CAP:
            guard += 1
            if (year, month) > (target.year, target.month):
                break
            for dom in sorted(monthdays or {start.day}):
                try:
                    current = date(year, month, dom)
                except ValueError:
                    continue
                if current < start or current > target:
                    continue
                if current == target:
                    return seen
                seen += 1
            month += interval
            while month > 12:
                month -= 12
                year += 1
        return None
    return None


def _rrule_hits(start: date, rule: dict[str, str], day: date) -> Optional[bool]:
    """True/False when understood, None when the rule uses something we skip."""
    freq = rule.get("FREQ", "").upper()
    if freq not in _SUPPORTED_FREQ:
        return None
    if day < start:
        return False

    try:
        interval = max(1, int(rule.get("INTERVAL", "1") or 1))
    except ValueError:
        interval = 1

    until = _parse_until(rule.get("UNTIL"))
    if until and day > until:
        return False

    weekdays: Optional[set[int]] = None
    monthdays: Optional[set[int]] = None

    if freq == "DAILY":
        if (day - start).days % interval:
            return False
    elif freq == "WEEKLY":
        if "BYDAY" in rule:
            weekdays = _byday_weekdays(rule["BYDAY"])
            if weekdays is None:
                return None
        else:
            weekdays = {start.weekday()}
        if day.weekday() not in weekdays:
            return False
        week_start = start - timedelta(days=start.weekday())
        if ((day - week_start).days // 7) % interval:
            return False
    else:  # MONTHLY
        if "BYDAY" in rule:
            # Ordinal weekday ("second Tuesday") is out of scope, and a plain
            # BYDAY under MONTHLY means every such weekday in the month, which
            # is a different shape than BYMONTHDAY. Skip rather than guess.
            return None
        if "BYMONTHDAY" in rule:
            try:
                monthdays = {int(x) for x in rule["BYMONTHDAY"].split(",")}
            except ValueError:
                return None
            # A negative BYMONTHDAY counts back from the end of the month
            # (-1 is the last day). No calendar day number is negative, so
            # without this the rule matches nothing and reports nothing,
            # which is the silent disappearance the module promises not to do.
            if any(d < 1 for d in monthdays):
                return None
        else:
            monthdays = {start.day}
        if day.day not in monthdays:
            return False
        months = (day.year - start.year) * 12 + (day.month - start.month)
        if months % interval:
            return False

    raw_count = rule.get("COUNT")
    if raw_count and raw_count.isdigit():
        index = _occurrence_index(
            start, freq, interval, weekdays, monthdays, day
        )
        if index is None or index >= int(raw_count):
            return False
    return True


# ---------------------------------------------------------------------------
# VEVENT -> normalized events for one day
# ---------------------------------------------------------------------------


def _covers(raw: dict, start_date: date, day: date) -> bool:
    """Does this non-recurring block cover ``day``?

    True on the start day always. Beyond that, a block with a DTEND later than
    its DTSTART is a span: an all-day DTEND is exclusive (so the last covered
    day is DTEND minus one), a timed one is inclusive of the day it falls on.
    Recurring blocks never reach here - an occurrence is placed by its rule.
    """
    if start_date == day:
        return True
    end_date = _as_date(raw.get("end"))
    if end_date is None or day < start_date:
        return False
    if raw.get("all_day", False):
        end_date -= timedelta(days=1)
    return day <= end_date


def _emit(raw: dict, day: date, calendar: str, zone) -> Optional[dict]:
    """Build the normalized event for ``day``, shifting a recurrence forward."""
    start_value = raw.get("start")
    if start_value is None:
        return None
    all_day = raw.get("all_day", False)
    start_date = _as_date(start_value)
    if start_date is None:
        return None

    shift = day - start_date
    end_value = raw.get("end")

    def _local(value):
        """Into the agenda's own zone. A feed states 09:00Z or 09:00 Berlin;
        the reader wants the wall-clock time on THEIR wall, and the API labels
        the whole agenda with that one zone. Without this the hour is printed
        as the feed wrote it and an evening event lands on the wrong day."""
        if zone is None or not isinstance(value, datetime) or value.tzinfo is None:
            return value
        try:
            return value.astimezone(zone)
        except (ValueError, OSError):
            return value

    if all_day:
        start_out = day.isoformat()
        end_date = _as_date(end_value)
        if end_date is not None:
            # DTEND is EXCLUSIVE for all-day events. Subtract a day so `end`
            # is the last day the event actually covers.
            end_out = (end_date + shift - timedelta(days=1)).isoformat()
            if end_out < start_out:
                end_out = start_out
        else:
            end_out = start_out
    else:
        start_dt = _local(start_value + shift)
        start_out = start_dt.isoformat()
        if isinstance(end_value, datetime):
            end_out = _local(end_value + shift).isoformat()
        elif isinstance(raw.get("duration"), timedelta):
            end_out = (start_dt + raw["duration"]).isoformat()
        else:
            end_out = start_out

    return {
        "start": start_out,
        "end": end_out,
        "all_day": all_day,
        "title": raw.get("title") or "(no title)",
        "location": raw.get("location") or "",
        "calendar": calendar,
    }


def looks_like_ics(text: str) -> bool:
    """Is this body actually an iCalendar feed?

    An expired share link or a login-wall answers 200 with HTML. That yields
    zero VEVENT blocks and would report a healthy, empty day, which is the one
    thing a calendar must never do: "no meetings" and "I could not read your
    calendar" are different statements.
    """
    return "BEGIN:VCALENDAR" in (text or "").upper()


def parse_ics(text: str, day: date, *, calendar: str, tz) -> tuple[list[dict], dict]:
    """Events from ``text`` that fall on ``day``.

    Returns ``(events, notes)`` where notes carries ``skipped_rules`` (rules
    using something out of scope) and ``tz_fallbacks`` (TZIDs this machine
    could not resolve). Both are reported rather than swallowed: a silent gap
    reads as "nothing scheduled", which is the wrong answer.

    Never raises. Malformed content yields fewer events, not an exception.
    """
    events: list[dict] = []
    skipped = 0
    fallbacks: set[str] = set()

    # A RECURRENCE-ID override replaces one instance of its series. Collect
    # the overridden dates per UID first, then drop those dates from the
    # master's occurrences, or the meeting renders twice.
    overridden: dict[str, set[date]] = {}
    blocks: list[dict] = []

    current: Optional[dict] = None
    nested = 0
    for line in _unfold(text).split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        upper = stripped.upper()
        if upper == "BEGIN:VEVENT":
            current = {"exdates": set(), "rule": None}
            nested = 0
            continue
        if upper == "END:VEVENT":
            if current is not None:
                blocks.append(current)
            current = None
            nested = 0
            continue
        if current is None:
            continue
        # A VEVENT can contain a VALARM, and a VALARM carries SUMMARY (email
        # reminders) and DURATION (repeating ones). Read flat, those overwrite
        # the meeting's own title and length: a real calendar renders as
        # "Reminder: <title>" running five minutes. Only depth 0 is the event.
        if upper.startswith("BEGIN:"):
            nested += 1
            continue
        if upper.startswith("END:"):
            nested = max(0, nested - 1)
            continue
        if nested:
            continue

        parsed = _split_property(stripped)
        if parsed is None:
            continue
        name, params, value = parsed

        if name == "DTSTART":
            when, is_date, fallback = _parse_datetime(value, params, tz)
            current["start"] = when
            current["all_day"] = is_date
            if fallback:
                fallbacks.add(fallback)
        elif name == "DTEND":
            when, _, fallback = _parse_datetime(value, params, tz)
            current["end"] = when
            if fallback:
                fallbacks.add(fallback)
        elif name == "DURATION":
            current["duration"] = _parse_duration(value)
        elif name == "SUMMARY":
            current["title"] = _unescape(value).strip()
        elif name == "LOCATION":
            current["location"] = _unescape(value).strip()
        elif name == "UID":
            current["uid"] = value.strip()
        elif name == "STATUS":
            current["status"] = value.strip().upper()
        elif name == "RRULE":
            current["rule"] = _parse_rule(value)
        elif name == "EXDATE":
            for piece in value.split(","):
                when, _, _fb = _parse_datetime(piece, params, tz)
                as_date = _as_date(when)
                if as_date:
                    current["exdates"].add(as_date)
        elif name == "RECURRENCE-ID":
            when, _, _fb = _parse_datetime(value, params, tz)
            current["recurrence_id"] = _as_date(when)

    for block in blocks:
        rid = block.get("recurrence_id")
        uid = block.get("uid")
        if rid and uid:
            overridden.setdefault(uid, set()).add(rid)

    for block in blocks:
        if block.get("status") == "CANCELLED":
            continue
        start_date = _as_date(block.get("start"))
        if start_date is None:
            continue

        rule = block.get("rule")
        if not rule or block.get("recurrence_id"):
            # A single event, or one moved instance of a series. Either way it
            # is on the day its own DTSTART says - or on any day its span
            # covers, since a week of PTO is on the calendar all week and not
            # only on its first morning.
            if _covers(block, start_date, day):
                built = _emit(block, day, calendar, tz)
                if built:
                    events.append(built)
            continue

        excluded = set(block.get("exdates") or ())
        excluded |= overridden.get(block.get("uid", ""), set())
        if day in excluded:
            continue

        verdict = _rrule_hits(start_date, rule, day)
        if verdict is None:
            skipped += 1
            continue
        if verdict:
            built = _emit(block, day, calendar, tz)
            if built:
                events.append(built)

    return events, {"skipped_rules": skipped, "tz_fallbacks": sorted(fallbacks)}


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


def _read_ics_location(location: str, timeout: int) -> tuple[str, bool]:
    """-> ``(text, cacheable)``. Raises on failure; the caller reports it.

    Only a network fetch is cacheable. A file or a glob is already backed by
    the filesystem, so caching it would add staleness for nothing.
    """
    target = location.strip()
    if target.lower().startswith(("http://", "https://")):
        request = urllib.request.Request(
            target, headers={"User-Agent": "missioncache-agenda"}
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            # Bounded read. `response.read()` with no argument will happily
            # pull a multi-gigabyte body into memory on a threadpool thread,
            # and no real calendar is anywhere near this size.
            raw = response.read(_MAX_FETCH_BYTES + 1)
        if len(raw) > _MAX_FETCH_BYTES:
            raise ValueError(f"feed larger than {_MAX_FETCH_BYTES // (1024 * 1024)}MB")
        text = raw.decode("utf-8", errors="replace")
        if not looks_like_ics(text):
            raise ValueError("response is not an ICS feed (no BEGIN:VCALENDAR)")
        return text, True

    expanded = os.path.expanduser(target)
    if any(char in expanded for char in _GLOB_CHARS):
        matches = sorted(glob(expanded))
        if not matches:
            raise FileNotFoundError("no files matched")
        chunks = []
        for match in matches:
            with open(match, encoding="utf-8", errors="replace") as handle:
                chunks.append(handle.read())
        joined = "\n".join(chunks)
        if not looks_like_ics(joined):
            raise ValueError("no matched file is an ICS feed (no BEGIN:VCALENDAR)")
        return joined, False

    with open(expanded, encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    if not looks_like_ics(text):
        raise ValueError("not an ICS feed (no BEGIN:VCALENDAR)")
    return text, False


def _run_source_command(argv: list, day: date, timeout: int) -> str:
    """Run a configured command and return its stdout. Raises on failure.

    ``shell=False`` and an absolute-path check on argv[0]: this is
    config-file content being executed, and it gets the same guard the
    statusline addons get.
    """
    if not isinstance(argv, list) or not argv or not all(
        isinstance(part, str) for part in argv
    ):
        raise ValueError("command must be a non-empty list of strings")
    # Substitute FIRST, then validate what will actually run. Validating
    # argv[0] before substitution checks a string that is never executed:
    # "/tmp/tool-{date}" passes when a file of that literal name exists, and
    # then a different file runs.
    stamp = day.isoformat()
    resolved = [part.replace("{date}", stamp) for part in argv]
    program = resolved[0]
    if not os.path.isabs(program):
        raise ValueError(f"command must be an absolute path, got {program!r}")
    if not os.path.exists(program):
        raise FileNotFoundError(f"command not found: {program}")

    completed = subprocess.run(
        resolved,
        capture_output=True,
        text=True,
        timeout=timeout,
        shell=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip().replace("\n", " ")[:200]
        raise RuntimeError(f"exit {completed.returncode}: {detail}" if detail
                           else f"exit {completed.returncode}")
    return completed.stdout


def _events_from_json(text: str, calendar: str) -> list[dict]:
    """Normalize a command's JSON output. Accepts a list or ``{"events": [...]}``."""
    payload = json.loads(text)
    if isinstance(payload, dict):
        payload = payload.get("events", [])
    if not isinstance(payload, list):
        raise ValueError("output was not a list of events")
    events = []
    for item in payload:
        if not isinstance(item, dict) or not item.get("start"):
            continue
        start = str(item["start"])
        events.append(
            {
                "start": start,
                "end": str(item.get("end") or start),
                "all_day": bool(item.get("all_day", len(start) == 10)),
                "title": str(item.get("title") or "(no title)"),
                "location": str(item.get("location") or ""),
                "calendar": str(item.get("calendar") or calendar),
            }
        )
    return events


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _sort_key(event: dict) -> tuple:
    # All-day first, then by start, then title. An all-day event is context
    # for the whole day and belongs above the timed ones.
    return (0 if event.get("all_day") else 1, event.get("start", ""), event.get("title", ""))


def agenda_for(
    day: Optional[date] = None,
    *,
    config: Optional[dict] = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Events on ``day`` across every enabled source. Never raises.

    ``configured`` is False when nothing is set up, and callers must render
    nothing at all in that case rather than an empty calendar - "no calendar
    configured" and "no meetings today" are different statements.
    """
    target_day = day or date.today()
    settings = config if config is not None else read_calendar_config()
    zone = _resolve_zone(settings.get("timezone"))
    ttl = settings.get("cache_ttl_seconds", DEFAULT_CALENDAR["cache_ttl_seconds"])

    enabled = [s for s in settings.get("sources", []) if s.get("enabled", True)]
    result: dict[str, Any] = {
        "date": target_day.isoformat(),
        "timezone": _zone_label(zone),
        "configured": bool(enabled),
        "events": [],
        "sources": [],
    }
    if not enabled:
        return result

    for index, source in enumerate(enabled):
        name = str(source.get("name") or f"source-{index + 1}")
        kind = str(source.get("kind") or "ics").lower()
        entry = {
            "name": name,
            "kind": kind,
            "status": "ok",
            "count": 0,
            "cached": False,
            "error": None,
            "skipped_rules": 0,
        }
        try:
            events, entry = _load_source(source, name, kind, target_day, zone, ttl,
                                         use_cache, entry)
            result["events"].extend(events)
            entry["count"] = len(events)
        except Exception as exc:  # noqa: BLE001 - a calendar must never break a brief
            entry["status"] = "error"
            entry["error"] = _short_reason(exc)
        result["sources"].append(entry)

    result["events"].sort(key=_sort_key)
    return result


def _short_reason(exc: BaseException) -> str:
    """A one-line reason a person can act on, not a traceback."""
    if isinstance(exc, subprocess.TimeoutExpired):
        return "command timed out"
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return f"could not reach: {exc.reason}"
    if isinstance(exc, json.JSONDecodeError):
        return "output was not JSON"
    text = str(exc).strip().replace("\n", " ")
    return text[:200] or exc.__class__.__name__


def _load_source(source, name, kind, target_day, zone, ttl, use_cache, entry):
    """One source's events plus its filled-in status entry."""
    if kind == "command":
        timeout = int(source.get("timeout_seconds") or DEFAULT_COMMAND_TIMEOUT)
        argv = source.get("command")
        key = f"command:{json.dumps(argv, sort_keys=True)}:{target_day.isoformat()}"
        body, cached = _body_with_cache(
            lambda: (_run_source_command(argv, target_day, timeout), True),
            key, ttl, use_cache, entry,
        )
        entry["cached"] = cached
        return _events_from_json(body, name), entry

    location = str(source.get("location") or "").strip()
    if not location:
        raise ValueError("source has no location")
    timeout = int(source.get("timeout_seconds") or FETCH_TIMEOUT_SECONDS)
    key = f"ics:{location}"
    body, cached = _body_with_cache(
        lambda: _read_ics_location(location, timeout), key, ttl, use_cache, entry
    )
    entry["cached"] = cached
    events, notes = parse_ics(body, target_day, calendar=name, tz=zone)
    entry["skipped_rules"] = notes["skipped_rules"]
    if notes["tz_fallbacks"]:
        warning = (
            "unknown time zone "
            + ", ".join(notes["tz_fallbacks"])
            + ", used "
            + _zone_label(zone)
        )
        # Append, never assign: the stale path already put the cache age in
        # here and that reason is not less important than this one. And mark
        # the source non-ok, because the dashboard decides what to warn about
        # from `status` alone - an "ok" source with times in the wrong zone
        # tells the reader nothing at all.
        entry["error"] = f"{entry['error']}; {warning}" if entry.get("error") else warning
        if entry.get("status") == "ok":
            entry["status"] = "warn"
    return events, entry


def _body_with_cache(produce, key, ttl, use_cache, entry):
    """-> ``(body, came_from_cache)``.

    Serving a stale body when a fetch fails is deliberate: a calendar from 20
    minutes ago beats no calendar, and the source is marked ``stale`` so the
    reader knows which one they are looking at.
    """
    path = _cache_path(key)
    if use_cache and ttl > 0:
        cached_body, age = _cache_read(path)
        if cached_body is not None and age is not None and age < ttl:
            return cached_body, True

    try:
        body, cacheable = produce()
    except Exception:
        if use_cache:
            stale_body, age = _cache_read(path)
            if stale_body is not None:
                entry["status"] = "stale"
                entry["cached"] = True
                entry["error"] = f"refresh failed, showing a copy {int((age or 0) // 60)}m old"
                return stale_body, True
        raise

    if cacheable and use_cache and ttl > 0:
        _cache_write(path, body)
    return body, False

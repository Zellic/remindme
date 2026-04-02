"""
Port of modules/auditreminders/time.kt — ConvenientDateTimeParser.

Parses strings like:
  "5pm EDT tomorrow"
  "tomorrow, 5:00 pm, EDT"
  "EDT 17:00 friday"
  "monday 5 am"
  "17:00 PST"
"""

import re
from dataclasses import dataclass
from datetime import date, time, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Mirrors idToZoneIdMap in commands/superping/time.kt
ID_TO_ZONE: dict[str, str] = {
    "EST":  "America/New_York",
    "EDT":  "America/New_York",
    "CST":  "America/Chicago",
    "CDT":  "America/Chicago",
    "MST":  "America/Denver",
    "MDT":  "America/Denver",
    "PST":  "America/Los_Angeles",
    "PDT":  "America/Los_Angeles",
    "AST":  "America/Halifax",
    "ADT":  "America/Halifax",
    "NST":  "America/St_Johns",
    "NDT":  "America/St_Johns",
    "GMT":  "GMT",
    "UTC":  "UTC",
    "BST":  "Europe/London",
    "CET":  "Europe/Paris",
    "CEST": "Europe/Paris",
    "EET":  "Europe/Helsinki",
    "EEST": "Europe/Helsinki",
    "WET":  "Europe/Lisbon",
    "WEST": "Europe/Lisbon",
}

_DAYS_OF_WEEK  = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_DAY_HELPERS   = ["today", "tomorrow", "yesterday"]
_ALL_VALID_DAYS = set(_DAYS_OF_WEEK + _DAY_HELPERS)

_TIME_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.IGNORECASE)


@dataclass
class ParseSuccess:
    parsed_time: time
    zone_id: Optional[ZoneInfo]
    parsed_date: date

    def to_datetime(self, fallback_tz: ZoneInfo) -> datetime:
        tz = self.zone_id or fallback_tz
        return datetime.combine(self.parsed_date, self.parsed_time, tzinfo=tz)


@dataclass
class ParseError:
    message: str


ParseResult = ParseSuccess | ParseError


def _get_zone(name: str) -> Optional[ZoneInfo]:
    upper = name.strip().upper()
    iana = ID_TO_ZONE.get(upper)
    if iana:
        return ZoneInfo(iana)
    # Lenient fallback: try exact IANA name (case-insensitive)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, KeyError):
        return None


def _parse_time(match: re.Match) -> Optional[time]:
    hour_s, minute_s, ampm = match.group(1), match.group(2), match.group(3)

    # Require either minutes or am/pm to avoid matching bare integers
    if not minute_s and not ampm:
        return None

    hour = int(hour_s)
    ampm_lower = (ampm or "").lower()

    if ampm_lower == "pm" and hour != 12:
        hour += 12
    elif ampm_lower == "am" and hour == 12:
        hour = 0

    if not (0 <= hour <= 23):
        return None

    minute = int(minute_s) if minute_s else 0
    if not (0 <= minute <= 59):
        return None

    return time(hour, minute)


def _parse_date(word: str) -> date:
    today = date.today()
    lower = word.lower()
    if lower == "today":
        return today
    if lower == "tomorrow":
        return today + timedelta(days=1)
    if lower == "yesterday":
        return today - timedelta(days=1)
    if lower in _DAYS_OF_WEEK:
        target = _DAYS_OF_WEEK.index(lower)  # 0=Monday
        days_ahead = (target - today.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7  # next occurrence
        return today + timedelta(days=days_ahead)
    return today


def parse(input_str: str) -> ParseResult:
    trimmed = input_str.strip()
    if not trimmed:
        return ParseError("Input is empty.")

    time_matches = list(_TIME_RE.finditer(trimmed))
    # Filter to only matches that have minutes or am/pm
    valid_time_matches = [m for m in time_matches if m.group(2) or m.group(3)]

    if len(valid_time_matches) > 1:
        return ParseError(f"Specified multiple times: {', '.join(m.group(0) for m in valid_time_matches)}")
    if len(valid_time_matches) == 0:
        return ParseError("Missing time component.")

    tm_match = valid_time_matches[0]
    parsed_time = _parse_time(tm_match)
    if parsed_time is None:
        return ParseError(f"Invalid time format: {tm_match.group(0)!r}")

    remaining = trimmed[: tm_match.start()] + trimmed[tm_match.end() :]

    tokens = [t for t in re.split(r"[\s,]+", remaining) if t]

    # Extract timezone token
    tz_tokens = [(i, t) for i, t in enumerate(tokens) if t.upper() in ID_TO_ZONE]
    if len(tz_tokens) > 1:
        return ParseError(f"Specified multiple timezones: {', '.join(t for _, t in tz_tokens)}")

    zone_id: Optional[ZoneInfo] = None
    if tz_tokens:
        idx, tz_word = tz_tokens[0]
        zone_id = _get_zone(tz_word)
        tokens.pop(idx)

    # Extract date token — re-scan after timezone removal so indices are fresh
    date_tokens = [(i, t) for i, t in enumerate(tokens) if t.lower() in _ALL_VALID_DAYS]
    if len(date_tokens) > 1:
        return ParseError(f"Specified multiple dates: {', '.join(t for _, t in date_tokens)}")

    parsed_date = date.today()
    if date_tokens:
        idx, date_word = date_tokens[0]
        parsed_date = _parse_date(date_word)
        tokens.pop(idx)

    if tokens:
        return ParseError(f"Unexpected components: {', '.join(tokens)}")

    return ParseSuccess(parsed_time=parsed_time, zone_id=zone_id, parsed_date=parsed_date)

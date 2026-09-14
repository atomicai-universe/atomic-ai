"""Pure spoken date/time parsing for the voice accessibility feature.

This module turns a *spoken* time phrase ("tomorrow at 3pm", "in 2 hours",
"next monday at 9am", "tonight") into an ISO-8601 string, using ONLY the
standard library (:mod:`datetime`, :mod:`re`). It is deliberately **pure**: no
I/O, no DB, no AWS, and — critically — no implicit "now". All relative parsing
is anchored to an injected ``now`` so the result is deterministic and
unit-testable.

Contract:

- :func:`parse_spoken_datetime(text, *, now)` returns an ISO-8601 string
  (``datetime.isoformat()``) when it can confidently resolve a time, else
  ``None``. A ``None`` return is the signal for the caller to simply open the
  scheduler picker rather than guess.
- The returned datetime carries whatever tzinfo ``now`` carries (naive stays
  naive, aware stays aware in ``now``'s tzinfo), so the caller controls the
  timezone by controlling ``now``.

Supported (at least):
  "tomorrow at 3pm", "today at 9", "at 3:30pm", "in 2 hours",
  "next monday at 9am", "tonight", "this afternoon", "this morning",
  "this evening", plain "3pm" / "3:30 pm".

Design notes — why conservative:
  Scheduling is a high-impact action. When a phrase is ambiguous or unparseable
  we prefer ``None`` (open the picker) over a wrong guess. Bare clock times with
  no explicit day are resolved to the NEXT occurrence (today if still in the
  future, otherwise tomorrow) so "3pm" never schedules into the past.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

__all__ = ["parse_spoken_datetime"]


_WEEKDAYS: dict[str, int] = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

#: Named parts of day -> (hour, minute). Used for "tonight", "this afternoon",
#: etc. — a spoken phrase that implies a time without a clock reading.
_DAYPARTS: dict[str, tuple[int, int]] = {
    "morning": (9, 0),
    "afternoon": (15, 0),
    "evening": (18, 0),
    "night": (20, 0),
    "tonight": (20, 0),
    "noon": (12, 0),
    "midnight": (0, 0),
}

#: Month names -> month number, for absolute dates like "september fourteenth".
_MONTHS: dict[str, int] = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
    # common spoken/abbreviated forms
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

#: Ordinal / cardinal day words -> day-of-month, for "the fourteenth",
#: "september fifteenth", "the third". Covers 1..31.
_DAY_ORDINALS: dict[str, int] = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
    "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11,
    "twelfth": 12, "thirteenth": 13, "fourteenth": 14, "fifteenth": 15,
    "sixteenth": 16, "seventeenth": 17, "eighteenth": 18, "nineteenth": 19,
    "twentieth": 20, "twenty first": 21, "twenty second": 22,
    "twenty third": 23, "twenty fourth": 24, "twenty fifth": 25,
    "twenty sixth": 26, "twenty seventh": 27, "twenty eighth": 28,
    "twenty ninth": 29, "thirtieth": 30, "thirty first": 31,
}

_PUNCT_RE = re.compile(r"[^a-z0-9:\s]+")

# A clock reading: "3", "3pm", "3:30", "3:30 pm", "15:00". Captures hour,
# optional minute, optional am/pm.
_CLOCK_RE = re.compile(
    r"\b(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)?\b"
)

#: Spoken hour words -> digit, so "five pm" / "at nine" resolve. Only 1..12 (a
#: clock hour); larger cardinals are day-of-month territory handled elsewhere.
_HOUR_WORDS: dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "noon": 12, "midnight": 0,
}

#: "five pm" / "nine am" / "at five" — a spoken-word hour with optional am/pm.
_WORD_CLOCK_RE = re.compile(
    r"\b(?P<word>one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b"
    r"\s*(?P<ampm>am|pm|o'?clock)?"
)

# "in N hours" / "in N minutes" / "in an hour".
_IN_DURATION_RE = re.compile(
    r"\bin\s+(?P<num>\d+|an?|a)\s+(?P<unit>hours?|minutes?|mins?|days?)\b"
)


def _normalize(text: str) -> str:
    """Lowercase, strip punctuation (keep ``:``), collapse whitespace (pure)."""
    lowered = _PUNCT_RE.sub(" ", text.lower())
    return " ".join(lowered.split())


def _apply_clock(base: datetime, hour: int, minute: int) -> datetime:
    """Return ``base`` with the time-of-day set to ``hour:minute`` (pure)."""
    return base.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _resolve_clock(match: re.Match[str]) -> tuple[int, int] | None:
    """Turn a clock regex match into a 24h ``(hour, minute)`` or ``None``."""
    try:
        hour = int(match.group("hour"))
    except (TypeError, ValueError):
        return None
    minute_raw = match.group("minute")
    minute = int(minute_raw) if minute_raw is not None else 0
    ampm = match.group("ampm")

    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def _next_weekday(now: datetime, weekday: int, *, force_next_week: bool) -> datetime:
    """Date of the given weekday on/after ``now`` (pure, date only).

    When ``force_next_week`` (the user said "next monday"), always land on a day
    strictly in the future — at least the coming occurrence, and if today is
    that weekday, the following week's.
    """
    days_ahead = (weekday - now.weekday()) % 7
    if force_next_week and days_ahead == 0:
        days_ahead = 7
    return now + timedelta(days=days_ahead)


def _parse_month_day(norm: str, now: datetime) -> datetime | None:
    """Resolve an absolute month+day date ("september fourteenth", "sep 14",
    "the 14th of september", "14 september") to a datetime at ``now``'s time,
    or ``None`` when no month/day is present.

    The YEAR is inferred: the current year, rolled to NEXT year if that date has
    already passed relative to ``now`` (so "september fourteenth" said in
    October schedules next year, never the past). Day-of-month is read from a
    numeric token, a "14th"-style token, or an ordinal word ("fourteenth",
    "twenty first"). Pure.
    """
    month: int | None = None
    for name, num in _MONTHS.items():
        if re.search(rf"\b{name}\b", norm):
            month = num
            break
    if month is None:
        return None

    day: int | None = None
    # Numeric day: "14", "14th", "3rd".
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\b", norm)
    if m is not None:
        val = int(m.group(1))
        if 1 <= val <= 31:
            day = val
    # Ordinal words: check the two-word forms first ("twenty first").
    if day is None:
        for word, num in sorted(
            _DAY_ORDINALS.items(), key=lambda kv: -len(kv[0])
        ):
            if re.search(rf"\b{word}\b", norm):
                day = num
                break
    if day is None:
        return None

    year = now.year
    try:
        candidate = now.replace(month=month, day=day)
    except ValueError:
        return None  # invalid day for the month
    # If that date already passed this year, roll to next year.
    if candidate.date() < now.date():
        try:
            candidate = candidate.replace(year=year + 1)
        except ValueError:
            return None
    return candidate


def parse_spoken_datetime(text: str, *, now: datetime) -> str | None:
    """Parse a spoken time phrase into an ISO-8601 string, anchored to ``now``.

    Returns ``None`` when nothing parseable is found (caller then opens the
    picker). All relative resolution is anchored to ``now`` so the function is
    pure and deterministic. See the module docstring for supported phrasings.
    """
    if not isinstance(text, str):
        return None
    norm = _normalize(text)
    if not norm:
        return None

    # 1. Relative duration: "in 2 hours", "in 30 minutes", "in a day".
    dur = _IN_DURATION_RE.search(norm)
    if dur is not None:
        num_raw = dur.group("num")
        num = 1 if num_raw in ("a", "an") else int(num_raw)
        unit = dur.group("unit")
        if unit.startswith("hour"):
            delta = timedelta(hours=num)
        elif unit.startswith("day"):
            delta = timedelta(days=num)
        else:  # minutes / mins
            delta = timedelta(minutes=num)
        return (now + delta).replace(microsecond=0).isoformat()

    # 2. Establish the target DAY from day words, and whether a day was named.
    base_day = now
    day_specified = False

    month_day = _parse_month_day(norm, now)
    if "tomorrow" in norm:
        base_day = now + timedelta(days=1)
        day_specified = True
    elif "today" in norm or "tonight" in norm or "this " in norm:
        base_day = now
        day_specified = True
    elif month_day is not None:
        # Absolute calendar date: "september fourteenth", "sep 14".
        base_day = month_day
        day_specified = True
    else:
        # Weekday name, optionally "next <weekday>".
        force_next = "next" in norm
        for name, wd in _WEEKDAYS.items():
            if re.search(rf"\b{name}\b", norm):
                base_day = _next_weekday(now, wd, force_next_week=force_next)
                day_specified = True
                break

    # 3. Establish the TIME of day. When an absolute month/day was parsed, strip
    #    the month name and the day-of-month token so a day like "14" in
    #    "september 14" is not misread as a 14:00 clock hour.
    time_norm = norm
    if month_day is not None:
        for name in _MONTHS:
            time_norm = re.sub(rf"\b{name}\b", " ", time_norm)
        # Drop a numeric day token and ordinal day words.
        time_norm = re.sub(r"\b\d{1,2}(?:st|nd|rd|th)?\b", " ", time_norm)
        for word in _DAY_ORDINALS:
            time_norm = re.sub(rf"\b{word}\b", " ", time_norm)
        time_norm = " ".join(time_norm.split())

    clock = _CLOCK_RE.search(time_norm)
    resolved = _resolve_clock(clock) if clock is not None else None

    # Spoken-word hour ("five pm", "at nine") when no digit clock was found.
    if resolved is None:
        wm = _WORD_CLOCK_RE.search(time_norm)
        if wm is not None:
            hour = _HOUR_WORDS.get(wm.group("word"))
            if hour is not None:
                ampm = wm.group("ampm")
                if ampm == "pm" and hour != 12:
                    hour += 12
                elif ampm == "am" and hour == 12:
                    hour = 0
                resolved = (hour, 0)

    if resolved is not None:
        hour, minute = resolved
        candidate = _apply_clock(base_day, hour, minute)
        # Bare clock with no day: resolve to the next future occurrence so a
        # plain "3pm" never schedules into the past.
        if not day_specified and candidate <= now:
            candidate = _apply_clock(base_day + timedelta(days=1), hour, minute)
        return candidate.isoformat()

    # 4. No clock reading — try a named part of day ("tonight", "this
    #    afternoon", "this morning", "noon").
    for name, (hour, minute) in _DAYPARTS.items():
        if re.search(rf"\b{name}\b", norm):
            candidate = _apply_clock(base_day, hour, minute)
            if not day_specified and candidate <= now:
                candidate = _apply_clock(base_day + timedelta(days=1), hour, minute)
            return candidate.isoformat()

    # 5. A day was named but no time given (e.g. "next monday"): default to a
    #    reasonable morning hour so the caller still has a concrete time.
    if day_specified and ("tomorrow" in norm or "next" in norm
                          or month_day is not None
                          or any(re.search(rf"\b{n}\b", norm) for n in _WEEKDAYS)):
        return _apply_clock(base_day, 9, 0).isoformat()

    return None

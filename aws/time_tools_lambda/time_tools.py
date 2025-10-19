#!/usr/bin/env python3
"""
time_tools.py
-------------
Utilities for converting natural-language time phrases and arbitrary date-like strings
into ISO-8601 dates. Designed to be used standalone or from AWS Lambda (as a Bedrock
Agent Action Group tool).

Key functions
- parse_human_time(phrase, ...): natural language → future-only ISO date (YYYY-MM-DD).
- normalize_any_date_to_iso(text, ...): any date-like string → ISO date, without forcing future.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from dateutil import tz

try:
    import dateparser  # type: ignore
except Exception:
    dateparser = None

try:
    from dateutil import parser as duparser  # type: ignore
except Exception:
    duparser = None

WEEKDAYS_EN = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]
MONTHS_EN = [
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
]

ORDINAL_WORDS = {
    "first": 1,
    "1st": 1,
    "second": 2,
    "2nd": 2,
    "third": 3,
    "3rd": 3,
    "fourth": 4,
    "4th": 4,
    "fifth": 5,
    "5th": 5,
    "last": -1,
}

WORD_NUMS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}

PAST_TO_FUTURE_SUBS = [
    (r"\byesterday\b", "tomorrow"),
    (r"\bthe day before\b", "the day after"),
    (r"\b(last|previous)\b", "next"),
    (r"\b(?:an|one)\s+week\s+ago\b", "in one week"),
    (r"\b(\d+)\s+weeks?\s+ago\b", r"in \1 week"),
    (
        r"\b("
        + "|".join(WORD_NUMS.keys())
        + r")\s+weeks?\s+ago\b",
        lambda m: f"in {WORD_NUMS[m.group(1).lower()]} week",
    ),
    (r"\b(?:an|one)\s+day\s+ago\b", "tomorrow"),
    (r"\b(\d+)\s+days?\s+ago\b", r"in \1 day"),
    (
        r"\b("
        + "|".join(WORD_NUMS.keys())
        + r")\s+days?\s+ago\b",
        lambda m: "tomorrow"
        if WORD_NUMS[m.group(1).lower()] == 1
        else f"in {WORD_NUMS[m.group(1).lower()]} day",
    ),
    (r"\b(?:an|one)\s+month\s+ago\b", "in one month"),
    (r"\b(\d+)\s+months?\s+ago\b", r"in \1 month"),
    (
        r"\b("
        + "|".join(WORD_NUMS.keys())
        + r")\s+months?\s+ago\b",
        lambda m: f"in {WORD_NUMS[m.group(1).lower()]} month",
    ),
    (r"\b(?:an|one)\s+year\s+ago\b", "in one year"),
    (r"\b(\d+)\s+years?\s+ago\b", r"in \1 year"),
    (
        r"\b("
        + "|".join(WORD_NUMS.keys())
        + r")\s+years?\s+ago\b",
        lambda m: f"in {WORD_NUMS[m.group(1).lower()]} year",
    ),
]

NTH_WEEKDAY_IN_MONTH_RE = re.compile(
    r"\b(?P<ord>(first|second|third|fourth|fifth|last|1st|2nd|3rd|4th|5th))\s+"
    r"(?P<wday>monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r"\s+(?:of|in)\s+"
    r"(?P<month>january|february|march|april|may|june|july|august|september|october|november|december)\b",
    re.IGNORECASE,
)

NEXT_WEEKDAY_RE = re.compile(
    r"\b(next|this)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.IGNORECASE,
)

ORDINAL_SUFFIX_RE = re.compile(r"(?<=\d)(st|nd|rd|th)\b", re.IGNORECASE)


@dataclass
class ParseOptions:
    locales: Optional[List[str]] = None
    timezone: str = "UTC"
    translation_map: Optional[Dict[str, str]] = None


def _normalize(text: str, translation_map: Optional[Dict[str, str]]) -> str:
    s = text.strip()
    if translation_map:
        for src, dst in translation_map.items():
            s = re.sub(src, dst, s, flags=re.IGNORECASE)

    def wordnum_to_digit(match: re.Match[str]) -> str:
        word = match.group(0).lower()
        return str(WORD_NUMS.get(word, word))

    s = re.sub(
        r"\b("
        + "|".join(map(re.escape, WORD_NUMS.keys()))
        + r")\b(?=\s+(day|days|week|weeks|month|months|year|years)\s+ago\b)",
        wordnum_to_digit,
        s,
        flags=re.IGNORECASE,
    )

    for pattern, replacement in PAST_TO_FUTURE_SUBS:
        s = re.sub(pattern, replacement, s, flags=re.IGNORECASE)

    return ORDINAL_SUFFIX_RE.sub("", s)


def _tz_now(timezone: str) -> datetime:
    zone = tz.gettz(timezone) if timezone else tz.UTC
    now = datetime.utcnow().replace(tzinfo=tz.UTC)
    return now.astimezone(zone)


def _format_date(dt_obj: datetime) -> str:
    return dt_obj.strftime("%Y-%m-%d")


def parse_human_time(
    phrase: str,
    *,
    locales: Optional[List[str]] = None,
    timezone: str = "UTC",
    translation_map: Optional[Dict[str, str]] = None,
) -> str:
    if not phrase or not phrase.strip():
        raise ValueError("A non-empty phrase is required.")

    normalized = _normalize(phrase, translation_map)
    now = _tz_now(timezone)

    nth_match = NTH_WEEKDAY_IN_MONTH_RE.search(normalized.lower())
    if nth_match:
        ordinal_word = nth_match.group("ord").lower()
        weekday_name = nth_match.group("wday").lower()
        month_name = nth_match.group("month").lower()
        year = now.year
        if MONTHS_EN.index(month_name) + 1 < now.month:
            year += 1
        month_num = MONTHS_EN.index(month_name) + 1
        order = ORDINAL_WORDS.get(ordinal_word, 1)
        return _format_date(_nth_weekday_of_month(year, month_num, weekday_name, order))

    next_match = NEXT_WEEKDAY_RE.search(normalized.lower())
    if next_match:
        weekday_name = next_match.group(2).lower()
        return _format_date(_next_weekday(now, weekday_name))

    if dateparser:
        parsed = dateparser.parse(
            normalized,
            settings={
                "PREFER_DATES_FROM": "future",
                "RELATIVE_BASE": now,
            },
            languages=locales,
        )
        if parsed:
            return _format_date(parsed.astimezone(tz.UTC))

    if duparser:
        try:
            parsed = duparser.parse(normalized, fuzzy=True)
            if parsed:
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=tz.UTC)
                return _format_date(parsed.astimezone(tz.UTC))
        except Exception as exc:
            raise ValueError(f"Could not parse phrase '{phrase}': {exc}") from exc

    raise ValueError(f"Unable to parse phrase '{phrase}'.")


def normalize_any_date_to_iso(
    text: str,
    *,
    locales: Optional[List[str]] = None,
    timezone: str = "UTC",
) -> str:
    if not text or not text.strip():
        raise ValueError("A non-empty text value is required.")

    cleaned = text.strip()
    if dateparser:
        parsed = dateparser.parse(
            cleaned,
            settings={
                "PREFER_DATES_FROM": "future",
                "RELATIVE_BASE": _tz_now(timezone),
                "STRICT_PARSING": True,
            },
            languages=locales,
        )
        if parsed:
            return _format_date(parsed.astimezone(tz.UTC))

    if duparser:
        try:
            parsed = duparser.parse(cleaned, fuzzy=True, dayfirst=True)
            if parsed:
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=tz.UTC)
                return _format_date(parsed.astimezone(tz.UTC))
        except Exception as exc:
            raise ValueError(f"Could not normalize text '{text}': {exc}") from exc

    raise ValueError(f"Unable to normalize text '{text}'.")


def _nth_weekday_of_month(year: int, month: int, weekday_name: str, ordinal: int) -> datetime:
    calendar_month = calendar.monthcalendar(year, month)
    weekday_index = WEEKDAYS_EN.index(weekday_name)
    if ordinal == -1:
        for week in reversed(calendar_month):
            if week[weekday_index] != 0:
                day = week[weekday_index]
                return datetime(year, month, day, tzinfo=tz.UTC)
    occurrences = [week[weekday_index] for week in calendar_month if week[weekday_index] != 0]
    index = ordinal - 1
    if index < 0 or index >= len(occurrences):
        raise ValueError("Ordinal out of range for the given month.")
    return datetime(year, month, occurrences[index], tzinfo=tz.UTC)


def _next_weekday(reference: datetime, weekday_name: str) -> datetime:
    target_index = WEEKDAYS_EN.index(weekday_name)
    days_ahead = (target_index - reference.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return reference + timedelta(days=days_ahead)

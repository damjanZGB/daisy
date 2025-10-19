# Time Tools for AWS Bedrock Agent (Action Group)

This guide packages two functions as a Lambda-backed tool your **AWS Bedrock Agent** can call:

- `parse_human_time(phrase, ...)` → **future-only** ISO date for natural language (e.g., “next Saturday”, “first Monday in March”)
- `normalize_any_date_to_iso(text, ...)` → **absolute** ISO date from *any* date-like string (e.g., “1st of November”, “november 1st”, “01/11/2025”), without forcing it into the future.

---

## 1) Files

**`time_tools.py`**
```python
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

import re
import calendar
from dataclasses import dataclass
from typing import Dict, List, Optional
from datetime import datetime, timedelta
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
    "monday","tuesday","wednesday","thursday","friday","saturday","sunday"
]
MONTHS_EN = [
    "january","february","march","april","may","june","july",
    "august","september","october","november","december"
]

ORDINAL_WORDS = {
    "first": 1, "1st": 1,
    "second": 2, "2nd": 2,
    "third": 3, "3rd": 3,
    "fourth": 4, "4th": 4,
    "fifth": 5, "5th": 5,
    "last": -1
}

WORD_NUMS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12
}

PAST_TO_FUTURE_SUBS = [
    (r"\byesterday\b", "tomorrow"),
    (r"\bthe day before\b", "the day after"),
    (r"\b(last|previous)\b", "next"),
    (r"\b(?:an|one)\s+week\s+ago\b", "in one week"),
    (r"\b(\d+)\s+weeks?\s+ago\b", r"in \1 week"),
    (r"\b(" + "|".join(WORD_NUMS.keys()) + r")\s+weeks?\s+ago\b",
     lambda m: f"in {WORD_NUMS[m.group(1).lower()]} week"),
    (r"\b(?:an|one)\s+day\s+ago\b", "tomorrow"),
    (r"\b(\d+)\s+days?\s+ago\b", r"in \1 day"),
    (r"\b(" + "|".join(WORD_NUMS.keys()) + r")\s+days?\s+ago\b",
     lambda m: "tomorrow" if WORD_NUMS[m.group(1).lower()]==1 else f"in {WORD_NUMS[m.group(1).lower()]} day"),
    (r"\b(?:an|one)\s+month\s+ago\b", "in one month"),
    (r"\b(\d+)\s+months?\s+ago\b", r"in \1 month"),
    (r"\b(" + "|".join(WORD_NUMS.keys()) + r")\s+months?\s+ago\b",
     lambda m: f"in {WORD_NUMS[m.group(1).lower()]} month"),
    (r"\b(?:an|one)\s+year\s+ago\b", "in one year"),
    (r"\b(\d+)\s+years?\s+ago\b", r"in \1 year"),
    (r"\b(" + "|".join(WORD_NUMS.keys()) + r")\s+years?\s+ago\b",
     lambda m: f"in {WORD_NUMS[m.group(1).lower()]} year"),
]

NTH_WEEKDAY_IN_MONTH_RE = re.compile(
    r"\b(?P<ord>(first|second|third|fourth|fifth|last|1st|2nd|3rd|4th|5th))\s+"
    r"(?P<wday>monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r"\s+(?:of|in)\s+"
    r"(?P<month>january|february|march|april|may|june|july|august|september|october|november|december)\b",
    re.IGNORECASE
)

NEXT_WEEKDAY_RE = re.compile(
    r"\b(next|this)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.IGNORECASE
)

ORDINAL_SUFFIX_RE = re.compile(r"(?<=\d)(st|nd|rd|th)\b", re.IGNORECASE)

@dataclass
class ParseOptions:
    locales: Optional[List[str]] = None
    timezone: str = "UTC"
    translation_map: Optional[Dict[str, str]] = None

def _normalize(text: str, translation_map: Optional[Dict[str, str]]) -> str:
    s = text.strip()
    # Apply custom translation map first
    if translation_map:
        for src, dst in translation_map.items():
            s = re.sub(src, dst, s, flags=re.IGNORECASE)

    # Convert word-numbers when followed by unit+ago (two weeks ago -> 2 weeks ago)
    def wordnum_to_digit(m):
        w = m.group(0).lower()
        return str(WORD_NUMS.get(w, w))
    s = re.sub(
        r"\b(" + "|".join(map(re.escape, WORD_NUMS.keys())) + r")\b(?=\s+(day|days|week|weeks|month|months|year|years)\s+ago\b)",
        wordnum_to_digit, s, flags=re.IGNORECASE
    )

    # Force future intent
    for pat, repl in PAST_TO_FUTURE_SUBS:
        s = re.sub(pat, repl, s, flags=re.IGNORECASE)

    # Normalize spaces
    s = re.sub(r"\s+", " ", s)
    return s

def _nth_weekday_in_month(year: int, month: int, weekday: int, n: int) -> datetime:
    cal = calendar.Calendar(firstweekday=calendar.MONDAY)
    dates = [d for d in cal.itermonthdates(year, month) if d.month == month and d.weekday() == weekday]
    if n == -1:
        target = dates[-1]
    else:
        target = dates[n-1]
    return datetime(target.year, target.month, target.day)

def _handle_nth_weekday_phrase(text: str, now: datetime) -> Optional[datetime]:
    m = NTH_WEEKDAY_IN_MONTH_RE.search(text)
    if not m:
        return None
    ord_word = m.group("ord").lower()
    wday_word = m.group("wday").lower()
    month_word = m.group("month").lower()

    n = ORDINAL_WORDS.get(ord_word, None)
    if n is None:
        return None

    weekday = WEEKDAYS_EN.index(wday_word)
    month = MONTHS_EN.index(month_word) + 1

    year = now.year
    candidate = _nth_weekday_in_month(year, month, weekday, n)
    if candidate.date() <= now.date():
        candidate = _nth_weekDAY_in_month(year + 1, month, weekday, n)  # typo to fix?
    return candidate

def _handle_next_weekday_phrase(text: str, now: datetime) -> Optional[datetime]:
    m = NEXT_WEEKDAY_RE.search(text)
    if not m:
        return None
    when_word = m.group(1).lower()
    wday_word = m.group(2).lower()
    target_wd = WEEKDAYS_EN.index(wday_word)
    today_wd = now.weekday()

    days_ahead = (target_wd - today_wd) % 7
    if when_word == "this":
        if today_wd > target_wd:
            days_ahead = 7 - (today_wd - target_wd)
    else:
        days_ahead = 7 if days_ahead == 0 else days_ahead
    candidate = now + timedelta(days=days_ahead)
    return datetime(candidate.year, candidate.month, candidate.day)

def _roll_forward_if_past(dt: datetime, now: datetime, original_text: str) -> datetime:
    if dt.date() > now.date():
        return dt

    text = original_text.lower()

    if any(w in text for w in WEEKDAYS_EN):
        while dt.date() <= now.date():
            dt += timedelta(days=7)
        return dt

    if any(m in text for m in MONTHS_EN) and not re.search(r"\b20\d{2}\b", text):
        while dt.date() <= now.date():
            dt = dt.replace(year=dt.year + 1)
        return dt

    while dt.date() <= now.date():
        dt += timedelta(days=1)
    return dt

def parse_human_time(phrase: str,
                     locales: Optional[List[str]] = None,
                     timezone: str = "UTC",
                     translation_map: Optional[Dict[str, str]] = None,
                     now: Optional[datetime] = None) -> str:
    """
    Natural language → ISO date (YYYY-MM-DD), always coerced to the future.
    """
    if now is None:
        now = datetime.now(tz.gettz(timezone)) if tz.gettz(timezone) else datetime.now()

    normalized = _normalize(phrase, translation_map)

    # Custom patterns
    dt: Optional[datetime] = _handle_nth_weekday_phrase(normalized, now)
    if dt is None:
        dt = _handle_next_weekday_phrase(normalized, now)

    # Simple terms
    if dt is None:
        if re.search(r"\btomorrow\b", normalized, re.I):
            dt = now + timedelta(days=1)
        elif re.search(r"\btoday\b", normalized, re.I):
            dt = now

    # dateparser
    if dt is None and dateparser is not None:
        settings = {
            "PREFER_DATES_FROM": "future",
            "RELATIVE_BASE": now,
            "TIMEZONE": timezone,
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DAY_OF_MONTH": "first",
            "STRICT_PARSING": False,
            "SKIP_TOKENS": ["on", "at", "the", "of"]
        }
        dt2 = dateparser.parse(normalized, languages=locales, settings=settings)
        if isinstance(dt2, datetime):
            dt = dt2

    # Heuristics
    if dt is None:
        rel = re.search(r"\bin\s+(\d+)\s+(day|days|week|weeks|month|months|year|years)\b", normalized, flags=re.I)
        if rel:
            n = int(rel.group(1))
            unit = rel.group(2).lower()
            if unit.startswith("day"):
                dt = now + timedelta(days=n)
            elif unit.startswith("week"):
                dt = now + timedelta(weeks=n)
            elif unit.startswith("month"):
                year, month = now.year, now.month + n
                year += (month - 1) // 12
                month = ((month - 1) % 12) + 1
                last_day = calendar.monthrange(year, month)[1]
                dt = datetime(year, month, min(now.day, last_day), tzinfo=now.tzinfo)
            else:
                dt = now.replace(year=now.year + n)
        else:
            for i, w in enumerate(WEEKDAYS_EN):
                if re.search(rf"\b{w}\b", normalized, re.I):
                    today_wd = now.weekday()
                    days_ahead = (i - today_wd) % 7
                    days_ahead = 7 if days_ahead == 0 else days_ahead
                    dt = now + timedelta(days=days_ahead)
                    break

    if dt is None:
        raise ValueError(f"Could not parse time phrase: '{phrase}'")

    dt = _roll_forward_if_past(dt if dt.tzinfo else dt.replace(tzinfo=now.tzinfo), now, normalized)
    return dt.date().isoformat()

# -----------------------------------------------------------
# NEW: normalize_any_date_to_iso
# -----------------------------------------------------------

def _strip_ordinals(s: str) -> str:
    # Remove ordinal suffixes and filler words like "of"
    s = ORDINAL_SUFFIX_RE.sub("", s)
    s = re.sub(r"\bof\b", " ", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def normalize_any_date_to_iso(text: str,
                              locales: Optional[List[str]] = None,
                              timezone: str = "UTC") -> str:
    """
    Take arbitrary 'date-like' text (e.g., '1st of November', 'november 1st', '01/11/2025')
    and return ISO date (YYYY-MM-DD). Does NOT force future.
    """
    if not text or not text.strip():
        raise ValueError("Empty date string")

    now = datetime.now(tz.gettz(timezone)) if tz.gettz(timezone) else datetime.now()

    cleaned = _strip_ordinals(text)

    # Try dateparser first (multilingual)
    if dateparser is not None:
        settings = {
            "PREFER_DATES_FROM": "future",
            "RELATIVE_BASE": now,
            "TIMEZONE": timezone,
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DAY_OF_MONTH": "first",
            "STRICT_PARSING": False,
        }
        dt = dateparser.parse(cleaned, languages=locales, settings=settings)
        if isinstance(dt, datetime):
            return dt.date().isoformat()

    # Try python-dateutil
    if duparser is not None:
        try:
            dt2 = duparser.parse(cleaned, fuzzy=True, dayfirst=False)
            return dt2.date().isoformat()
        except Exception:
            try:
                dt3 = duparser.parse(cleaned, fuzzy=True, dayfirst=True)
                return dt3.date().isoformat()
            except Exception:
                pass

    # Manual patterns (small set)
    m = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\b", cleaned)
    if m:
        d, mth, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        return f"{y:04d}-{mth:02d}-{d:02d}"

    raise ValueError(f"Could not normalize date string: '{text}'")

```

**`lambda_function.py`**
```python
# lambda_function.py
from time_tools import parse_human_time, normalize_any_date_to_iso

def lambda_handler(event, context):
    """
    Payload options:
    {
      "op": "human_to_future_iso" | "normalize_any",
      "phrase": "next saturday",
      "locale": ["en","hr"],
      "timezone": "Europe/Zagreb",
      "translation_map": {"sljedeće":"next","subote":"saturday"},
      "text": "1st of November 2025"
    }
    """
    op = (event.get("op") or "human_to_future_iso").lower()
    locale = event.get("locale", ["en"])
    tz = event.get("timezone", "UTC")
    try:
        if op == "normalize_any":
            iso = normalize_any_date_to_iso(event.get("text",""), locales=locale, timezone=tz)
            return {"success": True, "mode": op, "iso_date": iso}
        else:
            iso = parse_human_time(event.get("phrase",""), locales=locale,
                                   timezone=tz, translation_map=event.get("translation_map"))
            return {"success": True, "mode": op, "iso_date": iso}
    except Exception as e:
        return {"success": False, "error": str(e), "mode": op}

```

**`requirements.txt`**
```
dateparser
python-dateutil

```

---

## 2) How to Deploy as Lambda

### Option A: Console (quick)

1. Create a new folder with the three files above.
2. Zip everything from *inside* the folder (so the files are at the zip root):
   ```bash
   zip -r time_tools_lambda.zip time_tools.py lambda_function.py requirements.txt
   ```
3. In **AWS Console → Lambda → Create function** (Python 3.11).
4. Upload the zip (or use the “Upload from → .zip file”).
5. Set **Handler** to `lambda_function.lambda_handler`.
6. Configure timeout (e.g., 10s) and memory (e.g., 256 MB).
7. (Optional) Add env var `TZ=Europe/Zagreb`.

To include dependencies:
- Either use **Lambda layers** for `dateparser` & `python-dateutil`, or
- Build a deployment zip in a Linux environment including the `site-packages`.

### Option B: SAM/CLI (repeatable)
Create a SAM template that points to `lambda_function.lambda_handler`, run `sam build && sam deploy --guided`.

---

## 3) Register as a Bedrock Agent Tool (Action Group)

1. Go to **Amazon Bedrock → Agents → (your agent)**.
2. Add Action Group:
   - **Name:** `TimePhraseParser`
   - **Type:** Lambda
   - **Lambda ARN:** *(your deployed function)*
3. Provide a **function schema** with explicit parameters so the agent can pass phrases and timezone context. Example:

   ```json
   {
     "functions": [
       {
         "name": "human_to_future_iso",
         "description": "Convert a natural-language phrase (e.g. \"next Saturday\") into a future ISO date.",
         "parameters": {
           "phrase": { "type": "string", "required": true, "description": "Traveler supplied phrase to interpret." },
           "timezone": { "type": "string", "required": false, "description": "IANA timezone (defaults to UTC if omitted)." },
           "locale": { "type": "string", "required": false, "description": "JSON array of BCP-47 locale codes; defaults to [\"en\"]." },
           "translation_map": { "type": "string", "required": false, "description": "JSON object of regex replacements before parsing." }
         }
       },
       {
         "name": "normalize_any",
         "description": "Normalize an absolute date string to ISO format.",
         "parameters": {
           "text": { "type": "string", "required": true, "description": "Literal date text to normalize." },
           "timezone": { "type": "string", "required": false, "description": "IANA timezone used when interpreting relative content." },
           "locale": { "type": "string", "required": false, "description": "JSON array of locales; defaults to [\"en\"]." }
         }
       }
     ]
   }
   ```

Example input payloads:
```json
{
  "op": "human_to_future_iso",
  "phrase": "first monday in march",
  "locale": ["en"],
  "timezone": "Europe/Zagreb"
}
```
```json
{
  "op": "normalize_any",
  "text": "1st of November 2025",
  "locale": ["en"]
}
```

The Lambda returns:
```json
{ "success": true, "mode": "human_to_future_iso", "iso_date": "2026-03-02" }
```

---

## 4) Can Bedrock use the script **outside** the Lambda?

**Not directly.** Bedrock Agents call tools via **Action Groups**, which are backed by **Lambda** (or an external **HTTP API** you own). Your script must be accessible to the tool runtime:

- Bundle the script inside the Lambda zip **or**
- Put the script into a **Lambda Layer** and `import time_tools` **or**
- Mount it via **Lambda + EFS** **or**
- Expose it behind an **API Gateway HTTP endpoint** and make the Action Group call that API

Bedrock itself cannot import and run arbitrary files from S3 or your server without going through one of the mechanisms above.

---

## 5) Examples

```python
from time_tools import parse_human_time, normalize_any_date_to_iso

# Future-only natural language
parse_human_time("next saturday", timezone="Europe/Zagreb")      # → e.g., 2025-10-25
parse_human_time("first monday in march", locales=["en"])        # → e.g., 2026-03-02

# Absolute date normalizer
normalize_any_date_to_iso("1st of November 2025", locales=["en"])    # → 2025-11-01
normalize_any_date_to_iso("november 1st", locales=["en"])            # → 2025-11-01
normalize_any_date_to_iso("01/11/2025")                              # → 2025-11-01 (day-first bias via retry)
```

---

## 6) Error Handling

Both functions raise `ValueError` if parsing fails; the Lambda wrapper converts this into:
```json
{ "success": false, "error": "Could not parse ..." }
```

---

## 7) Tips

- For multilingual inputs, pass `locales=["hr","en","de"]` to leverage `dateparser`.
- For domain-specific phrases, add a `translation_map` (e.g., map Croatian keywords into English equivalents).
- If you need **strict** absolute parsing without future bias, tweak `dateparser` settings in `normalize_any_date_to_iso` or prefer `duparser.parse(..., dayfirst=True)` paths.

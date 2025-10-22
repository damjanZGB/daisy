# lambda_function.py
# AWS Lambda for Agents for Amazon Bedrock (Action Group).
# Searches real flight offers via Amadeus Self-Service API and returns simplified offers.
# Default filters to Lufthansa Group carriers unless overridden.
# Runtime: Python 3.12
# Deps: (none - stdlib only)

from __future__ import annotations

import calendar
import contextvars
import json
import os
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib import error as _urlerr
from urllib import parse as _urlparse
from urllib import request as _urlreq
import os
import uuid
from zoneinfo import ZoneInfo
import threading
import time
import hashlib

# Optional S3 debug capture for tool I/O
try:
    import boto3  # type: ignore
except Exception:  # pragma: no cover
    boto3 = None

DEBUG_S3_BUCKET = (os.getenv("DEBUG_S3_BUCKET") or "").strip()
DEBUG_S3_PREFIX = (os.getenv("DEBUG_S3_PREFIX") or "debug-tool-io").strip().strip("/")
DEBUG_TOOL_IO = (os.getenv("DEBUG_TOOL_IO") or "").strip().lower() in {"1", "true", "yes", "on"}
_S3_CLIENT = None
if DEBUG_S3_BUCKET and boto3 is not None:  # pragma: no cover
    try:
        _S3_CLIENT = boto3.client("s3", region_name=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-west-2")
    except Exception:
        _S3_CLIENT = None

# ------- Recommender verbosity and size controls -------
# If false, omit THEN lines in recommender message to keep payloads small (useful in replay/stress).
RECOMMENDER_VERBOSE = (os.getenv("RECOMMENDER_VERBOSE") or "true").strip().lower() in {"1", "true", "yes", "on"}
# Max number of options to include in the recommender response.
RECOMMENDER_MAX_OPTIONS = int((os.getenv("RECOMMENDER_MAX_OPTIONS") or "10").strip() or 10)
# If the formatted message text exceeds this many bytes, switch to a compact format (limit options, drop THEN lines).
RECOMMENDER_MAX_TEXT_BYTES = int((os.getenv("RECOMMENDER_MAX_TEXT_BYTES") or "4000").strip() or 4000)

# --------------- Destination Catalog (recommender) ----------------
_DEST_CATALOG_PATHS = [
    os.path.join(os.getcwd(), "data", "lh_destinations_catalog.json"),
    os.path.join(os.path.dirname(__file__), "data", "lh_destinations_catalog.json"),
    os.path.join(os.path.dirname(__file__), "..", "data", "lh_destinations_catalog.json"),
]
_DEST_CATALOG: Optional[list] = None
_IATA_COORDS_CACHE: Optional[dict] = None

def _load_catalog() -> list:
    global _DEST_CATALOG
    if _DEST_CATALOG is not None:
        return _DEST_CATALOG
    for p in _DEST_CATALOG_PATHS:
        try:
            with open(p, "r", encoding="utf-8") as f:
                _DEST_CATALOG = json.load(f)
                _log("Destination catalog loaded", entries=len(_DEST_CATALOG), path=p)
                return _DEST_CATALOG
        except Exception:
            continue
    _DEST_CATALOG = []
    _log("Destination catalog missing", tried=_DEST_CATALOG_PATHS)
    return _DEST_CATALOG


def _load_iata_coords() -> dict:
    global _IATA_COORDS_CACHE
    if _IATA_COORDS_CACHE is not None:
        return _IATA_COORDS_CACHE
    candidates = [
        os.path.join(os.getcwd(), "iata.json"),
        os.path.join(os.getcwd(), "backend", "iata.json"),
    ]
    for p in candidates:
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
                coords = {}
                for rec in data:
                    code = rec.get("code") or rec.get("iata_code") or rec.get("iata")
                    lat = rec.get("latitude")
                    lon = rec.get("longitude")
                    if code and lat is not None and lon is not None:
                        coords[str(code).upper()] = (float(lat), float(lon))
                _IATA_COORDS_CACHE = coords
                _log("IATA coords loaded", count=len(coords), path=p)
                return _IATA_COORDS_CACHE
        except Exception:
            continue
    _IATA_COORDS_CACHE = {}
    _log("IATA coords not found", tried=candidates)
    return _IATA_COORDS_CACHE


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    from math import radians, sin, cos, sqrt, atan2
    R = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon/2)**2
    c = 2 * atan2(sqrt(a), sqrt(1-a))
    return R * c


def _month_from_phrase(phrase: Optional[str], reference: Optional[date] = None) -> tuple[int, int]:
    """Return (year, month) from phrases like 'March 2026', '2026-03', 'next March'. Defaults to current/next.
    """
    if reference is None:
        reference = datetime.utcnow().date()
    if not phrase:
        return reference.year, reference.month
    txt = str(phrase).strip()
    # ISO YYYY-MM
    m = re.fullmatch(r"(\d{4})-(\d{2})", txt)
    if m:
        return int(m.group(1)), int(m.group(2))
    # Month name and optional year
    months = [
        "january","february","march","april","may","june","july","august","september","october","november","december"
    ]
    lower = txt.lower()
    if "next year" in lower and any(mon in lower for mon in months):
        for idx, mon in enumerate(months, start=1):
            if mon in lower:
                return reference.year + 1, idx
    for idx, mon in enumerate(months, start=1):
        # 'march 2026' or 'march'
        m2 = re.search(mon, lower)
        if m2:
            ym = re.search(r"(\d{4})", lower)
            y = int(ym.group(1)) if ym else reference.year
            # if month already passed this year, roll to next year
            if ym is None and idx < reference.month:
                y += 1
            return y, idx
    # fallback: try YYYY-MM-DD
    m3 = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", txt)
    if m3:
        return int(m3.group(1)), int(m3.group(2))
    return reference.year, reference.month


def _canonicalize_theme_tags(raw: List[str], phrase: Optional[str] = None) -> List[str]:
    """Normalize theme tags to catalog tags.

    Maps common synonyms like 'ski', 'skiing', 'snowboard' to 'winter_sports', etc.
    """
    aliases = {
        "ski": ["winter_sports", "cold", "mountain"],
        "skiing": ["winter_sports", "cold", "mountain"],
        "snow": ["winter_sports", "cold", "mountain"],
        "snowboard": ["winter_sports", "cold", "mountain"],
        "alps": ["winter_sports", "cold", "mountain"],
        "winter": ["winter_sports", "cold"],
        "city": ["city_break"],
        "city-break": ["city_break"],
        "city_break": ["city_break"],
        "weekend": ["city_break"],
        "urban": ["city_break"],
        "beach": ["beach", "warm"],
        "sun": ["beach", "warm"],
        "sunny": ["beach", "warm"],
        "seaside": ["beach", "warm"],
        "coast": ["beach", "warm"],
        "island": ["beach", "warm"],
        "resort": ["beach", "warm"],
        "diving": ["diving", "beach", "warm"],
        "scuba": ["diving", "beach", "warm"],
        "snorkeling": ["diving", "beach", "warm"],
        "fishing": ["fishing"],
        "tuna": ["tuna_fishing", "fishing"],
        "tuna_fishing": ["tuna_fishing", "fishing"],
        "big_game_fishing": ["tuna_fishing", "fishing"],
        "safari": ["safari", "wildlife"],
        "wildlife": ["wildlife", "safari"],
        "photo": ["wildlife_photography", "wildlife", "safari"],
        "photography": ["wildlife_photography", "wildlife", "safari"],
        "wildlife photography": ["wildlife_photography", "wildlife", "safari"],
        "bike": ["cycling"],
        "bicycle": ["cycling"],
        "cycling": ["cycling"],
        "mtb": ["cycling"],
        "hunting": ["hunting"],
    }
    out: List[str] = []
    seen = set()
    def add(tag: str):
        t = tag.strip().lower()
        if t and t not in seen:
            out.append(t)
            seen.add(t)
    for t in raw or []:
        key = str(t).strip().lower()
        if key in aliases:
            for mapped in aliases[key]:
                add(mapped)
        else:
            add(key)
    # Phrase hint (if includes 'ski')
    if phrase:
        low = phrase.lower()
        if any(k in low for k in ("ski", "skiing", "snowboard")):
            for m in ("winter_sports", "cold", "mountain"):
                add(m)
    return out


def _parse_month_range(value: Optional[str], reference: Optional[date] = None) -> Tuple[Tuple[int, int], Tuple[int, int]]:
    """Parse a month or month range and return ((start_year, start_month), (end_year, end_month)).

    Supports:
    - "YYYY-MM..YYYY-MM" explicit ranges
    - Fallback to a single month via _month_from_phrase
    """
    if reference is None:
        reference = datetime.utcnow().date()
    if not value:
        y, m = _month_from_phrase(None, reference)
        return (y, m), (y, m)
    text = str(value).strip()
    m = re.fullmatch(r"(\d{4})-(\d{2})\.\.(\d{4})-(\d{2})", text)
    if m:
        y1, m1, y2, m2 = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        return (y1, m1), (y2, m2)
    # Minimal natural-language: just resolve first month and mirror
    y, mon = _month_from_phrase(text, reference)
    return (y, mon), (y, mon)


def _score_destination(dest: dict, theme_tags: set[str], target_month: int, origin_code: Optional[str]) -> tuple[float, str]:
    score = 0.0
    reasons = []
    tags = set(str(t).lower() for t in dest.get("tags", []))
    if theme_tags & tags:
        score += 0.2
    # Warm/beach
    if "beach" in theme_tags or "warm" in theme_tags:
        avg = (dest.get("avgHighCByMonth") or {}).get(str(target_month))
        if isinstance(avg, (int, float)):
            temp_score = max(0.0, min(1.0, (float(avg) - 20.0) / 10.0))
            score += temp_score
            reasons.append(f"avgHighC={avg}C")
        water = (dest.get("waterTempCByMonth") or {}).get(str(target_month))
        if isinstance(water, (int, float)):
            water_score = max(0.0, min(1.0, (float(water) - 18.0) / 8.0))
            score += water_score
            reasons.append(f"water={water}C")
    # Winter sports
    if "winter_sports" in theme_tags or "cold" in theme_tags:
        snow = (dest.get("snowReliability") or {}).get(str(target_month))
        if isinstance(snow, (int, float)):
            snow_score = max(0.0, min(1.0, float(snow)))
            score += snow_score
            reasons.append(f"snowRel={snow}")
        elif isinstance(dest.get("elevationM"), (int, float)):
            elev = float(dest["elevationM"])
            elev_score = max(0.0, min(1.0, (elev - 500.0) / 1500.0))
            score += elev_score * 0.5
            reasons.append(f"elev={elev}m")
    # Activity-specific boosts
    activity_tags = {"diving", "tuna_fishing", "fishing", "safari", "wildlife", "cycling", "hunting"}
    for tag in activity_tags:
        if tag in theme_tags and tag in (dest.get("tags") or []):
            score += 0.2
            reasons.append(tag)
    # Carrier bias (prefer LH   Group presence)
    brands = set(str(x) for x in dest.get("lhGroupCarriers", []))
    if brands:
        score += 0.1
        if any(b in brands for b in ("EW","4Y")):
            score += 0.1
    # Distance penalty (heavier for short city breaks)
    if origin_code:
        coords = _load_iata_coords()
        o = coords.get(origin_code.upper())
        d = coords.get(str(dest.get("code", "")).upper())
        if o and d:
            km = _haversine_km(o[0], o[1], d[0], d[1])
            penalty_cap = 0.6 if ("city_break" in theme_tags) else 0.4
            penalty = min(penalty_cap, max(0.0, km / 5000.0 * penalty_cap))
            score -= penalty
            reasons.append(f"dist~{int(km)}km")
    reason = ", ".join(reasons) if reasons else "tag match"
    return score, reason

LH_GROUP_CODES = ["LH", "LX", "OS", "SN", "EW", "4Y", "EN"]

# Short-TTL pricing cache and in-flight coalescing (per-container)
_PRICE_CACHE: Dict[str, Tuple[Dict[str, Any], float]] = {}
_PRICE_INFLIGHT: Dict[str, threading.Event] = {}
_PRICE_LOCK = threading.Lock()
_PRICE_TTL_SEC = 10.0

def _build_price_key(
    origin: str,
    destination: str,
    departure_date: Optional[str],
    return_date: Optional[str],
    adults: int,
    cabin: str,
    nonstop: bool,
    currency: Optional[str],
) -> str:
    parts = [
        (origin or '').upper(),
        (destination or '').upper(),
        (departure_date or '-')[:10],
        (return_date or '-')[:10],
        str(max(1, int(adults or 1))),
        (cabin or 'ECONOMY').upper(),
        '1' if nonstop else '0',
        (currency or '-').upper(),
        'LHONLY',
    ]
    return hashlib.sha256('|'.join(parts).encode('utf-8')).hexdigest()

def _amadeus_search_cached(key: str, do_request, *, timeout: float = 8.0) -> Dict[str, Any]:
    now = time.time()
    with _PRICE_LOCK:
        tup = _PRICE_CACHE.get(key)
        if tup and tup[1] > now:
            _log("Amadeus dedupe hit", cacheKey=key[:8])
            return tup[0]
        ev = _PRICE_INFLIGHT.get(key)
        if ev is None:
            ev = threading.Event()
            _PRICE_INFLIGHT[key] = ev
        else:
            start = time.time()
            ev.wait(timeout=max(0.5, min(timeout, 8.0)))
            waited_ms = int((time.time() - start) * 1000)
            _log("Amadeus coalesce wait", cacheKey=key[:8], waitedMs=waited_ms)
            tup2 = _PRICE_CACHE.get(key)
            if tup2 and tup2[1] > time.time():
                return tup2[0]
    try:
        resp = do_request()
        with _PRICE_LOCK:
            if len(_PRICE_CACHE) > 200:
                _PRICE_CACHE.clear()
            _PRICE_CACHE[key] = (resp, time.time() + _PRICE_TTL_SEC)
        return resp
    finally:
        with _PRICE_LOCK:
            try:
                _PRICE_INFLIGHT.get(key) and _PRICE_INFLIGHT[key].set()
            finally:
                _PRICE_INFLIGHT.pop(key, None)

PROXY_BASE_URL = (os.getenv("PROXY_BASE_URL") or "http://localhost:8787").rstrip("/")

MAX_LOOKAHEAD_DAYS = 365

# Action group names (function-details)
ACTION_GROUP_NAME = os.getenv("ACTION_GROUP_NAME", "daisy_in_action")
ACTION_GROUP_RECOMMENDER = os.getenv("ACTION_GROUP_RECOMMENDER", "DestinationRecommender")

_MONTH_LOOKUP = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
_WEEKDAY_LOOKUP = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

_STOPWORDS = {"in", "on", "the", "of"}
_PUNCT_TRAILING_RE = re.compile(r"[.!?]+$")
_NON_WORD_RE = re.compile(r"[^\w\s]")


def _strip_trailing_punctuation(text: str) -> str:
    if not text:
        return ""
    return _PUNCT_TRAILING_RE.sub("", text.strip())


def _normalise_phrase_tokens(raw: str) -> List[str]:
    if not raw:
        return []
    text = _strip_trailing_punctuation(raw)
    # Replace unicode dashes with spaces before stripping separators so ?next?Saturday? works.
    text = text.replace("?", " ").replace("?", " ")
    text = text.replace("?", "'")
    text = re.sub(r"[,\-/]", " ", text)
    text = re.sub(r"[()]", " ", text)
    text = _NON_WORD_RE.sub(" ", text)
    tokens = [tok for tok in text.lower().split() if tok]
    return tokens


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name, "")
    if not v:
        return default
    v = v.strip().lower()
    if v in ("1", "true", "t", "yes", "y", "on"):
        return True
    if v in ("0", "false", "f", "no", "n", "off"):
        return False
    return default


def _now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _safe_detail(value: Any, *, limit: int = 400) -> str:
    try:
        if isinstance(value, (dict, list, tuple)):
            text = json.dumps(value, default=str, ensure_ascii=False)
        else:
            text = str(value)
    except Exception:
        text = repr(value)
    text = text.replace("\n", " ").replace("\r", " ")
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


class InvocationLogger:
    def __init__(self) -> None:
        self.entries: List[Dict[str, Any]] = []

    def log(self, message: str, **details: Any) -> None:
        timestamp = _now_iso()
        entry: Dict[str, Any] = {"time": timestamp, "message": str(message)}
        if details:
            safe_details = {
                key: _safe_detail(value) for key, value in details.items() if value is not None
            }
            if safe_details:
                entry["details"] = safe_details
        self.entries.append(entry)
        pretty = entry["message"]
        if "details" in entry:
            try:
                pretty_details = json.dumps(entry["details"], ensure_ascii=False)
            except Exception:
                pretty_details = str(entry["details"])
            pretty = f"{pretty} | {pretty_details}"
        print(f"[lambda] {timestamp} {pretty}")

    def has_entries(self) -> bool:
        return bool(self.entries)

    def export(self) -> List[Dict[str, Any]]:
        return list(self.entries)

    def as_text(self) -> str:
        lines = []
        for entry in self.entries:
            line = f"{entry['time']} - {entry['message']}"
            details = entry.get("details")
            if details:
                try:
                    details_txt = json.dumps(details, ensure_ascii=False)
                except Exception:
                    details_txt = str(details)
                line = f"{line} | {details_txt}"
            lines.append(line)
        return "\n".join(lines)


_CURRENT_LOGGER: contextvars.ContextVar[Optional[InvocationLogger]] = contextvars.ContextVar(
    "current_invocation_logger", default=None
)


def _get_logger() -> Optional[InvocationLogger]:
    return _CURRENT_LOGGER.get()


def _log(message: str, **details: Any) -> None:
    logger = _get_logger()
    if logger:
        logger.log(message, **details)


def _attach_logs(payload: Dict[str, Any], logger: Optional[InvocationLogger]) -> Dict[str, Any]:
    if not logger or not logger.has_entries():
        return payload
    enriched = dict(payload)
    enriched["debugLog"] = logger.export()
    enriched["debugLogText"] = logger.as_text()
    return enriched


_FLIGHT_FIELD_ALIASES = {
    "origin": ("origin", "originLocationCode", "origin_code", "originCode"),
    "destination": ("destination", "destinationLocationCode", "destination_code", "destinationCode"),
    "departureDate": (
        "departureDate",
        "departure_date",
        "departure",
        "outboundDate",
        "outbound_date",
    ),
    "returnDate": ("returnDate", "return_date", "return", "inboundDate", "inbound_date"),
    "adults": ("adults", "adultCount", "numberOfAdults"),
    "children": ("children", "childCount", "numberOfChildren"),
    "infants": ("infants", "infantCount", "numberOfInfants"),
    "nonstop": ("nonstop", "nonStop", "non_stop", "direct"),
    "cabin": ("cabin", "travelClass", "class"),
    "currency": ("currency", "currencyCode", "currency_code"),
    "max": ("max", "maxResults", "limit", "topK"),
    "lhGroupOnly": ("lhGroupOnly", "lh_group_only", "lufthansaOnly", "lufthansaGroupOnly"),
    "sessionId": ("sessionId", "session_id"),
}


def _value_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True


def _extract_alias_value(data: Dict[str, Any], aliases: Tuple[str, ...]) -> Any:
    if not data:
        return None
    for alias in aliases:
        if alias in data and _value_present(data[alias]):
            return data[alias]
        if isinstance(alias, str):
            alias_lower = alias.lower()
            for key, value in data.items():
                if isinstance(key, str) and key.lower() == alias_lower and _value_present(value):
                    return value
    return None


def _normalize_flight_request_fields(data: Dict[str, Any]) -> Dict[str, Any]:
    normalized: Dict[str, Any] = {}
    if not isinstance(data, dict):
        return normalized
    for canonical, aliases in _FLIGHT_FIELD_ALIASES.items():
        value = _extract_alias_value(data, aliases)
        if value is not None:
            normalized[canonical] = value
    return normalized


_ORIGIN_SENTINELS_DEFAULT = {
    "default_departure_airport",
    "default_airport",
    "default_origin",
    "defaultdepartureairport",
    "system_default_airport",
    "infer_origin",
}
_ORIGIN_SENTINELS_NEAREST = {
    "nearest_airport",
    "nearest_airport_within_100km",
    "nearest_airport_within_100 kilometres",
    "nearest_airport_100km",
    "nearest_airport_by_location",
    "nearest_lh_airport",
    "nearest_lufthansa_airport",
}


def _apply_contextual_defaults(
    normalized: Dict[str, Any],
    event: Dict[str, Any],
) -> Dict[str, Any]:
    if not isinstance(normalized, dict) or not normalized:
        return normalized
    session_attrs = event.get("sessionAttributes") or {}
    prompt_attrs = event.get("promptSessionAttributes") or {}
    default_origin = (
        str(session_attrs.get("default_origin") or prompt_attrs.get("default_origin") or "")
        .strip()
        .upper()
    )

    raw_origin = normalized.get("origin")
    if raw_origin:
        origin_text = str(raw_origin).strip()
        origin_lower = origin_text.lower()
        if origin_lower in _ORIGIN_SENTINELS_DEFAULT or origin_lower == "default":
            if default_origin:
                normalized["origin"] = default_origin
                _log(
                    "Context origin substituted",
                    sentinel=origin_text,
                    resolved=default_origin,
                )
            else:
                normalized.pop("origin", None)
                _log(
                    "Context origin sentinel without geolocation default",
                    sentinel=origin_text,
                )
        elif origin_lower in _ORIGIN_SENTINELS_NEAREST:
            if default_origin:
                normalized["origin"] = default_origin
                _log(
                    "Context origin substituted (nearest)",
                    sentinel=origin_text,
                    resolved=default_origin,
                )
            else:
                normalized.pop("origin", None)
                _log(
                    "Context nearest origin sentinel without geolocation default",
                    sentinel=origin_text,
                )
    elif default_origin:
        normalized["origin"] = default_origin
        _log("Context origin filled from defaults", resolved=default_origin)
    return normalized


def _extract_iso_date(value: str) -> Optional[str]:
    text = (value or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    return None


def _extract_month(value: str) -> Optional[str]:
    text = (value or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}", text):
        return text
    return None


def _extract_date_window(body: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Return (month, date_from, date_to) from common field permutations."""
    month = _extract_month(str(body.get("month") or ""))
    date_from = _extract_iso_date(str(body.get("departureDateFrom") or body.get("departureDateStart") or ""))
    date_to = _extract_iso_date(str(body.get("departureDateTo") or body.get("departureDateEnd") or ""))

    if not month and not date_from:
        departure_field = str(body.get("departureDate") or body.get("departureDates") or "").strip()
        if departure_field:
            parts = [part.strip() for part in departure_field.split(",") if part.strip()]
            if len(parts) == 1:
                month_candidate = _extract_month(parts[0])
                if month_candidate:
                    month = month_candidate
                else:
                    single = _extract_iso_date(parts[0])
                    if single:
                        date_from = single
                        date_to = single
            elif len(parts) >= 2:
                first = _extract_iso_date(parts[0])
                second = _extract_iso_date(parts[1])
                if first:
                    date_from = first
                if second:
                    date_to = second

    return month, date_from, date_to


def _to_bool(val: Any, default: bool = False) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        v = val.strip().lower()
        if v in {"1", "true", "t", "yes", "y", "on"}:
            return True
        if v in {"0", "false", "f", "no", "n", "off"}:
            return False
    return default


def _to_int(val: Any, default: int = 1) -> int:
    try:
        return int(val)
    except Exception:
        return default


def _iso_date(val: str) -> Optional[str]:
    if val is None:
        return None
    text = str(val).strip()
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    return None


def _parse_iso_date(val: str) -> Optional[date]:
    try:
        return datetime.strptime(val, "%Y-%m-%d").date()
    except Exception:
        return None
    

def _roll_forward_recent_past_date(date_str: Optional[str], *, threshold: int = 6) -> Optional[str]:
    """If the provided date is within the last `threshold` days, roll it forward by one week.

    This guards against natural-language interpretations like "next Saturday" being resolved
    to the most recent occurrence instead of the upcoming one.
    """
    if not date_str:
        return date_str
    parsed = _parse_iso_date(date_str)
    if parsed is None:
        return date_str
    today = datetime.utcnow().date()
    if parsed < today:
        delta = (today - parsed).days
        if 0 < delta <= threshold:
            adjusted = parsed + timedelta(days=7)
            new_value = adjusted.strftime("%Y-%m-%d")
            _log(
                "Rolled forward recent past date",
                original=date_str,
                adjusted=new_value,
                delta_days=delta,
            )
            return new_value
    return date_str


def _advance_far_past_date(date_str: Optional[str]) -> Optional[str]:
    """For ISO dates far in the past, advance by whole years until it lands in the future.

    Occasionally upstream natural-language parsing produces a prior-year date (for example,
    "next Saturday" resolving to 2023 when the current year is 2025). Rather than failing
    the request outright, promote the date in 1-year increments while ensuring it remains
    within the allowed booking window.
    """

    if not date_str:
        return date_str
    parsed = _parse_iso_date(date_str)
    if parsed is None:
        return date_str
    today = datetime.utcnow().date()
    if parsed >= today:
        return date_str
    max_date = today + timedelta(days=MAX_LOOKAHEAD_DAYS)
    candidate = parsed
    years_added = 0
    while candidate < today:
        years_added += 1
        try:
            candidate = candidate.replace(year=candidate.year + 1)
        except ValueError:
            # Handle leap-day edge cases by stepping back to Feb 28.
            candidate = candidate.replace(month=2, day=28, year=candidate.year + 1)
        if candidate > max_date:
            _log(
                "Advance far past date aborted",
                original=date_str,
                promoted=candidate.isoformat(),
                reason="exceeds_booking_window",
            )
            return date_str
    promoted = candidate.strftime("%Y-%m-%d")
    _log(
        "Advanced far past date",
        original=date_str,
        promoted=promoted,
        years_added=years_added,
    )
    return promoted


def _validate_booking_window(
    departure: str, return_date: Optional[str]
) -> Optional[str]:

    _log("Validate booking window", departure=departure, return_date=return_date)
    today = datetime.utcnow().date()
    max_date = today + timedelta(days=MAX_LOOKAHEAD_DAYS)
    dep = _parse_iso_date(departure)
    if dep is None:
        _log("Validate booking window failed", reason="invalid_departure_format")
        return "Provide 'departureDate' in YYYY-MM-DD."
    if dep < today:
        _log("Validate booking window failed", reason="departure_in_past", departure=departure)
        return "Departure date must be today or later. Please use the TimePhraseParser action group to confirm the correct year."
    if dep > max_date:
        _log("Validate booking window failed", reason="departure_too_far", departure=departure)
        return "Departure date must be within the next 12 months."
    if return_date:
        ret = _parse_iso_date(return_date)
        if ret is None:
            _log("Validate booking window failed", reason="invalid_return_format")
            return "Provide 'returnDate' in YYYY-MM-DD."
        if ret < dep:
            _log("Validate booking window failed", reason="return_before_departure")
            return "Return date must be on or after the departure date."
        if ret > max_date:
            _log("Validate booking window failed", reason="return_too_far")
            return "Return date must be within the next 12 months."
    _log("Validate booking window passed", departure=departure, return_date=return_date)
    return None


def _normalized_iata(s: str) -> Optional[str]:
    if s and re.fullmatch(r"[A-Za-z]{3}", s):
        return s.upper()
    return None


def _get_param(parameters: List[Dict[str, Any]], name: str, default=None):
    for p in parameters or []:
        if p.get("name") == name:
            return p.get("value", default)
    return default


def _props_to_dict(props: List[Dict[str, Any]]) -> Dict[str, Any]:
    out = {}
    for item in props or []:
        name = item.get("name")
        if not isinstance(name, str) or not name:
            continue
        value = item.get("value")
        if isinstance(value, dict) and "value" in value and len(value) == 1:
            value = value["value"]
        out[name] = value
    return out


def _proxy_post(
    path: str, payload: Dict[str, Any], timeout: float = 8.0
) -> Dict[str, Any]:

    url = f"{PROXY_BASE_URL}{path}"
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    req = _urlreq.Request(url, data=data, headers=headers, method="POST")
    _log("Proxy POST request", path=path, url=url, timeout=timeout, payload=payload)
    try:
        with _urlreq.urlopen(req, timeout=timeout) as resp:
            raw_bytes = resp.read()
            text = raw_bytes.decode("utf-8")
            status = getattr(resp, "status", None)
            _log(
                "Proxy POST response received",
                path=path,
                status=status,
                bytes=len(raw_bytes),
            )
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = {"raw": text}
            # Optional S3 debug capture
            if DEBUG_TOOL_IO and _S3_CLIENT and DEBUG_S3_BUCKET:
                try:
                    key = f"{DEBUG_S3_PREFIX}/{datetime.utcnow().strftime('%Y/%m/%d')}/{uuid.uuid4().hex}_post.json"
                    body = json.dumps({
                        "path": path,
                        "url": url,
                        "request_payload": payload,
                        "status": status,
                        "response": parsed,
                    }, default=str).encode("utf-8")
                    _S3_CLIENT.put_object(Bucket=DEBUG_S3_BUCKET, Key=key, Body=body, ContentType="application/json")
                    _log("Tool I/O debug stored", key=key, bytes=len(body))
                except Exception as exc:  # pragma: no cover
                    _log("Tool I/O debug store failed", error=str(exc))
            return parsed if isinstance(parsed, dict) else {"raw": text}
    except _urlerr.HTTPError as e:
        body = (e.read() or b"").decode("utf-8", errors="replace")
        _log(
            "Proxy POST HTTP error",
            path=path,
            status=e.code,
            body_preview=body[:200],
        )
        raise RuntimeError(f"Proxy HTTP {e.code}: {body[:500]}")
    except _urlerr.URLError as e:
        _log("Proxy POST network error", path=path, error=getattr(e, "reason", e))
        raise RuntimeError(f"Proxy network error: {getattr(e, 'reason', e)}")


def _proxy_get(
    path: str, params: Dict[str, str], timeout: float = 5.0
) -> Dict[str, Any]:

    qs = _urlparse.urlencode(params)
    url = f"{PROXY_BASE_URL}{path}"
    if qs:
        url = f"{url}?{qs}"
    headers = {"Accept": "application/json"}
    req = _urlreq.Request(url, headers=headers, method="GET")
    _log("Proxy GET request", path=path, url=url, timeout=timeout, params=params)
    try:
        with _urlreq.urlopen(req, timeout=timeout) as resp:
            raw_bytes = resp.read()
            text = raw_bytes.decode("utf-8")
            status = getattr(resp, "status", None)
            _log(
                "Proxy GET response received",
                path=path,
                status=status,
                bytes=len(raw_bytes),
            )
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = {"raw": text}
            if DEBUG_TOOL_IO and _S3_CLIENT and DEBUG_S3_BUCKET:
                try:
                    key = f"{DEBUG_S3_PREFIX}/{datetime.utcnow().strftime('%Y/%m/%d')}/{uuid.uuid4().hex}_get.json"
                    body = json.dumps({
                        "path": path,
                        "url": url,
                        "request_params": params,
                        "status": status,
                        "response": parsed,
                    }, default=str).encode("utf-8")
                    _S3_CLIENT.put_object(Bucket=DEBUG_S3_BUCKET, Key=key, Body=body, ContentType="application/json")
                    _log("Tool I/O debug stored", key=key, bytes=len(body))
                except Exception as exc:  # pragma: no cover
                    _log("Tool I/O debug store failed", error=str(exc))
            return parsed if isinstance(parsed, dict) else {"raw": text}
    except _urlerr.HTTPError as e:
        body = (e.read() or b"").decode("utf-8", errors="replace")
        _log(
            "Proxy GET HTTP error",
            path=path,
            status=e.code,
            body_preview=body[:200],
        )
        raise RuntimeError(f"Proxy HTTP {e.code}: {body[:500]}")
    except _urlerr.URLError as e:
        _log("Proxy GET network error", path=path, error=getattr(e, "reason", e))
        raise RuntimeError(f"Proxy network error: {getattr(e, 'reason', e)}")


def proxy_lookup_iata(term: str, limit: int = 20) -> List[Dict[str, Any]]:
    text = (term or "").strip()
    if not text:
        _log("IATA lookup skipped (empty term)")
        return []
    _log("IATA lookup via proxy", term=text, limit=limit)
    try:
        response = _proxy_get(
            "/tools/iata/lookup", {"term": text, "limit": str(limit)}
        )
    except Exception as exc:  # pragma: no cover - surface proxy error
        _log("IATA lookup failed", term=text, error=str(exc))
        raise RuntimeError(f"IATA lookup failed: {exc}") from exc
    matches = response.get("matches")
    if isinstance(matches, list):
        _log("IATA lookup success", term=text, matches=len(matches))
        return matches
    _log("IATA lookup response missing 'matches'", term=text)
    return []


def _resolve_iata_code(raw: Any) -> Tuple[Optional[str], List[str]]:
    if raw is None:
        _log("Resolve IATA: no value provided")
        return None, []
    raw_text = str(raw).strip()
    if not raw_text:
        _log("Resolve IATA: empty string provided")
        return None, []
    _log("Resolve IATA: attempting normalization", raw=raw_text)
    normalized = _normalized_iata(raw_text)
    if normalized:
        _log("Resolve IATA: normalized successfully", code=normalized)
        return normalized, []
    try:
        matches = proxy_lookup_iata(raw_text)
    except Exception as exc:
        _log("Resolve IATA: lookup failed", raw=raw_text, error=str(exc))
        return None, []
    codes: List[str] = []
    for match in matches:
        if isinstance(match, dict):
            code = match.get("code")
            if code:
                code_norm = code.upper()
                if code_norm not in codes:
                    codes.append(code_norm)
    if len(codes) == 1:
        resolved = _normalized_iata(codes[0])
        _log("Resolve IATA: unique suggestion", raw=raw_text, code=resolved)
        return resolved, codes
    _log("Resolve IATA: ambiguous suggestions", raw=raw_text, suggestions=codes)
    return None, codes


def amadeus_search_flight_offers(
    origin: str,
    destination: str,
    departure_date: str,
    return_date: Optional[str],
    adults: int,
    cabin: str,
    nonstop: bool,
    currency: Optional[str],
    lh_group_only: bool,
    max_results: int = 10,
    timeout: float = 8.0,
) -> Dict[str, Any]:

    payload: Dict[str, Any] = {
        "originLocationCode": origin,
        "destinationLocationCode": destination,
        "departureDate": departure_date,
        "adults": max(1, adults),
        "nonStop": nonstop,
        "max": max(1, min(50, max_results)),
    }
    travel_class = cabin.upper()
    if travel_class in {"ECONOMY", "PREMIUM_ECONOMY", "BUSINESS", "FIRST"}:
        payload["travelClass"] = travel_class
    if return_date:
        payload["returnDate"] = return_date
    if currency:
        payload["currencyCode"] = currency.upper()
    if lh_group_only:
        payload["includedAirlineCodes"] = ",".join(LH_GROUP_CODES)
    _log(
        "Amadeus search request prepared",
        origin=origin,
        destination=destination,
        departure=departure_date,
        returnDate=return_date,
        adults=adults,
        nonstop=nonstop,
        cabin=cabin,
        currency=currency,
        lhGroupOnly=lh_group_only,
        maxResults=max_results,
    )
    _key = _build_price_key(
        origin,
        destination,
        payload.get("departureDate"),
        payload.get("returnDate"),
        payload.get("adults", 1),
        payload.get("travelClass", "ECONOMY"),
        bool(payload.get("nonStop")),
        payload.get("currencyCode"),
    )
    def _do():
        return _proxy_post("/tools/amadeus/search", payload, timeout=timeout)
    response = _amadeus_search_cached(_key, _do, timeout=timeout)
    if isinstance(response, dict):
        offers = response.get("offers")
        offer_count = len(offers) if isinstance(offers, list) else None
    else:
        offer_count = None
    _log("Amadeus search completed", offers=offer_count)
    return response


def _nearest_date_alternatives(
    origin: str,
    destination: str,
    departure_date: str,
    return_date: Optional[str],
    adults: int,
    cabin: str,
    currency: Optional[str],
    lh_group_only: bool,
    max_results: int,
    *,
    nonstop: bool,
    limit: int = 5,
    timeout: float = 7.0,
) -> List[Dict[str, Any]]:
    """When no offers are found for the requested dates, probe nearby dates and
    return up to `limit` alternative offers for the same route.

    Offsets searched (in days): -1,+1,-2,+2,-3,+3,-7,+7,-14,+14
    
    We allow connections in this fallback to maximize chance of results.
    """
    try:
        dep_dt = datetime.fromisoformat(str(departure_date))
    except Exception:
        return []
    ret_dt: Optional[datetime] = None
    if return_date:
        try:
            ret_dt = datetime.fromisoformat(str(return_date))
        except Exception:
            ret_dt = None

    seen_dates: set[str] = set()
    results: List[Dict[str, Any]] = []
    offsets = [-1, 1, -2, 2, -3, 3, -7, 7, -14, 14, -21, 21, -28, 28]
    for off in offsets:
        if len(results) >= max(1, limit):
            break
        d2 = (dep_dt + timedelta(days=off)).date().isoformat()
        if d2 in seen_dates:
            continue
        seen_dates.add(d2)
        r2 = (ret_dt + timedelta(days=off)).date().isoformat() if ret_dt else None
        try:
            raw = amadeus_search_flight_offers(
                origin,
                destination,
                d2,
                r2,
                adults,
                cabin,
                False,  # allow connections in fallback
                currency,
                lh_group_only,
                max_results,
                timeout,
            )
            offers = _summarize_offers(raw, currency, lh_group_only)
            if offers:
                best = offers[0]
                # brief form to keep payload small
                brief = {
                    "id": best.get("id"),
                    "departureAirport": best.get("departureAirport"),
                    "arrivalAirport": best.get("arrivalAirport"),
                    "departureTime": best.get("departureTime"),
                    "arrivalTime": best.get("arrivalTime"),
                    "duration": best.get("duration"),
                    "stops": best.get("stops"),
                    "carriers": best.get("carriers"),
                    "totalPrice": best.get("totalPrice"),
                    "currency": best.get("currency") or currency,
                }
                results.append({
                    "date": d2,
                    **({"returnDate": r2} if r2 else {}),
                    "offer": brief,
                })
        except Exception as exc:
            _log("Nearest-date alternative probe failed", offset=off, error=str(exc))
            continue
    _log("Nearest-date alternatives prepared", count=len(results))
    return results

def iata_lookup_via_proxy(term: Optional[str]) -> Dict[str, Any]:
    if not term:
        _log("Proxy IATA lookup helper called without term")
        return {"matches": []}
    _log("Proxy IATA lookup helper executing", term=term)
    result = _proxy_get("/tools/iata/lookup", {"term": term}, timeout=5.0)
    matches = result.get("matches")
    count = len(matches) if isinstance(matches, list) else None
    _log("Proxy IATA lookup helper completed", term=term, matches=count)
    return result


def _summarize_offers(
    amadeus_json: Dict[str, Any], currency_hint: Optional[str], lh_group_only: bool = True
) -> List[Dict[str, Any]]:

    # Prefer canonical Amadeus payload under 'raw' if the proxy wraps it; otherwise use provided object
    src = amadeus_json.get("raw") if isinstance(amadeus_json.get("raw"), dict) else amadeus_json
    keys = list(src.keys())
    _log("Summarizing offers (canonical)", keys=keys, currency_hint=currency_hint)

    data = src.get("data") or []
    if not isinstance(data, list) or not data:
        _log("No canonical 'data' list present", has_data=bool(data))
        return []

    # Canonical dictionaries are optional
    dictionaries = src.get("dictionaries") or {}
    carriers_map = dictionaries.get("carriers") or {}

    normalized: List[Dict[str, Any]] = []
    for item in data:
        try:
            price_block = item.get("price") or {}
            total_price = price_block.get("grandTotal") or price_block.get("total")
            currency = price_block.get("currency") or currency_hint
            itineraries = item.get("itineraries") or []

            aggregated_segments: List[Dict[str, Any]] = []
            marketing_carriers: set[str] = set()
            normalized_itins: List[Dict[str, Any]] = []

            for itin in itineraries:
                segs_out: List[Dict[str, Any]] = []
                for s in (itin.get("segments") or []):
                    dep = s.get("departure") or {}
                    arr = s.get("arrival") or {}
                    op = s.get("operating") or {}
                    mkt = s.get("carrierCode")  # canonical marketing carrier
                    if isinstance(mkt, str) and mkt.strip():
                        marketing_carriers.add(mkt.strip().upper())
                    seg_out = {
                        "carrier": mkt,
                        "operatingCarrier": op.get("carrierCode"),
                        "flightNumber": s.get("number"),
                        "from": dep.get("iataCode"),
                        "departureTime": dep.get("at"),
                        "to": arr.get("iataCode"),
                        "arrivalTime": arr.get("at"),
                        "duration": s.get("duration"),
                        "stops": s.get("numberOfStops"),
                    }
                    segs_out.append(seg_out)
                    aggregated_segments.append(seg_out)
                normalized_itins.append({
                    "duration": itin.get("duration"),
                    "segments": segs_out,
                })

            first_segment = aggregated_segments[0] if aggregated_segments else {}
            last_segment = aggregated_segments[-1] if aggregated_segments else {}
            total_duration = item.get("duration") or (normalized_itins[0].get("duration") if normalized_itins else None)
            stop_count = max(len(aggregated_segments) - 1, 0) if aggregated_segments else None

            normalized.append({
                "id": item.get("id"),
                "oneWay": len(itineraries) == 1,
                "totalPrice": total_price,
                "currency": currency,
                "carriers": sorted({c.strip().upper() for c in marketing_carriers if isinstance(c, str)}),
                "segments": aggregated_segments,
                "itineraries": normalized_itins or None,
                "primaryCarrier": (sorted(marketing_carriers)[0] if marketing_carriers else None),
                "departureAirport": first_segment.get("from"),
                "departureTime": first_segment.get("departureTime"),
                "arrivalAirport": last_segment.get("to"),
                "arrivalTime": last_segment.get("arrivalTime"),
                "duration": total_duration,
                "stops": stop_count,
            })
        except Exception as exc:
            _log("Offer normalization failed", error=str(exc))
            continue

    normalized.sort(key=lambda o: float(o.get("totalPrice") or 0))
    _log("Summarized offers (canonical)", count=len(normalized))
    return normalized

def _filter_lh_group_offers(offers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    allowed = set(LH_GROUP_CODES)
    name_to_code = {
        "LUFTHANSA": "LH",
        "SWISS": "LX",
        "SWISS INTERNATIONAL AIR LINES": "LX",
        "AUSTRIAN": "OS",
        "AUSTRIAN AIRLINES": "OS",
        "BRUSSELS AIRLINES": "SN",
        "EUROWINGS": "EW",
        "EUROWINGS DISCOVER": "4Y",
        "DISCOVER AIRLINES": "4Y",
        "AIR DOLOMITI": "EN",
    }

    def _norm_code(v: Any) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        if not s:
            return None
        su = s.upper()
        # two-char IATA code
        if re.fullmatch(r"[A-Z0-9]{2}", su):
            return su
        # sometimes values are names
        if su in name_to_code:
            return name_to_code[su]
        # occasionally concatenated like 'LX 123' or 'LX123'
        m = re.match(r"^([A-Z0-9]{2})\s?\d{1,4}$", su)
        if m:
            return m.group(1)
        return None

    filtered: List[Dict[str, Any]] = []
    for offer in offers:
        codes: set[str] = set()
        for c in (offer.get("carriers") or []):
            code = _norm_code(c)
            if code:
                codes.add(code)
        for seg in offer.get("segments") or []:
            for key in (
                "carrier",
                "carrierCode",
                "marketingCarrier",
                "marketingCarrierCode",
                "operatingCarrier",
                "operatingCarrierCode",
            ):
                val = seg.get(key)
                code = _norm_code(val)
                if code:
                    codes.add(code)
        # If we couldn't determine any codes, keep the offer (avoid false negatives)
        if codes and not codes.issubset(allowed):
            continue
        filtered.append(offer)
    return filtered


def search_best_itineraries(
    origin: str,
    candidates: List[Dict[str, Any]],
    month_range_text: Optional[str],
    *,
    currency: Optional[str] = None,
    lh_group_only: bool = True,
    max_per_destination: int = 2,
    timeout: float = 6.0,
) -> List[Dict[str, Any]]:
    """Search several dates per destination and return Good-Better-Best.

    - Samples up to 3 dates around mid-month (15th, nearest Saturday, +3 days).
    - Ranks offers by price, duration, and stops to produce a varied top set.
    - Adds lightweight microcopy labels.
    """
    (start_y, start_m), _end = _parse_month_range(month_range_text)

    def _iso(y: int, m: int, d: int) -> str:
        return f"{y:04d}-{m:02d}-{d:02d}"

    def _month_len(y: int, m: int) -> int:
        return calendar.monthrange(y, m)[1]

    def _nearest_saturday(y: int, m: int, d: int) -> int:
        from datetime import date as _d
        target = _d(y, m, min(d, _month_len(y, m)))
        # weekday: Monday=0..Sunday=6; Saturday=5
        wd = target.weekday()
        delta = (5 - wd) % 7
        candidate = target.day + delta
        if candidate > _month_len(y, m):
            candidate = max(1, target.day - ((wd - 5) % 7))
        return candidate

    mid = 15
    dates: List[str] = []
    ml = _month_len(start_y, start_m)
    # Weekend-biased  14d elasticity around mid-month
    cands_days = [
        max(1, mid - 14),
        max(1, mid - 7),
        _nearest_saturday(start_y, start_m, max(1, mid - 7)),
        max(1, mid - 3),
        min(ml, mid),
        _nearest_saturday(start_y, start_m, mid),
        min(ml, mid + 3),
        min(ml, mid + 7),
        min(ml, _nearest_saturday(start_y, start_m, min(ml, mid + 7))),
        min(ml, mid + 14),
    ]
    for day in cands_days:
        d = _iso(start_y, start_m, min(ml, max(1, int(day))))
        if d not in dates:
            dates.append(d)

    results: List[Dict[str, Any]] = []
    from datetime import datetime
    started = datetime.utcnow()
    time_budget_s = float(os.getenv("AGGR_TIME_BUDGET_S", "75") or 75)
    api_calls = 0
    max_calls = int(os.getenv("AGGR_MAX_CALLS", "30") or 30)

    def time_left() -> bool:
        return (datetime.utcnow() - started).total_seconds() < time_budget_s

    # Two-phase sampling: first pass on the main weekend date for each destination
    date_indices = list(range(len(dates)))
    for phase in (0, 1):
        idxs = [0] if phase == 0 else date_indices[1:]
        for di in idxs:
            if not time_left():
                break
            for dest in candidates:
                if not time_left() or api_calls >= max_calls:
                    break
                code = dest.get("code")
                if not code:
                    continue
                dep_date = dates[di]
                try:
                    raw = amadeus_search_flight_offers(
                        origin,
                        code,
                        dep_date,
                        None,
                        adults=1,
                        cabin="ECONOMY",
                        nonstop=False,
                        currency=currency,
                        lh_group_only=lh_group_only,
                        max_results=10,
                        timeout=timeout,
                    )
                    api_calls += 1
                    offers = _summarize_offers(raw, currency, lh_group_only)
                    if offers:
                        top = offers[: max(1, max_per_destination)]
                        results.append({"destination": code, "date": dep_date, "offers": top})
                except Exception as exc:
                    _log(
                        "Aggregator search failed for destination",
                        destination=code,
                        date=dep_date,
                        error=str(exc),
                    )
                    continue
            # Early exit if we already have enough pools
            if sum(len(b.get("offers") or []) for b in results) >= 5:
                break

    # Build pool and destination availability counts
    pool: List[Dict[str, Any]] = []
    dest_hits: Dict[str, int] = {}
    for block in results:
        dest_code = block.get("destination")
        date_used = block.get("date")
        if dest_code:
            dest_hits[dest_code] = dest_hits.get(dest_code, 0) + len(block.get("offers") or [])
        for offer in block.get("offers") or []:
            o = dict(offer)
            o["destination"] = dest_code
            o["date"] = date_used
            pool.append(o)

    def _price(o: Dict[str, Any]) -> float:
        try:
            return float(o.get("totalPrice") or 0)
        except Exception:
            return 0.0

    def _duration_minutes(dur: Optional[str]) -> Optional[int]:
        if not isinstance(dur, str) or not dur.startswith("PT"):
            return None
        # Parse very simple PT#H#M or PT#H
        h = 0
        m = 0
        text = dur[2:]
        try:
            if "H" in text and "M" in text:
                h_part, m_part = text.split("H", 1)
                h = int(h_part or 0)
                m = int(m_part.replace("M", "") or 0)
            elif "H" in text:
                h = int(text.replace("H", "") or 0)
            elif "M" in text:
                m = int(text.replace("M", "") or 0)
            else:
                return None
            return h * 60 + m
        except Exception:
            return None

    def _stops(o: Dict[str, Any]) -> int:
        v = o.get("stops")
        try:
            return int(v)
        except Exception:
            return 0

    # Rankers (availability-first bias and gentle nonstop boost)
    by_price = sorted(
        pool,
        key=lambda o: (
            _price(o),
            _stops(o),
            -(dest_hits.get(str(o.get("destination") or ""), 0)),
        ),
    )
    with_dur = [o for o in pool if _duration_minutes(o.get("duration")) is not None]
    by_dur = sorted(with_dur, key=lambda o: _duration_minutes(o.get("duration")) or 10**9)
    by_flex = sorted(pool, key=lambda o: (_stops(o), _price(o), -(dest_hits.get(str(o.get("destination") or ""), 0))))

    # Composite "best" rank: price + stops*50 + duration*0.1 - dest_hits*10
    def _rank(o: Dict[str, Any]) -> float:
        pr = _price(o)
        st = _stops(o)
        dm = _duration_minutes(o.get("duration")) or 0
        avail = dest_hits.get(str(o.get("destination") or ""), 0)
        return pr + st * 50 + dm * 0.1 - avail * 10

    by_best = sorted(pool, key=_rank)

    # Select Good-Better-Best ensuring uniqueness
    picked: List[Dict[str, Any]] = []
    seen = set()

    def _key(o: Dict[str, Any]) -> str:
        return f"{o.get('destination')}|{o.get('id') or o.get('departureTime')}|{o.get('date')}"

    def _pick_from(lst: List[Dict[str, Any]]):
        for o in lst:
            k = _key(o)
            if k not in seen:
                seen.add(k)
                picked.append(o)
                return

    _pick_from(by_best)
    _pick_from(by_dur)
    _pick_from(by_flex)
    # Fill up to 5
    for o in by_best:
        if len(picked) >= 5:
            break
        if _key(o) not in seen:
            seen.add(_key(o))
            picked.append(o)

    # Add labels and microcopy
    label_map = ["Best Value", "Shortest Travel Time", "Flex"]
    options: List[Dict[str, Any]] = []
    # Quick lookup for tags by destination
    tag_lookup = {str(d.get("code")): set(str(t).lower() for t in (d.get("tags") or [])) for d in candidates}

    def _pitch(tags: set[str]) -> str:
        if "beach" in tags or "warm" in tags:
            return "Great value for a sunny beach break."
        if "winter_sports" in tags:
            return "Maximize slope time with sensible travel."
        if "city_break" in tags:
            return "Perfect for a quick city escape."
        return "Solid choice based on your preferences."

    for idx, o in enumerate(picked):
        code = str(o.get("destination") or "")
        tags = tag_lookup.get(code, set())
        label = label_map[idx] if idx < len(label_map) else "Also Noteworthy"
        options.append({
            "label": label,
            "pitch": _pitch(tags),
            "destination": code,
            "date": o.get("date"),
            "offer": o,
            "stops": _stops(o),
        })

    return options


# ------------------------ Response wrappers ------------------------


def _wrap_openapi(
    event: Dict[str, Any],
    status: int,
    body_obj: Dict[str, Any],
    logger: Optional[InvocationLogger] = None,
) -> Dict[str, Any]:

    if logger is None:
        logger = _get_logger()
    # Avoid bloating action-group responses unless explicitly debugging
    payload = body_obj
    if DEBUG_TOOL_IO:
        try:
            payload = _attach_logs(body_obj, logger)
        except Exception:
            payload = body_obj
    response_body = {"application/json": {"body": json.dumps(payload)}}
    action_response = {
        "actionGroup": event.get("actionGroup"),
        "apiPath": event.get("apiPath"),
        "httpMethod": event.get("httpMethod"),
        "httpStatusCode": status,
        "responseBody": response_body,
    }
    _log("OpenAPI response wrapped", status=status)
    return {
        "messageVersion": "1.0",
        "response": action_response,
        "sessionAttributes": event.get("sessionAttributes", {}),
        "promptSessionAttributes": event.get("promptSessionAttributes", {}),
    }


def _wrap_function(
    event: Dict[str, Any],
    status: int,
    body_obj: Dict[str, Any],
    logger: Optional[InvocationLogger] = None,
) -> Dict[str, Any]:

    if logger is None:
        logger = _get_logger()
    response_payload = {
        "status": status,
        "data": body_obj,
    }
    # Avoid large responses causing agent runtime failures: omit verbose logs for heavy functions
    func_name = str(event.get("function") or "").strip().lower()
    if func_name not in {"recommend_destinations"}:
        response_payload = _attach_logs(response_payload, logger)
    # Surface a plain text body so the agent renders lists immediately
    text_body: str = ""
    try:
        # Prefer a direct 'message' field provided by the handler body
        if isinstance(body_obj, dict):
            msg = body_obj.get("message")
            if isinstance(msg, str) and msg.strip():
                text_body = msg.strip()
        if not text_body:
            # Fallback to a compact JSON string
            text_body = json.dumps(response_payload)
    except Exception:
        try:
            text_body = json.dumps(response_payload)
        except Exception:
            text_body = str(response_payload)
    _log("Function response wrapped", status=status, function=event.get("function"))
    # Resolve action group for function-details responses. Event may not include actionGroup.
    # Use env-configured names to avoid drift with Bedrock agent setup.
    action_group_default = ACTION_GROUP_NAME
    action_group_reco = ACTION_GROUP_RECOMMENDER
    func_for_group = func_name  # already lower-cased above
    action_group_val = (
        event.get("actionGroup")
        or (action_group_reco if func_for_group == "recommend_destinations" else action_group_default)
    )
    return {
        "messageVersion": "1.0",
        "response": {
            "actionGroup": action_group_val,
            "function": event.get("function", "search_flights"),
            "functionResponse": {
                "responseBody": {
                    # For function calls, Agents expect TEXT only
                    "TEXT": {"body": text_body},
                }
            },
        },
        "sessionAttributes": event.get("sessionAttributes", {}),
        "promptSessionAttributes": event.get("promptSessionAttributes", {}),
    }


# ------------------------ Handlers ------------------------


def _handle_openapi(event: Dict[str, Any]) -> Dict[str, Any]:
    # NEW: detect IATA lookup path first
    api_path_raw = (event.get("apiPath") or "").strip()
    api_path_lower = api_path_raw.lower()
    http_method = (event.get("httpMethod") or "POST").upper()
    _log("Handling OpenAPI event", api_path=api_path_lower or "<none>", method=http_method)
    if api_path_lower == "/iata/lookup":
        props = (
            event.get("requestBody", {})
            .get("content", {})
            .get("application/json", {})
            .get("properties", [])
        )
        body = _props_to_dict(props)
        term = (
            body.get("term")
            or body.get("code")
            or body.get("q")
            or body.get("query")
        )
        _log("OpenAPI IATA lookup request parsed", term=term)
        try:
            data = iata_lookup_via_proxy(term)
            _log("OpenAPI IATA lookup success", term=term, count=len(data.get("matches", [])))
            return _wrap_openapi(
                event,
                200,
                {
                    "generatedAt": _now_iso(),
                    "query": {"term": term},
                    "result": data,
                },
            )
        except Exception as e:
            _log("OpenAPI IATA lookup error", term=term, error=str(e))
            return _wrap_openapi(event, 502, {"error": str(e)[:1200]})

    # (existing flight-search code continues below)
    props = (
        event.get("requestBody", {})
        .get("content", {})
        .get("application/json", {})
        .get("properties", [])
    )
    body = _props_to_dict(props)
    params = event.get("parameters")
    if not body:
        _log("OpenAPI request body empty, using parameters", param_type=type(params).__name__)
        if isinstance(params, list) and params:
            sample = []
            for entry in params[:3]:
                if isinstance(entry, dict):
                    sample.append({k: entry.get(k) for k in ("name", "value")})
            if sample:
                _log("OpenAPI parameters sample", sample=sample)
        body = _props_to_dict(params if isinstance(params, list) else [])
    _log("OpenAPI request body parsed", keys=list(body.keys()))
    params_map = _props_to_dict(params if isinstance(params, list) else [])

    if api_path_lower == "/tools/s3escalator":
        allowed_keys = {
            "type",
            "path",
            "sender",
            "fileName",
            "fileEncoding",
            "contentType",
            "file",
            "fileBase64",
            "fileData",
        }
        payload: Dict[str, Any] = {}
        for key in allowed_keys:
            val = body.get(key)
            if val is None:
                val = params_map.get(key)
            if val is not None:
                payload[key] = val
        required_missing = [k for k in ("type", "path", "sender") if not str(payload.get(k) or "").strip()]
        if required_missing:
            _log("Escalator upload missing required fields", missing=required_missing)
            return _wrap_openapi(
                event,
                400,
                {"error": f"Missing required fields: {', '.join(required_missing)}"},
            )
        if not any(payload.get(k) for k in ("file", "fileBase64", "fileData")):
            return _wrap_openapi(
                event,
                400,
                {"error": "Provide a UTF-8 `file` or Base64 payload (`fileBase64`/`fileData`)."},
            )
        try:
            result = _proxy_post("/tools/s3escalator", payload, timeout=15.0)
        except Exception as exc:
            _log("Escalator upload failed", error=str(exc))
            return _wrap_openapi(event, 502, {"error": f"Upload failed: {exc}"})
        key = result.get("key")
        msg = result.get("message")
        if not msg and key:
            msg = f"Uploaded payload to {key}."
        if not msg:
            msg = "Upload completed successfully."
        return _wrap_openapi(event, 200, {"message": msg, "result": result})

    if api_path_lower == "/tools/give_me_tools":
        try:
            result = _proxy_get("/tools/give_me_tools", {}, timeout=4.0)
        except Exception as exc:
            _log("give_me_tools fetch failed", error=str(exc))
            return _wrap_openapi(event, 502, {"error": f"Unable to retrieve tool catalog: {exc}"})
        tools = result.get("tools") if isinstance(result, dict) else None
        count = len(tools) if isinstance(tools, list) else 0
        message = f"Proxy reports {count} tool(s)." if tools is not None else "Tool catalog retrieved."
        return _wrap_openapi(event, 200, {"message": message, "result": result})

    if api_path_lower == "/tools/antiphaser":
        source = params_map if http_method == "GET" else body
        phrase = str(source.get("phrase") or source.get("text") or "").strip()
        if not phrase:
            return _wrap_openapi(event, 400, {"error": "Provide 'phrase' to interpret."})
        timezone = (source.get("timezone") or source.get("timeZone") or "").strip()
        reference = (source.get("referenceDate") or source.get("reference") or "").strip()
        payload = {"phrase": phrase}
        if timezone:
            payload["timezone"] = timezone
        if reference:
            payload["referenceDate"] = reference
        try:
            if http_method == "GET":
                query = {k: v for k, v in payload.items() if v}
                result = _proxy_get("/tools/antiPhaser", query, timeout=6.0)
            else:
                result = _proxy_post("/tools/antiPhaser", payload, timeout=6.0)
        except Exception as exc:
            _log("antiPhaser request failed", error=str(exc))
            return _wrap_openapi(event, 502, {"error": f"antiPhaser failed: {exc}"})
        iso = result.get("isoDate") or result.get("date")
        message = f"Phrase '{phrase}' parsed to {iso}." if iso else f"Phrase '{phrase}' processed."
        return _wrap_openapi(event, 200, {"message": message, "result": result})

    if api_path_lower == "/tools/derdrucker/wannacandy":
        if not body:
            return _wrap_openapi(event, 400, {"error": "Provide offer payload to format."})
        try:
            result = _proxy_post("/tools/derDrucker/wannaCandy", body, timeout=12.0)
        except Exception as exc:
            _log("derDrucker wannaCandy failed", error=str(exc))
            return _wrap_openapi(event, 502, {"error": f"Markdown generation failed: {exc}"})
        message = result.get("summary") or result.get("message") or "Itinerary Markdown ready."
        return _wrap_openapi(event, 200, {"message": message, "result": result})

    if api_path_lower == "/tools/derdrucker/generatetickets":
        if not body:
            return _wrap_openapi(event, 400, {"error": "Provide ticket generation payload."})
        try:
            result = _proxy_post("/tools/derDrucker/generateTickets", body, timeout=20.0)
        except Exception as exc:
            _log("derDrucker generateTickets failed", error=str(exc))
            return _wrap_openapi(event, 502, {"error": f"Ticket generation failed: {exc}"})
        message = result.get("message") or "PDF tickets generated."
        return _wrap_openapi(event, 200, {"message": message, "result": result})

    if api_path_lower == "/tools/datetime/interpret":
        phrase = str(body.get("phrase") or params_map.get("phrase") or "").strip()
        if not phrase:
            return _wrap_openapi(event, 400, {"error": "Provide 'phrase' to interpret."})
        payload = {"phrase": phrase}
        reference = body.get("referenceDate") or params_map.get("referenceDate")
        timezone = (
            body.get("timeZone")
            or body.get("timezone")
            or params_map.get("timeZone")
            or params_map.get("timezone")
        )
        if reference:
            payload["referenceDate"] = reference
        if timezone:
            payload["timeZone"] = timezone
        try:
            result = _proxy_post("/tools/datetime/interpret", payload, timeout=6.0)
        except Exception as exc:
            _log("proxy datetime interpret failed", error=str(exc))
            return _wrap_openapi(event, 502, {"error": f"Datetime interpretation failed: {exc}"})
        iso = result.get("isoDate")
        message = f"Phrase '{phrase}' interpreted as {iso}." if iso else f"Phrase '{phrase}' interpreted."
        return _wrap_openapi(event, 200, {"message": message, "result": result})

    normalized = _normalize_flight_request_fields(body)
    had_origin_before_context = bool((normalized or {}).get("origin"))
    normalized = _apply_contextual_defaults(normalized, event)
    if normalized:
        _log("OpenAPI normalized flight fields", normalized=normalized)
    if api_path_lower == "/tools/iata/lookup":
        term = body.get("term")
        if not term or (isinstance(term, str) and term.strip().lower() in {"nearest airport", "closest airport", "nearest", "closest", "nearest airport to my location", "closest airport to me"}):
            # Prefer default_origin (IATA code) over any label when substituting 'nearest/closest'
            ctx = event.get("promptSessionAttributes") or event.get("sessionAttributes") or {}
            code = str(ctx.get("default_origin") or "").strip().upper()
            label = ctx.get("default_origin_label")
            if code:
                _log("IATA lookup: substituting nearest/closest with default_origin code", code=code)
                term = code
        if not term:
            _log("OpenAPI validation error", reason="missing_term")
            return _wrap_openapi(
                event,
                400,
                {"error": "Provide 'term' to perform an IATA lookup."},
            )
        # If the term is already a three-letter IATA code, return it directly
        if isinstance(term, str) and re.fullmatch(r"[A-Z]{3}", term.strip().upper()):
            return _wrap_openapi(event, 200, {"matches": [{"code": term.strip().upper()}]})
        try:
            matches = proxy_lookup_iata(term)
        except Exception as exc:
            _log("OpenAPI proxy IATA lookup failed", term=term, error=str(exc))
            return _wrap_openapi(event, 502, {"error": f"IATA lookup failed: {exc}"})
        _log("OpenAPI proxy IATA lookup success", term=term, matches=len(matches))
        return _wrap_openapi(event, 200, {"matches": matches})

    if api_path_lower == "/tools/amadeus/dates":
        origin = (
            body.get("origin")
            or body.get("originLocationCode")
            or body.get("origin_code")
            or ""
        )
        destination = (
            body.get("destination")
            or body.get("destinationLocationCode")
            or body.get("destination_code")
            or ""
        )
        origin = str(origin).strip().upper()
        destination = str(destination).strip().upper()
        month, date_from, date_to = _extract_date_window(body)
        one_way = bool(body.get("oneWay", False))
        non_stop = bool(body.get("nonStop", False))
        currency = (body.get("currencyCode") or "").strip().upper()
        limit = body.get("limit")
        try:
            limit_n = int(limit) if limit is not None else 3
        except Exception:
            limit_n = 3
        # Pricing controls
        price_top = bool(body.get("priceTop", False)) or bool(body.get("price", False))
        price_limit = body.get("priceLimit")
        try:
            price_limit_n = int(price_limit) if price_limit is not None else limit_n
        except Exception:
            price_limit_n = limit_n
        price_limit_n = max(1, min(10, price_limit_n))
        adults = _to_int(body.get("adults", 1), 1)
        cabin = (body.get("cabin") or body.get("travelClass") or "ECONOMY").upper()

        params: Dict[str, str] = {
            "origin": origin,
            "destination": destination,
            "oneWay": str(bool(one_way)).lower(),
            "nonStop": str(bool(non_stop)).lower(),
        }
        if month:
            params["month"] = month
        if date_from and date_to:
            params["departureDate"] = f"{date_from},{date_to}" if date_from != date_to else date_from
        elif date_from:
            params["departureDate"] = date_from
        elif date_to:
            params["departureDate"] = date_to
        params["limit"] = str(max(1, min(10, limit_n)))
        if currency:
            params["currencyCode"] = currency
        max_price = body.get("maxPrice")
        if max_price not in (None, ""):
            try:
                max_price_val = int(max_price)
                if max_price_val > 0:
                    params["maxPrice"] = str(max_price_val)
            except Exception:
                pass
        view_by = (body.get("viewBy") or "").strip().upper()
        if view_by in {"DATE", "DURATION", "WEEK"}:
            params["viewBy"] = view_by
        # Validate IATA quickly (proxy will re-validate)
        if not re.fullmatch(r"[A-Z]{3}", origin or "") or not re.fullmatch(r"[A-Z]{3}", destination or ""):
            _log("OpenAPI validation error", reason="invalid_iata", origin=origin, destination=destination)
            return _wrap_openapi(event, 400, {"error": "invalid_iata"})
        try:
            resp = _proxy_get("/tools/amadeus/dates", params, timeout=7.0)
        except Exception as exc:
            _log("OpenAPI amadeus dates error", error=str(exc))
            return _wrap_openapi(event, 502, {"error": f"amadeus_dates_failed: {exc}"})

        if not price_top:
            # Return proxy-normalized calendar and top suggestions only
            return _wrap_openapi(event, 200, resp if isinstance(resp, dict) else {"raw": resp})

        # If pricing requested but not one-way, keep it simple: return calendar with a hint
        if not one_way:
            out = resp if isinstance(resp, dict) else {"raw": resp}
            if isinstance(out, dict):
                out["note"] = "Pricing top days currently supports oneWay=true only."
            return _wrap_openapi(event, 200, out)

        # Price up to N top departure dates (LH Group only)
        top_days = []
        if isinstance(resp, dict):
            top_days = resp.get("top") or []
        priced: List[Dict[str, Any]] = []
        for item in top_days[: price_limit_n]:
            d = (item.get("date") if isinstance(item, dict) else None) or ""
            d_iso = _iso_date(str(d))
            if not d_iso:
                continue
            try:
                raw = amadeus_search_flight_offers(
                    origin,
                    destination,
                    d_iso,
                    None,
                    adults,
                    cabin,
                    non_stop,
                    currency or None,
                    True,  # enforce LH Group only at pricing
                    10,
                )
                offers = _summarize_offers(raw, currency or None, True)
            except Exception as exc:
                _log("Flexible dates pricing error", date=d_iso, error=str(exc))
                offers = []
            priced.append({
                "departureDate": d_iso,
                "offers": offers,
            })
        out = resp if isinstance(resp, dict) else {"raw": resp}
        if isinstance(out, dict):
                out["priced"] = priced
        return _wrap_openapi(event, 200, out)

    if api_path_lower == "/tools/amadeus/flex":
        origin = (
            body.get("origin")
            or body.get("originLocationCode")
            or body.get("origin_code")
            or ""
        )
        destination = (
            body.get("destination")
            or body.get("destinationLocationCode")
            or body.get("destination_code")
            or ""
        )
        origin = str(origin).strip().upper()
        destination = str(destination).strip().upper()
        month, date_from, date_to = _extract_date_window(body)
        one_way = bool(body.get("oneWay", True))
        non_stop = bool(body.get("nonStop", False))
        currency = (body.get("currencyCode") or "").strip().upper()
        limit = body.get("limit")
        try:
            limit_n = int(limit) if limit is not None else 5
        except Exception:
            limit_n = 5
        price_limit = body.get("priceLimit")
        try:
            price_limit_n = int(price_limit) if price_limit is not None else limit_n
        except Exception:
            price_limit_n = limit_n
        price_limit_n = max(1, min(10, price_limit_n))
        adults = _to_int(body.get("adults", 1), 1)
        cabin = (body.get("cabin") or body.get("travelClass") or "ECONOMY").upper()

        # Validate IATA quickly (proxy will re-validate)
        if not re.fullmatch(r"[A-Z]{3}", origin or "") or not re.fullmatch(r"[A-Z]{3}", destination or ""):
            _log("OpenAPI validation error", reason="invalid_iata", origin=origin, destination=destination)
            return _wrap_openapi(event, 400, {"error": "invalid_iata"})

        params: Dict[str, str] = {
            "origin": origin,
            "destination": destination,
            "oneWay": str(bool(one_way)).lower(),
            "nonStop": str(bool(non_stop)).lower(),
            "limit": str(max(1, min(10, limit_n))),
        }
        if currency:
            params["currencyCode"] = currency
        if month:
            params["month"] = month
        if date_from and date_to:
            params["departureDate"] = f"{date_from},{date_to}" if date_from != date_to else date_from
        elif date_from:
            params["departureDate"] = date_from
        elif date_to:
            params["departureDate"] = date_to
        max_price = body.get("maxPrice")
        if max_price not in (None, ""):
            try:
                max_price_val = int(max_price)
                if max_price_val > 0:
                    params["maxPrice"] = str(max_price_val)
            except Exception:
                pass
        view_by = (body.get("viewBy") or "").strip().upper()
        if view_by in {"DATE", "DURATION", "WEEK"}:
            params["viewBy"] = view_by

        # Step 1: calendar (proxy)
        try:
            cal = _proxy_get("/tools/amadeus/dates", params, timeout=7.0)
        except Exception as exc:
            _log("OpenAPI flex: calendar error", error=str(exc))
            return _wrap_openapi(event, 502, {"error": f"amadeus_dates_failed: {exc}"})

        # Step 2: pricing (one-way only)
        if not one_way:
            # Do not return unfiltered calendar. Require oneWay or explicit dates.
            return _wrap_openapi(event, 400, {"error": "one_way_required", "message": "Set oneWay=true or provide explicit departure/return dates to return priced results."})

        top_days = []
        if isinstance(cal, dict):
            top_days = cal.get("top") or []
        priced: List[Dict[str, Any]] = []
        for item in top_days[: price_limit_n]:
            d = (item.get("date") if isinstance(item, dict) else None) or ""
            d_iso = _iso_date(str(d))
            if not d_iso:
                continue
            try:
                raw = amadeus_search_flight_offers(
                    origin,
                    destination,
                    d_iso,
                    None,
                    adults,
                    cabin,
                    non_stop,
                    currency or None,
                    True,  # LH Group only at pricing
                    10,
                )
                offers = _summarize_offers(raw, currency or None, True)
            except Exception as exc:
                _log("OpenAPI flex: pricing error", date=d_iso, error=str(exc))
                offers = []
            priced.append({
                "departureDate": d_iso,
                "offers": offers,
            })
        # Return priced results only; do not include unfiltered calendar in response
        return _wrap_openapi(event, 200, {
            "generatedAt": _now_iso(),
            "query": {
                "origin": origin,
                "destination": destination,
                "oneWay": True,
                "nonstop": non_stop,
                "adults": adults,
                "cabin": cabin,
                "currency": currency or None,
                "lhGroupOnly": True,
                "priceLimit": price_limit_n,
            },
            "priced": priced,
            "provider": "Amadeus Flight Cheapest Date + Flight Offers Search v2 (LH-only)",
        })

    raw_origin = normalized.get("origin")
    raw_destination = normalized.get("destination")
    origin, origin_suggestions = _resolve_iata_code(raw_origin)
    filled_from_context = (not had_origin_before_context) and bool(origin)
    destination, destination_suggestions = _resolve_iata_code(raw_destination)
    departure_input = normalized.get("departureDate")
    departure_date = _iso_date(str(departure_input) if departure_input is not None else "")
    return_input = normalized.get("returnDate")
    return_date = _iso_date(str(return_input)) if return_input else None
    adults = _to_int(normalized.get("adults", 1), 1)
    cabin = (normalized.get("cabin") or "ECONOMY").upper()
    nonstop = _to_bool(normalized.get("nonstop", False), False)
    currency = (normalized.get("currency") or os.getenv(
        "DEFAULT_CURRENCY") or "EUR").upper()
    # Always enforce LH group via Amadeus (includedAirlineCodes) and do not post-filter
    lh_group_only = True
    max_results = _to_int(normalized.get("max", 10), 10)
    _log(
        "OpenAPI flight request prepared",
        origin=origin,
        destination=destination,
        departureDate=departure_date,
        returnDate=return_date,
        adults=adults,
        nonstop=nonstop,
        cabin=cabin,
        currency=currency,
        lhGroupOnly=lh_group_only,
        max=max_results,
    )
    if not origin:
        msg = "Please choose a departure airport IATA code (for example, MUC)."
        suggestions = origin_suggestions or []
        _log("OpenAPI validation message", reason="missing_origin", suggestions=suggestions)
        return _wrap_openapi(
            event,
            200,
            {"message": msg, "suggestions": suggestions},
        )
    if not destination:
        msg = "Please choose an arrival airport IATA code (for example, ZRH)."
        suggestions = destination_suggestions or []
        _log(
            "OpenAPI validation message",
            reason="missing_destination",
            suggestions=suggestions,
        )
        return _wrap_openapi(
            event,
            200,
            {"message": msg, "suggestions": suggestions},
        )
    if not departure_date:
        _log("OpenAPI validation error", reason="missing_departure_date")
        return _wrap_openapi(
            event, 400, {"error": "Provide 'departureDate' in YYYY-MM-DD."}
        )
    departure_date = _roll_forward_recent_past_date(departure_date) or departure_date
    departure_date = _advance_far_past_date(departure_date) or departure_date
    if return_date:
        return_date = _roll_forward_recent_past_date(return_date) or return_date
        return_date = _advance_far_past_date(return_date) or return_date
    window_error = _validate_booking_window(departure_date, return_date)
    if window_error:
        _log("OpenAPI validation error", reason="window_error", detail=window_error)
        return _wrap_openapi(event, 400, {"error": window_error})
    try:
        raw = amadeus_search_flight_offers(
            origin,
            destination,
            departure_date,
            return_date,
            adults,
            cabin,
            nonstop,
            currency,
            lh_group_only,
            max_results,
        )
        offers = _summarize_offers(raw, currency, lh_group_only)
        # Keep LH group restriction; do not relax to other carriers
        # Fallback: if nonstop requested and none found, retry with connections allowed.
        if nonstop and not offers:
            _log("OpenAPI: No nonstop offers; retrying with connections allowed")
            raw = amadeus_search_flight_offers(
                origin,
                destination,
                departure_date,
                return_date,
                adults,
                cabin,
                False,
                currency,
                lh_group_only,
                max_results,
            )
            offers = _summarize_offers(raw, currency, lh_group_only)
        # If still none, probe nearest alternative dates and include up to 5.
        alternatives: List[Dict[str, Any]] = []
        if not offers:
            alternatives = _nearest_date_alternatives(
                origin,
                destination,
                departure_date,
                return_date,
                adults,
                cabin,
                currency,
                lh_group_only,
                max_results,
                nonstop=nonstop,
                limit=5,
            )
        _log("OpenAPI flight search success", origin=origin, destination=destination, offers=len(offers), alternatives=len(alternatives))
        # If we auto-filled origin from context, cache it into session and surface a brief note.
        note = None
        if filled_from_context and origin:
            try:
                event.setdefault("sessionAttributes", {})["default_origin"] = origin
            except Exception:
                pass
            note = f"Using your nearest airport as departure location ({origin}). Say 'change departure location' to update it."
        # Build textual summary for offers so the agent surfaces concrete details
        offer_text_block = None
        if offers:
            try:
                def _hhmm(ts: Optional[str]) -> str:
                    try:
                        if not ts:
                            return "?"
                        t = str(ts).replace("T", " ").replace("Z", "").split(" ")[1]
                        return t[:5]
                    except Exception:
                        return "?"
                # Partition into direct and connecting; enforce max 10 total
                direct = [o for o in offers if (o.get("stops") == 0)]
                conn = [o for o in offers if (o.get("stops") and o.get("stops") > 0) or o.get("stops") is None]
                max_total = min(10, RECOMMENDER_MAX_OPTIONS)
                take_direct = min(len(direct), max_total)
                take_conn = min(len(conn), max_total - take_direct)
                lines2: List[str] = []
                def _fmt_price(val: Any, curr: Optional[str]) -> str:
                    try:
                        return f"{float(val):.0f} {curr or ''}".strip() if val is not None else "?"
                    except Exception:
                        return f"{val} {curr or ''}".strip()
                if take_direct:
                    lines2.append("Direct Flights")
                    for idx, off in enumerate(direct[:take_direct], start=1):
                        dep2 = off.get("departureAirport") or "?"
                        arr2 = off.get("arrivalAirport") or "?"
                        dt2 = _hhmm(off.get("departureTime"))
                        dur2 = off.get("duration") or "?"
                        price2 = _fmt_price(off.get("totalPrice"), off.get("currency") or currency)
                        segs2 = off.get("segments")
                        seg02 = segs2[0] if isinstance(segs2, list) and segs2 else None
                        c02 = (seg02.get("carrier") or seg02.get("marketingCarrier") or seg02.get("operatingCarrier")) if isinstance(seg02, dict) else None
                        fn02 = (seg02.get("flightNumber") if isinstance(seg02, dict) else None) or ""
                        c02fn = f"{c02}{fn02}".strip() if (c02 or fn02) else None
                        parts2 = [f"{idx}) {dep2} {dt2}", "->", f"{arr2}", "|", departure_date, "|", "nonstop", "|", dur2]
                        if c02fn:
                            parts2.extend(["|", c02fn])
                        parts2.extend(["|", f"**{price2}**"])
                        lines2.append(" ".join(str(p) for p in parts2 if str(p)))
                        if isinstance(segs2, list) and segs2:
                            seg_limit = len(segs2) if return_date else 1
                            for s in segs2[:seg_limit]:
                                try:
                                    sc = s.get("carrier") or s.get("marketingCarrier") or s.get("operatingCarrier") or "?"
                                    sfn = s.get("flightNumber") or ""
                                    sfrom = s.get("from") or "?"
                                    sdt = _hhmm(s.get("departureTime") or s.get("depTime"))
                                    sto = s.get("to") or "?"
                                    sat = _hhmm(s.get("arrivalTime") or s.get("arrTime"))
                                    lines2.append(f"    - THEN {sc}{sfn} {sfrom} {sdt} -> {sto} {sat}")
                                except Exception:
                                    continue
                if take_conn:
                    if take_direct:
                        lines2.append("")
                    lines2.append("Connecting Flights")
                    for idx, off in enumerate(conn[:take_conn], start=1):
                        dep2 = off.get("departureAirport") or "?"
                        arr2 = off.get("arrivalAirport") or "?"
                        dt2 = _hhmm(off.get("departureTime"))
                        dur2 = off.get("duration") or "?"
                        price2 = _fmt_price(off.get("totalPrice"), off.get("currency") or currency)
                        stops2 = off.get("stops")
                        stops_txt2 = "nonstop" if stops2 == 0 else (f"{stops2} stop" if stops2 == 1 else f"{stops2} stops")
                        segs2 = off.get("segments")
                        seg02 = segs2[0] if isinstance(segs2, list) and segs2 else None
                        c02 = (seg02.get("carrier") or seg02.get("marketingCarrier") or seg02.get("operatingCarrier")) if isinstance(seg02, dict) else None
                        fn02 = (seg02.get("flightNumber") if isinstance(seg02, dict) else None) or ""
                        c02fn = f"{c02}{fn02}".strip() if (c02 or fn02) else None
                        parts2 = [f"{idx}) {dep2} {dt2}", "->", f"{arr2}", "|", departure_date, "|", stops_txt2, "|", dur2]
                        if c02fn:
                            parts2.extend(["|", c02fn])
                        parts2.extend(["|", f"**{price2}**"])
                        lines2.append(" ".join(str(p) for p in parts2 if str(p)))
                        if isinstance(segs2, list) and segs2:
                            seg_limit = len(segs2) if return_date else 3
                            for s in segs2[:seg_limit]:
                                try:
                                    sc = s.get("carrier") or s.get("marketingCarrier") or s.get("operatingCarrier") or "?"
                                    sfn = s.get("flightNumber") or ""
                                    sfrom = s.get("from") or "?"
                                    sdt = _hhmm(s.get("departureTime") or s.get("depTime"))
                                    sto = s.get("to") or "?"
                                    sat = _hhmm(s.get("arrivalTime") or s.get("arrTime"))
                                    lines2.append(f"    - THEN {sc}{sfn} {sfrom} {sdt} -> {sto} {sat}")
                                except Exception:
                                    continue
                offer_text_block = "\n".join(lines2)
            except Exception:
                offer_text_block = None
        # Build textual alternatives list if applicable (partitioned, compact, PDF-compatible)
        def _alt_sections(alts: List[Dict[str, Any]]) -> List[str]:
            lines: List[str] = ["No offers on requested dates. Nearby alternatives:"]
            direct = [a for a in alts if ((a.get("offer") or {}).get("stops") == 0)]
            conn = [a for a in alts if ((a.get("offer") or {}).get("stops") or 0) > 0]
            max_total = min(10, RECOMMENDER_MAX_OPTIONS)
            take_direct = min(len(direct), max_total)
            take_conn = min(len(conn), max_total - take_direct)
            def _fmt_price(val: Any, curr: Optional[str]) -> str:
                try:
                    return f"{float(val):.0f} {curr or ''}".strip() if val is not None else "?"
                except Exception:
                    return f"{val} {curr or ''}".strip()
            def _hhmm2(ts: Optional[str]) -> str:
                try:
                    if not ts:
                        return "?"
                    t = str(ts).replace("T", " ").replace("Z", "").split(" ")[1]
                    return t[:5]
                except Exception:
                    return "?"
            if take_direct:
                lines.append("Direct Alternatives")
                for idx, alt in enumerate(direct[:take_direct], start=1):
                    off = alt.get("offer") or {}
                    dep_ap = off.get("departureAirport") or "?"
                    arr_ap = off.get("arrivalAirport") or "?"
                    dep_tm = _hhmm2(off.get("departureTime"))
                    dur = off.get("duration") or "?"
                    price_txt = _fmt_price(off.get("totalPrice"), off.get("currency") or currency)
                    segs = off.get("segments")
                    seg0 = segs[0] if isinstance(segs, list) and segs else None
                    c0 = (seg0.get("carrier") or seg0.get("marketingCarrier") or seg0.get("operatingCarrier")) if isinstance(seg0, dict) else None
                    fn0 = (seg0.get("flightNumber") if isinstance(seg0, dict) else None) or ""
                    c0fn = f"{c0}{fn0}".strip() if (c0 or fn0) else None
                    parts = [f"{idx}) {dep_ap} {dep_tm}", "->", f"{arr_ap}", "|", alt.get("date") or "?", "|", "nonstop", "|", dur]
                    if c0fn:
                        parts.extend(["|", c0fn])
                    parts.extend(["|", f"**{price_txt}**"])
                    lines.append(" ".join(str(p) for p in parts if str(p)))
                    carriers2 = off.get("carriers") or []
                    carriers_txt2 = ",".join(carriers2) if isinstance(carriers2, list) else str(carriers2 or "")
                    if carriers_txt2:
                        lines.append(f"    - Carriers: {carriers_txt2}")
            if take_conn:
                if take_direct:
                    lines.append("")
                lines.append("Connecting Alternatives")
                for idx, alt in enumerate(conn[:take_conn], start=1):
                    off = alt.get("offer") or {}
                    dep_ap = off.get("departureAirport") or "?"
                    arr_ap = off.get("arrivalAirport") or "?"
                    dep_tm = _hhmm2(off.get("departureTime"))
                    dur = off.get("duration") or "?"
                    price_txt = _fmt_price(off.get("totalPrice"), off.get("currency") or currency)
                    stops = off.get("stops")
                    stops_txt = "nonstop" if stops == 0 else (f"{stops} stop" if stops == 1 else f"{stops} stops")
                    segs = off.get("segments")
                    seg0 = segs[0] if isinstance(segs, list) and segs else None
                    c0 = (seg0.get("carrier") or seg0.get("marketingCarrier") or seg0.get("operatingCarrier")) if isinstance(seg0, dict) else None
                    fn0 = (seg0.get("flightNumber") if isinstance(seg0, dict) else None) or ""
                    c0fn = f"{c0}{fn0}".strip() if (c0 or fn0) else None
                    parts = [f"{idx}) {dep_ap} {dep_tm}", "->", f"{arr_ap}", "|", alt.get("date") or "?", "|", stops_txt, "|", dur]
                    if c0fn:
                        parts.extend(["|", c0fn])
                    parts.extend(["|", f"**{price_txt}**"])
                    lines.append(" ".join(str(p) for p in parts if str(p)))
                    carriers2 = off.get("carriers") or []
                    carriers_txt2 = ",".join(carriers2) if isinstance(carriers2, list) else str(carriers2 or "")
                    if carriers_txt2:
                        lines.append(f"    - Carriers: {carriers_txt2}")
            return lines
        alt_text_block = None
        if not offers and alternatives:
            # Persist alternatives in session for later confirmation
            try:
                sess = event.setdefault("sessionAttributes", {})
                sess["last_alternatives"] = [
                    {
                        "date": a.get("date"),
                        "price": (a.get("offer") or {}).get("totalPrice"),
                        "currency": (a.get("offer") or {}).get("currency") or currency,
                        "stops": (a.get("offer") or {}).get("stops"),
                        "duration": (a.get("offer") or {}).get("duration"),
                    }
                    for a in alternatives[: max(1, min(10, RECOMMENDER_MAX_OPTIONS))]
                ]
            except Exception:
                pass
            # Compact, sectioned alternatives for display
            alt_text_block = "\n".join(_alt_sections(alternatives))
        return _wrap_openapi(
            event,
            200,
            {
                "generatedAt": _now_iso(),
                "query": {
                    "origin": origin,
                    "destination": destination,
                    "departureDate": departure_date,
                    "returnDate": return_date,
                    "adults": adults,
                    "cabin": cabin,
                    "nonstop": nonstop,
                    "currency": currency,
                    "lhGroupOnly": lh_group_only,
                    "max": max_results,
                },
                **({"note": note, "message": note} if note else {}),
                "offers": offers,
                **({
                    "message": alt_text_block,
                    "alternatives": alternatives,
                } if alt_text_block else {}),
                "provider": "Amadeus Flight Offers Search v2",
            },
        )
    except Exception as e:
        _log("OpenAPI flight search error", error=str(e))
        return _wrap_openapi(event, 502, {"error": str(e)[:1200]})


def _handle_function(event: Dict[str, Any]) -> Dict[str, Any]:
    func_name = (event.get("function") or "").strip().lower()

    # NEW: function-details variant
    if func_name == "iata_lookup":
        _log("Handling function event", function=func_name)
        params = event.get("parameters", [])
        term = (
            _get_param(params, "term")
            or _get_param(params, "code")
            or _get_param(params, "q")
            or _get_param(params, "query")
        )
        _log("Function IATA lookup request", term=term)
        try:
            data = iata_lookup_via_proxy(term)
            _log(
                "Function IATA lookup success",
                term=term,
                count=len(data.get("matches", [])),
            )
            return _wrap_function(
                event,
                200,
                {
                    "generatedAt": _now_iso(),
                    "query": {"term": term},
                    "result": data,
                },
            )
        except Exception as e:
            _log("Function IATA lookup error", term=term, error=str(e))
            return _wrap_function(event, 502, {"error": str(e)[:1200]})

    if func_name == "recommend_destinations":
        _log("Handling function event", function=func_name)
        params = event.get("parameters", [])
        origin_code = _get_param(params, "originCode")
        month_text = _get_param(params, "month")
        month_range_text = _get_param(params, "monthRange") or month_text
        theme_raw = _get_param(params, "themeTags")
        min_avg_high = _get_param(params, "minAvgHighC")
        max_candidates = _to_int(_get_param(params, "maxCandidates", 8), 8)
        # Default to True so suggestions always include flight options.
        with_itins = _to_bool(_get_param(params, "withItineraries", True), True)
        currency = _get_param(params, "currency") or (os.getenv("DEFAULT_CURRENCY") or "EUR")

        # Accept JSON array or comma-separated string for themeTags
        theme_tags: List[str] = []
        if isinstance(theme_raw, list):
            theme_tags = [str(x).strip().lower() for x in theme_raw if str(x).strip()]
        elif isinstance(theme_raw, str):
            txt = theme_raw.strip()
            loaded = None
            try:
                loaded = json.loads(txt)
            except Exception:
                loaded = None
            if isinstance(loaded, list):
                theme_tags = [str(x).strip().lower() for x in loaded if str(x).strip()]
            else:
                theme_tags = [s.strip().lower() for s in txt.split(",") if s.strip()]
        # Canonicalize/expand synonyms so 'skiing' etc. map to 'winter_sports'
        theme_tags = _canonicalize_theme_tags(theme_tags, month_text)

        # Fallback to session default origin if not provided
        if not origin_code:
            origin_code = (event.get("sessionAttributes") or {}).get("default_origin")

        # Resolve target month for scoring
        (year, target_month), _ = _parse_month_range(month_range_text)
        theme_set = set(theme_tags)
        catalog = _load_catalog()
        if not catalog:
            _log("Destination catalog is empty")
            return _wrap_function(
                event,
                200,
                {"message": "Destination catalog not available yet. Please try again later."},
            )
        # Filter by theme if provided
        filtered = []
        for dest in catalog:
            tags = set(str(t).lower() for t in dest.get("tags", []))
            if theme_set and not (theme_set & tags):
                continue
            if min_avg_high is not None:
                try:
                    min_val = float(min_avg_high)
                except Exception:
                    min_val = None
                if min_val is not None:
                    avg = (dest.get("avgHighCByMonth") or {}).get(str(target_month))
                    if isinstance(avg, (int, float)) and float(avg) < min_val:
                        continue
            filtered.append(dest)
        # If user intent was skiing but filter ended empty, relax to all winter_sports
        if not filtered and ("winter_sports" in theme_set or any(t in theme_set for t in ("ski", "skiing"))):
            filtered = [d for d in catalog if "winter_sports" in set(str(t).lower() for t in d.get("tags", []))]
        scored: List[Tuple[float, Dict[str, Any], str]] = []
        for d in filtered:
            s, reason = _score_destination(d, theme_set, target_month, origin_code)
            scored.append((s, d, reason))
        scored.sort(key=lambda t: t[0], reverse=True)
        top = scored[: max(1, max_candidates)]
        candidates = [
            {
                "code": d.get("code"),
                "city": d.get("city"),
                "country": d.get("country"),
                "score": round(float(s), 3),
                "reason": r,
                "tags": d.get("tags") or [],
            }
            for (s, d, r) in top
        ]
        payload: Dict[str, Any] = {
            "generatedAt": _now_iso(),
            "query": {
                "originCode": origin_code,
                "month": month_text,
                "monthRange": month_range_text,
                "themeTags": theme_tags,
                "minAvgHighC": min_avg_high,
                "maxCandidates": max_candidates,
            },
            "candidates": candidates,
        }
        # Optional: try aggregator if requested and origin is present
        if with_itins and origin_code and candidates:
            try:
                options = search_best_itineraries(
                    origin_code,
                    [d for (_, d, _) in top],
                    month_range_text,
                    currency=currency,
                    lh_group_only=True,
                )
                # Partition options into direct and connecting; enforce max 10 total
                direct_opts = [o for o in options if (o.get("stops") == 0)]
                conn_opts = [o for o in options if (o.get("stops") and o.get("stops") > 0) or o.get("stops") is None]
                max_total = min(10, RECOMMENDER_MAX_OPTIONS)
                take_direct = min(len(direct_opts), max_total)
                take_conn = min(len(conn_opts), max_total - take_direct)
                payload["options"] = (direct_opts[:take_direct] + conn_opts[:take_conn])
                # Build a concise, sectioned message so the agent surfaces concrete options.
                def _fmt_price(val: Any, curr: Optional[str]) -> str:
                    try:
                        return f"{float(val):.0f} {curr or ''}".strip()
                    except Exception:
                        return str(val)
                lines: List[str] = []
                def _hhmm(ts: Optional[str]) -> str:
                    try:
                        if not ts:
                            return "?"
                        # Accept 'YYYY-MM-DDTHH:MM' or 'YYYY-MM-DD HH:MM'
                        t = ts.replace("T", " ").replace("Z", "").split(" ")[1]
                        return t[:5]
                    except Exception:
                        return "?"
                if take_direct:
                    lines.append("Direct Flights")
                    for idx, opt in enumerate(direct_opts[:take_direct], start=1):
                        offer = opt.get("offer") or {}
                        price = _fmt_price(offer.get("totalPrice"), offer.get("currency") or currency)
                        dep = offer.get("departureAirport") or "?"
                        arr = offer.get("arrivalAirport") or (opt.get("destination") or "?")
                        dur = offer.get("duration") or "?"
                        label = opt.get("label") or "Option"
                        carriers = offer.get("carriers") or []
                        carriers_txt = ",".join(carriers) if isinstance(carriers, list) else str(carriers or "")
                        header = f"{idx}) {label} - {dep} -> {arr} | {opt.get('date')} | nonstop | {dur} | **{price}**"
                        lines.append(header)
                        if carriers_txt:
                            lines.append(f"    - Carriers: {carriers_txt}")
                        if RECOMMENDER_VERBOSE and isinstance(offer.get("segments"), list) and offer["segments"]:
                            for s in offer["segments"][: (len(offer["segments"]) if True else 1)]:
                                c = s.get("carrier") or s.get("marketingCarrier") or s.get("operatingCarrier") or "?"
                                fn = s.get("flightNumber") or ""
                                s_dep = s.get("from") or "?"
                                s_arr = s.get("to") or "?"
                                s_dt = _hhmm(s.get("departureTime") or s.get("depTime"))
                                s_at = _hhmm(s.get("arrivalTime") or s.get("arrTime"))
                                lines.append(f"    - THEN {c}{fn} {s_dep} {s_dt} -> {s_arr} {s_at}")
                if take_conn:
                    lines.append("")
                    lines.append("Connecting Flights")
                    for idx, opt in enumerate(conn_opts[:take_conn], start=1):
                        offer = opt.get("offer") or {}
                        price = _fmt_price(offer.get("totalPrice"), offer.get("currency") or currency)
                        dep = offer.get("departureAirport") or "?"
                        arr = offer.get("arrivalAirport") or (opt.get("destination") or "?")
                        dur = offer.get("duration") or "?"
                        label = opt.get("label") or "Option"
                        stops = opt.get("stops")
                        stops_txt = "nonstop" if stops == 0 else (f"{stops} stop" if stops == 1 else f"{stops} stops")
                        carriers = offer.get("carriers") or []
                        carriers_txt = ",".join(carriers) if isinstance(carriers, list) else str(carriers or "")
                        # Header line with bold price
                        header = f"{idx}) {label} - {dep} -> {arr} | {opt.get('date')} | {stops_txt} | {dur} | **{price}**"
                        # Rebuild header to include first segment carrier/flight and departure HH:MM when available
                        try:
                            segs = offer.get("segments")
                            seg0 = segs[0] if isinstance(segs, list) and segs else None
                            c0 = (seg0.get("carrier") or seg0.get("marketingCarrier") or seg0.get("operatingCarrier")) if isinstance(seg0, dict) else None
                            fn0 = (seg0.get("flightNumber") if isinstance(seg0, dict) else None) or ""
                            dt0 = _hhmm((seg0.get("departureTime") if isinstance(seg0, dict) else None) or offer.get("departureTime") or "")
                            c0fn = f"{c0}{fn0}".strip() if c0 or fn0 else (carriers[0] if isinstance(carriers, list) and carriers else "")
                            header_parts = [
                                f"{idx}) {label} - {dep} {dt0}".strip(),
                                "->",
                                f"{arr}",
                                "|",
                                f"{opt.get('date')}",
                                "|",
                                f"{stops_txt}",
                                "|",
                                f"{dur}",
                            ]
                            if c0fn:
                                header_parts.extend(["|", c0fn])
                            header_parts.extend(["|", f"**{price}**"])
                            header = " ".join(str(p) for p in header_parts if str(p))
                        except Exception:
                            pass
                        lines.append(header)
                        if carriers_txt:
                            lines.append(f"    - Carriers: {carriers_txt}")
                        if RECOMMENDER_VERBOSE and isinstance(offer.get("segments"), list) and offer["segments"]:
                            # Show up to 3 segments as THEN lines
                            for s in offer["segments"][:3]:
                                c = s.get("carrier") or s.get("marketingCarrier") or s.get("operatingCarrier") or "?"
                                fn = s.get("flightNumber") or ""
                                s_dep = s.get("from") or "?"
                                s_arr = s.get("to") or "?"
                                s_dt = _hhmm(s.get("departureTime") or s.get("depTime"))
                                s_at = _hhmm(s.get("arrivalTime") or s.get("arrTime"))
                                lines.append(f"    - THEN {c}{fn} {s_dep} {s_dt} -> {s_arr} {s_at}")
                    offer = opt.get("offer") or {}
                    price = _fmt_price(offer.get("totalPrice"), offer.get("currency") or currency)
                    dep = offer.get("departureAirport") or "?"
                    arr = offer.get("arrivalAirport") or (opt.get("destination") or "?")
                    dep_time = offer.get("departureTime") or "?"
                    dur = offer.get("duration") or "?"
                    label = opt.get("label") or "Option"
                    pitch = opt.get("pitch") or ""
                    stops = opt.get("stops")
                    stops_txt = "nonstop" if stops == 0 else (f"{stops} stop" if stops == 1 else f"{stops} stops")
                    carriers = offer.get("carriers") or []
                    carriers_txt = ",".join(carriers) if isinstance(carriers, list) else str(carriers or "")
                    # Header line with bold price
                    header = f"{idx}) {label} - {dep} ? {arr}   {opt.get('date')}   {stops_txt}   {dur}   **{price}**"
                    # Rebuild header to include first carrier/flight and departure HH:MM when available
                    try:
                        segs = offer.get("segments")
                        seg0 = segs[0] if isinstance(segs, list) and segs else None
                        c0 = (seg0.get("carrier") or seg0.get("marketingCarrier") or seg0.get("operatingCarrier")) if isinstance(seg0, dict) else None
                        fn0 = (seg0.get("flightNumber") if isinstance(seg0, dict) else None) or ""
                        # dep_time may be full ISO; convert to HH:MM
                        dt0 = _hhmm((seg0.get("departureTime") if isinstance(seg0, dict) else None) or dep_time)
                        c0fn = f"{c0}{fn0}".strip() if c0 or fn0 else (carriers[0] if isinstance(carriers, list) and carriers else "")
                        header_parts = [
                            f"{idx}) {label} - {dep} {dt0}".strip(),
                            "->",
                            f"{arr}",
                            "|",
                            f"{opt.get('date')}",
                            "|",
                            f"{stops_txt}",
                            "|",
                            f"{dur}",
                        ]
                        if c0fn:
                            header_parts.extend(["|", c0fn])
                        header_parts.extend(["|", f"**{price}**"])
                        header = " ".join(str(p) for p in header_parts if str(p))
                    except Exception:
                        pass
                    lines.append(header)
                    if carriers_txt:
                        lines.append(f"    - Carriers: {carriers_txt}")
                    if RECOMMENDER_VERBOSE and isinstance(offer.get("segments"), list) and offer["segments"]:
                        # Show up to 3 segments as THEN lines
                        for s in offer["segments"][:3]:
                            c = s.get("carrier") or s.get("marketingCarrier") or s.get("operatingCarrier") or "?"
                            fn = s.get("flightNumber") or ""
                            s_dep = s.get("from") or "?"
                            s_arr = s.get("to") or "?"
                            s_dt = _hhmm(s.get("departureTime") or s.get("depTime"))
                            s_at = _hhmm(s.get("arrivalTime") or s.get("arrTime"))
                            lines.append(f"    - THEN {c}{fn} {s_dep} {s_dt} ? {s_arr} {s_at}")
                    if pitch:
                        lines.append(f"    - {pitch}")
                if lines:
                    closing = "Shall I hold Option 1 for 15 minutes or adjust dates?"
                    m = "\n".join(lines + ["", closing])
                    # Sanitize any non-ASCII separators that may slip in due to encoding
                    m = m.replace("\u001a", "->").replace("\u0007", " | ")
                    # Convert any HTML breaks to newlines and strip stray tags so the agent displays clean text
                    try:
                        m = re.sub(r"<br\s*/?>", "\n", m, flags=re.IGNORECASE)
                        m = re.sub(r"<[^>]+>", "", m)
                    except Exception:
                        pass
                    payload["message"] = m
                    # Compact the message if it exceeds the configured byte threshold
                    try:
                        _msg_bytes = len(payload["message"].encode("utf-8")) if isinstance(payload.get("message"), str) else 0
                    except Exception:
                        _msg_bytes = len(str(payload.get("message", "")))
                    if _msg_bytes > RECOMMENDER_MAX_TEXT_BYTES:
                        compact_lines: List[str] = []
                        _max_opts = min(RECOMMENDER_MAX_OPTIONS, 2)
                        for _idx2, _opt2 in enumerate(options[:_max_opts], start=1):
                            _offer2 = _opt2.get("offer") or {}
                            _price2 = _fmt_price(_offer2.get("totalPrice"), _offer2.get("currency") or currency)
                            _dep2 = _offer2.get("departureAirport") or "?"
                            _arr2 = _offer2.get("arrivalAirport") or (_opt2.get("destination") or "?")
                            _dur2 = _offer2.get("duration") or "?"
                            _stops2 = _opt2.get("stops")
                            _stops_txt2 = "nonstop" if _stops2 == 0 else (f"{_stops2} stop" if _stops2 == 1 else f"{_stops2} stops")
                            # Add first segment carrier/flight and departure HH:MM when available in compact header
                            _segs2 = _offer2.get("segments")
                            _seg0 = _segs2[0] if isinstance(_segs2, list) and _segs2 else None
                            _c0 = (_seg0.get("carrier") or _seg0.get("marketingCarrier") or _seg0.get("operatingCarrier")) if isinstance(_seg0, dict) else None
                            _fn0 = (_seg0.get("flightNumber") if isinstance(_seg0, dict) else None) or ""
                            _dt0 = _hhmm((_seg0.get("departureTime") if isinstance(_seg0, dict) else None) or _offer2.get("departureTime") or "")
                            _c0fn = f"{_c0}{_fn0}".strip() if (_c0 or _fn0) else ""
                            _pieces = [
                                f"{_idx2}) {_opt2.get('label') or 'Option'} - {_dep2} {_dt0}".strip(),
                                "->",
                                f"{_arr2}",
                                "|",
                                f"{_opt2.get('date')}",
                                "|",
                                f"{_stops_txt2}",
                                "|",
                                f"{_dur2}",
                            ]
                            if _c0fn:
                                _pieces.extend(["|", _c0fn])
                            _pieces.extend(["|", f"**{_price2}**"])
                            _header2 = " ".join(str(x) for x in _pieces if str(x))
                            compact_lines.append(_header2)
                            _carriers2 = _offer2.get("carriers") or []
                            _carriers_txt2 = ",".join(_carriers2) if isinstance(_carriers2, list) else str(_carriers2 or "")
                            if _carriers_txt2:
                                compact_lines.append(f"    - Carriers: {_carriers_txt2}")
                        _compact_msg = "\n".join(compact_lines + ["", closing])
                        payload["message"] = _compact_msg
                        _log(
                            "Recommender message compacted",
                            originalBytes=_msg_bytes,
                            threshold=RECOMMENDER_MAX_TEXT_BYTES,
                            maxOptions=RECOMMENDER_MAX_OPTIONS,
                            verbose=RECOMMENDER_VERBOSE,
                        )
                    # Cache a minimal selection map in session for smoother follow-ups.
                    try:
                        sess = event.setdefault("sessionAttributes", {})
                        sess["last_recommendation"] = {
                            "origin": origin_code,
                            "month": month_text or month_range_text,
                            "options": [
                                {
                                    "label": o.get("label"),
                                    "destination": o.get("destination"),
                                    "date": o.get("date"),
                                    "id": (o.get("offer") or {}).get("id"),
                                    "price": (o.get("offer") or {}).get("totalPrice"),
                                    "currency": (o.get("offer") or {}).get("currency") or currency,
                                }
                                for o in options[:3]
                            ],
                        }
                    except Exception:
                        pass
                # Return minimal options only (avoid large payloads)
                def _brief(o: Dict[str, Any]) -> Dict[str, Any]:
                    off = o.get("offer") or {}
                    first_seg = (off.get("segments") or [None])[0]
                    dep_time0 = None
                    carrier0 = None
                    flight_no0 = None
                    if isinstance(first_seg, dict):
                        dep_time0 = first_seg.get("departureTime")
                        carrier0 = first_seg.get("carrier") or first_seg.get("marketingCarrier") or first_seg.get("operatingCarrier")
                        flight_no0 = first_seg.get("flightNumber")
                    def _hhmm2(ts: Optional[str]) -> str:
                        try:
                            if not ts:
                                return "?"
                            t = str(ts).replace("T", " ").replace("Z", "").split(" ")[1]
                            return t[:5]
                        except Exception:
                            return "?"
                    c0fn2 = f"{carrier0 or ''}{flight_no0 or ''}".strip()
                    dep_ap = off.get("departureAirport")
                    arr_ap = off.get("arrivalAirport")
                    dur0 = off.get("duration")
                    price0 = off.get("totalPrice")
                    curr0 = off.get("currency") or currency
                    try:
                        price_txt0 = f"{float(price0):.0f} {curr0}".strip() if price0 is not None else "?"
                    except Exception:
                        price_txt0 = f"{price0} {curr0}".strip()
                    preview = " ".join(
                        [
                            f"{dep_ap or '?'} {_hhmm2(dep_time0 or off.get('departureTime'))}",
                            "->",
                            f"{arr_ap or '?'}",
                            "|",
                            f"{off.get('duration') or '?'}",
                            "|",
                            c0fn2 or "",
                            "|",
                            f"**{price_txt0}**",
                        ]
                    ).strip()
                    return {
                        "label": o.get("label"),
                        "pitch": o.get("pitch"),
                        "destination": o.get("destination"),
                        "date": o.get("date"),
                        "price": off.get("totalPrice"),
                        "currency": off.get("currency") or currency,
                        "duration": off.get("duration"),
                        "stops": o.get("stops"),
                        "carriers": off.get("carriers"),
                        "departureAirport": off.get("departureAirport"),
                        "arrivalAirport": off.get("arrivalAirport"),
                        "departureTime": dep_time0 or off.get("departureTime"),
                        "firstCarrier": carrier0,
                        "firstFlightNumber": flight_no0,
                        "previewText": preview,
                        "id": off.get("id"),
                    }
                payload["options"] = [ _brief(o) for o in options[: max(1, RECOMMENDER_MAX_OPTIONS) ] ]
            except Exception as exc:
                _log("Itinerary aggregator failed", error=str(exc))
        # If we still don't have a message (no options), provide a short, clean candidate list
        if not payload.get("message"):
            header = "Inspiration   top matches (ASCII)"
            msg_lines = [header]
            for idx, c in enumerate(candidates[:5], start=1):
                city = c.get("city") or "?"
                country = c.get("country") or "?"
                code = c.get("code") or "?"
                reason = (c.get("reason") or "").replace(" ", " ").replace("\u00B0", " ")
                msg_lines.append(f"{idx}) {city}, {country} ({code}) - {reason}")
            payload["message"] = "\n".join(msg_lines)
        # Log payload size to help diagnose occasional agent runtime size limits
        try:
            _resp_bytes = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        except Exception:
            _resp_bytes = None
        try:
            _msg_bytes2 = len((payload.get("message") or "").encode("utf-8")) if isinstance(payload.get("message"), str) else None
        except Exception:
            _msg_bytes2 = None
        _log(
            "Destination recommendations prepared",
            origin=origin_code,
            month=month_text,
            monthRange=month_range_text,
            themeTags=theme_tags,
            candidates=len(candidates),
            messageBytes=_msg_bytes2,
            responseBytes=_resp_bytes,
        )
        return _wrap_function(event, 200, payload)

    if func_name == "datetime_interpret":
        _log(
            "Deprecated datetime_interpret invocation",
            note="Use TimePhraseParser action group.",
        )
        return _wrap_function(
            event,
            410,
            {"error": "Datetime parsing is handled by the TimePhraseParser action group Lambda."},
        )

    # (existing flight-search code continues below)
    _log("Handling function event", function=func_name or "search_flights")
    params = event.get("parameters", [])
    param_body = _props_to_dict(params)
    normalized = _normalize_flight_request_fields(param_body)
    had_origin_before_context = bool((normalized or {}).get("origin"))
    normalized = _apply_contextual_defaults(normalized, event)
    if normalized:
        _log("Function normalized flight fields", normalized=normalized)
    origin_raw = normalized.get("origin")
    destination_raw = normalized.get("destination")
    origin, origin_suggestions = _resolve_iata_code(origin_raw)
    filled_from_context = (not had_origin_before_context) and bool(origin)
    destination, destination_suggestions = _resolve_iata_code(destination_raw)
    departure_input = normalized.get("departureDate")
    departure_date = _iso_date(str(departure_input) if departure_input is not None else "")
    return_input = normalized.get("returnDate")
    return_date = _iso_date(str(return_input)) if return_input else None
    adults = _to_int(normalized.get("adults", 1), 1)
    cabin = (normalized.get("cabin") or "ECONOMY").upper()
    nonstop = _to_bool(normalized.get("nonstop", False), False)
    currency = (
        normalized.get("currency") or os.getenv(
            "DEFAULT_CURRENCY") or "EUR"
    ).upper()
    # Always enforce LH group via Amadeus (includedAirlineCodes) and do not post-filter
    lh_group_only = True
    max_results = _to_int(normalized.get("max", 10), 10)
    _log(
        "Function flight request prepared",
        origin=origin,
        destination=destination,
        departureDate=departure_date,
        returnDate=return_date,
        adults=adults,
        nonstop=nonstop,
        cabin=cabin,
        currency=currency,
        lhGroupOnly=lh_group_only,
        max=max_results,
    )
    if not origin:
        msg = "Please choose a departure airport IATA code (for example, MUC)."
        suggestions = origin_suggestions or []
        _log("Function validation message", reason="missing_origin", suggestions=suggestions)
        return _wrap_function(
            event,
            200,
            {"message": msg, "suggestions": suggestions},
        )
    if not destination:
        msg = "Please choose an arrival airport IATA code (for example, ZRH)."
        suggestions = destination_suggestions or []
        _log(
            "Function validation message",
            reason="missing_destination",
            suggestions=suggestions,
        )
        return _wrap_function(
            event,
            200,
            {"message": msg, "suggestions": suggestions},
        )
    if not departure_date:
        _log("Function validation error", reason="missing_departure_date")
        return _wrap_function(
            event, 400, {"error": "Provide 'departureDate' in YYYY-MM-DD."}
        )
    departure_date = _roll_forward_recent_past_date(departure_date) or departure_date
    departure_date = _advance_far_past_date(departure_date) or departure_date
    if return_date:
        return_date = _roll_forward_recent_past_date(return_date) or return_date
        return_date = _advance_far_past_date(return_date) or return_date
    window_error = _validate_booking_window(departure_date, return_date)
    if window_error:
        _log("Function validation error", reason="window_error", detail=window_error)
        return _wrap_function(event, 400, {"error": window_error})
    try:
        raw = amadeus_search_flight_offers(
            origin,
            destination,
            departure_date,
            return_date,
            adults,
            cabin,
            nonstop,
            currency,
            lh_group_only,
            max_results,
        )
        offers = _summarize_offers(raw, currency, lh_group_only)
        # Fallback: if user asked for nonstop and none found, retry with connections allowed.
        if nonstop and not offers:
            _log("No nonstop offers; retrying with connections allowed")
            raw = amadeus_search_flight_offers(
                origin,
                destination,
                departure_date,
                return_date,
                adults,
                cabin,
                False,  # allow connections
                currency,
                lh_group_only,
                max_results,
            )
        # If still none, probe nearest alternative dates and include up to 5.
        alternatives: List[Dict[str, Any]] = []
        if not offers:
            alternatives = _nearest_date_alternatives(
                origin,
                destination,
                departure_date,
                return_date,
                adults,
                cabin,
                currency,
                lh_group_only,
                max_results,
                nonstop=nonstop,
                limit=5,
            )
        # Add previewText to each offer for easy UI display
        try:
            def _hhmm_txt(ts: Optional[str]) -> str:
                try:
                    if not ts:
                        return "?"
                    t = str(ts).replace("T", " ").replace("Z", "").split(" ")[1]
                    return t[:5]
                except Exception:
                    return "?"
            def _price_txt(val: Any, curr: Optional[str]) -> str:
                try:
                    return f"{float(val):.0f} {curr or ''}".strip() if val is not None else "?"
                except Exception:
                    return f"{val} {curr or ''}".strip()
            for _o in offers or []:
                _dep_ap = _o.get("departureAirport") or "?"
                _dep_tm = _hhmm_txt(_o.get("departureTime"))
                _arr_ap = _o.get("arrivalAirport") or "?"
                _dur = _o.get("duration") or "?"
                _segs = _o.get("segments")
                _seg0 = _segs[0] if isinstance(_segs, list) and _segs else None
                _c0 = (_seg0.get("carrier") or _seg0.get("marketingCarrier") or _seg0.get("operatingCarrier")) if isinstance(_seg0, dict) else None
                _fn0 = (_seg0.get("flightNumber") if isinstance(_seg0, dict) else None) or ""
                _c0fn = f"{_c0 or ''}{_fn0}".strip()
                _pr = _price_txt(_o.get("totalPrice"), _o.get("currency") or currency)
                _parts = [f"{_dep_ap} {_dep_tm}", "->", f"{_arr_ap}", "|", _dur]
                if _c0fn:
                    _parts.extend(["|", _c0fn])
                _parts.extend(["|", f"**{_pr}**"])
                _o["previewText"] = " ".join(str(x) for x in _parts if str(x))
        except Exception:
            pass
        # Build a human-readable offer text block so the agent renders concrete options
        offer_text_block = None
        try:
            def _hhmm(ts: Optional[str]) -> str:
                try:
                    if not ts:
                        return "?"
                    t = str(ts).replace("T", " ").replace("Z", "").split(" ")[1]
                    return t[:5]
                except Exception:
                    return "?"
            def _fmt_price(val: Any, curr: Optional[str]) -> str:
                try:
                    return f"{float(val):.0f} {curr or ''}".strip()
                except Exception:
                    return str(val)
            if offers:
                # Partition into direct and connecting, enforce max 10 total
                direct = [o for o in offers if (o.get("stops") == 0)]
                conn = [o for o in offers if (o.get("stops") and o.get("stops") > 0) or o.get("stops") is None]
                max_total = min(10, RECOMMENDER_MAX_OPTIONS)
                take_direct = min(len(direct), max_total)
                take_conn = min(len(conn), max_total - take_direct)
                lines2: List[str] = []
                if take_direct:
                    lines2.append("Direct Flights")
                    for idx, off in enumerate(direct[:take_direct], start=1):
                        dep2 = off.get("departureAirport") or "?"
                        arr2 = off.get("arrivalAirport") or "?"
                        dt2 = _hhmm(off.get("departureTime"))
                        dur2 = off.get("duration") or "?"
                        price2 = _fmt_price(off.get("totalPrice"), off.get("currency") or currency)
                        segs2 = off.get("segments")
                        seg02 = segs2[0] if isinstance(segs2, list) and segs2 else None
                        c02 = (seg02.get("carrier") or seg02.get("marketingCarrier") or seg02.get("operatingCarrier")) if isinstance(seg02, dict) else None
                        fn02 = (seg02.get("flightNumber") if isinstance(seg02, dict) else None) or ""
                        c02fn = f"{c02}{fn02}".strip() if (c02 or fn02) else None
                        parts2 = [f"{idx}) {dep2} {dt2}", "->", f"{arr2}", "|", departure_date, "|", "nonstop", "|", dur2]
                        if c02fn:
                            parts2.extend(["|", c02fn])
                        parts2.extend(["|", f"**{price2}**"])
                        lines2.append(" ".join(str(p) for p in parts2 if str(p)))
                        if isinstance(segs2, list) and segs2:
                            for s in segs2[: (len(segs2) if return_date else 1)]:
                                try:
                                    sc = s.get("carrier") or s.get("marketingCarrier") or s.get("operatingCarrier") or "?"
                                    sfn = s.get("flightNumber") or ""
                                    sfrom = s.get("from") or "?"
                                    sdt = _hhmm(s.get("departureTime") or s.get("depTime"))
                                    sto = s.get("to") or "?"
                                    sat = _hhmm(s.get("arrivalTime") or s.get("arrTime"))
                                    lines2.append(f"    - THEN {sc}{sfn} {sfrom} {sdt} -> {sto} {sat}")
                                except Exception:
                                    continue
                if take_conn:
                    lines2.append("")
                    lines2.append("Connecting Flights")
                    for idx, off in enumerate(conn[:take_conn], start=1):
                        dep2 = off.get("departureAirport") or "?"
                        arr2 = off.get("arrivalAirport") or "?"
                        dt2 = _hhmm(off.get("departureTime"))
                        dur2 = off.get("duration") or "?"
                        price2 = _fmt_price(off.get("totalPrice"), off.get("currency") or currency)
                        stops2 = off.get("stops")
                        stops_txt2 = "nonstop" if stops2 == 0 else (f"{stops2} stop" if stops2 == 1 else f"{stops2} stops")
                        segs2 = off.get("segments")
                        seg02 = segs2[0] if isinstance(segs2, list) and segs2 else None
                        c02 = (seg02.get("carrier") or seg02.get("marketingCarrier") or seg02.get("operatingCarrier")) if isinstance(seg02, dict) else None
                        fn02 = (seg02.get("flightNumber") if isinstance(seg02, dict) else None) or ""
                        c02fn = f"{c02}{fn02}".strip() if (c02 or fn02) else None
                        parts2 = [f"{idx}) {dep2} {dt2}", "->", f"{arr2}", "|", departure_date, "|", stops_txt2, "|", dur2]
                        if c02fn:
                            parts2.extend(["|", c02fn])
                        parts2.extend(["|", f"**{price2}**"])
                        lines2.append(" ".join(str(p) for p in parts2 if str(p)))
                        if isinstance(segs2, list) and segs2:
                            seg_limit = len(segs2) if return_date else 3
                            for s in segs2[:seg_limit]:
                                try:
                                    sc = s.get("carrier") or s.get("marketingCarrier") or s.get("operatingCarrier") or "?"
                                    sfn = s.get("flightNumber") or ""
                                    sfrom = s.get("from") or "?"
                                    sdt = _hhmm(s.get("departureTime") or s.get("depTime"))
                                    sto = s.get("to") or "?"
                                    sat = _hhmm(s.get("arrivalTime") or s.get("arrTime"))
                                    lines2.append(f"    - THEN {sc}{sfn} {sfrom} {sdt} -> {sto} {sat}")
                                except Exception:
                                    continue
                offer_text_block = "\n".join(lines2)
        except Exception:
            offer_text_block = None
        _log("Function flight search success", origin=origin, destination=destination, offers=len(offers), alternatives=len(alternatives))
        note = None
        if filled_from_context and origin:
            try:
                event.setdefault("sessionAttributes", {})["default_origin"] = origin
            except Exception:
                pass
            note = f"Using your nearest airport as departure location ({origin}). Say 'change departure location' to update it."
        # Build textual alternatives list if applicable (partitioned, compact, PDF-compatible)
        def _alt_sections(alts: List[Dict[str, Any]]) -> List[str]:
            lines: List[str] = ["No offers on requested dates. Nearby alternatives:"]
            direct = [a for a in alts if ((a.get("offer") or {}).get("stops") == 0)]
            conn = [a for a in alts if ((a.get("offer") or {}).get("stops") or 0) > 0]
            max_total = min(10, RECOMMENDER_MAX_OPTIONS)
            take_direct = min(len(direct), max_total)
            take_conn = min(len(conn), max_total - take_direct)
            def _fmt_price(val: Any, curr: Optional[str]) -> str:
                try:
                    return f"{float(val):.0f} {curr or ''}".strip() if val is not None else "?"
                except Exception:
                    return f"{val} {curr or ''}".strip()
            def _hhmm2(ts: Optional[str]) -> str:
                try:
                    if not ts:
                        return "?"
                    t = str(ts).replace("T", " ").replace("Z", "").split(" ")[1]
                    return t[:5]
                except Exception:
                    return "?"
            if take_direct:
                lines.append("Direct Alternatives")
                for idx, alt in enumerate(direct[:take_direct], start=1):
                    off = alt.get("offer") or {}
                    dep_ap = off.get("departureAirport") or "?"
                    arr_ap = off.get("arrivalAirport") or "?"
                    dep_tm = _hhmm2(off.get("departureTime"))
                    dur = off.get("duration") or "?"
                    price_txt = _fmt_price(off.get("totalPrice"), off.get("currency") or currency)
                    segs = off.get("segments")
                    seg0 = segs[0] if isinstance(segs, list) and segs else None
                    c0 = (seg0.get("carrier") or seg0.get("marketingCarrier") or seg0.get("operatingCarrier")) if isinstance(seg0, dict) else None
                    fn0 = (seg0.get("flightNumber") if isinstance(seg0, dict) else None) or ""
                    c0fn = f"{c0}{fn0}".strip() if (c0 or fn0) else None
                    parts = [f"{idx}) {dep_ap} {dep_tm}", "->", f"{arr_ap}", "|", alt.get("date") or "?", "|", "nonstop", "|", dur]
                    if c0fn:
                        parts.extend(["|", c0fn])
                    parts.extend(["|", f"**{price_txt}**"])
                    lines.append(" ".join(str(p) for p in parts if str(p)))
                    carriers2 = off.get("carriers") or []
                    carriers_txt2 = ",".join(carriers2) if isinstance(carriers2, list) else str(carriers2 or "")
                    if carriers_txt2:
                        lines.append(f"    - Carriers: {carriers_txt2}")
            if take_conn:
                if take_direct:
                    lines.append("")
                lines.append("Connecting Alternatives")
                for idx, alt in enumerate(conn[:take_conn], start=1):
                    off = alt.get("offer") or {}
                    dep_ap = off.get("departureAirport") or "?"
                    arr_ap = off.get("arrivalAirport") or "?"
                    dep_tm = _hhmm2(off.get("departureTime"))
                    dur = off.get("duration") or "?"
                    price_txt = _fmt_price(off.get("totalPrice"), off.get("currency") or currency)
                    stops = off.get("stops")
                    stops_txt = "nonstop" if stops == 0 else (f"{stops} stop" if stops == 1 else f"{stops} stops")
                    segs = off.get("segments")
                    seg0 = segs[0] if isinstance(segs, list) and segs else None
                    c0 = (seg0.get("carrier") or seg0.get("marketingCarrier") or seg0.get("operatingCarrier")) if isinstance(seg0, dict) else None
                    fn0 = (seg0.get("flightNumber") if isinstance(seg0, dict) else None) or ""
                    c0fn = f"{c0}{fn0}".strip() if (c0 or fn0) else None
                    parts = [f"{idx}) {dep_ap} {dep_tm}", "->", f"{arr_ap}", "|", alt.get("date") or "?", "|", stops_txt, "|", dur]
                    if c0fn:
                        parts.extend(["|", c0fn])
                    parts.extend(["|", f"**{price_txt}**"])
                    lines.append(" ".join(str(p) for p in parts if str(p)))
                    carriers2 = off.get("carriers") or []
                    carriers_txt2 = ",".join(carriers2) if isinstance(carriers2, list) else str(carriers2 or "")
                    if carriers_txt2:
                        lines.append(f"    - Carriers: {carriers_txt2}")
            return lines
        alt_text_block = None
        if not offers and alternatives:
            alt_text_block = "\n".join(_alt_sections(alternatives))
        return _wrap_function(
            event,
            200,
            {
                "generatedAt": _now_iso(),
                "query": {
                    "origin": origin,
                    "destination": destination,
                    "departureDate": departure_date,
                    "returnDate": return_date,
                    "adults": adults,
                    "cabin": cabin,
                    "nonstop": nonstop,
                    "currency": currency,
                    "lhGroupOnly": lh_group_only,
                    "max": max_results,
                },
                **({"note": note, "message": note} if note else {}),
                "offers": offers,
                **({"message": offer_text_block} if offer_text_block else {}),
                **({
                    "message": alt_text_block,
                    "alternatives": alternatives,
                } if alt_text_block else {}),
                "provider": "Amadeus Flight Offers Search v2",
            },
        )
    except Exception as e:
        _log("Function flight search error", error=str(e))
        return _wrap_function(event, 502, {"error": str(e)[:1200]})


def lambda_handler(event, context):
    logger = InvocationLogger()
    token = _CURRENT_LOGGER.set(logger)
    _log(
        "Lambda invocation started",
        event_keys=list(event.keys()),
        has_context=bool(context),
    )
    try:
        if "apiPath" in event and "httpMethod" in event:
            _log("Routing to OpenAPI handler")
            response = _handle_openapi(event)
            _log("Lambda invocation completed via OpenAPI handler")
            return response
        if "function" in event and "parameters" in event:
            _log("Routing to function handler")
            response = _handle_function(event)
            _log("Lambda invocation completed via function handler")
            return response
        _log("Falling back to diagnostic response")
        payload = {
            "note": "Unsupported event shape. Use action-group OpenAPI or function-details.",
            "eventKeys": list(event.keys()),
        }
        if "apiPath" in event:
            return _wrap_openapi(
                event,
                200,
                {
                    "message": "Missing request body. Gather origin, destination, and ISO dates before invoking the action group.",
                    **payload,
                },
                logger,
            )
        if "function" in event:
            return _wrap_function(
                event,
                200,
                {
                    "message": "Missing parameters. Collect all required flight inputs before invoking the action group.",
                    **payload,
                },
                logger,
            )
        payload = _attach_logs(payload, logger)
        fallback = {
            "messageVersion": "1.0",
            "response": {
                "actionGroup": event.get("actionGroup", "daisy_in_action"),
                "function": event.get("function", "diagnostic"),
                "functionResponse": {
                    "responseBody": {
                        "TEXT": {"body": json.dumps(payload)}
                    }
                },
            },
            "sessionAttributes": event.get("sessionAttributes", {}),
            "promptSessionAttributes": event.get("promptSessionAttributes", {}),
        }
        return fallback
    except Exception as exc:
        _log("Lambda invocation raised exception", error=str(exc))
        raise
    finally:
        _CURRENT_LOGGER.reset(token)

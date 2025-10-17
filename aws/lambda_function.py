# lambda_function.py
# AWS Lambda for Agents for Amazon Bedrock (Action Group).
# Searches real flight offers via Amadeus Self-Service API and returns simplified offers.
# Default filters to Lufthansa Group carriers unless overridden.
# Runtime: Python 3.12
# Deps: (none - stdlib only)

from __future__ import annotations

import contextvars
import json
import os
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib import error as _urlerr
from urllib import parse as _urlparse
from urllib import request as _urlreq


LH_GROUP_CODES = ["LH", "LX", "OS", "SN", "EW", "4Y", "EN"]

PROXY_BASE_URL = (os.getenv("PROXY_BASE_URL") or "http://localhost:8787").rstrip("/")

MAX_LOOKAHEAD_DAYS = 365


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
    try:
        dt = datetime.strptime(val, "%Y-%m-%d")
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return None


def _parse_iso_date(val: str) -> Optional[date]:
    try:
        return datetime.strptime(val, "%Y-%m-%d").date()
    except Exception:
        return None


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
        return "Departure date must be today or later."
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
        out[item.get("name")] = item.get("value")
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
            return json.loads(text)
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
            return json.loads(text)
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
    response = _proxy_post("/tools/amadeus/search", payload, timeout=timeout)
    if isinstance(response, dict):
        offers = response.get("offers")
        offer_count = len(offers) if isinstance(offers, list) else None
    else:
        offer_count = None
    _log("Amadeus search completed", offers=offer_count)
    return response

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
    amadeus_json: Dict[str, Any], currency_hint: Optional[str]
) -> List[Dict[str, Any]]:

    _log(
        "Summarizing offers",
        keys=list(amadeus_json.keys()),
        currency_hint=currency_hint,
    )
    offers = amadeus_json.get("offers")
    if isinstance(offers, list) and offers:
        normalized = []

        def _normalize_segment(segment: Dict[str, Any]) -> Dict[str, Any]:
            seg = dict(segment or {})

            def _extract_airport(data: Any, fallback_key: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
                if isinstance(data, dict):
                    return data.get("iataCode"), data.get("terminal"), data.get("at")
                airport = seg.get(fallback_key) or seg.get(fallback_key.capitalize())
                terminal = seg.get(f"{fallback_key}Terminal") or seg.get(f"{fallback_key}terminal")
                time = seg.get(f"{fallback_key}Time") or seg.get(f"{fallback_key.capitalize()}Time")
                return airport, terminal, time

            dep_airport, dep_terminal, dep_time = _extract_airport(seg.get("departure"), "departure")
            arr_airport, arr_terminal, arr_time = _extract_airport(seg.get("arrival"), "arrival")

            carrier = (
                seg.get("carrier")
                or seg.get("carrierCode")
                or seg.get("marketingCarrier")
                or seg.get("operatingCarrier")
            )
            aircraft = seg.get("aircraft")
            aircraft_code = aircraft.get("code") if isinstance(aircraft, dict) else aircraft
            services = seg.get("services")
            if isinstance(services, str):
                services = [services]
            elif not isinstance(services, list):
                services = []

            return {
                "carrier": carrier,
                "marketingCarrier": seg.get("marketingCarrier"),
                "operatingCarrier": seg.get("operatingCarrier"),
                "flightNumber": seg.get("number") or seg.get("flightNumber"),
                "aircraft": aircraft_code,
                "cabin": seg.get("cabin"),
                "fareClass": seg.get("fareClass") or seg.get("class"),
                "from": dep_airport,
                "fromTerminal": dep_terminal,
                "departureTime": dep_time,
                "to": arr_airport,
                "toTerminal": arr_terminal,
                "arrivalTime": arr_time,
                "duration": seg.get("duration"),
                "mileage": seg.get("mileage"),
                "stops": seg.get("numberOfStops"),
                "layoverDuration": seg.get("layoverDuration"),
                "services": services,
            }

        for item in offers:
            total_price = item.get("price") or item.get("totalPrice")
            currency = item.get("currency") or currency_hint
            carriers_set: set[str] = set()
            itineraries_source = item.get("itineraries") or []
            normalized_itineraries: List[Dict[str, Any]] = []
            aggregated_segments: List[Dict[str, Any]] = []

            for itin in itineraries_source:
                normalized_segments: List[Dict[str, Any]] = []
                for seg in itin.get("segments") or []:
                    seg_normalized = _normalize_segment(seg)
                    if seg_normalized["carrier"]:
                        carriers_set.add(seg_normalized["carrier"])
                    normalized_segments.append(seg_normalized)
                    aggregated_segments.append(seg_normalized)
                normalized_itineraries.append(
                    {
                        "duration": itin.get("duration"),
                        "segments": normalized_segments,
                    }
                )

            if not aggregated_segments:
                for seg in item.get("segments") or []:
                    seg_normalized = _normalize_segment(seg)
                    if seg_normalized["carrier"]:
                        carriers_set.add(seg_normalized["carrier"])
                    aggregated_segments.append(seg_normalized)

            carriers = sorted(carriers_set)
            first_segment = aggregated_segments[0] if aggregated_segments else {}
            last_segment = aggregated_segments[-1] if aggregated_segments else {}
            total_duration = (
                item.get("duration")
                or (normalized_itineraries[0].get("duration") if normalized_itineraries else None)
            )
            stop_count = max(len(aggregated_segments) - 1, 0) if aggregated_segments else None

            normalized.append(
                {
                    "id": item.get("id"),
                    "oneWay": item.get("oneWay"),
                    "totalPrice": total_price,
                    "currency": currency,
                    "carriers": carriers,
                    "segments": aggregated_segments,
                    "itineraries": normalized_itineraries or None,
                    "primaryCarrier": carriers[0] if carriers else None,
                    "departureAirport": first_segment.get("from"),
                    "departureTime": first_segment.get("departureTime"),
                    "arrivalAirport": last_segment.get("to"),
                    "arrivalTime": last_segment.get("arrivalTime"),
                    "duration": total_duration,
                    "stops": stop_count,
                }
            )
        normalized.sort(key=lambda o: float(o.get("totalPrice") or 0))
        _log("Summarized offers (modern payload)", count=len(normalized))
        return normalized
    # Fallback for legacy Amadeus payloads
    data = amadeus_json.get("data") or []
    dictionaries = amadeus_json.get("dictionaries") or {}
    carriers_map = dictionaries.get("carriers") or {}
    legacy = []
    for item in data:
        price_block = item.get("price") or {}
        total_price = price_block.get("grandTotal") or price_block.get("total")
        currency = price_block.get("currency") or currency_hint
        itineraries = item.get("itineraries") or []
        segments_summary: List[Dict[str, Any]] = []
        marketing_carriers = set()
        for itin in itineraries:
            for s in itin.get("segments") or []:
                carrier_code = s.get("carrierCode") or s.get(
                    "marketingCarrier")
                if carrier_code:
                    marketing_carriers.add(carrier_code)
                segments_summary.append(
                    {
                        "carrier": carrier_code,
                        "carrierName": carriers_map.get(carrier_code, carrier_code),
                        "flightNumber": s.get("number"),
                        "from": s.get("departure", {}).get("iataCode"),
                        "to": s.get("arrival", {}).get("iataCode"),
                        "depTime": s.get("departure", {}).get("at"),
                        "arrTime": s.get("arrival", {}).get("at"),
                        "duration": s.get("duration"),
                        "numberOfStops": s.get("numberOfStops"),
                    }
                )
        legacy.append(
            {
                "id": item.get("id"),
                "oneWay": len(itineraries) == 1,
                "totalPrice": total_price,
                "currency": currency,
                "carriers": sorted(list(marketing_carriers)),
                "segments": segments_summary,
            }
        )
    legacy.sort(key=lambda o: float(o.get("totalPrice") or 0))
    _log("Summarized offers (legacy payload)", count=len(legacy))
    return legacy


# ------------------------ Response wrappers ------------------------


def _wrap_openapi(
    event: Dict[str, Any],
    status: int,
    body_obj: Dict[str, Any],
    logger: Optional[InvocationLogger] = None,
) -> Dict[str, Any]:

    if logger is None:
        logger = _get_logger()
    payload = _attach_logs(body_obj, logger)
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
    response_payload = _attach_logs(response_payload, logger)
    _log("Function response wrapped", status=status, function=event.get("function"))
    return {
        "messageVersion": "1.0",
        "response": {
            "actionGroup": event.get("actionGroup"),
            "function": event.get("function", "search_flights"),
            "functionResponse": {
                "responseBody": {
                    "TEXT": {"body": json.dumps(response_payload)}
                }
            },
        },
        "sessionAttributes": event.get("sessionAttributes", {}),
        "promptSessionAttributes": event.get("promptSessionAttributes", {}),
    }


# ------------------------ Handlers ------------------------


def _handle_openapi(event: Dict[str, Any]) -> Dict[str, Any]:
    # NEW: detect IATA lookup path first
    api_path = (event.get("apiPath") or "").strip().lower()
    _log("Handling OpenAPI event", api_path=api_path or "<none>")
    if api_path == "/iata/lookup":
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
    _log("OpenAPI request body parsed", keys=list(body.keys()))
    normalized = _normalize_flight_request_fields(body)
    if normalized:
        _log("OpenAPI normalized flight fields", normalized=normalized)
    api_path = (event.get("apiPath") or "").strip()
    if api_path == "/tools/iata/lookup":
        term = body.get("term")
        if not term:
            _log("OpenAPI validation error", reason="missing_term")
            return _wrap_openapi(
                event,
                400,
                {"error": "Provide 'term' to perform an IATA lookup."},
            )
        try:
            matches = proxy_lookup_iata(term)
        except Exception as exc:
            _log("OpenAPI proxy IATA lookup failed", term=term, error=str(exc))
            return _wrap_openapi(event, 502, {"error": f"IATA lookup failed: {exc}"})
        _log("OpenAPI proxy IATA lookup success", term=term, matches=len(matches))
        return _wrap_openapi(event, 200, {"matches": matches})
    raw_origin = normalized.get("origin")
    raw_destination = normalized.get("destination")
    origin, origin_suggestions = _resolve_iata_code(raw_origin)
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
    lh_group_only = _to_bool(
        normalized.get("lhGroupOnly", os.getenv("LH_GROUP_ONLY", "true")), True
    )
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
        offers = _summarize_offers(raw, currency)
        _log("OpenAPI flight search success", origin=origin, destination=destination, offers=len(offers))
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
                "offers": offers,
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

    # (existing flight-search code continues below)
    _log("Handling function event", function=func_name or "search_flights")
    params = event.get("parameters", [])
    param_body = _props_to_dict(params)
    normalized = _normalize_flight_request_fields(param_body)
    if normalized:
        _log("Function normalized flight fields", normalized=normalized)
    origin_raw = normalized.get("origin")
    destination_raw = normalized.get("destination")
    origin, origin_suggestions = _resolve_iata_code(origin_raw)
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
    lh_group_only = _to_bool(
        normalized.get("lhGroupOnly", os.getenv(
            "LH_GROUP_ONLY", "true")), True
    )
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
        offers = _summarize_offers(raw, currency)
        _log("Function flight search success", origin=origin, destination=destination, offers=len(offers))
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
                "offers": offers,
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
        if "apiPath" in event and "httpMethod" in event and "requestBody" in event:
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
        payload = {"note": "Unsupported event shape. Use action-group OpenAPI or function-details."}
        payload = _attach_logs(payload, logger)
        fallback = {
            "messageVersion": "1.0",
            "response": {
                "text": json.dumps(payload)
            },
        }
        return fallback
    except Exception as exc:
        _log("Lambda invocation raised exception", error=str(exc))
        raise
    finally:
        _CURRENT_LOGGER.reset(token)

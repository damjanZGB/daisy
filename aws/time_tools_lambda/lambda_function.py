from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from time_tools import normalize_any_date_to_iso, parse_human_time

DEFAULT_LOCALE = ["en"]
DEFAULT_TIMEZONE = "UTC"


def _ensure_locale(value: Any) -> List[str]:
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    if isinstance(value, str):
        return [value]
    return DEFAULT_LOCALE


def _coerce_json(value: Any) -> Any:
    if isinstance(value, str):
        trimmed = value.strip()
        if trimmed and trimmed[0] in "{[" and trimmed[-1] in "]}" and len(trimmed) >= 2:
            try:
                return json.loads(trimmed)
            except Exception:
                return value
    return value


def _param_list_to_dict(parameters: Any) -> Dict[str, Any]:
    mapped: Dict[str, Any] = {}
    if isinstance(parameters, list):
        for entry in parameters:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                continue
            mapped[name] = _coerce_json(entry.get("value"))
    elif isinstance(parameters, dict):
        for key, value in parameters.items():
            if isinstance(key, str):
                mapped[key] = _coerce_json(value)
    return mapped


def _coerce_translation_map(value: Any) -> Optional[Dict[str, str]]:
    raw = _coerce_json(value)
    if isinstance(raw, dict):
        cleaned: Dict[str, str] = {}
        for key, val in raw.items():
            if isinstance(key, str) and isinstance(val, str):
                cleaned[key] = val
        return cleaned if cleaned else None
    return None


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    try:
        print(
            json.dumps(
                {
                    "debug": "incoming_event",
                    "keys": list(event.keys()),
                    "op_hint": event.get("op") or event.get("function"),
                    "has_parameters": bool(event.get("parameters")),
                }
            )
        )
    except Exception:
        pass
    parameter_map = _param_list_to_dict(event.get("parameters"))
    try:
        print(
            json.dumps(
                {
                    "debug": "parameter_map",
                    "keys": list(parameter_map.keys()),
                }
            )
        )
    except Exception:
        pass

    def _pick_str(*candidates: Any, default: str = "") -> str:
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                return candidate
        return default

    op = _pick_str(
        parameter_map.get("op"),
        event.get("op"),
        event.get("function"),
        default="human_to_future_iso",
    ).lower()

    locale_candidate = _coerce_json(parameter_map.get("locale", event.get("locale")))
    locales = _ensure_locale(locale_candidate)

    timezone = _pick_str(
        parameter_map.get("timezone"),
        parameter_map.get("timeZone"),
        event.get("timezone"),
        event.get("timeZone"),
        default=DEFAULT_TIMEZONE,
    )

    translation_map = _coerce_translation_map(
        parameter_map.get("translation_map", event.get("translation_map"))
    )

    def _wrap_if_bedrock(payload: Dict[str, Any]) -> Dict[str, Any]:
        if isinstance(event, dict) and event.get("actionGroup") and event.get("function"):
            body_text = json.dumps(payload)
            return {
                "messageVersion": "1.0",
                "response": {
                    "actionGroup": event.get("actionGroup"),
                    "function": event.get("function"),
                    "functionResponse": {
                        "responseBody": {"TEXT": {"body": body_text}}
                    },
                },
                "sessionAttributes": event.get("sessionAttributes", {}),
                "promptSessionAttributes": event.get("promptSessionAttributes", {}),
            }
        return payload

    try:
        if op == "normalize_any":
            iso_value = normalize_any_date_to_iso(
                _pick_str(parameter_map.get("text"), event.get("text")),
                locales=locales,
                timezone=timezone,
            )
        else:
            iso_value = parse_human_time(
                _pick_str(
                    parameter_map.get("phrase"),
                    event.get("phrase"),
                    parameter_map.get("text"),
                    event.get("text"),
                ),
                locales=locales,
                timezone=timezone,
                translation_map=translation_map,
            )
        return _wrap_if_bedrock({
            "success": True,
            "mode": op,
            "iso_date": iso_value,
        })
    except Exception as exc:
        return _wrap_if_bedrock({
            "success": False,
            "mode": op,
            "error": str(exc),
        })


if __name__ == "__main__":
    import sys

    payload = json.loads(sys.stdin.read() or "{}")
    result = lambda_handler(payload, None)
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")

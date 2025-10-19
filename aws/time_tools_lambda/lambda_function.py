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


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    op = (event.get("op") or "human_to_future_iso").lower()
    locale = _ensure_locale(event.get("locale"))
    timezone = event.get("timezone") or DEFAULT_TIMEZONE
    translation_map: Optional[Dict[str, str]] = event.get("translation_map")

    try:
        if op == "normalize_any":
            iso_value = normalize_any_date_to_iso(
                event.get("text", ""), locales=locale, timezone=timezone
            )
        else:
            iso_value = parse_human_time(
                event.get("phrase", ""),
                locales=locale,
                timezone=timezone,
                translation_map=translation_map,
            )
        return {
            "success": True,
            "mode": op,
            "iso_date": iso_value,
        }
    except Exception as exc:
        return {
            "success": False,
            "mode": op,
            "error": str(exc),
        }


if __name__ == "__main__":
    import sys

    payload = json.loads(sys.stdin.read() or "{}")
    result = lambda_handler(payload, None)
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
